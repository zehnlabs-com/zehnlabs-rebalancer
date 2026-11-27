from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
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
from app.services.notification_service import NotificationKind
from app.services.strategy_executor import ExecutionDecision, StrategyExecutor
from app.services.work_manager import WorkManager
from app.settings import RuntimeSettings, WorkerSettings
from app.state.models import (
    RetryDecision,
    RetryDisposition,
    RunStatus,
    WorkItem,
    WorkSource,
    WorkStatus,
)
from app.state.sqlite import SQLiteState, StateError
from fakes import FakeClock, RecordingNotifier, RecordingPool


ACCOUNT = AccountConfig(
    account_id="U100001",
    type="paper",
    enabled=True,
    strategy_name="strategy-one",
)
ACCOUNT_TWO = AccountConfig(
    account_id="U100002",
    type="paper",
    enabled=True,
    strategy_name="strategy-one",
)


def result_for(request) -> AccountRunResult:
    stamp = "2026-11-26T15:00:00.000000Z"
    record = RunRecord(
        run_id=request.run_id,
        account_id=request.account.account_id,
        strategy_name=request.account.strategy_name,
        mode=request.mode,
        source=request.source,
        started_at=stamp,
        finished_at=stamp,
        outcome=RunOutcome.PREVIEWED,
    )
    return AccountRunResult(
        run_id=request.run_id,
        account_id=request.account.account_id,
        strategy_name=request.account.strategy_name,
        mode=request.mode,
        source=request.source,
        outcome=RunOutcome.PREVIEWED,
        record=record,
    )


class StrategyExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "state.db")
        self.state.initialize()
        self.state.replace_accounts("paper", [ACCOUNT])
        self.pool = RecordingPool()
        self.captured_pool_options = {}

        def pool_factory(*args, **kwargs):
            self.captured_pool_options.update(kwargs)
            return self.pool

        with patch.object(executor_module, "get_context", return_value="fork-context"), patch.object(
            executor_module, "ProcessPoolExecutor", side_effect=pool_factory
        ):
            self.notifier = RecordingNotifier()
            self.executor = StrategyExecutor(
                config=AppConfig(executor={"max_workers": 2}),
                settings=RuntimeSettings(
                    dashboard_admin_password="safe-dashboard-password-123",
                    trading_mode="paper",
                    zehnlabs_api_key="synthetic-key",
                ),
                clock=FakeClock(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)),
                notifier=self.notifier,
                state=self.state,
                logger=logging.getLogger("test_strategy_executor"),
            )

    async def test_parent_persists_runs_before_minimal_worker_submission(self):
        observed_statuses = []

        def fake_batch(strategy, requests, config, settings):
            observed_statuses.extend(
                self.state.get_run(request.run_id).status for request in requests
            )
            self.assertEqual(strategy, ACCOUNT.strategy_name)
            self.assertIsInstance(config, AppConfig)
            self.assertIsInstance(settings, WorkerSettings)
            self.assertFalse(hasattr(settings, "dashboard_admin_password"))
            self.assertFalse(hasattr(settings, "state_path"))
            self.assertFalse(hasattr(settings, "fastapi"))
            self.assertFalse(hasattr(settings, "ably"))
            return [result_for(request) for request in requests]

        with patch.object(executor_module, "execute_strategy_batch", side_effect=fake_batch):
            batch = await self.executor.execute(
                ACCOUNT.strategy_name,
                [ACCOUNT],
                RunMode.PREVIEW,
                RunSource.SIGNAL,
            )

        self.assertEqual(batch.decision, ExecutionDecision.ACCEPTED)
        self.assertEqual(observed_statuses, [RunStatus.RUNNING])
        self.assertEqual(len(self.pool.submissions), 1)
        stored = self.state.get_run(batch.results[0].run_id)
        self.assertEqual(stored.status, RunStatus.TERMINAL)
        self.assertEqual(stored.result, batch.results[0])

    async def test_state_failure_prevents_worker_submission_and_releases_gate(self):
        with patch.object(self.state, "start_runs", side_effect=StateError("synthetic")):
            with self.assertRaises(StateError):
                await self.executor.execute(
                    ACCOUNT.strategy_name,
                    [ACCOUNT],
                    RunMode.PREVIEW,
                    RunSource.SIGNAL,
                )
        self.assertEqual(self.pool.submissions, [])
        self.assertNotIn(ACCOUNT.strategy_name, self.executor.running)

    async def test_start_construction_failure_releases_gate_and_later_signal_runs(self):
        with patch.object(
            self.executor,
            "release",
            wraps=self.executor.release,
        ) as release, patch.object(
            self.executor,
            "build_starts",
            side_effect=ValueError("synthetic start construction failure"),
        ):
            with self.assertRaisesRegex(ValueError, "synthetic start construction failure"):
                await self.executor.execute(
                    ACCOUNT.strategy_name,
                    [ACCOUNT],
                    RunMode.PREVIEW,
                    RunSource.SIGNAL,
                )

        release.assert_called_once_with(ACCOUNT.strategy_name)
        self.assertNotIn(ACCOUNT.strategy_name, self.executor.running)
        self.assertTrue(self.executor.accepting)
        self.assertEqual(self.state.list_runs(), [])
        self.assertEqual(self.pool.submissions, [])
        self.assertEqual(self.notifier.count, 0)

        def fake_batch(strategy, requests, config, settings):
            return [result_for(request) for request in requests]

        with patch.object(
            executor_module, "execute_strategy_batch", side_effect=fake_batch
        ):
            batch = await self.executor.execute(
                ACCOUNT.strategy_name,
                [ACCOUNT],
                RunMode.PREVIEW,
                RunSource.SIGNAL,
            )

        self.assertEqual(batch.decision, ExecutionDecision.ACCEPTED)
        self.assertEqual(len(batch.results), 1)

    async def test_terminal_persistence_failure_releases_gate_and_sends_no_notification(self):
        def fake_batch(strategy, requests, config, settings):
            return [result_for(request) for request in requests]

        with patch.object(
            executor_module, "execute_strategy_batch", side_effect=fake_batch
        ), patch.object(
            self.state, "finish_run", side_effect=StateError("terminal write failed")
        ):
            with self.assertRaisesRegex(StateError, "terminal write failed"):
                await self.executor.execute(
                    ACCOUNT.strategy_name,
                    [ACCOUNT],
                    RunMode.PREVIEW,
                    RunSource.SIGNAL,
                )

        self.assertFalse(self.executor.accepting)
        self.assertNotIn(ACCOUNT.strategy_name, self.executor.running)
        self.assertEqual(self.notifier.count, 0)

    async def test_untyped_worker_result_is_rejected(self):
        def fake_batch(strategy, requests, config, settings):
            return [result_for(request).model_dump(mode="json") for request in requests]

        with self.assertLogs("test_strategy_executor", level="ERROR") as captured, patch.object(
            executor_module, "execute_strategy_batch", side_effect=fake_batch
        ):
            batch = await self.executor.execute(
                ACCOUNT.strategy_name,
                [ACCOUNT],
                RunMode.PREVIEW,
                RunSource.SIGNAL,
            )

        self.assertEqual(batch.results[0].outcome, RunOutcome.FAILED)
        self.assertEqual(
            batch.results[0].failure_reason,
            FailureReason.INTERNAL_ERROR,
        )
        output = "\n".join(captured.output)
        self.assertNotIn("worker returned an untyped result", output)
        self.assertIn("TypeError", output)

    async def test_fallback_failures_do_not_log_exception_text(self):
        secret = "raw-internal-secret"

        class FailingNotifier:
            async def send(self, result, context):
                raise RuntimeError(secret)

        class FailingPool:
            def shutdown(self, *, wait, cancel_futures):
                raise RuntimeError(secret)

        start = self.executor.build_starts(
            [ACCOUNT], RunMode.PREVIEW, RunSource.DASHBOARD
        )[0]
        result = self.executor.static_result(start, RunOutcome.PREVIEWED)
        self.executor.notifier = FailingNotifier()

        with self.assertLogs("test_strategy_executor", level="ERROR") as captured:
            await self.executor.postprocess(result)
            self.executor.record_storage_failure(RuntimeError(secret))
            self.executor._pool = FailingPool()
            self.executor.cleanup()

        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertEqual(output.count("RuntimeError"), 3)

    async def test_busy_signal_is_dropped_without_run_or_worker(self):
        self.executor.running.add(ACCOUNT.strategy_name)
        batch = await self.executor.execute(
            ACCOUNT.strategy_name,
            [ACCOUNT],
            RunMode.PREVIEW,
            RunSource.SIGNAL,
        )
        self.assertEqual(batch.decision, ExecutionDecision.BUSY)
        self.assertEqual(self.state.list_runs(), [])
        self.assertEqual(self.pool.submissions, [])
        self.assertEqual(self.notifier.count, 0)

    async def test_dashboard_and_scheduled_busy_work_use_failed_run_notification_path(self):
        manager = WorkManager(
            state=self.state,
            registry=AccountRegistry.from_accounts([ACCOUNT], "paper"),
            settings=self.executor.settings,
            clock=self.executor.clock,
            executor=self.executor,
        )
        self.executor.running.add(ACCOUNT.strategy_name)

        dashboard = manager.submit(
            ACCOUNT.account_id, WorkAction.REBALANCE, "busy-dashboard"
        )
        await manager.dispatch_due()
        next_open = self.executor.clock.next_scheduled_run()
        scheduled = manager.submit(
            ACCOUNT.account_id,
            WorkAction.REBALANCE,
            "busy-scheduled",
            source=WorkSource.SCHEDULED,
            schedule_next_open=True,
        )
        self.executor.clock.set_now(next_open)
        await manager.dispatch_due()

        for index, creation in enumerate((dashboard, scheduled)):
            stored = self.state.get_run(
                self.state.get_work_item(creation.item.work_id).run_id
            )
            self.assertEqual(stored.failure_reason, FailureReason.STRATEGY_BUSY)
            self.assertEqual(
                self.notifier.sends[index][1].kind,
                NotificationKind.FAILED_FINAL,
            )
        self.assertEqual(self.notifier.count, 2)

    async def test_in_flight_signal_failure_finishes_and_retries_during_shutdown(self):
        manager = WorkManager(
            state=self.state,
            registry=AccountRegistry.from_accounts([ACCOUNT], "paper"),
            settings=self.executor.settings,
            clock=self.executor.clock,
            executor=self.executor,
        )
        self.executor.set_retry_coordinator(manager)
        self.pool.deferred = True
        self.pool.submitted = asyncio.Event()

        execution = asyncio.create_task(
            self.executor.execute(
                ACCOUNT.strategy_name,
                [ACCOUNT],
                RunMode.LIVE,
                RunSource.SIGNAL,
            )
        )
        await asyncio.wait_for(self.pool.submitted.wait(), timeout=1)
        request = self.pool.submissions[0][1][1][0]

        manager.stop_accepting()
        self.executor.stop_accepting()
        failed = self.executor.static_result(
            self.executor.build_starts(
                [ACCOUNT], RunMode.LIVE, RunSource.SIGNAL
            )[0].model_copy(update={"run_id": request.run_id}),
            RunOutcome.FAILED,
            reason=FailureReason.CONNECTION_FAILED,
            error="Synthetic connection failure.",
        )
        self.pool.future.set_result([failed])

        batch = await execution

        self.assertEqual(batch.results[0].outcome, RunOutcome.FAILED)
        self.assertEqual(self.state.get_run(request.run_id).status, RunStatus.TERMINAL)
        retries = [
            item
            for item in self.state.list_work_items()
            if item.source == WorkSource.RETRY
        ]
        self.assertEqual(len(retries), 1)

    async def test_multi_account_gate_covers_all_persistence_before_notifications(self):
        self.state.replace_accounts("paper", [ACCOUNT, ACCOUNT_TWO])
        notification_started = asyncio.Event()
        notification_release = asyncio.Event()

        class BlockingNotifier:
            def __init__(self):
                self.sends = []

            async def send(inner_self, result, context):
                inner_self.sends.append((result, context))
                if len(inner_self.sends) == 1:
                    notification_started.set()
                    await notification_release.wait()

        notifier = BlockingNotifier()
        self.executor.notifier = notifier
        gate_decisions = []
        original_finish = self.state.finish_run

        def finish_while_owned(*args, **kwargs):
            gate_decisions.append(self.executor.try_accept(ACCOUNT.strategy_name))
            return original_finish(*args, **kwargs)

        def fake_batch(strategy, requests, config, settings):
            return [result_for(request) for request in requests]

        with patch.object(self.state, "finish_run", side_effect=finish_while_owned), patch.object(
            executor_module, "execute_strategy_batch", side_effect=fake_batch
        ):
            execution = asyncio.create_task(
                self.executor.execute(
                    ACCOUNT.strategy_name,
                    [ACCOUNT, ACCOUNT_TWO],
                    RunMode.PREVIEW,
                    RunSource.SIGNAL,
                )
            )
            await asyncio.wait_for(notification_started.wait(), timeout=1)
            running = [run for run in self.state.list_runs() if run.status == RunStatus.RUNNING]
            self.assertEqual(running, [])
            self.assertEqual(
                gate_decisions,
                [ExecutionDecision.BUSY, ExecutionDecision.BUSY],
            )
            self.assertNotIn(ACCOUNT.strategy_name, self.executor.running)
            notification_release.set()
            batch = await execution

        self.assertEqual(len(batch.results), 2)
        self.assertEqual(len(notifier.sends), 2)

    def test_pool_contract_and_cleanup(self):
        self.assertEqual(self.captured_pool_options["max_workers"], 2)
        self.assertEqual(self.captured_pool_options["mp_context"], "fork-context")
        self.executor.cleanup()
        self.assertEqual(self.pool.shutdown_args, (True, True))

    def test_notification_wording_uses_durable_retry_disposition(self):
        start = self.executor.build_starts(
            [ACCOUNT], RunMode.LIVE, RunSource.DASHBOARD
        )[0]
        failed = self.executor.static_result(
            start,
            RunOutcome.FAILED,
            reason=FailureReason.CONNECTION_FAILED,
            error="Synthetic failure.",
        )
        self.assertEqual(
            self.executor.notification_context(
                failed,
                retry_decision=RetryDecision(
                    disposition=RetryDisposition.NOT_CREATED
                ),
            ).kind,
            NotificationKind.FAILED_FINAL,
        )
        self.assertEqual(
            self.executor.notification_context(
                failed,
                retry_decision=RetryDecision(
                    disposition=RetryDisposition.CREATED,
                    item=WorkItem(
                        work_id="retry-work",
                        submission_id="retry-submission",
                        account_id=ACCOUNT.account_id,
                        action=WorkAction.REBALANCE,
                        source=WorkSource.RETRY,
                        status=WorkStatus.PENDING,
                        execute_at="2026-11-27T14:35:00.000000Z",
                        request_token_hash="0" * 64,
                        created_at="2026-11-26T15:00:00.000000Z",
                    ),
                ),
            ).kind,
            NotificationKind.FAILED_WILL_RETRY,
        )
        busy = failed.model_copy(update={"failure_reason": FailureReason.STRATEGY_BUSY})
        self.assertEqual(
            self.executor.notification_context(
                busy,
                retry_decision=RetryDecision(
                    disposition=RetryDisposition.NOT_CREATED
                ),
            ).kind,
            NotificationKind.FAILED_FINAL,
        )

    def test_retry_notification_uses_pending_work_item_execute_at(self):
        start = self.executor.build_starts(
            [ACCOUNT], RunMode.LIVE, RunSource.SIGNAL
        )[0]
        failed = self.executor.static_result(
            start,
            RunOutcome.FAILED,
            reason=FailureReason.CONNECTION_FAILED,
            error="Synthetic failure.",
        )
        for source, execute_at in (
            (
                WorkSource.DASHBOARD,
                "2026-11-26T15:01:00.000000Z",
            ),
            (
                WorkSource.SCHEDULED,
                "2026-11-27T14:35:00.000000Z",
            ),
        ):
            with self.subTest(source=source, execute_at=execute_at):
                item = WorkItem(
                    work_id=f"retry-{source}-{execute_at}",
                    submission_id="retry-submission",
                    account_id=ACCOUNT.account_id,
                    action=WorkAction.REBALANCE,
                    source=source,
                    status=WorkStatus.PENDING,
                    execute_at=execute_at,
                    request_token_hash="0" * 64,
                    created_at="2026-11-26T14:50:00.000000Z",
                )
                context = self.executor.notification_context(
                    failed,
                    retry_decision=RetryDecision(
                        disposition=RetryDisposition.REUSED,
                        item=item,
                    ),
                )
                self.assertEqual(context.retry_decision.item.execute_at, execute_at)

    def test_running_work_cannot_be_a_reused_retry(self):
        item = WorkItem(
            work_id="running-work",
            submission_id="running-submission",
            account_id=ACCOUNT.account_id,
            action=WorkAction.REBALANCE,
            source=WorkSource.DASHBOARD,
            status=WorkStatus.RUNNING,
            execute_at="2026-11-26T14:55:00.000000Z",
            request_token_hash="0" * 64,
            created_at="2026-11-26T14:50:00.000000Z",
        )
        with self.assertRaises(ValueError):
            RetryDecision(disposition=RetryDisposition.REUSED, item=item)


if __name__ == "__main__":
    unittest.main()
