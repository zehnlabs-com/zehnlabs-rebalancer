"""Typed run results crossing the parent/worker process boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


INTERNAL_ERROR_MESSAGE = "An unexpected internal error occurred."


class _ValueModel(BaseModel):
    """Strict base for values crossing the parent/worker boundary."""

    model_config = ConfigDict(extra="forbid")


class RunMode(StrEnum):
    PREVIEW = "preview"
    LIVE = "live"


class RunSource(StrEnum):
    SIGNAL = "signal"
    DASHBOARD = "dashboard"
    INITIAL = "initial"
    SCHEDULED = "scheduled"
    RETRY = "retry"


class WorkAction(StrEnum):
    PREVIEW = "preview"
    REBALANCE = "rebalance"


class RunOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MARKET_CLOSED = "MARKET_CLOSED"
    PREVIEWED = "PREVIEWED"


class FailureReason(StrEnum):
    ALLOCATIONS_UNAVAILABLE = "ALLOCATIONS_UNAVAILABLE"
    TARGETS_INVALID = "TARGETS_INVALID"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CANCEL_NOT_CONFIRMED = "CANCEL_NOT_CONFIRMED"
    FOREIGN_ORDERS = "FOREIGN_ORDERS"
    ACCOUNT_DATA_UNAVAILABLE = "ACCOUNT_DATA_UNAVAILABLE"
    ACCOUNT_DATA_MISMATCH = "ACCOUNT_DATA_MISMATCH"
    PRICING_FAILED = "PRICING_FAILED"
    OPTIMIZATION_FAILED = "OPTIMIZATION_FAILED"
    MARKET_CLOSED_DURING_RUN = "MARKET_CLOSED_DURING_RUN"
    ORDER_PLACEMENT_FAILED = "ORDER_PLACEMENT_FAILED"
    ORDERS_FAILED = "ORDERS_FAILED"
    ORDERS_TIMED_OUT = "ORDERS_TIMED_OUT"
    STRATEGY_BUSY = "STRATEGY_BUSY"
    WORKER_CRASHED = "WORKER_CRASHED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExecutedOrder(_ValueModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    filled_quantity: float
    average_fill_price: Optional[float] = None
    status: str


class PlannedOrderOut(_ValueModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    limit_price: Optional[float] = None


class NoteOut(_ValueModel):
    level: Literal["info", "warning"]
    text: str


class RecordedPosition(_ValueModel):
    symbol: str
    quantity: float


class RecordedAccountState(_ValueModel):
    cash: float
    net_liquidation: float
    positions: list[RecordedPosition] = Field(default_factory=list)


class PriceEvidence(_ValueModel):
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    close: Optional[float] = None
    bar_close: Optional[float] = None
    used_mark: Optional[float] = None
    mark_source: Optional[str] = None
    buy_reference: Optional[float] = None
    buy_source: Optional[str] = None


class RunRecord(_ValueModel):
    """Lossless typed investigation record stored as ``runs.result_json``."""

    run_id: str
    account_id: str
    strategy_name: str
    mode: RunMode
    source: RunSource
    started_at: str
    finished_at: Optional[str] = None
    outcome: Optional[RunOutcome] = None
    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None
    raw_weights: Optional[dict[str, float]] = None
    scaled_weights: Optional[dict[str, float]] = None
    targets: Optional[dict[str, float]] = None
    replacement_set: Optional[str] = None
    state0: Optional[RecordedAccountState] = None
    prices: dict[str, PriceEvidence] = Field(default_factory=dict)
    V0: Optional[float] = None
    reserve: Optional[float] = None
    planned_sells: list[PlannedOrderOut] = Field(default_factory=list)
    cash_base: Optional[float] = None
    V1: Optional[float] = None
    planned_buys: list[PlannedOrderOut] = Field(default_factory=list)
    trimmed: list[str] = Field(default_factory=list)
    executed: list[ExecutedOrder] = Field(default_factory=list)
    notes: list[NoteOut] = Field(default_factory=list)


class AccountRunResult(_ValueModel):
    run_id: str
    account_id: str
    strategy_name: str
    mode: RunMode
    source: RunSource
    outcome: RunOutcome
    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None
    executed: list[ExecutedOrder] = Field(default_factory=list)
    planned_sells: list[PlannedOrderOut] = Field(default_factory=list)
    planned_buys: list[PlannedOrderOut] = Field(default_factory=list)
    notes: list[NoteOut] = Field(default_factory=list)
    value_end: Optional[float] = None
    cash_end: Optional[float] = None
    record: RunRecord
