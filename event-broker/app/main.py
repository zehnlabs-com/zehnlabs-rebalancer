"""
Event Broker Service - Single Service Architecture

Subscribes to Ably strategy events and executes trades in parallel subprocesses.
Startup order follows PRD §13.1 (BOOT-1).
"""
import asyncio
import signal
import sys
import logging
import os
import json
from pathlib import Path
from app_config import get_config, load_config
from .accounts import AccountRegistry, load_accounts
from .allocations import AllocationsClient
from .market_clock import MarketClock
from .settings import build_settings
from .services.ably_service import AblyEventSubscriber, _mode_for_exec
from .services.notification_service import NtfyNotifier
from .services.run_records import RunRecords
from .services.scheduler_service import SchedulerService
from .services.strategy_executor import StrategyExecutor

# Setup logging
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# Load configuration early
try:
    config_path = Path(os.getenv("CONFIG_PATH", "/app/config.yaml"))
    logger.info("Loading configuration from: %s", config_path)
    load_config(config_path)
    logger.info("Configuration loaded successfully")
except Exception as e:
    logger.error("Failed to load configuration: %s", e)
    sys.exit(1)

# Reduce verbosity of ably logs
for _name in ("ably", "ably.realtime", "ably.realtime.realtime_channel"):
    logging.getLogger(_name).setLevel(logging.WARNING)

_RUNS_DIR = os.getenv("RUNS_DIR", "/app/data/runs")


