"""SQLite schema and all durable-state SQL for the parent process."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from pydantic import TypeAdapter

from ..accounts import AccountConfig
from ..results import AccountRunResult, RunOutcome, RunSource, WorkAction
from .models import (
    LoginFailure,
    OnboardingInitializationResult,
    RunStart,
    RunStatus,
    StoredRun,
    SubmissionWorkItemReceipt,
    WebSession,
    WorkCreation,
    WorkItem,
    WorkRunStart,
    WorkSubmissionReceipt,
    WorkSource,
    WorkStatus,
    parse_utc,
    utc_now_text,
)

SCHEMA_VERSION = 2
RETENTION_DAYS = 30
MAX_RUN_QUERY_LIMIT = 200


class StateError(RuntimeError):
    """A safe durable-state failure."""


class OutstandingWorkError(StateError):
    """Another token already owns the Account/action outstanding slot."""

    def __init__(self, existing: WorkItem):
        super().__init__("An outstanding request already exists for this Account and action.")
        self.existing = existing


_SCHEMA_V1 = (
    """CREATE TABLE accounts (
        account_id TEXT PRIMARY KEY,
        account_type TEXT NOT NULL CHECK (account_type IN ('paper', 'live')),
        broker TEXT NOT NULL CHECK (broker = 'ibkr'),
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        strategy_name TEXT NOT NULL,
        replacement_set TEXT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        strategy_name TEXT NOT NULL,
        replacement_set TEXT NULL,
        trading_mode TEXT NOT NULL CHECK (trading_mode IN ('paper', 'live')),
        run_mode TEXT NOT NULL CHECK (run_mode IN ('preview', 'live')),
        source TEXT NOT NULL CHECK (source IN ('signal', 'dashboard', 'initial', 'scheduled', 'retry')),
        status TEXT NOT NULL CHECK (status IN ('running', 'terminal', 'interrupted')),
        outcome TEXT NULL,
        failure_reason TEXT NULL,
        error_message TEXT NULL,
        requested_at TEXT NOT NULL,
        started_at TEXT NULL,
        finished_at TEXT NULL,
        result_json TEXT NULL
    )""",
    "CREATE INDEX runs_requested_at_idx ON runs(requested_at DESC)",
    "CREATE INDEX runs_account_requested_at_idx ON runs(account_id, requested_at DESC)",
    "CREATE INDEX runs_status_idx ON runs(status)",
    "CREATE INDEX runs_outcome_idx ON runs(outcome)",
    """CREATE TABLE work_items (
        work_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('preview', 'rebalance')),
        source TEXT NOT NULL CHECK (source IN ('dashboard', 'initial', 'scheduled', 'retry')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'cancelled', 'interrupted')),
        execute_at TEXT NOT NULL,
        request_token_hash TEXT NOT NULL UNIQUE,
        run_id TEXT NULL REFERENCES runs(run_id) ON DELETE SET NULL,
        message TEXT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT NULL,
        finished_at TEXT NULL
    )""",
    "CREATE INDEX work_items_status_execute_at_idx ON work_items(status, execute_at)",
    "CREATE INDEX work_items_account_id_idx ON work_items(account_id)",
    """CREATE UNIQUE INDEX work_items_outstanding_account_action_idx
       ON work_items(account_id, action) WHERE status IN ('pending', 'running')""",
    """CREATE TABLE web_sessions (
        session_hash TEXT PRIMARY KEY,
        csrf_hash TEXT NOT NULL,
        authenticated INTEGER NOT NULL CHECK (authenticated IN (0, 1)),
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE login_failures (
        client_key TEXT PRIMARY KEY,
        failure_count INTEGER NOT NULL,
        window_started_at TEXT NOT NULL,
        blocked_until TEXT NULL
    )""",
)

_WORK_ITEMS_V2_SQL = """CREATE TABLE work_items (
        work_id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('preview', 'rebalance')),
        source TEXT NOT NULL CHECK (source IN ('dashboard', 'initial', 'scheduled', 'retry')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'cancelled', 'interrupted')),
        execute_at TEXT NOT NULL,
        request_token_hash TEXT NOT NULL UNIQUE,
        run_id TEXT NULL REFERENCES runs(run_id) ON DELETE SET NULL,
        message TEXT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT NULL,
        finished_at TEXT NULL
    )"""

_WORK_ITEMS_V2_INDEXES = (
    "CREATE INDEX work_items_status_execute_at_idx ON work_items(status, execute_at)",
    "CREATE INDEX work_items_account_id_idx ON work_items(account_id)",
    "CREATE INDEX work_items_submission_id_idx ON work_items(submission_id)",
    """CREATE UNIQUE INDEX work_items_outstanding_account_action_idx
       ON work_items(account_id, action) WHERE status IN ('pending', 'running')""",
)

_ONBOARDING_INITIALIZATIONS_SQL = """CREATE TABLE onboarding_initializations (
        trading_mode TEXT NOT NULL CHECK (trading_mode IN ('paper', 'live')),
        configuration_fingerprint TEXT NOT NULL,
        submission_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        created_items_json TEXT NOT NULL,
        PRIMARY KEY (trading_mode, configuration_fingerprint)
    )"""

# The first six v1 statements create Accounts, Runs, and Run indexes; the final
# two create session and throttle tables. Work Items are the only replacement.
_SCHEMA_V2 = (
    *_SCHEMA_V1[:6],
    _WORK_ITEMS_V2_SQL,
    *_WORK_ITEMS_V2_INDEXES,
    *_SCHEMA_V1[10:],
    _ONBOARDING_INITIALIZATIONS_SQL,
)

_MIGRATION_V1_TO_V2 = (
    _WORK_ITEMS_V2_SQL.replace("work_items (", "work_items_v2 (", 1),
    """INSERT INTO work_items_v2
       (work_id, submission_id, account_id, action, source, status, execute_at,
        request_token_hash, run_id, message, created_at, started_at, finished_at)
       SELECT work_id, work_id, account_id, action, source, status, execute_at,
              request_token_hash, run_id, message, created_at, started_at, finished_at
       FROM work_items""",
    "DROP TABLE work_items",
    "ALTER TABLE work_items_v2 RENAME TO work_items",
    *_WORK_ITEMS_V2_INDEXES,
    _ONBOARDING_INITIALIZATIONS_SQL,
)

_WORK_ITEM_LIST_ADAPTER = TypeAdapter(tuple[WorkItem, ...])
_ONBOARDING_FINGERPRINT_DOMAIN = "zehnlabs-rebalancer:onboarding-configuration:v1"


class SQLiteState:
    """Cohesive, synchronous SQLite state boundary.

    Each operation opens, configures, and closes its own connection. The object
    stores only the path, so no connection can be inherited by a forked worker.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise StateError("SQLite state operation failed.") from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError("SQLite state directory is unavailable.") from exc
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StateError(
                    f"SQLite schema version {version} is newer than supported version {SCHEMA_VERSION}."
                )
            self._quick_check(connection)
            if version == 0:
                try:
                    connection.execute("BEGIN")
                    for statement in _SCHEMA_V2:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            elif version == 1:
                try:
                    connection.execute("BEGIN")
                    for statement in _MIGRATION_V1_TO_V2:
                        connection.execute(statement)
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            self._quick_check(connection)

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise StateError("SQLite integrity check failed.")

    def health_check(self) -> bool:
        with self._connection() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def connection_pragmas(self) -> tuple[int, str, int]:
        """Diagnostic values used by the durability acceptance tests."""
        with self._connection() as connection:
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            return foreign_keys, journal, synchronous

    # Accounts ---------------------------------------------------------
    def list_accounts(self, trading_mode: Optional[str] = None) -> list[AccountConfig]:
        sql = "SELECT * FROM accounts"
        params: tuple[object, ...] = ()
        if trading_mode is not None:
            sql += " WHERE account_type = ?"
            params = (trading_mode,)
        sql += " ORDER BY account_id"
        with self._connection() as connection:
            return [self._account(row) for row in connection.execute(sql, params)]

    def account_updated_at(self, account_id: str) -> Optional[str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT updated_at FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            return str(row[0]) if row is not None else None

    def replace_accounts(
        self,
        trading_mode: str,
        accounts: Sequence[AccountConfig],
        now: Optional[datetime] = None,
    ) -> None:
        if trading_mode not in ("paper", "live") or any(a.type != trading_mode for a in accounts):
            raise ValueError("Account set must contain only the selected trading mode.")
        if len({a.account_id for a in accounts}) != len(accounts):
            raise ValueError("Account IDs must be unique.")
        timestamp = utc_now_text(now)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                current_rows = connection.execute(
                    "SELECT * FROM accounts WHERE account_type = ?", (trading_mode,)
                ).fetchall()
                current = {str(row["account_id"]): row for row in current_rows}
                incoming = {account.account_id: account for account in accounts}
                affected: set[str] = set(current).difference(incoming)
                for account_id, account in incoming.items():
                    old = current.get(account_id)
                    if old is not None and (
                        bool(old["enabled"]) != account.enabled
                        or old["strategy_name"] != account.strategy_name
                        or old["replacement_set"] != account.replacement_set
                    ):
                        affected.add(account_id)
                if affected:
                    placeholders = ",".join("?" for _ in affected)
                    connection.execute(
                        f"""UPDATE work_items SET status = 'cancelled',
                               message = ?, finished_at = ?
                               WHERE status = 'pending' AND account_id IN ({placeholders})""",
                        (
                            "Cancelled because the Account configuration changed.",
                            timestamp,
                            *sorted(affected),
                        ),
                    )
                connection.execute("DELETE FROM accounts WHERE account_type = ?", (trading_mode,))
                for account in accounts:
                    old = current.get(account.account_id)
                    unchanged = old is not None and self._same_account(old, account)
                    created_at = str(old["created_at"]) if old is not None else timestamp
                    updated_at = str(old["updated_at"]) if unchanged else timestamp
                    connection.execute(
                        """INSERT INTO accounts
                           (account_id, account_type, broker, enabled, strategy_name,
                            replacement_set, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            account.account_id,
                            account.type,
                            account.broker,
                            int(account.enabled),
                            account.strategy_name,
                            account.replacement_set,
                            created_at,
                            updated_at,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def replace_all_accounts(
        self, accounts: Sequence[AccountConfig], now: Optional[datetime] = None
    ) -> None:
        """Initial YAML import: insert both modes atomically into an empty database."""
        if len({a.account_id for a in accounts}) != len(accounts):
            raise ValueError("Account IDs must be unique.")
        timestamp = utc_now_text(now)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                if connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None:
                    raise StateError("Account YAML import is available only before Accounts exist.")
                for account in accounts:
                    connection.execute(
                        """INSERT INTO accounts
                           (account_id, account_type, broker, enabled, strategy_name,
                            replacement_set, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            account.account_id,
                            account.type,
                            account.broker,
                            int(account.enabled),
                            account.strategy_name,
                            account.replacement_set,
                            timestamp,
                            timestamp,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _same_account(row: sqlite3.Row, account: AccountConfig) -> bool:
        return (
            row["account_type"] == account.type
            and row["broker"] == account.broker
            and bool(row["enabled"]) == account.enabled
            and row["strategy_name"] == account.strategy_name
            and row["replacement_set"] == account.replacement_set
        )

    @staticmethod
    def _account(row: sqlite3.Row) -> AccountConfig:
        return AccountConfig(
            account_id=row["account_id"],
            type=row["account_type"],
            broker=row["broker"],
            enabled=bool(row["enabled"]),
            strategy_name=row["strategy_name"],
            replacement_set=row["replacement_set"],
        )

    # Work Items -------------------------------------------------------
    def create_work_item(self, item: WorkItem) -> WorkCreation:
        self._validate_new_work_item(item)
        with self._connection() as connection:
            try:
                connection.execute(
                    """INSERT INTO work_items
                       (work_id, submission_id, account_id, action, source, status, execute_at,
                        request_token_hash, run_id, message, created_at, started_at, finished_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._work_values(item),
                )
                connection.commit()
                return WorkCreation(item=item, created=True)
            except sqlite3.IntegrityError:
                connection.rollback()
                duplicate = connection.execute(
                    "SELECT * FROM work_items WHERE request_token_hash = ?",
                    (item.request_token_hash,),
                ).fetchone()
                if duplicate is not None:
                    return WorkCreation(item=self._work(duplicate), created=False)
                outstanding = connection.execute(
                    """SELECT * FROM work_items
                       WHERE account_id = ? AND action = ?
                         AND status IN ('pending', 'running')""",
                    (item.account_id, item.action.value),
                ).fetchone()
                if outstanding is not None:
                    raise OutstandingWorkError(self._work(outstanding))
                raise

    def create_work_items(self, items: Sequence[WorkItem]) -> list[WorkCreation]:
        """Create a bulk form's complete Work Item set atomically."""
        for item in items:
            self._validate_new_work_item(item)
        if items and len({item.submission_id for item in items}) != 1:
            raise ValueError("Bulk Work Items must share one submission ID.")
        creations: list[WorkCreation] = []
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                for item in items:
                    try:
                        connection.execute(
                            """INSERT INTO work_items
                               (work_id, submission_id, account_id, action, source, status, execute_at,
                                request_token_hash, run_id, message, created_at, started_at,
                                finished_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            self._work_values(item),
                        )
                        creations.append(WorkCreation(item=item, created=True))
                    except sqlite3.IntegrityError:
                        duplicate = connection.execute(
                            "SELECT * FROM work_items WHERE request_token_hash = ?",
                            (item.request_token_hash,),
                        ).fetchone()
                        if duplicate is not None:
                            creations.append(
                                WorkCreation(item=self._work(duplicate), created=False)
                            )
                            continue
                        outstanding = connection.execute(
                            """SELECT * FROM work_items
                               WHERE account_id = ? AND action = ?
                                 AND status IN ('pending', 'running')""",
                            (item.account_id, item.action.value),
                        ).fetchone()
                        if outstanding is not None:
                            raise OutstandingWorkError(self._work(outstanding))
                        raise
                if creations and len({entry.item.submission_id for entry in creations}) != 1:
                    raise StateError("Bulk Work Items did not resolve to one submission.")
                connection.commit()
                return creations
            except BaseException:
                connection.rollback()
                raise

    def current_onboarding_configuration_fingerprint(self, trading_mode: str) -> str:
        with self._connection() as connection:
            fingerprint, _ = self._onboarding_configuration(connection, trading_mode)
            return fingerprint

    def get_onboarding_initialization(
        self,
        trading_mode: str,
        configuration_fingerprint: str,
        account_ids: Sequence[str],
    ) -> Optional[OnboardingInitializationResult]:
        """Atomically return the receipt when the supplied configuration is unchanged."""

        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                current_fingerprint, enabled_account_ids = self._onboarding_configuration(
                    connection, trading_mode
                )
                if current_fingerprint != configuration_fingerprint:
                    raise StateError("The Account configuration changed; Preview it again.")
                if tuple(sorted(account_ids)) != enabled_account_ids:
                    raise StateError(
                        "Initial Work Items must exactly match the enabled Accounts."
                    )
                row = connection.execute(
                    """SELECT submission_id, created_items_json
                       FROM onboarding_initializations
                       WHERE trading_mode = ? AND configuration_fingerprint = ?""",
                    (trading_mode, configuration_fingerprint),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                receipt = self._submission_receipt(
                    connection,
                    str(row["submission_id"]),
                    str(row["created_items_json"]),
                )
                if receipt is None:
                    raise StateError("Stored onboarding initialization is unavailable.")
                connection.commit()
                return OnboardingInitializationResult(created=False, receipt=receipt)
            except BaseException:
                connection.rollback()
                raise

    def finish_onboarding_initialization(
        self,
        trading_mode: str,
        configuration_fingerprint: str,
        items: Sequence[WorkItem],
        now: Optional[datetime] = None,
    ) -> OnboardingInitializationResult:
        """Atomically create or replay the unchanged Account configuration's initial work."""

        for item in items:
            self._validate_new_work_item(item)
            if (
                item.source not in (WorkSource.INITIAL, WorkSource.SCHEDULED)
                or item.action != WorkAction.REBALANCE
            ):
                raise ValueError(
                    "Onboarding completion accepts only immediate or scheduled initial rebalance work."
                )
        submission_ids = {item.submission_id for item in items}
        if len(submission_ids) != 1:
            raise ValueError("Initial Work Items must share one submission ID.")
        submission_id = next(iter(submission_ids))

        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                current_fingerprint, enabled_account_ids = self._onboarding_configuration(
                    connection, trading_mode
                )
                if current_fingerprint != configuration_fingerprint:
                    raise StateError("The Account configuration changed; Preview it again.")
                if not enabled_account_ids:
                    raise StateError("At least one enabled Account is required for onboarding.")
                if tuple(sorted(item.account_id for item in items)) != enabled_account_ids:
                    raise StateError(
                        "Initial Work Items must exactly match the enabled Accounts."
                    )

                existing = connection.execute(
                    """SELECT submission_id, created_items_json
                       FROM onboarding_initializations
                       WHERE trading_mode = ? AND configuration_fingerprint = ?""",
                    (trading_mode, configuration_fingerprint),
                ).fetchone()
                if existing is not None:
                    receipt = self._submission_receipt(
                        connection,
                        str(existing["submission_id"]),
                        str(existing["created_items_json"]),
                    )
                    if receipt is None:
                        raise StateError("Stored onboarding initialization is unavailable.")
                    connection.commit()
                    return OnboardingInitializationResult(created=False, receipt=receipt)

                for item in items:
                    try:
                        connection.execute(
                            """INSERT INTO work_items
                               (work_id, submission_id, account_id, action, source, status,
                                execute_at, request_token_hash, run_id, message, created_at,
                                started_at, finished_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            self._work_values(item),
                        )
                    except sqlite3.IntegrityError:
                        outstanding = connection.execute(
                            """SELECT * FROM work_items
                               WHERE account_id = ? AND action = ?
                                 AND status IN ('pending', 'running')""",
                            (item.account_id, item.action.value),
                        ).fetchone()
                        if outstanding is not None:
                            raise OutstandingWorkError(self._work(outstanding))
                        raise
                created_items_json = _WORK_ITEM_LIST_ADAPTER.dump_json(tuple(items)).decode(
                    "utf-8"
                )
                connection.execute(
                    """INSERT INTO onboarding_initializations
                       (trading_mode, configuration_fingerprint, submission_id, created_at,
                        created_items_json) VALUES (?, ?, ?, ?, ?)""",
                    (
                        trading_mode,
                        configuration_fingerprint,
                        submission_id,
                        utc_now_text(now),
                        created_items_json,
                    ),
                )
                receipt = self._submission_receipt(connection, submission_id)
                if receipt is None:
                    raise StateError("Initial Work submission could not be read.")
                connection.commit()
                return OnboardingInitializationResult(created=True, receipt=receipt)
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _onboarding_configuration(
        connection: sqlite3.Connection, trading_mode: str
    ) -> tuple[str, tuple[str, ...]]:
        if trading_mode not in ("paper", "live"):
            raise ValueError("Trading mode must be paper or live.")
        rows = connection.execute(
            """SELECT account_id, broker, strategy_name, replacement_set, created_at, updated_at
               FROM accounts
               WHERE account_type = ? AND enabled = 1
               ORDER BY account_id""",
            (trading_mode,),
        ).fetchall()
        canonical = {
            "domain": _ONBOARDING_FINGERPRINT_DOMAIN,
            "trading_mode": trading_mode,
            "accounts": [
                {
                    "account_id": row["account_id"],
                    "broker": row["broker"],
                    "strategy_name": row["strategy_name"],
                    "replacement_set": row["replacement_set"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ],
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest(), tuple(
            str(row["account_id"]) for row in rows
        )

    @staticmethod
    def _validate_new_work_item(item: WorkItem) -> None:
        if (
            item.status != WorkStatus.PENDING
            or item.run_id is not None
            or item.started_at is not None
            or item.finished_at is not None
        ):
            raise ValueError("New Work Items must begin pending and unlinked to a Run.")

    @staticmethod
    def _work_values(item: WorkItem) -> tuple[object, ...]:
        return (
            item.work_id,
            item.submission_id,
            item.account_id,
            item.action.value,
            item.source.value,
            item.status.value,
            item.execute_at,
            item.request_token_hash,
            item.run_id,
            item.message,
            item.created_at,
            item.started_at,
            item.finished_at,
        )

    def get_work_item(self, work_id: str) -> Optional[WorkItem]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._work(row) if row is not None else None

    def list_submission_work_items(self, submission_id: str) -> list[WorkItem]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM work_items WHERE submission_id = ?
                   ORDER BY account_id, execute_at, work_id""",
                (submission_id,),
            ).fetchall()
            return [self._work(row) for row in rows]

    def get_work_submission(self, submission_id: str) -> Optional[WorkSubmissionReceipt]:
        with self._connection() as connection:
            archived = connection.execute(
                """SELECT created_items_json FROM onboarding_initializations
                   WHERE submission_id = ?""",
                (submission_id,),
            ).fetchone()
            return self._submission_receipt(
                connection,
                submission_id,
                str(archived["created_items_json"]) if archived is not None else None,
            )

    def _submission_receipt(
        self,
        connection: sqlite3.Connection,
        submission_id: str,
        archived_items_json: Optional[str] = None,
    ) -> Optional[WorkSubmissionReceipt]:
        rows = connection.execute(
            """SELECT * FROM work_items WHERE submission_id = ?
               ORDER BY account_id, execute_at, work_id""",
            (submission_id,),
        ).fetchall()
        retained = {str(row["work_id"]): self._work(row) for row in rows}
        archived: tuple[WorkItem, ...] = ()
        if archived_items_json is not None:
            try:
                archived = _WORK_ITEM_LIST_ADAPTER.validate_json(archived_items_json)
            except ValueError as exc:
                raise StateError("Stored onboarding receipt is invalid.") from exc
            if any(item.submission_id != submission_id for item in archived):
                raise StateError("Stored onboarding receipt has an invalid submission ID.")
        items_by_id = {item.work_id: item for item in archived}
        items_by_id.update(retained)
        if not items_by_id:
            return None
        entries: list[SubmissionWorkItemReceipt] = []
        for item in sorted(
            items_by_id.values(), key=lambda value: (value.account_id, value.execute_at, value.work_id)
        ):
            run = None
            if item.run_id is not None:
                run_row = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (item.run_id,)
                ).fetchone()
                run = self._run(run_row) if run_row is not None else None
            entries.append(
                SubmissionWorkItemReceipt(
                    item=item,
                    run=run,
                    history_retained=item.work_id in retained,
                )
            )
        return WorkSubmissionReceipt(submission_id=submission_id, items=tuple(entries))

    def list_work_items(
        self,
        *,
        statuses: Optional[Sequence[WorkStatus]] = None,
        account_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[WorkItem]:
        clauses: list[str] = []
        values: list[object] = []
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(status.value for status in statuses)
        if account_id is not None:
            clauses.append("account_id = ?")
            values.append(account_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT * FROM work_items{where}
                    ORDER BY execute_at, CASE action WHEN 'rebalance' THEN 0 ELSE 1 END,
                             created_at, work_id LIMIT ?""",
                tuple(values),
            ).fetchall()
            return [self._work(row) for row in rows]

    def earliest_pending_execution_time(self) -> Optional[str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT MIN(execute_at) FROM work_items WHERE status = 'pending'"
            ).fetchone()
            return row[0] if row and row[0] is not None else None

    def due_pending_items(self, due_at: str) -> list[WorkItem]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM work_items
                   WHERE status = 'pending' AND execute_at <= ?
                   ORDER BY execute_at,
                            CASE action WHEN 'rebalance' THEN 0 ELSE 1 END,
                            created_at, work_id""",
                (due_at,),
            ).fetchall()
            return [self._work(row) for row in rows]

    def cancel_work_item(
        self, work_id: str, message: str, now: Optional[datetime] = None
    ) -> Optional[WorkItem]:
        timestamp = utc_now_text(now)
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE work_items SET status = 'cancelled', message = ?, finished_at = ?
                   WHERE work_id = ? AND status = 'pending'""",
                (message, timestamp, work_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._work(row)

    def find_outstanding_work(
        self,
        account_id: str,
        action: WorkAction,
        *,
        exclude_work_id: Optional[str] = None,
    ) -> Optional[WorkItem]:
        sql = """SELECT * FROM work_items
                 WHERE account_id = ? AND action = ?
                   AND status IN ('pending', 'running')"""
        values: list[object] = [account_id, action.value]
        if exclude_work_id is not None:
            sql += " AND work_id != ?"
            values.append(exclude_work_id)
        sql += " ORDER BY created_at, work_id LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, tuple(values)).fetchone()
            return self._work(row) if row is not None else None

    def find_pending_rebalance_for_retry(
        self, account_id: str, *, exclude_work_id: Optional[str] = None
    ) -> Optional[WorkItem]:
        sql = """SELECT * FROM work_items
                 WHERE account_id = ? AND action = 'rebalance' AND status = 'pending'"""
        values: list[object] = [account_id]
        if exclude_work_id is not None:
            sql += " AND work_id != ?"
            values.append(exclude_work_id)
        sql += " ORDER BY execute_at, created_at, work_id LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, tuple(values)).fetchone()
            return self._work(row) if row is not None else None

    def cancel_pending_retry(
        self, account_id: str, now: Optional[datetime] = None
    ) -> Optional[WorkItem]:
        timestamp = utc_now_text(now)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM work_items WHERE account_id = ? AND action = 'rebalance'
                     AND source = 'retry' AND status = 'pending'
                   ORDER BY created_at, work_id LIMIT 1""",
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE work_items SET status = 'cancelled', finished_at = ?, message = ?
                   WHERE work_id = ? AND status = 'pending'""",
                (
                    timestamp,
                    "Cancelled because a newer immediate Run succeeded.",
                    row["work_id"],
                ),
            )
            connection.commit()
            changed = connection.execute(
                "SELECT * FROM work_items WHERE work_id = ?", (row["work_id"],)
            ).fetchone()
            return self._work(changed)

    def reschedule_work_item(self, work_id: str, execute_at: str, message: str) -> WorkItem:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE work_items SET execute_at = ?, message = ?
                   WHERE work_id = ? AND status = 'pending'""",
                (execute_at, message, work_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise StateError("Only pending Work Items may be rescheduled.")
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_id = ?", (work_id,)
            ).fetchone()
            return self._work(row)

    def start_work_batch(self, starts: Sequence[WorkRunStart], started_at: str) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                for assignment in starts:
                    self._insert_run(connection, assignment.run)
                    cursor = connection.execute(
                        """UPDATE work_items
                           SET status = 'running', run_id = ?, started_at = ?
                           WHERE work_id = ? AND status = 'pending'""",
                        (assignment.run.run_id, started_at, assignment.work_id),
                    )
                    if cursor.rowcount != 1:
                        raise StateError("Work Item was no longer pending.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def complete_work_item(
        self,
        work_id: str,
        result: AccountRunResult,
        message: Optional[str] = None,
        retry_item: Optional[WorkItem] = None,
    ) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                cursor = connection.execute(
                    """UPDATE work_items
                       SET status = 'completed', message = ?, finished_at = ?
                       WHERE work_id = ? AND status = 'running' AND run_id = ?""",
                    (message, result.record.finished_at, work_id, result.run_id),
                )
                if cursor.rowcount != 1:
                    raise StateError("Running Work Item could not be completed.")
                if retry_item is not None:
                    self._validate_new_work_item(retry_item)
                    connection.execute(
                        """INSERT INTO work_items
                           (work_id, submission_id, account_id, action, source, status, execute_at,
                            request_token_hash, run_id, message, created_at, started_at,
                            finished_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        self._work_values(retry_item),
                    )
                self._finish_run(connection, result)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def complete_busy_work(
        self,
        work_id: str,
        start: RunStart,
        result: AccountRunResult,
        message: str,
    ) -> None:
        """Create a direct terminal STRATEGY_BUSY Run and complete pending work atomically."""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                self._insert_terminal_run(connection, start, result)
                cursor = connection.execute(
                    """UPDATE work_items SET status = 'completed', run_id = ?, message = ?,
                           started_at = ?, finished_at = ?
                       WHERE work_id = ? AND status = 'pending'""",
                    (
                        result.run_id,
                        message,
                        result.record.started_at,
                        result.record.finished_at,
                        work_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateError("Pending Work Item could not be completed as busy.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _work(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            work_id=row["work_id"],
            submission_id=row["submission_id"],
            account_id=row["account_id"],
            action=WorkAction(row["action"]),
            source=WorkSource(row["source"]),
            status=WorkStatus(row["status"]),
            execute_at=row["execute_at"],
            request_token_hash=row["request_token_hash"],
            run_id=row["run_id"],
            message=row["message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    # Runs -------------------------------------------------------------
    def start_runs(self, starts: Sequence[RunStart]) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                for start in starts:
                    self._insert_run(connection, start)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _insert_run(connection: sqlite3.Connection, start: RunStart) -> None:
        connection.execute(
            """INSERT INTO runs
               (run_id, batch_id, account_id, strategy_name, replacement_set,
                trading_mode, run_mode, source, status, outcome, failure_reason,
                error_message, requested_at, started_at, finished_at, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, ?, NULL, NULL, NULL)""",
            (
                start.run_id,
                start.batch_id,
                start.account.account_id,
                start.account.strategy_name,
                start.account.replacement_set,
                start.trading_mode,
                start.run_mode.value,
                start.source.value,
                start.requested_at,
            ),
        )

    def finish_run(
        self, result: AccountRunResult, retry_item: Optional[WorkItem] = None
    ) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                if retry_item is not None:
                    self._validate_new_work_item(retry_item)
                    connection.execute(
                        """INSERT INTO work_items
                           (work_id, submission_id, account_id, action, source, status, execute_at,
                            request_token_hash, run_id, message, created_at, started_at,
                            finished_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        self._work_values(retry_item),
                    )
                self._finish_run(connection, result)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _finish_run(connection: sqlite3.Connection, result: AccountRunResult) -> None:
        cursor = connection.execute(
            """UPDATE runs SET status = 'terminal', outcome = ?, failure_reason = ?,
                   error_message = ?, started_at = ?, finished_at = ?, result_json = ?
               WHERE run_id = ? AND status = 'running'""",
            (
                result.outcome.value,
                result.failure_reason.value if result.failure_reason else None,
                result.error_message,
                result.record.started_at,
                result.record.finished_at,
                result.model_dump_json(),
                result.run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StateError("Running Run could not be finished.")

    @staticmethod
    def _insert_terminal_run(
        connection: sqlite3.Connection, start: RunStart, result: AccountRunResult
    ) -> None:
        connection.execute(
            """INSERT INTO runs
               (run_id, batch_id, account_id, strategy_name, replacement_set,
                trading_mode, run_mode, source, status, outcome, failure_reason,
                error_message, requested_at, started_at, finished_at, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'terminal', ?, ?, ?, ?, ?, ?, ?)""",
            (
                start.run_id,
                start.batch_id,
                start.account.account_id,
                start.account.strategy_name,
                start.account.replacement_set,
                start.trading_mode,
                start.run_mode.value,
                start.source.value,
                result.outcome.value,
                result.failure_reason.value if result.failure_reason else None,
                result.error_message,
                start.requested_at,
                result.record.started_at,
                result.record.finished_at,
                result.model_dump_json(),
            ),
        )

    def get_run(self, run_id: str) -> Optional[StoredRun]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._run(row) if row is not None else None

    def list_runs(
        self,
        *,
        account_id: Optional[str] = None,
        outcome: Optional[RunOutcome] = None,
        source: Optional[RunSource] = None,
        since: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[StoredRun]:
        if not 1 <= limit <= MAX_RUN_QUERY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_RUN_QUERY_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("account_id", account_id),
            ("outcome", outcome.value if outcome else None),
            ("source", source.value if source else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        if since is not None:
            clauses.append("requested_at >= ?")
            values.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, offset))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} "
                "ORDER BY requested_at DESC, run_id DESC LIMIT ? OFFSET ?",
                tuple(values),
            ).fetchall()
            return [self._run(row) for row in rows]

    def has_successful_preview_since(self, account_id: str, since: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM runs
                   WHERE account_id = ? AND run_mode = 'preview' AND status = 'terminal'
                     AND outcome = 'PREVIEWED' AND requested_at > ?
                   LIMIT 1""",
                (account_id, since),
            ).fetchone()
            return row is not None

    @staticmethod
    def _run(row: sqlite3.Row) -> StoredRun:
        payload = row["result_json"]
        result = AccountRunResult.model_validate_json(payload) if payload else None
        return StoredRun(
            run_id=row["run_id"],
            batch_id=row["batch_id"],
            account_id=row["account_id"],
            strategy_name=row["strategy_name"],
            replacement_set=row["replacement_set"],
            trading_mode=row["trading_mode"],
            run_mode=row["run_mode"],
            source=row["source"],
            status=RunStatus(row["status"]),
            outcome=row["outcome"],
            failure_reason=row["failure_reason"],
            error_message=row["error_message"],
            requested_at=row["requested_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result=result,
        )

    def recover_interrupted(self, now: Optional[datetime] = None) -> tuple[int, int]:
        timestamp = utc_now_text(now)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                work = connection.execute(
                    """UPDATE work_items SET status = 'interrupted', finished_at = ?,
                           message = 'Interrupted by process restart; not replayed.'
                       WHERE status = 'running'""",
                    (timestamp,),
                ).rowcount
                runs = connection.execute(
                    """UPDATE runs SET status = 'interrupted'
                       WHERE status = 'running'"""
                ).rowcount
                connection.commit()
                return work, runs
            except BaseException:
                connection.rollback()
                raise

    def prune_terminal(self, now: Optional[datetime] = None) -> tuple[int, int]:
        value = now or datetime.now(timezone.utc)
        cutoff = utc_now_text(value - timedelta(days=RETENTION_DAYS))
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                work = connection.execute(
                    """DELETE FROM work_items
                       WHERE status IN ('completed', 'cancelled') AND finished_at < ?""",
                    (cutoff,),
                ).rowcount
                runs = connection.execute(
                    "DELETE FROM runs WHERE status = 'terminal' AND finished_at < ?",
                    (cutoff,),
                ).rowcount
                connection.commit()
                return work, runs
            except BaseException:
                connection.rollback()
                raise

    # Sessions ---------------------------------------------------------
    def create_session(self, session: WebSession) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO web_sessions
                   (session_hash, csrf_hash, authenticated, created_at, last_seen_at,
                    absolute_expires_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session.session_hash,
                    session.csrf_hash,
                    int(session.authenticated),
                    session.created_at,
                    session.last_seen_at,
                    session.absolute_expires_at,
                ),
            )
            connection.commit()

    def rotate_session(self, old_hash: str, session: WebSession) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN")
                connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (old_hash,))
                connection.execute(
                    """INSERT INTO web_sessions
                       (session_hash, csrf_hash, authenticated, created_at, last_seen_at,
                        absolute_expires_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session.session_hash,
                        session.csrf_hash,
                        int(session.authenticated),
                        session.created_at,
                        session.last_seen_at,
                        session.absolute_expires_at,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def get_session(self, session_hash: str) -> Optional[WebSession]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE session_hash = ?", (session_hash,)
            ).fetchone()
            return self._session(row) if row is not None else None

    def touch_session(self, session_hash: str, now: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE web_sessions SET last_seen_at = ? WHERE session_hash = ?",
                (utc_now_text(now), session_hash),
            )
            connection.commit()

    def rotate_session_csrf(self, session_hash: str, csrf_hash: str, now: datetime) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE web_sessions SET csrf_hash = ?, last_seen_at = ?
                   WHERE session_hash = ?""",
                (csrf_hash, utc_now_text(now), session_hash),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise StateError("Dashboard session is no longer available.")

    def delete_session(self, session_hash: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM web_sessions WHERE session_hash = ?", (session_hash,))
            connection.commit()

    def invalidate_sessions(self) -> int:
        with self._connection() as connection:
            count = connection.execute("DELETE FROM web_sessions").rowcount
            connection.commit()
            return count

    def prune_sessions(self, now: datetime, idle_minutes: int = 30) -> int:
        idle_cutoff = utc_now_text(now - timedelta(minutes=idle_minutes))
        current = utc_now_text(now)
        with self._connection() as connection:
            count = connection.execute(
                """DELETE FROM web_sessions
                   WHERE last_seen_at <= ? OR absolute_expires_at <= ?""",
                (idle_cutoff, current),
            ).rowcount
            connection.commit()
            return count

    @staticmethod
    def _session(row: sqlite3.Row) -> WebSession:
        return WebSession(
            session_hash=row["session_hash"],
            csrf_hash=row["csrf_hash"],
            authenticated=bool(row["authenticated"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            absolute_expires_at=row["absolute_expires_at"],
        )

    # Login throttles --------------------------------------------------
    def get_login_failure(self, client_key: str) -> Optional[LoginFailure]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM login_failures WHERE client_key = ?", (client_key,)
            ).fetchone()
            return self._login_failure(row) if row is not None else None

    def record_login_failure(self, client_key: str, now: datetime) -> LoginFailure:
        current = self.get_login_failure(client_key)
        if current is None or parse_utc(current.window_started_at) + timedelta(minutes=15) <= now:
            count = 1
            window_started = utc_now_text(now)
        else:
            count = current.failure_count + 1
            window_started = current.window_started_at
        blocked = utc_now_text(now + timedelta(minutes=15)) if count >= 5 else None
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO login_failures
                   (client_key, failure_count, window_started_at, blocked_until)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(client_key) DO UPDATE SET
                     failure_count = excluded.failure_count,
                     window_started_at = excluded.window_started_at,
                     blocked_until = excluded.blocked_until""",
                (client_key, count, window_started, blocked),
            )
            connection.commit()
        return LoginFailure(
            client_key=client_key,
            failure_count=count,
            window_started_at=window_started,
            blocked_until=blocked,
        )

    def clear_login_failure(self, client_key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM login_failures WHERE client_key = ?", (client_key,))
            connection.commit()

    def prune_login_failures(self, now: datetime) -> int:
        window_cutoff = utc_now_text(now - timedelta(hours=24, minutes=15))
        block_cutoff = utc_now_text(now - timedelta(hours=24))
        with self._connection() as connection:
            count = connection.execute(
                """DELETE FROM login_failures
                   WHERE window_started_at < ? AND (blocked_until IS NULL OR blocked_until < ?)""",
                (window_cutoff, block_cutoff),
            ).rowcount
            connection.commit()
            return count

    @staticmethod
    def _login_failure(row: sqlite3.Row) -> LoginFailure:
        return LoginFailure(
            client_key=row["client_key"],
            failure_count=row["failure_count"],
            window_started_at=row["window_started_at"],
            blocked_until=row["blocked_until"],
        )
