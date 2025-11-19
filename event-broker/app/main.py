"""
Enhanced Event Broker Service - Single Service Architecture

This service subscribes to Ably strategy events and executes trades directly
in parallel subprocesses for maximum performance.
"""
import asyncio
import signal
import sys
import logging
import os
import json
from pathlib import Path
from app_config import load_config
from app.services.ably_service import AblyEventSubscriber
from app.services.strategy_executor import StrategyExecutor

# Setup logging
logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Load configuration early
logger = logging.getLogger(__name__)
try:
    config_path = Path(os.getenv('CONFIG_PATH', '/app/config.yaml'))
    logger.info(f"Loading configuration from: {config_path}")
    load_config(config_path)
    logger.info("Configuration loaded successfully")
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    sys.exit(1)

# Reduce verbosity of ib_async and ably logs - set to WARNING level
# to prevent DEBUG/INFO logs from showing up
# ib_async_loggers = ['ib_async', 'ib_async.wrapper', 'ib_async.client', 'ib_async.ib']
# for logger_name in ib_async_loggers:
#     logger_instance = logging.getLogger(logger_name)
#     logger_instance.setLevel(logging.WARNING)

# Reduce verbosity of ably logs
ably_loggers = ['ably', 'ably.realtime', 'ably.realtime.realtime_channel']
for logger_name in ably_loggers:
    logger_instance = logging.getLogger(logger_name)
    logger_instance.setLevel(logging.WARNING)

class EventBrokerApp:
    """Main application class for the Enhanced Event Broker Service"""

    def __init__(self):
        from app_config import get_config
        self.config = get_config()
        self.strategy_executor = StrategyExecutor(logger=logger)
        self.ably_subscriber = AblyEventSubscriber(
            strategy_executor=self.strategy_executor,
            logger=logger
        )
        self.running = False
        self.manual_event_file = self.config.service.manual_event_file_path

    async def start(self):
        """Start the Enhanced Event Broker Service"""
        try:
            logger.info("Starting Enhanced Event Broker Service...")

            # Start the Ably event subscriber
            await self.ably_subscriber.start()

            self.running = True
            logger.info("Enhanced Event Broker Service started successfully")

            # Start manual event file watcher and keep the service running
            await asyncio.gather(
                self._run_forever(),
                self._watch_manual_events()
            )

        except Exception as e:
            logger.error(f"Failed to start Enhanced Event Broker Service: {e}")
            raise

    async def stop(self):
        """Stop the Enhanced Event Broker Service"""
        if not self.running:
            return

        logger.info("Stopping Enhanced Event Broker Service...")
        self.running = False

        try:
            # Stop Ably subscriber
            await self.ably_subscriber.stop()

            # Cleanup strategy executor
            self.strategy_executor.cleanup()

            logger.info("Enhanced Event Broker Service stopped successfully")

        except Exception as e:
            logger.error(f"Error stopping Enhanced Event Broker Service: {e}")

    async def _run_forever(self):
        """Keep the service running and handle graceful shutdown"""
        try:
            while self.running:
                await asyncio.sleep(self.config.service.heartbeat_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Service shutdown requested")
            await self.stop()

    async def _watch_manual_events(self):
        """Watch for manual event files and process them"""
        logger.info(f"Starting manual event file watcher: {self.manual_event_file}")

        while self.running:
            try:
                if os.path.exists(self.manual_event_file):
                    await self._process_manual_event()
                await asyncio.sleep(self.config.service.manual_event_check_interval_seconds)
            except Exception as e:
                logger.error(f"Error in manual event watcher: {e}")
                await asyncio.sleep(self.config.service.error_recovery_delay_seconds)

    async def _process_manual_event(self):
        """Process a manual event file"""
        try:
            logger.info(f"Processing manual event file: {self.manual_event_file}")

            # Read and parse the event file
            with open(self.manual_event_file, 'r') as f:
                event_data = json.loads(f.read())

            # Delete the file immediately to prevent reprocessing
            os.remove(self.manual_event_file)

            # Validate required fields
            account_id = event_data.get('account_id')
            exec_command = event_data.get('exec', 'print-rebalance')

            if not account_id:
                logger.error("Manual event missing 'account_id' field")
                return

            logger.info(f"Manual event: account='{account_id}', exec='{exec_command}'")

            # Find the account and get its strategy
            account = self._find_account_by_id(account_id)
            if not account:
                logger.error(f"Account '{account_id}' not found in configuration")
                return

            strategy_name = account['strategy_name']
            logger.info(f"Manual event: Found account {account_id} in strategy '{strategy_name}'")

            # Execute with single account
            result = await self.strategy_executor.execute_strategy(
                strategy_name,
                [account],  # Single account in list
                event_data
            )

            # Log results
            if result.get('status') == 'already_running':
                logger.info(f"Manual event: Account {account_id} strategy already running")
            elif result.get('status') == 'failed':
                logger.error(f"Manual event: Account {account_id} failed: {result.get('error')}")
            else:
                results = result.get('results', [])
                if results and results[0].get('success', False):
                    logger.info(f"Manual event: Account {account_id} completed successfully")
                else:
                    error = results[0].get('error', 'Unknown error') if results else 'No results'
                    logger.error(f"Manual event: Account {account_id} failed: {error}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in manual event file: {e}")
            # Remove the invalid file
            try:
                os.remove(self.manual_event_file)
            except:
                pass
        except Exception as e:
            logger.error(f"Error processing manual event: {e}")
            # Remove the file on any error to prevent infinite loops
            try:
                os.remove(self.manual_event_file)
            except:
                pass

    def _find_account_by_id(self, account_id: str):
        """Find an account by ID across all strategies"""
        for strategy_name, accounts in self.ably_subscriber.strategies.items():
            for account in accounts:
                if account.get('account_id') == account_id:
                    return account
        return None

    def _get_strategy_accounts(self, strategy_name: str):
        """Get accounts for a specific strategy from the Ably subscriber"""
        return self.ably_subscriber.strategies.get(strategy_name, [])

    async def get_status(self):
        """Get current status of the service"""
        ably_status = await self.ably_subscriber.get_status()

        return {
            **ably_status,
            "service_type": "enhanced_event_broker",
            "version": "2.0.0",
            "active_strategies": len(self.strategy_executor.active_strategies) if self.strategy_executor else 0,
            "manual_event_file": self.manual_event_file
        }

# Global app instance
app = EventBrokerApp()

async def shutdown_handler(sig_name):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received {sig_name} signal, initiating graceful shutdown...")
    await app.stop()

def setup_signal_handlers():
    """Set up signal handlers for graceful shutdown"""
    def signal_handler(sig_num, frame):
        logger.info(f"Received signal {sig_num}")
        # Create new event loop if none exists
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Schedule the shutdown
        asyncio.create_task(shutdown_handler(signal.Signals(sig_num).name))

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

async def main():
    """Main entry point"""
    try:
        # Set up signal handlers
        setup_signal_handlers()

        # Start the application
        await app.start()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        await app.stop()
    except Exception as e:
        logger.error(f"Unhandled exception in main: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        await app.stop()
        sys.exit(1)

if __name__ == "__main__":
    # Run the application
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)