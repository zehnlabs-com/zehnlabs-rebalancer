"""Run result types (PRD §8.4). `AccountRunResult` crosses the process boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Optional

import pydantic
from pydantic import BaseModel


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
    MARKET_CLOSED_DURING_RUN = "MARKET_CLOSED_DURING_RUN"
    ORDER_PLACEMENT_FAILED = "ORDER_PLACEMENT_FAILED"
    ORDERS_FAILED = "ORDERS_FAILED"
    ORDERS_TIMED_OUT = "ORDERS_TIMED_OUT"
    WORKER_CRASHED = "WORKER_CRASHED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ExecutedOrder(BaseModel):
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: float
    filled_quantity: float
    average_fill_price: Optional[float] = None
    status: str  # OrderStatus value


class PlannedOrderOut(BaseModel):
    symbol: str
    side: str
    quantity: float
    limit_price: Optional[float] = None


class NoteOut(BaseModel):
    level: Literal["info", "warning"]
    text: str


class AccountRunResult(BaseModel):
    run_id: str
    account_id: str
    strategy_name: str
    mode: Literal["live", "preview"]
    source: Literal["signal", "manual", "scheduled"]
    outcome: RunOutcome
    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None
    executed: list[ExecutedOrder] = pydantic.Field(default_factory=list)
    planned_sells: list[PlannedOrderOut] = pydantic.Field(default_factory=list)
    planned_buys: list[PlannedOrderOut] = pydantic.Field(default_factory=list)
    notes: list[NoteOut] = pydantic.Field(default_factory=list)
    value_end: Optional[float] = None
    cash_end: Optional[float] = None
    record: dict = pydantic.Field(default_factory=dict)