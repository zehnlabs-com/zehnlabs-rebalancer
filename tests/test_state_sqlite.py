from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.accounts import AccountConfig
from app.results import (
    AccountRunResult,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
    WorkAction,
)
from app.state.models import (
    RunStatus,
    RunStart,
    WebSession,
    WorkItem,
    WorkRunStart,
    WorkSource,
    WorkStatus,
    utc_now_text,
)
from app.state.sqlite import (
    OutstandingWorkError,
    SCHEMA_VERSION,
    SQLiteState,
    StateError,
    _SCHEMA_V1,
)


PAPER = AccountConfig(
    account_id="U100001", type="paper", strategy_name="strategy-one", enabled=True
)
LIVE = AccountConfig(
    account_id="U200002", type="live", strategy_name="strategy-two", enabled=True
)


def result(run_id: str, account: AccountConfig = PAPER) -> AccountRunResult:
    record = RunRecord(
        run_id=run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=RunMode.PREVIEW,
        source=RunSource.DASHBOARD,
        started_at="2026-11-26T15:00:00.000000Z",
        finished_at="2026-11-26T15:00:01.000000Z",
        outcome=RunOutcome.PREVIEWED,
        raw_weights={"SPY": 1.0},
        scaled_weights={"SPY": 1.0},
        targets={"SPY": 1.0},
    )
    return AccountRunResult(
        run_id=run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=RunMode.PREVIEW,
        source=RunSource.DASHBOARD,
        outcome=RunOutcome.PREVIEWED,
        record=record,
    )


class SQLiteStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.state = SQLiteState(self.path)
        self.state.initialize()

    def work(self, token="token", action=WorkAction.PREVIEW) -> WorkItem:
        return WorkItem(
            work_id=f"work-{token}",
            submission_id=f"submission-{token}",
            account_id=PAPER.account_id,
            action=action,
            source=WorkSource.DASHBOARD,
            status=WorkStatus.PENDING,
            execute_at="2026-11-26T15:00:00.000000Z",
            request_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            created_at="2026-11-26T14:00:00.000000Z",
        )

    def start(self, run_id="run-1") -> RunStart:
        return RunStart(
            run_id=run_id,
            batch_id="00000000-0000-4000-8000-000000000001",
            account=PAPER,
            trading_mode="paper",
            run_mode=RunMode.PREVIEW,
            source=RunSource.DASHBOARD,
            requested_at="2026-11-26T14:00:00.000000Z",
        )

    def create_populated_v1_database(self, name: str = "v1.db") -> Path:
        path = Path(self.tmp.name) / name
        with closing(sqlite3.connect(path)) as connection:
            for statement in _SCHEMA_V1:
                connection.execute(statement)
            connection.execute(
                """INSERT INTO accounts VALUES
                   ('U100001', 'paper', 'ibkr', 1, 'strategy-one', NULL,
                    '2026-11-01T00:00:00.000000Z', '2026-11-02T00:00:00.000000Z')"""
            )
            connection.execute(
                """INSERT INTO runs VALUES
                   ('legacy-run', 'legacy-batch', 'U100001', 'strategy-one', NULL,
                    'paper', 'preview', 'dashboard', 'interrupted', NULL, NULL, NULL,
                    '2026-11-03T00:00:00.000000Z', NULL, NULL, NULL)"""
            )
            connection.execute(
                """INSERT INTO work_items VALUES
                   ('legacy-work', 'U100001', 'preview', 'dashboard', 'interrupted',
                    '2026-11-03T00:00:00.000000Z', ?, 'legacy-run', 'legacy message',
                    '2026-11-03T00:00:00.000000Z',
                    '2026-11-03T00:00:01.000000Z',
                    '2026-11-03T00:00:02.000000Z')""",
                (hashlib.sha256(b"legacy").hexdigest(),),
            )
            connection.execute(
                """INSERT INTO web_sessions VALUES
                   ('legacy-session', 'legacy-csrf', 1,
                    '2026-11-03T00:00:00.000000Z',
                    '2026-11-03T00:01:00.000000Z',
                    '2026-11-03T12:00:00.000000Z')"""
            )
            connection.execute(
                """INSERT INTO login_failures VALUES
                   ('legacy-client', 3, '2026-11-03T00:00:00.000000Z', NULL)"""
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        return path

    def test_schema_version_tables_indexes_and_pragmas(self):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(
                tables,
                {
                    "accounts",
                    "work_items",
                    "runs",
                    "web_sessions",
                    "login_failures",
                    "onboarding_initializations",
                },
            )
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
                )
            }
            self.assertEqual(
                indexes,
                {
                    "runs_requested_at_idx",
                    "runs_account_requested_at_idx",
                    "runs_status_idx",
                    "runs_outcome_idx",
                    "work_items_status_execute_at_idx",
                    "work_items_account_id_idx",
                    "work_items_submission_id_idx",
                    "work_items_outstanding_account_action_idx",
                },
            )
            columns = {
                table: [
                    (row[1], row[2], row[3], row[5])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                for table in tables
            }
            self.assertEqual(
                columns["accounts"],
                [
                    ("account_id", "TEXT", 0, 1),
                    ("account_type", "TEXT", 1, 0),
                    ("broker", "TEXT", 1, 0),
                    ("enabled", "INTEGER", 1, 0),
                    ("strategy_name", "TEXT", 1, 0),
                    ("replacement_set", "TEXT", 0, 0),
                    ("created_at", "TEXT", 1, 0),
                    ("updated_at", "TEXT", 1, 0),
                ],
            )
            self.assertEqual(
                columns["runs"],
                [
                    ("run_id", "TEXT", 0, 1),
                    ("batch_id", "TEXT", 1, 0),
                    ("account_id", "TEXT", 1, 0),
                    ("strategy_name", "TEXT", 1, 0),
                    ("replacement_set", "TEXT", 0, 0),
                    ("trading_mode", "TEXT", 1, 0),
                    ("run_mode", "TEXT", 1, 0),
                    ("source", "TEXT", 1, 0),
                    ("status", "TEXT", 1, 0),
                    ("outcome", "TEXT", 0, 0),
                    ("failure_reason", "TEXT", 0, 0),
                    ("error_message", "TEXT", 0, 0),
                    ("requested_at", "TEXT", 1, 0),
                    ("started_at", "TEXT", 0, 0),
                    ("finished_at", "TEXT", 0, 0),
                    ("result_json", "TEXT", 0, 0),
                ],
            )
            self.assertEqual(
                columns["work_items"],
                [
                    ("work_id", "TEXT", 0, 1),
                    ("submission_id", "TEXT", 1, 0),
                    ("account_id", "TEXT", 1, 0),
                    ("action", "TEXT", 1, 0),
                    ("source", "TEXT", 1, 0),
                    ("status", "TEXT", 1, 0),
                    ("execute_at", "TEXT", 1, 0),
                    ("request_token_hash", "TEXT", 1, 0),
                    ("run_id", "TEXT", 0, 0),
                    ("message", "TEXT", 0, 0),
                    ("created_at", "TEXT", 1, 0),
                    ("started_at", "TEXT", 0, 0),
                    ("finished_at", "TEXT", 0, 0),
                ],
            )
            self.assertEqual(
                columns["web_sessions"],
                [
                    ("session_hash", "TEXT", 0, 1),
                    ("csrf_hash", "TEXT", 1, 0),
                    ("authenticated", "INTEGER", 1, 0),
                    ("created_at", "TEXT", 1, 0),
                    ("last_seen_at", "TEXT", 1, 0),
                    ("absolute_expires_at", "TEXT", 1, 0),
                ],
            )
            self.assertEqual(
                columns["login_failures"],
                [
                    ("client_key", "TEXT", 0, 1),
                    ("failure_count", "INTEGER", 1, 0),
                    ("window_started_at", "TEXT", 1, 0),
                    ("blocked_until", "TEXT", 0, 0),
                ],
            )
            self.assertEqual(
                columns["onboarding_initializations"],
                [
                    ("trading_mode", "TEXT", 1, 1),
                    ("configuration_fingerprint", "TEXT", 1, 2),
                    ("submission_id", "TEXT", 1, 0),
                    ("created_at", "TEXT", 1, 0),
                    ("created_items_json", "TEXT", 1, 0),
                ],
            )
        self.assertEqual(self.state.connection_pragmas(), (1, "delete", 2))

    def test_initialize_is_idempotent_and_newer_version_fails_without_change(self):
        self.state.replace_accounts("paper", [PAPER])
        self.state.initialize()
        self.assertEqual(self.state.list_accounts("paper"), [PAPER])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
        with self.assertRaisesRegex(StateError, "newer"):
            self.state.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_populated_v1_migrates_to_v2_without_losing_state(self):
        path = self.create_populated_v1_database()
        migrated = SQLiteState(path)
        migrated.initialize()

        self.assertEqual(migrated.list_accounts("paper"), [PAPER])
        item = migrated.get_work_item("legacy-work")
        self.assertEqual(
            item,
            WorkItem(
                work_id="legacy-work",
                submission_id="legacy-work",
                account_id="U100001",
                action=WorkAction.PREVIEW,
                source=WorkSource.DASHBOARD,
                status=WorkStatus.INTERRUPTED,
                execute_at="2026-11-03T00:00:00.000000Z",
                request_token_hash=hashlib.sha256(b"legacy").hexdigest(),
                run_id="legacy-run",
                message="legacy message",
                created_at="2026-11-03T00:00:00.000000Z",
                started_at="2026-11-03T00:00:01.000000Z",
                finished_at="2026-11-03T00:00:02.000000Z",
            ),
        )
        self.assertEqual(migrated.get_run("legacy-run").status.value, "interrupted")
        self.assertEqual(migrated.get_session("legacy-session").csrf_hash, "legacy-csrf")
        self.assertEqual(migrated.get_login_failure("legacy-client").failure_count, 3)
        migrated.initialize()
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT * FROM accounts").fetchone(),
                (
                    "U100001",
                    "paper",
                    "ibkr",
                    1,
                    "strategy-one",
                    None,
                    "2026-11-01T00:00:00.000000Z",
                    "2026-11-02T00:00:00.000000Z",
                ),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM runs").fetchone(),
                (
                    "legacy-run",
                    "legacy-batch",
                    "U100001",
                    "strategy-one",
                    None,
                    "paper",
                    "preview",
                    "dashboard",
                    "interrupted",
                    None,
                    None,
                    None,
                    "2026-11-03T00:00:00.000000Z",
                    None,
                    None,
                    None,
                ),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM web_sessions").fetchone(),
                (
                    "legacy-session",
                    "legacy-csrf",
                    1,
                    "2026-11-03T00:00:00.000000Z",
                    "2026-11-03T00:01:00.000000Z",
                    "2026-11-03T12:00:00.000000Z",
                ),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM login_failures").fetchone(),
                ("legacy-client", 3, "2026-11-03T00:00:00.000000Z", None),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM onboarding_initializations"
                ).fetchone()[0],
                0,
            )

    def test_failed_v1_migration_rolls_back_every_schema_change(self):
        path = self.create_populated_v1_database("failed-v1.db")
        state = SQLiteState(path)
        broken_migration = (
            "CREATE TABLE migration_probe (value TEXT)",
            "THIS IS NOT VALID SQL",
        )
        with mock.patch("app.state.sqlite._MIGRATION_V1_TO_V2", broken_migration):
            with self.assertRaises(StateError):
                state.initialize()

        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
                ).fetchone()
            )
            columns = [row[1] for row in connection.execute("PRAGMA table_info(work_items)")]
            self.assertNotIn("submission_id", columns)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1
            )

    def test_replace_mode_is_atomic_preserves_other_mode_and_cancels_changed_work(self):
        self.state.replace_all_accounts([PAPER, LIVE])
        item = self.work()
        self.state.create_work_item(item)
        changed = PAPER.model_copy(update={"strategy_name": "strategy-three"})
        self.state.replace_accounts("paper", [changed])
        self.assertEqual(self.state.list_accounts("live"), [LIVE])
        self.assertEqual(self.state.get_work_item(item.work_id).status, WorkStatus.CANCELLED)
        with self.assertRaises(Exception):
            self.state.replace_accounts("paper", [LIVE])
        self.assertEqual(self.state.list_accounts("paper"), [changed])

    def test_request_token_idempotency_and_outstanding_invariant(self):
        original = self.state.create_work_item(self.work("a"))
        duplicate = self.state.create_work_item(
            self.work("a").model_copy(update={"work_id": "other-id"})
        )
        self.assertTrue(original.created)
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.item.work_id, original.item.work_id)
        with self.assertRaises(OutstandingWorkError) as caught:
            self.state.create_work_item(self.work("b"))
        self.assertEqual(caught.exception.existing.work_id, original.item.work_id)

    def test_initial_work_is_atomic_accepts_both_sources_and_replays_tokens(self):
        immediate = self.work("initial", WorkAction.REBALANCE).model_copy(
            update={"source": WorkSource.INITIAL}
        )
        scheduled = self.work("scheduled", WorkAction.REBALANCE).model_copy(
            update={
                "submission_id": immediate.submission_id,
                "account_id": "U100002",
                "source": WorkSource.SCHEDULED,
                "execute_at": "2026-11-27T14:35:00.000000Z",
            }
        )
        self.state.replace_accounts(
            "paper",
            [PAPER, PAPER.model_copy(update={"account_id": "U100002"})],
        )
        fingerprint = self.state.current_onboarding_configuration_fingerprint("paper")
        creation = self.state.finish_onboarding_initialization(
            "paper", fingerprint, [immediate, scheduled]
        )
        self.assertTrue(creation.created)

        replay = self.state.finish_onboarding_initialization(
            "paper",
            fingerprint,
            [
                immediate.model_copy(
                    update={
                        "work_id": "another-immediate-work",
                        "submission_id": "different-submission",
                        "request_token_hash": hashlib.sha256(b"another-immediate").hexdigest(),
                    }
                ),
                scheduled.model_copy(
                    update={
                        "work_id": "another-scheduled-work",
                        "submission_id": "different-submission",
                        "request_token_hash": hashlib.sha256(b"another-scheduled").hexdigest(),
                    }
                ),
            ],
        )

        self.assertFalse(replay.created)
        self.assertEqual(
            [entry.item.work_id for entry in replay.receipt.items],
            [immediate.work_id, scheduled.work_id],
        )
        self.assertEqual(len(self.state.list_work_items()), 2)

    def test_initialization_archive_survives_terminal_work_pruning(self):
        self.state.replace_accounts("paper", [PAPER])
        fingerprint = self.state.current_onboarding_configuration_fingerprint("paper")
        item = self.work("initial-archive", WorkAction.REBALANCE).model_copy(
            update={"source": WorkSource.INITIAL}
        )
        created = self.state.finish_onboarding_initialization(
            "paper",
            fingerprint,
            [item],
            now=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(created.created)
        self.assertTrue(created.receipt.items[0].history_retained)
        self.state.cancel_work_item(
            item.work_id,
            "terminal",
            now=datetime(2026, 11, 2, tzinfo=timezone.utc),
        )
        self.state.prune_terminal(datetime(2026, 12, 3, tzinfo=timezone.utc))

        self.assertIsNone(self.state.get_work_item(item.work_id))
        archived = self.state.get_work_submission(item.submission_id)
        self.assertFalse(archived.items[0].history_retained)
        self.assertEqual(archived.items[0].item, item)

        replay_item = self.work("initial-replay", WorkAction.REBALANCE).model_copy(
            update={"source": WorkSource.INITIAL}
        )
        replay = self.state.finish_onboarding_initialization(
            "paper", fingerprint, [replay_item]
        )
        self.assertFalse(replay.created)
        self.assertEqual(replay.receipt.submission_id, item.submission_id)
        self.assertEqual(len(self.state.list_work_items()), 0)

    def test_configuration_fingerprint_uses_enabled_durable_configuration(self):
        self.state.replace_accounts(
            "paper", [PAPER], now=datetime(2026, 11, 1, tzinfo=timezone.utc)
        )
        original = self.state.current_onboarding_configuration_fingerprint("paper")
        disabled = PAPER.model_copy(update={"account_id": "U100009", "enabled": False})
        self.state.replace_accounts(
            "paper",
            [PAPER, disabled],
            now=datetime(2026, 11, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.state.current_onboarding_configuration_fingerprint("paper"), original
        )
        changed = PAPER.model_copy(update={"strategy_name": "strategy-changed"})
        self.state.replace_accounts(
            "paper",
            [changed, disabled],
            now=datetime(2026, 11, 3, tzinfo=timezone.utc),
        )
        self.assertNotEqual(
            self.state.current_onboarding_configuration_fingerprint("paper"), original
        )

    def test_initialization_rolls_back_all_items_on_outstanding_conflict(self):
        second = PAPER.model_copy(update={"account_id": "U100002"})
        self.state.replace_accounts("paper", [PAPER, second])
        existing = self.work("existing", WorkAction.REBALANCE).model_copy(
            update={"account_id": second.account_id}
        )
        self.state.create_work_item(existing)
        first = self.work("new-first", WorkAction.REBALANCE).model_copy(
            update={"source": WorkSource.INITIAL, "submission_id": "new-initial"}
        )
        conflicting = self.work("new-second", WorkAction.REBALANCE).model_copy(
            update={
                "account_id": second.account_id,
                "source": WorkSource.INITIAL,
                "submission_id": "new-initial",
            }
        )
        fingerprint = self.state.current_onboarding_configuration_fingerprint("paper")
        with self.assertRaises(OutstandingWorkError):
            self.state.finish_onboarding_initialization(
                "paper", fingerprint, [first, conflicting]
            )
        self.assertIsNone(self.state.get_work_item(first.work_id))
        self.assertIsNone(self.state.get_work_submission("new-initial"))

    def test_new_work_rejects_illegal_initial_transition_state(self):
        illegal = self.work().model_copy(
            update={
                "status": WorkStatus.RUNNING,
                "started_at": "2026-11-26T15:00:00.000000Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "must begin pending"):
            self.state.create_work_item(illegal)
        self.assertEqual(self.state.list_work_items(), [])

    def test_work_run_start_finish_and_lossless_round_trip(self):
        item = self.work()
        self.state.create_work_item(item)
        start = self.start()
        self.state.start_work_batch(
            [WorkRunStart(work_id=item.work_id, run=start)],
            "2026-11-26T15:00:00.000000Z",
        )
        self.assertEqual(self.state.get_run(start.run_id).status.value, "running")
        final = result(start.run_id)
        self.state.complete_work_item(item.work_id, final)
        stored = self.state.get_run(start.run_id)
        self.assertEqual(stored.result, final)
        self.assertEqual(self.state.get_work_item(item.work_id).status, WorkStatus.COMPLETED)
        receipt = self.state.get_work_submission(item.submission_id)
        self.assertEqual(receipt.items[0].run, stored)
        self.assertTrue(receipt.items[0].history_retained)
        with self.assertRaises(StateError):
            self.state.finish_run(final)

    def test_retry_candidate_query_never_reuses_running_work(self):
        item = self.work("running-rebalance", WorkAction.REBALANCE)
        self.state.create_work_item(item)
        self.assertEqual(
            self.state.find_pending_rebalance_for_retry(PAPER.account_id), item
        )
        live_start = self.start().model_copy(update={"run_mode": RunMode.LIVE})
        self.state.start_work_batch(
            [WorkRunStart(work_id=item.work_id, run=live_start)], item.execute_at
        )
        self.assertIsNone(self.state.find_pending_rebalance_for_retry(PAPER.account_id))

    def test_recovery_interrupts_running_and_does_not_return_it_due(self):
        item = self.work()
        self.state.create_work_item(item)
        run_id = self.start().run_id
        self.state.start_work_batch(
            [WorkRunStart(work_id=item.work_id, run=self.start(run_id))],
            "2026-11-26T15:00:00.000000Z",
        )
        self.assertEqual(self.state.recover_interrupted(), (1, 1))
        recovered_work = self.state.get_work_item(item.work_id)
        recovered_run = self.state.get_run(run_id)
        self.assertEqual(recovered_work.status, WorkStatus.INTERRUPTED)
        self.assertEqual(
            recovered_work.message,
            "Interrupted by process restart; not replayed.",
        )
        self.assertIsNotNone(recovered_work.finished_at)
        self.assertEqual(recovered_work.run_id, run_id)
        self.assertEqual(recovered_run.status, RunStatus.INTERRUPTED)
        self.assertIsNone(recovered_run.outcome)
        self.assertIsNone(recovered_run.result)
        self.assertEqual(self.state.due_pending_items("2099-01-01T00:00:00.000000Z"), [])
        self.assertEqual(self.state.recover_interrupted(), (0, 0))
        self.assertEqual(self.state.get_work_item(item.work_id), recovered_work)
        self.assertEqual(self.state.get_run(run_id), recovered_run)

    def test_run_history_paginates_with_bounded_parameterized_queries(self):
        for index in range(55):
            run_id = f"history-{index:03d}"
            start = self.start(run_id).model_copy(
                update={"requested_at": f"2026-11-26T15:{index:02d}:00.000000Z"}
            )
            self.state.start_runs([start])
            self.state.finish_run(result(run_id))

        second_page = self.state.list_runs(limit=50, offset=50)
        self.assertEqual(
            [run.run_id for run in second_page],
            ["history-004", "history-003", "history-002", "history-001", "history-000"],
        )
        with self.assertRaisesRegex(ValueError, "limit"):
            self.state.list_runs(limit=201)
        with self.assertRaisesRegex(ValueError, "offset"):
            self.state.list_runs(offset=-1)

    def test_sessions_and_throttles_survive_reopen(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        session = WebSession(
            session_hash="s",
            csrf_hash="c",
            authenticated=True,
            created_at=utc_now_text(now),
            last_seen_at=utc_now_text(now),
            absolute_expires_at=utc_now_text(now + timedelta(hours=12)),
        )
        self.state.create_session(session)
        for _ in range(5):
            failure = self.state.record_login_failure("client", now)
        reopened = SQLiteState(self.path)
        reopened.initialize()
        self.assertEqual(reopened.get_session("s"), session)
        self.assertEqual(reopened.get_login_failure("client").failure_count, 5)
        self.assertIsNotNone(failure.blocked_until)


if __name__ == "__main__":
    unittest.main()
