"""Import-safe composition root and ``python -m app.main`` entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import uvicorn
from app_config import AppConfig

from .accounts import AccountRegistry
from .application import RebalancerApplication
from .market_clock import MarketClock
from .services.ably_service import AblyEventSubscriber
from .services.account_manager import AccountManager
from .services.notification_service import NtfyNotifier
from .services.scheduler_service import SchedulerService
from .services.strategy_executor import StrategyExecutor
from .services.work_manager import WorkManager
from .settings import RuntimeSettings, build_settings
from .state.sqlite import SQLiteState
from .web.app import create_web_app
from .web.auth import AuthenticationService

logger = logging.getLogger(__name__)


class RebalancerRuntime:
    """Owns the one parent process's startup and idempotent shutdown sequence."""

    def __init__(
        self,
        *,
        state: SQLiteState,
        account_manager: AccountManager,
        scheduler: SchedulerService,
        subscriber: AblyEventSubscriber,
        executor: StrategyExecutor,
        application: RebalancerApplication,
    ):
        self.state = state
        self.account_manager = account_manager
        self.scheduler = scheduler
        self.subscriber = subscriber
        self.executor = executor
        self.application = application
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.started:
            return
        self.state.initialize()
        self.state.invalidate_sessions()
        self.state.recover_interrupted()
        self.state.prune_terminal()
        await self.account_manager.start()
        await self.scheduler.start()
        self.started = True

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.application.stop_accepting()
        await self.scheduler.stop()
        await self.account_manager.stop()
        await self.subscriber.stop()
        self.executor.cleanup()


def build_application(
    *,
    settings: Optional[RuntimeSettings] = None,
    config: Optional[AppConfig] = None,
    state_path: str | Path = "/app/state/rebalancer.db",
):
    """Build all parent-owned components without opening SQLite or external services."""
    runtime_settings = settings or build_settings()
    app_config = config or AppConfig()
    state = SQLiteState(state_path)
    registry = AccountRegistry()
    clock = MarketClock(app_config.scheduler.market_open_time)
    notifier = NtfyNotifier(
        runtime_settings.user_notifications_enabled,
        runtime_settings.user_notifications_channel,
        logger=logger,
    )
    executor = StrategyExecutor(
        config=app_config,
        settings=runtime_settings,
        clock=clock,
        notifier=notifier,
        state=state,
        logger=logger,
    )
    subscriber = AblyEventSubscriber(
        config=app_config,
        settings=runtime_settings,
        strategy_executor=executor,
        logger=logger,
    )
    account_manager = AccountManager(
        config=app_config,
        settings=runtime_settings,
        state=state,
        registry=registry,
        subscriber=subscriber,
        logger=logger,
    )
    work_manager = WorkManager(
        state=state,
        registry=registry,
        settings=runtime_settings,
        clock=clock,
        executor=executor,
        logger=logger,
    )
    scheduler = SchedulerService(state=state, work_manager=work_manager, logger=logger)
    work_manager.set_scheduler(scheduler)
    executor.set_retry_coordinator(work_manager)
    application = RebalancerApplication(
        settings=runtime_settings,
        state=state,
        accounts=account_manager,
        work=work_manager,
        executor=executor,
    )
    runtime = RebalancerRuntime(
        state=state,
        account_manager=account_manager,
        scheduler=scheduler,
        subscriber=subscriber,
        executor=executor,
        application=application,
    )
    auth = AuthenticationService(state, runtime_settings)
    web = create_web_app(application, auth, lifecycle=runtime)
    return web, runtime


def create_app():
    """Uvicorn-compatible factory used by tests and external launchers."""
    web, _runtime = build_application()
    return web


def main() -> None:
    settings = build_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    web, _runtime = build_application(settings=settings)
    uvicorn.run(
        web,
        host="0.0.0.0",
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