class EventBrokerApp:
    """Main application class (BOOT-1). Heavy components are built in `start()`."""

    def __init__(self):
        self.config = get_config()
        self.running = False
        self.settings = None
        self.registry: AccountRegistry | None = None
        self.clock: MarketClock | None = None
        self.notifier = None
        self.records: RunRecords | None = None
        self.strategy_executor: StrategyExecutor | None = None
        self.ably_subscriber: AblyEventSubscriber | None = None
        self.scheduler_service: SchedulerService | None = None
        self.manual_event_file = self.config.service.manual_event_file_path

    async def start(self):
        """BOOT-1: config -> settings -> accounts -> strategy check -> Ably -> scheduler."""
        try:
            logger.info("Starting Event Broker Service...")

            if self.settings is None:
                self.settings = build_settings()

            if self.registry is None:
                accounts_path = Path(os.getenv("ACCOUNTS_PATH", "/app/accounts.yaml"))
                self.registry = load_accounts(accounts_path, self.settings.trading_mode)

            if self.clock is None:
                self.clock = MarketClock(self.config.scheduler.market_open_time)
            if self.notifier is None:
                self.notifier = NtfyNotifier(
                    self.settings.user_notifications_enabled,
                    self.settings.user_notifications_channel,
                    logger=logger,
                )
            if self.records is None:
                self.records = RunRecords(_RUNS_DIR)

            await self._check_strategies()

            if self.strategy_executor is None:
                self.strategy_executor = StrategyExecutor(
                    config=self.config, settings=self.settings, registry=self.registry,
                    clock=self.clock, notifier=self.notifier, records=self.records,
                    logger=logger,
                )
            if self.ably_subscriber is None:
                self.ably_subscriber = AblyEventSubscriber(
                    strategy_executor=self.strategy_executor,
                    strategies=self._strategies_by_name(),
                    logger=logger,
                )
            await self.ably_subscriber.start()

            if self.scheduler_service is None:
                self.scheduler_service = SchedulerService(
                    config=self.config, strategy_executor=self.strategy_executor,
                    accounts_lookup=self.registry.get, clock=self.clock,
                    notifier=self.notifier, logger=logger,
                )
            self.records.prune()
            await self.scheduler_service.start()

            self.running = True
            logger.info("Event Broker Service started successfully")
            await asyncio.gather(self._run_forever(), self._watch_manual_events())
        except Exception as e:
            logger.error("Failed to start Event Broker Service: %s", e)
            raise

    async def _check_strategies(self):
        """BOOT step 4: confirm each strategy is available; do not validate weights."""
        strategies = self.registry.strategies()
        if not strategies:
            return
        async with AllocationsClient(
            self.settings.allocations_base_url,
            self.settings.zehnlabs_api_key,
            self.config.api.allocation_timeout_seconds,
        ) as client:
            for strategy in strategies:
                try:
                    await client.fetch(strategy)
                except Exception as exc:  # noqa: BLE001 - any fetch failure stops startup
                    raise RuntimeError(
                        f"Strategy '{strategy}' is not available from the allocations API "
                        f"({exc}). Check the name and your subscription."
                    ) from exc

    def _strategies_by_name(self) -> dict:
        return {s: self.registry.for_strategy(s) for s in self.registry.strategies()}

    async def stop(self):
        """Stop the Event Broker Service."""
        if not self.running:
            return
        logger.info("Stopping Event Broker Service...")
        self.running = False
        try:
            if self.scheduler_service:
                await self.scheduler_service.stop()
            if self.ably_subscriber:
                await self.ably_subscriber.stop()
            if self.strategy_executor:
                self.strategy_executor.cleanup()
            logger.info("Event Broker Service stopped successfully")
        except Exception as e:
            logger.error("Error stopping Event Broker Service: %s", e)

    async def _run_forever(self):
        """BOOT-2: heartbeat every loop iteration; wait for shutdown."""
        try:
            while self.running:
                try:
                    Path("/tmp/heartbeat").touch(exist_ok=True)
                except OSError as e:
                    logger.warning("heartbeat touch failed: %s", e)
                await asyncio.sleep(self.config.service.heartbeat_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Service shutdown requested")
            await self.stop()

    async def _watch_manual_events(self):
        logger.info("Starting manual event file watcher: %s", self.manual_event_file)
        while self.running:
            try:
                if os.path.exists(self.manual_event_file):
                    await self._process_manual_event()
                await asyncio.sleep(self.config.service.manual_event_check_interval_seconds)
            except Exception as e:
                logger.error("Error in manual event watcher: %s", e)
                await asyncio.sleep(self.config.service.error_recovery_delay_seconds)

    async def _process_manual_event(self):
        """SIG-3: read, delete, route a single account to the executor."""
        try:
            logger.info("Processing manual event file: %s", self.manual_event_file)
            with open(self.manual_event_file, "r") as f:
                event_data = json.loads(f.read())
            os.remove(self.manual_event_file)

            account_id = event_data.get("account_id")
            exec_command = event_data.get("exec", "print-rebalance")
            if not account_id:
                logger.error("Manual event missing 'account_id' field")
                return

            mode = _mode_for_exec(exec_command)
            if mode is None:
                logger.error("Unknown exec command '%s'; dropping manual event", exec_command)
                return

            account = self.registry.get(account_id) if self.registry else None
            if not account:
                logger.error("Account '%s' not found in configuration", account_id)
                return

            await self.strategy_executor.execute(
                account.strategy_name, [account], mode, "manual"
            )
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in manual event file: %s", e)
            try:
                os.remove(self.manual_event_file)
            except OSError:
                pass
        except Exception as e:
            logger.error("Error processing manual event: %s", e)
            try:
                os.remove(self.manual_event_file)
            except OSError:
                pass

    def _find_account_by_id(self, account_id: str):
        return self.registry.get(account_id) if self.registry else None

    def _get_strategy_accounts(self, strategy_name: str):
        return self.registry.for_strategy(strategy_name) if self.registry else []

    async def get_status(self):
        ably_status = await self.ably_subscriber.get_status() if self.ably_subscriber else {}
        active = len(self.strategy_executor.running) if self.strategy_executor else 0
        return {
            **ably_status,
            "service_type": "enhanced_event_broker",
            "version": "3.0.0",
            "active_strategies": active,
            "manual_event_file": self.manual_event_file,
            "scheduler_next_run": self.scheduler_service.get_next_run_time()
            if (self.scheduler_service and self.scheduler_service.scheduler) else None,
        }


# Global app instance (PRD §0.9: keep the module-level `app`)
app = EventBrokerApp()


async def main():
    """Main entry point: install signal handlers, run, then wait for shutdown."""
    shutdown_tasks: set[asyncio.Task] = set()

    def signal_handler(sig_num, frame):
        logger.info("Received signal %s", signal.Signals(sig_num).name)
        task = asyncio.create_task(app.stop())
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal_handler)

    try:
        await app.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Interrupt received")
        await app.stop()
    except Exception as e:
        logger.error("Unhandled exception in main: %s", e)
        import traceback
        logger.error("Traceback: %s", traceback.format_exc())
        await app.stop()
        sys.exit(1)
    finally:
        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as e:
        logger.error("Application failed: %s", e)
        sys.exit(1)