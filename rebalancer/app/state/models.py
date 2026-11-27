"""Typed durable-state models shared by parent-process services."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..accounts import AccountConfig
from ..results import AccountRunResult, FailureReason, RunMode, RunOutcome, RunSource, WorkAction


def utc_now_text(now: Optional[datetime] = None) -> str:
    """Return a UTC RFC 3339 timestamp using the storage-contract `Z` suffix."""
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class WorkSource(StrEnum):
    DASHBOARD = "dashboard"
    INITIAL = "initial"
    SCHEDULED = "scheduled"
    RETRY = "retry"


class WorkStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunStatus(StrEnum):
    RUNNING = "running"
    TERMINAL = "terminal"
    INTERRUPTED = "interrupted"


class RetryDisposition(StrEnum):
    """Truth used by persistence, dashboard notes, and notifications."""

    NOT_CREATED = "not_created"
    CREATED = "created"
    REUSED = "reused"


class WorkItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    work_id: str
    submission_id: str
    account_id: str
    action: WorkAction
    source: WorkSource
    status: WorkStatus
    execute_at: str
    request_token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: Optional[str] = None
    message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class WorkCreation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item: WorkItem
    created: bool


class RetryDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: RetryDisposition
    item: Optional[WorkItem] = None

    @model_validator(mode="after")
    def _validate_item(self) -> "RetryDecision":
        has_item = self.item is not None
        needs_item = self.disposition in {
            RetryDisposition.CREATED,
            RetryDisposition.REUSED,
        }
        if has_item != needs_item:
            requirement = "must" if needs_item else "must not"
            raise ValueError(
                f"{self.disposition.value} retry decisions {requirement} contain a Work Item"
            )
        if self.item is not None and self.item.status != WorkStatus.PENDING:
            raise ValueError(
                f"{self.disposition.value} retry decisions must contain a pending Work Item"
            )
        if (
            self.disposition == RetryDisposition.CREATED
            and self.item is not None
            and self.item.source != WorkSource.RETRY
        ):
            raise ValueError("created retry decisions must contain retry-source Work Items")
        return self


class RunStart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    batch_id: str
    account: AccountConfig
    trading_mode: str
    run_mode: RunMode
    source: RunSource
    requested_at: str


class WorkRunStart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    work_id: str
    run: RunStart


class StoredRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    batch_id: str
    account_id: str
    strategy_name: str
    replacement_set: Optional[str] = None
    trading_mode: str
    run_mode: RunMode
    source: RunSource
    status: RunStatus
    outcome: Optional[RunOutcome] = None
    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None
    requested_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[AccountRunResult] = None

    @property
    def source_label(self) -> str:
        return {
            RunSource.SIGNAL: "Signal",
            RunSource.DASHBOARD: "Dashboard",
            RunSource.INITIAL: "Initial setup",
            RunSource.SCHEDULED: "Scheduled",
            RunSource.RETRY: "Next-open retry",
        }[self.source]


class SubmissionWorkItemReceipt(BaseModel):
    """One item in a submission, from retained history or its onboarding archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item: WorkItem
    run: Optional[StoredRun] = None
    history_retained: bool


class WorkSubmissionReceipt(BaseModel):
    """Typed aggregate receipt at the state/application boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission_id: str
    items: tuple[SubmissionWorkItemReceipt, ...]


class OnboardingInitializationResult(BaseModel):
    """Explicitly distinguishes a new initialization from a durable replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created: bool
    receipt: WorkSubmissionReceipt


class WebSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_hash: str
    csrf_hash: str
    authenticated: bool
    created_at: str
    last_seen_at: str
    absolute_expires_at: str


class LoginFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_key: str
    failure_count: int
    window_started_at: str
    blocked_until: Optional[str] = None
