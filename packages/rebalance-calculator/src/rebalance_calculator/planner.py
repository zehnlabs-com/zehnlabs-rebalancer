"""Rebalance planning: sell plan, buy plan, cash floor (PRD §7.4–7.6).

Pure functions. `EPS = 1e-9` is used everywhere a tolerance is needed.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Mapping, Optional, Sequence

from broker_connector_base import Position, Quote, Side

from .models import BuyPlan, Note, PlannedOrder, SellPlan

EPS = 1e-9


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
    more than `budget = cash_base - reserve`, trimming the lowest-weight buys first."""
    marks = _marks_held(positions_after, quotes)
    held = _held_quantities(positions_after)
    total_value_1 = cash_base + sum(p.quantity * marks[p.symbol] for p in positions_after)

    raw_buys: list[dict] = []  # mutable working rows: symbol, quantity, limit
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
            notes.append(Note(
                "info",
                f"{symbol}: target ${w * total_value_1:,.2f} is less than one share "
                f"(${ref:,.2f}); not bought.",
            ))
            continue
        if qty > 0:
            raw_buys.append({"symbol": symbol, "orig": qty, "quantity": qty,
                             "limit": limit, "weight": w})

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

    cost = sum(row["quantity"] * row["limit"] for row in raw_buys)
    if cost > budget + EPS:
        for row in sorted(raw_buys, key=lambda r: (r["weight"], r["symbol"])):
            if cost <= budget + EPS:
                break
            need = math.ceil((cost - budget) / row["limit"] - EPS)
            cut = min(row["quantity"], need)
            if cut > 0:
                row["quantity"] -= cut
                cost -= cut * row["limit"]

    trimmed = [r for r in raw_buys if r["quantity"] < r["orig"]]
    if trimmed:
        parts = []
        for r in sorted(trimmed, key=lambda x: x["symbol"]):
            diff = r["orig"] - r["quantity"]
            if r["quantity"] == 0:
                parts.append(f"{r['symbol']} skipped ({r['weight'] * 100:.1f}% target)")
            else:
                plural = "share" if diff == 1 else "shares"
                parts.append(f"{r['symbol']} reduced by {int(diff)} {plural}")
        notes.append(Note("warning", "Not enough cash for all buys: " + "; ".join(parts) + "."))

    orders = tuple(
        PlannedOrder(r["symbol"], Side.BUY, r["quantity"], r["limit"])
        for r in raw_buys
        if r["quantity"] > 0
    )
    return BuyPlan(
        total_value=total_value_1,
        orders=orders,
        notes=tuple(notes),
        cash_base=cash_base,
        reserve=reserve,
        budget=budget,
    )