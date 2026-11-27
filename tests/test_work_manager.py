from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app_config import AppConfig

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
from app.services.strategy_executor import ExecutionDecision
from app.services.work_manager import WorkManager
from app.settings import RuntimeSettings
from app.state.models import RunStart, WorkSource, WorkStatus, utc_now_text
from app.state.sqlite import OutstandingWorkError, SQLiteState, StateError
from fakes import FakeClock


ACCOUNT = AccountConfig(
    account_id="U100001", type="paper", enabled=True, strategy_name="strategy-one"
)
ACCOUNT_TWO = AccountConfig(
    account_id="U100002", type="paper", enabled=True, strategy_name="strategy-one"
)


class FakeExecutor:
    def __init__(self, clock):
        self.clock = clock
        self.config = AppConfig()
        self.accepting = True
        self.decision = ExecutionDecision.ACCEPTED
        self.submissions = []
        self.postprocessed = []
        self.retry_dispositions = []
        self.live_outcome = RunOutcome.SUCCEEDED
        self.execution_started = None
        self.execution_release = None
        self.postprocess_started = None
        self.postprocess_release = None
        self.strategy_owned = False
        self.release_calls = []

    def try_accept(self, strategy):
        if self.decision == ExecutionDecision.ACCEPTED:
            self.strategy_owned = True
        return self.decision

    def release(self, strategy):
        self.release_calls.append(strategy)
        self.strategy_owned = False

    def build_starts(self, accounts, mode, source, *, batch_id=None, requested_at=None):
        return [
            RunStart(
                run_id=f"run-{account.account_id}-{uuid4()}",
                batch_id=batch_id or str(uuid4()),
                account=account,
                trading_mode="paper",
                run_mode=mode,
                source=source,
                requested_at=requested_at or utc_now_text(self.clock.now()),
            )
            for account in accounts
        ]

    def static_result(self, start, outcome, *, reason=None, error=None):
        now = utc_now_text(self.clock.now())
        record = RunRecord(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=start.run_mode,
            source=start.source,
            started_at=now,
            finished_at=now,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
        )
        return AccountRunResult(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=start.run_mode,
            source=start.source,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
            record=record,
        )

    async def execute_reserved(self, strategy, starts):
        self.submissions.append((strategy, tuple(starts)))
        if self.execution_started is not None:
            self.execution_started.set()
        if self.execution_release is not None:
            await self.execution_release.wait()
        return [
            self.static_result(
                start,
                (
                    RunOutcome.PREVIEWED
                    if start.run_mode == RunMode.PREVIEW
                    else self.live_outcome
                ),
                reason=(
                    FailureReason.CONNECTION_FAILED
                    if start.run_mode == RunMode.LIVE
                    and self.live_outcome == RunOutcome.FAILED
                    else None
                ),
                error=(
                    "Synthetic connection failure."
                    if start.run_mode == RunMode.LIVE
                    and self.live_outcome == RunOutcome.FAILED
                    else None
                ),
            )
            for start in starts
        ]

    async def postprocess(self, result, *, retry_decision=None):
        self.postprocessed.append(result)
        self.retry_dispositions.append(retry_decision.disposition)
        if self.postprocess_started is not None:
            self.postprocess_started.set()
        if self.postprocess_release is not None:
            await self.postprocess_release.wait()

    def record_storage_failure(self, exc):
        self.accepting = False


class WorkManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "db.sqlite")
        self.state.initialize()
        self.state.replace_accounts("paper", [ACCOUNT])
        self.registry = AccountRegistry.from_accounts([ACCOUNT], "paper")
        self.clock = FakeClock(
            datetime(2026, 11, 26, 8, 0, tzinfo=timezone(timedelta(hours=-5))),
            open_time=time(9, 30),
        )
        self.executor = FakeExecutor(self.clock)
        self.manager = WorkManager(
            state=self.state,
            registry=self.registry,
            settings=RuntimeSettings(
                dashboard_admin_password="safe-dashboard-password-123", trading_mode="paper"
            ),
            clock=self.clock,
            executor=self.executor,
        )

    def test_preview_is_immediate_and_closed_market_rebalance_is_scheduled(self):
        preview = self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        rebalance = self.manager.submit(ACCOUNT.account_id, WorkAction.REBALANCE, "two")
        self.assertEqual(preview.item.execute_at, utc_now_text(self.clock.now()))
        self.assertEqual(rebalance.item.source, WorkSource.SCHEDULED)
        self.assertEqual(
            rebalance.item.execute_at,
            utc_now_text(self.clock.next_scheduled_run(self.clock.now())),
        )

    def test_same_token_is_idempotent_and_different_token_conflicts(self):
        first = self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        repeated = self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        self.assertFalse(repeated.created)
        self.assertEqual(first.item.work_id, repeated.item.work_id)
        with self.assertRaises(OutstandingWorkError):
            self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "different")

    def test_bulk_form_replay_returns_the_complete_derived_token_set(self):
        second = ACCOUNT.model_copy(
            update={"account_id": "U100002", "strategy_name": "strategy-two"}
        )
        accounts = [ACCOUNT, second]
        self.state.replace_accounts("paper", accounts)
        self.registry.refresh(accounts, "paper")

        first = self.manager.submit_bulk(
            [second.account_id, ACCOUNT.account_id],
            WorkAction.PREVIEW,
            "one-form",
            source=WorkSource.INITIAL,
        )
        replay = self.manager.submit_bulk(
            [ACCOUNT.account_id, second.account_id],
            WorkAction.PREVIEW,
            "one-form",
            source=WorkSource.INITIAL,
        )

        self.assertEqual(first.submission_id, replay.submission_id)
        self.assertEqual(
            [entry.item.work_id for entry in replay.items],
            [entry.item.work_id for entry in first.items],
        )
        self.assertEqual(
            {entry.item.request_token_hash for entry in first.items},
            {
                hashlib.sha256(
                    f"one-form:{account.account_id}:preview".encode("utf-8")
                ).hexdigest()
                for account in accounts
            },
        )

    async def test_dispatch_completes_preview_and_never_calls_live_mode(self):
        self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        await self.manager.dispatch_due()
        item = self.state.list_work_items()[0]
        self.assertEqual(item.status, WorkStatus.COMPLETED)
        self.assertEqual(self.executor.submissions[0][1][0].run_mode, RunMode.PREVIEW)
        self.assertEqual(self.state.get_run(item.run_id).outcome, RunOutcome.PREVIEWED)

    async def test_multi_account_work_persists_every_result_before_postprocessing(self):
        self.state.replace_accounts("paper", [ACCOUNT, ACCOUNT_TWO])
        self.manager.registry.refresh([ACCOUNT, ACCOUNT_TWO], "paper")
        self.executor.postprocess_started = asyncio.Event()
        self.executor.postprocess_release = asyncio.Event()
        receipt = self.manager.submit_bulk(
            [ACCOUNT.account_id, ACCOUNT_TWO.account_id],
            WorkAction.PREVIEW,
            "two-account-preview",
            source=WorkSource.INITIAL,
        )
        ownership_during_persistence = []
        original_complete = self.state.complete_work_item

        def complete_while_owned(*args, **kwargs):
            ownership_during_persistence.append(self.executor.strategy_owned)
            return original_complete(*args, **kwargs)

        with patch.object(
            self.state, "complete_work_item", side_effect=complete_while_owned
        ):
            dispatch = asyncio.create_task(self.manager.dispatch_due())
            await asyncio.wait_for(self.executor.postprocess_started.wait(), timeout=1)
            stored = [self.state.get_work_item(entry.item.work_id) for entry in receipt.items]
            self.assertEqual(
                [item.status for item in stored],
                [WorkStatus.COMPLETED, WorkStatus.COMPLETED],
            )
            self.assertEqual(ownership_during_persistence, [True, True])
            self.assertFalse(self.executor.strategy_owned)
            self.executor.postprocess_release.set()
            await dispatch

    async def test_build_starts_failure_releases_accepted_work_without_starting_it(self):
        creation = self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        group = [(creation.item, ACCOUNT)]

        with patch.object(
            self.executor,
            "build_starts",
            side_effect=RuntimeError("synthetic start construction failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "start construction"):
                await self.manager._dispatch_group(ACCOUNT.strategy_name, group)

        pending = self.state.get_work_item(creation.item.work_id)
        self.assertEqual(self.executor.release_calls, [ACCOUNT.strategy_name])
        self.assertFalse(self.executor.strategy_owned)
        self.assertTrue(self.executor.accepting)
        self.assertEqual(pending.status, WorkStatus.PENDING)
        self.assertIsNone(pending.run_id)
        self.assertEqual(self.state.list_runs(), [])
        self.assertEqual(self.executor.submissions, [])
        self.assertEqual(self.executor.postprocessed, [])

        await self.manager._dispatch_group(ACCOUNT.strategy_name, group)

        completed = self.state.get_work_item(creation.item.work_id)
        self.assertEqual(completed.status, WorkStatus.COMPLETED)
        self.assertEqual(len(self.executor.submissions), 1)
        self.assertEqual(self.executor.release_calls, [ACCOUNT.strategy_name] * 2)

    async def test_busy_work_finishes_once_without_worker_submission(self):
        creation = self.manager.submit(ACCOUNT.account_id, WorkAction.PREVIEW, "one")
        self.executor.decision = ExecutionDecision.BUSY
        await self.manager.dispatch_due()
        item = self.state.get_work_item(creation.item.work_id)
        run = self.state.get_run(item.run_id)
        self.assertEqual(item.status, WorkStatus.COMPLETED)
        self.assertEqual(run.failure_reason, FailureReason.STRATEGY_BUSY)
        self.assertIn("No automatic retry was created", run.result.notes[0].text)
        self.assertEqual(self.executor.submissions, [])
        self.assertEqual(self.executor.release_calls, [])

    async def test_in_flight_dashboard_and_initial_failures_finish_and_retry_during_shutdown(self):
        self.clock.set_now(
            datetime(2026, 11, 26, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
        )
        for source in (WorkSource.DASHBOARD, WorkSource.INITIAL):
            with self.subTest(source=source):
                self.executor.live_outcome = RunOutcome.FAILED
                self.executor.execution_started = asyncio.Event()
                self.executor.execution_release = asyncio.Event()
                creation = self.manager.submit(
                    ACCOUNT.account_id,
                    WorkAction.REBALANCE,
                    f"shutdown-{source.value}",
                    source=source,
                )
                dispatch = asyncio.create_task(self.manager.dispatch_due())
                await self.executor.execution_started.wait()

                self.manager.stop_accepting()
                self.executor.accepting = False
                self.executor.execution_release.set()
                await dispatch

                completed = self.state.get_work_item(creation.item.work_id)
                self.assertEqual(completed.status, WorkStatus.COMPLETED)
                self.assertEqual(
                    self.state.get_run(completed.run_id).outcome, RunOutcome.FAILED
                )
                retries = [
                    item
                    for item in self.state.list_work_items()
                    if item.source == WorkSource.RETRY
                    and item.status == WorkStatus.PENDING
                ]
                self.assertEqual(len(retries), 1)
                self.state.cancel_work_item(
                    retries[0].work_id, "test cleanup", self.clock.now()
                )
                self.manager.accepting = True
                self.executor.accepting = True

    async def test_failed_immediate_work_persists_retry_and_run_note_atomically(self):
        self.clock.set_now(
            datetime(2026, 11, 26, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
        )
        self.executor.live_outcome = RunOutcome.FAILED
        creation = self.manager.submit(
            ACCOUNT.account_id, WorkAction.REBALANCE, "failed-dashboard"
        )

        await self.manager.dispatch_due()

        completed = self.state.get_work_item(creation.item.work_id)
        stored = self.state.get_run(completed.run_id)
        retry = [
            item
            for item in self.state.list_work_items()
            if item.source == WorkSource.RETRY
        ]
        self.assertEqual(len(retry), 1)
        self.assertNotEqual(retry[0].submission_id, creation.item.submission_id)
        self.assertTrue(
            any(retry[0].work_id in note.text for note in stored.result.notes)
        )
        self.assertEqual(self.executor.retry_dispositions[-1].value, "created")

    async def test_retry_persistence_failure_is_critical_and_never_notified_as_queued(self):
        self.clock.set_now(
            datetime(2026, 11, 26, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
        )
        self.executor.live_outcome = RunOutcome.FAILED
        creation = self.manager.submit(
            ACCOUNT.account_id, WorkAction.REBALANCE, "failed-retry-insert"
        )
        original_values = self.state._work_values

        def fail_retry(item):
            if item.source == WorkSource.RETRY:
                raise StateError("synthetic retry insert failure")
            return original_values(item)

        with patch.object(self.state, "_work_values", side_effect=fail_retry):
            with self.assertRaisesRegex(StateError, "retry insert"):
                await self.manager.dispatch_due()

        self.assertFalse(self.executor.accepting)
        self.assertFalse(self.executor.strategy_owned)
        self.assertEqual(self.executor.postprocessed, [])
        self.assertEqual(
            [item for item in self.state.list_work_items() if item.source == WorkSource.RETRY],
            [],
        )
        self.assertEqual(
            self.state.get_work_item(creation.item.work_id).status,
            WorkStatus.RUNNING,
        )

    async def test_immediate_rebalance_runs_between_open_and_configured_time(self):
        class Clock:
            def __init__(self):
                self.current = datetime(
                    2026, 11, 27, 9, 31, tzinfo=timezone(timedelta(hours=-5))
                )

            def now(self):
                return self.current

            def is_open(self, now=None):
                value = now or self.current
                return time(9, 30) <= value.timetz().replace(tzinfo=None) < time(16)

            def scheduled_time_has_passed(self, now=None):
                value = now or self.current
                return value.timetz().replace(tzinfo=None) >= time(9, 35)

            def next_scheduled_run(self, now=None):
                value = now or self.current
                today = value.replace(hour=9, minute=35, second=0, microsecond=0)
                return today if today > value else today + timedelta(days=1)

        self.clock = Clock()
        self.executor.clock = self.clock
        self.manager.clock = self.clock
        self.manager.submit(ACCOUNT.account_id, WorkAction.REBALANCE, "before-0935")

        await self.manager.dispatch_due()

        self.assertEqual(len(self.executor.submissions), 1)
        self.assertEqual(self.state.list_work_items()[0].status, WorkStatus.COMPLETED)

    def test_restart_recovery_keeps_overdue_rule_separate(self):
        class Clock:
            def __init__(self):
                self.current = datetime(
                    2026, 11, 27, 9, 31, tzinfo=timezone(timedelta(hours=-5))
                )

            def now(self):
                return self.current

            def is_open(self, now=None):
                return True

            def scheduled_time_has_passed(self, now=None):
                return (now or self.current).hour > 9 or (now or self.current).minute >= 35

            def next_scheduled_run(self, now=None):
                return self.current.replace(hour=9, minute=35, second=0, microsecond=0)

        self.clock = Clock()
        self.manager.clock = self.clock
        creation = self.manager.submit(
            ACCOUNT.account_id,
            WorkAction.REBALANCE,
            "overdue-restart",
        )
        self.state.reschedule_work_item(
            creation.item.work_id,
            "2026-11-26T14:31:00.000000Z",
            "Synthetic overdue item.",
        )

        self.manager.recover_overdue()

        item = self.state.get_work_item(creation.item.work_id)
        self.assertEqual(item.execute_at, utc_now_text(self.clock.next_scheduled_run()))
        self.assertIn("Missed execution time", item.message or "")


if __name__ == "__main__":
    unittest.main()
