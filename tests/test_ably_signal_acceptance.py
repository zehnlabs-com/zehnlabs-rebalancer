from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from concurrent.futures import Future
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
    RunOutcome,
    RunRecord,
    RunSource,
)
from app.services.ably_service import AblyEventSubscriber
from app.services.strategy_executor import StrategyExecutor
from app.services.work_manager import WorkManager
from app.settings import RuntimeSettings
from app.state.models import RunStatus, WorkSource, WorkStatus
from app.state.sqlite import SQLiteState
from fakes import FakeClock, RecordingNotifier


ACCOUNTS = [
    AccountConfig(
        account_id=f"U10000{index}",
        type="paper",
        enabled=True,
        strategy_name="strategy-one",
    )
    for index in (1, 2)
]


class ImmediatePool:
    def __init__(self):
        self.submissions = []

    def submit(self, function, *args, **kwargs):
        self.submissions.append((function, args, kwargs))
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        return None


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
            pool = ImmediatePool()

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

            with patch.object(
                executor_module, "execute_strategy_batch", side_effect=fake_batch
            ):
                subscriber._handler("strategy-one")(
                    SimpleNamespace(data=json.dumps({"exec": "rebalance"}))
                )
                tasks = tuple(subscriber._tasks)
                self.assertEqual(len(tasks), 1)
                await asyncio.gather(*tasks)

            runs = state.list_runs()
            self.assertEqual(len(runs), 2)
            self.assertEqual({run.account_id for run in runs}, {a.account_id for a in ACCOUNTS})
            self.assertTrue(all(run.source == RunSource.SIGNAL for run in runs))
            self.assertTrue(all(run.status == RunStatus.TERMINAL for run in runs))
            self.assertEqual(len(pool.submissions), 1)
            self.assertEqual(notifier.count, 2)

            retries = [
                item
                for item in state.list_work_items()
                if item.source == WorkSource.RETRY
            ]
            self.assertEqual(len(retries), 2)
            self.assertTrue(all(item.status == WorkStatus.PENDING for item in retries))


if __name__ == "__main__":
    unittest.main()
