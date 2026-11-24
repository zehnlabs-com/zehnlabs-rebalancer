"""Rebalance planning: sell plan, buy plan, cash floor (PRD §7.4–7.6).

Pure functions. `EPS = 1e-9` is used everywhere a tolerance is needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Mapping, Optional, Sequence

from broker_connector_base import Position, Quote, Side

from .models import BuyPlan, Note, PlannedOrder, SellPlan

EPS = 1e-9


@dataclass
class _BuyCandidate:
    symbol: str
    weight: float
    raw_quantity: float
    quantity: float
    limit_price: float


def round_quantity(x: float, supports_fractional: bool) -> float:
    """Round down a quantity to the broker's granularity (PRD §7)."""
    if x <= 0:
        return 0.0
    if not supports_fractional:
        return float(math.floor(x + EPS))
    return math.floor(x * 1_000_000 + EPS) / 1_000_000


def limit_price(reference: float, slippage_pct: float) -> float:
    """Buy limit price = reference * (1 + slippage_pct/100), rounded half-up.

    PRC-4: 2 decimals if the reference is >= 1.00, else 4 decimals, using
    Decimal with ROUND_HALF_UP.
    """
    raw = reference * (1.0 + slippage_pct / 100.0)
    if reference >= 1.00:
        quantum = Decimal("0.01")
    else:
        quantum = Decimal("0.0001")
    return float(Decimal(str(raw)).quantize(quantum, rounding=ROUND_HALF_UP))


def _marks_held(positions: Sequence[Position], quotes: Mapping[str, Quote]) -> Dict[str, float]:
    """mark price for every symbol currently held."""
    marks: Dict[str, float] = {}
    for p in positions:
        if p.symbol not in marks:
            marks[p.symbol] = quotes[p.symbol].mark()[0]
    return marks


def _held_quantities(positions: Sequence[Position]) -> Dict[str, float]:
    held: Dict[str, float] = {}
    for p in positions:
        held[p.symbol] = held.get(p.symbol, 0.0) + p.quantity
    return held


def plan_sells(
    positions: Sequence[Position],
    targets: Mapping[str, float],
    marks: Mapping[str, float],
    cash: float,
    supports_fractional: bool,
) -> SellPlan:
    """Build the sell plan (PRD §7.4). Marks are the caller-computed mark prices."""
    ordered = sorted(positions, key=lambda p: p.symbol)
    total_value = cash + sum(p.quantity * marks[p.symbol] for p in ordered)
    orders: list[PlannedOrder] = []
    notes: list[Note] = []

    for p in ordered:
        symbol = p.symbol
        w = targets.get(symbol, 0.0)
        if w == 0:  # not a target: sell all of it
            qty = round_quantity(p.quantity, supports_fractional)
            leftover = p.quantity - qty
            if leftover > EPS:
                notes.append(Note(
                    "warning",
                    f"{symbol}: {leftover:.4f} shares can't be sold through the API "
                    f"(fractional). Sell them manually in TWS or the IBKR app.",
                ))
        else:  # a target that is over its weight
            excess_value = p.quantity * marks[symbol] - w * total_value
            if excess_value <= 0:
                qty = 0
            else:
                qty = min(
                    round_quantity(p.quantity, supports_fractional),
                    round_quantity(excess_value / marks[symbol], supports_fractional),
                )
        if qty > 0:
            orders.append(PlannedOrder(symbol, Side.SELL, qty, None))

    return SellPlan(total_value=total_value, orders=tuple(orders), notes=tuple(notes))


def plan_buys(
    positions_after: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash_base: float,
    reserve: float,
    slippage_pct: float,
    supports_fractional: bool,
) -> BuyPlan:
    """Build the buy plan (PRD §7.5–7.6). Buys size at the ask and never spend
    more than `budget = cash_base - reserve`, funding higher-weight buys first."""
    marks = _marks_held(positions_after, quotes)
    held = _held_quantities(positions_after)
    total_value_1 = cash_base + sum(p.quantity * marks[p.symbol] for p in positions_after)

    raw_buys: list[_BuyCandidate] = []
    notes: list[Note] = []
    for symbol in sorted(targets):
        w = targets[symbol]
        held_value = held.get(symbol, 0.0) * marks.get(symbol, 0.0)
        # marks only covers held symbols; an unheld target's held_value is 0.
        gap = w * total_value_1 - held_value
        if gap <= 0:
            continue
        quote = quotes[symbol]
        ref = quote.buy_reference()[0]
        limit = limit_price(ref, slippage_pct)
        qty = round_quantity(gap / ref, supports_fractional)
        if qty == 0 and held.get(symbol, 0.0) == 0:
            minimum_quantity = (
                "the broker minimum of 0.000001 share"
                if supports_fractional else "one share"
            )
            notes.append(Note(
                "info",
                f"{symbol}: target ${w * total_value_1:,.2f} would buy less than "
                f"{minimum_quantity} at ${ref:,.2f}; not bought.",
            ))
            continue
        if qty > 0:
            raw_buys.append(_BuyCandidate(symbol, w, qty, qty, limit))

    budget = cash_base - reserve
    if budget <= 0:
        # CASH-3: no buys placed; return with just this warning note.
        notes.append(Note(
            "warning",
            f"Cash ${cash_base:,.2f} is at or below the minimum reserve "
            f"${reserve:,.2f}; no buys placed.",
        ))
        return BuyPlan(
            total_value=total_value_1, orders=(), notes=tuple(notes),
            cash_base=cash_base, reserve=reserve, budget=budget,
        )

    raw_cost = sum(row.quantity * row.limit_price for row in raw_buys)
    if raw_cost > budget + EPS:
        remaining = budget
        for row in sorted(
            raw_buys, key=lambda candidate: (candidate.weight, candidate.symbol), reverse=True
        ):
            affordable = round_quantity(remaining / row.limit_price, supports_fractional)
            funded = min(row.raw_quantity, affordable)
            if funded * row.limit_price > remaining + EPS:
                unit = 0.000001 if supports_fractional else 1.0
                funded = round_quantity(funded - unit, supports_fractional)
            row.quantity = funded
            remaining -= row.quantity * row.limit_price

    final_cost = sum(row.quantity * row.limit_price for row in raw_buys)
    if final_cost > budget + EPS:
        raise RuntimeError(
            f"Buy plan cost ${final_cost:.10f} exceeds budget ${budget:.10f}."
        )

    trimmed = [row for row in raw_buys if row.quantity < row.raw_quantity]
    if trimmed:
        parts = []
        for row in sorted(trimmed, key=lambda candidate: candidate.symbol):
            diff = row.raw_quantity - row.quantity
            if row.quantity == 0:
                parts.append(f"{row.symbol} skipped ({row.weight * 100:.1f}% target)")
            else:
                display_diff = (
                    f"{diff:.6f}".rstrip("0").rstrip(".")
                    if supports_fractional else str(int(diff))
                )
                plural = "share" if abs(diff - 1) <= EPS else "shares"
                parts.append(f"{row.symbol} reduced by {display_diff} {plural}")
        notes.append(Note("warning", "Not enough cash for all buys: " + "; ".join(parts) + "."))

    orders = tuple(
        PlannedOrder(row.symbol, Side.BUY, row.quantity, row.limit_price)
        for row in sorted(raw_buys, key=lambda candidate: candidate.symbol)
        if row.quantity > 0
    )
    return BuyPlan(
        total_value=total_value_1,
        orders=orders,
        notes=tuple(notes),
        trimmed_symbols=tuple(sorted(row.symbol for row in trimmed)),
        cash_base=cash_base,
        reserve=reserve,
        budget=budget,
    )
