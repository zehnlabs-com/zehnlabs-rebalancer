from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_config import AppConfig

import app.services.strategy_executor as executor_module
from app.accounts import AccountConfig, AccountRegistry
from app.results import (
    AccountRunResult,
    FailureReason,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
    WorkAction,
)
from app.services.ably_service import AblyEventSubscriber
from app.services.strategy_executor import StrategyExecutor
from app.services.work_manager import WorkManager
from app.settings import RuntimeSettings
from app.state.models import RunStatus, WorkSource, WorkStatus, utc_now_text
from app.state.sqlite import SQLiteState
from fakes import FakeClock, RecordingNotifier, RecordingPool


ACCOUNTS = [
    AccountConfig(
        account_id=f"U10000{index}",
        type="paper",
        enabled=True,
        strategy_name="strategy-one",
    )
    for index in (1, 2)
]


def failed_result(request) -> AccountRunResult:
    stamp = "2026-11-27T15:00:00.000000Z"
    record = RunRecord(
        run_id=request.run_id,
        account_id=request.account.account_id,
        strategy_name=request.account.strategy_name,
        mode=request.mode,
        source=request.source,
        started_at=stamp,
        finished_at=stamp,
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.CONNECTION_FAILED,
        error_message="Synthetic broker connection failure.",
    )
    return AccountRunResult(
        run_id=request.run_id,
        account_id=request.account.account_id,
        strategy_name=request.account.strategy_name,
        mode=request.mode,
        source=request.source,
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.CONNECTION_FAILED,
        error_message=record.error_message,
        record=record,
    )


class AblySignalAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_and_shutdown_failures_do_not_log_exception_text(self):
        secret = "raw-library-secret"
        logger = logging.getLogger("test_ably_safe_logging")

        class FailingExecutor:
            async def execute(self, *args, **kwargs):
                raise RuntimeError(secret)

        class FailingChannel:
            def unsubscribe(self):
                raise RuntimeError(secret)

        class FailingChannels:
            def get(self, name):
                return FailingChannel()

        class FailingClient:
            channels = FailingChannels()

            async def close(self):
                raise RuntimeError(secret)

        subscriber = AblyEventSubscriber(
            config=AppConfig(),
            settings=RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123",
                trading_mode="paper",
            ),
            strategy_executor=FailingExecutor(),
            logger=logger,
        )
        subscriber.strategies = {"strategy-one": [ACCOUNTS[0]]}

        with self.assertLogs(logger.name, level="WARNING") as captured:
            await subscriber._handle_strategy_event(
                "strategy-one",
                SimpleNamespace(data={"exec": "rebalance"}),
            )
            subscriber.ably = FailingClient()
            subscriber.actual_channels = {"strategy-one"}
            await subscriber.stop()

        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertEqual(output.count("RuntimeError"), 3)

    async def test_signal_callback_routes_every_account_persists_runs_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.db")
            state.initialize()
            state.replace_accounts("paper", ACCOUNTS)
            registry = AccountRegistry.from_accounts(ACCOUNTS, "paper")
            settings = RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123",
                trading_mode="paper",
                zehnlabs_api_key="synthetic-key",
            )
            clock = FakeClock(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc))
            pool = RecordingPool()

            with patch.object(
                executor_module, "get_context", return_value="fork-context"
            ), patch.object(
                executor_module, "ProcessPoolExecutor", return_value=pool
            ):
                notifier = RecordingNotifier()
                executor = StrategyExecutor(
                    config=AppConfig(executor={"max_workers": 2}),
                    settings=settings,
                    clock=clock,
                    notifier=notifier,
                    state=state,
                    logger=logging.getLogger("test_ably_signal_acceptance"),
                )

            work = WorkManager(
                state=state,
                registry=registry,
                settings=settings,
                clock=clock,
                executor=executor,
            )
            executor.set_retry_coordinator(work)
            subscriber = AblyEventSubscriber(
                config=AppConfig(),
                settings=settings,
                strategy_executor=executor,
            )
            subscriber.strategies = {"strategy-one": list(ACCOUNTS)}

            def fake_batch(strategy, requests, config, worker_settings):
                self.assertEqual(strategy, "strategy-one")
                return [failed_result(request) for request in requests]

            async def deliver(event_id: str) -> None:
                subscriber._handler("strategy-one")(
                    SimpleNamespace(
                        data=json.dumps(
                            {"exec": "rebalance", "eventId": event_id}
                        )
                    )
                )
                tasks = tuple(subscriber._tasks)
                self.assertEqual(len(tasks), 1)
                await asyncio.gather(*tasks)

            with patch.object(
                executor_module, "execute_strategy_batch", side_effect=fake_batch
            ):
                await deliver("rebalance:strategy-one:first")

                runs = state.list_runs()
                self.assertEqual(len(runs), 2)
                self.assertEqual(
                    {run.account_id for run in runs},
                    {account.account_id for account in ACCOUNTS},
                )
                self.assertTrue(all(run.source == RunSource.SIGNAL for run in runs))
                self.assertTrue(all(run.run_mode == RunMode.LIVE for run in runs))
                self.assertTrue(all(run.status == RunStatus.TERMINAL for run in runs))
                self.assertTrue(all(run.outcome == RunOutcome.FAILED for run in runs))
                self.assertTrue(
                    all(
                        run.failure_reason == FailureReason.CONNECTION_FAILED
                        for run in runs
                    )
                )

                retries = state.list_work_items()
                self.assertEqual(len(retries), 2)
                self.assertEqual(
                    {item.account_id for item in retries},
                    {account.account_id for account in ACCOUNTS},
                )
                self.assertTrue(all(item.source == WorkSource.RETRY for item in retries))
                self.assertTrue(all(item.action == WorkAction.REBALANCE for item in retries))
                self.assertTrue(all(item.status == WorkStatus.PENDING for item in retries))
                self.assertTrue(
                    all(
                        item.execute_at == utc_now_text(clock.next_scheduled_run())
                        for item in retries
                    )
                )
                retry_ids = {item.work_id for item in retries}

                await deliver("rebalance:strategy-one:second")

            self.assertEqual(len(state.list_runs()), 4)
            self.assertEqual(
                {item.work_id for item in state.list_work_items()},
                retry_ids,
            )
            self.assertEqual(len(pool.submissions), 2)
            self.assertEqual(notifier.count, 4)


if __name__ == "__main__":
    unittest.main()
