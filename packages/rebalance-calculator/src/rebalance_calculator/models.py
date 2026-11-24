"""Rebalance-calculator domain models (PRD §4.1, §7).

Pure value types. `Note` freely crosses process boundaries via the run record
and notification, but the plan/sell/buy models are internal to the planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Tuple

from broker_connector_base import Side


@dataclass(frozen=True)
class Note:
    level: Literal["info", "warning"]
    text: str


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: Side
    quantity: float            # whole number for a whole-share broker
    limit_price: Optional[float] = None  # None = market order; buys always have one


@dataclass(frozen=True)
class RawAllocation:
    symbol: Optional[str]
    weight: Optional[float]


@dataclass(frozen=True)
class Targets:
    """Final, scaled target weights plus the intermediate forms for logging/records.

    `weights` excludes weight-0 entries (TGT-4), sums to 1.0 (or is empty).
    `raw`/`scaled` keep every symbol for LOG-1 / REC-2.
    """

    weights: dict[str, float]
    raw: dict[str, float] = field(default_factory=dict)
    scaled: dict[str, float] = field(default_factory=dict)
    notes: Tuple[Note, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SellPlan:
    total_value: float  # V0
    orders: Tuple[PlannedOrder, ...] = field(default_factory=tuple)
    notes: Tuple[Note, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BuyPlan:
    total_value: float  # V1
    orders: Tuple[PlannedOrder, ...] = field(default_factory=tuple)
    notes: Tuple[Note, ...] = field(default_factory=tuple)
    cash_base: float = 0.0
    reserve: float = 0.0
    budget: float = 0.0
    trimmed_symbols: Tuple[str, ...] = field(default_factory=tuple)
