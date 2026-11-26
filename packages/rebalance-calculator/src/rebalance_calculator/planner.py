"""Reserve-aware sell and buy planning (PRD §7.4–7.6).

Pure functions. `EPS = 1e-9` is used everywhere a tolerance is needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Dict, Final, Mapping, Optional, Sequence

from broker_connector_base import Position, Quote, Side

from .models import BuyPlan, Note, PlannedOrder, SellPlan
from .optimizer import _optimize_whole_shares
from .pricing import limit_price

EPS = 1e-9
FRACTIONAL_QUANTITY_UNIT: Final[float] = 0.000001
_FRACTIONAL_QUANTITY_SCALE: Final[int] = int(round(1.0 / FRACTIONAL_QUANTITY_UNIT))
_FRACTIONAL_QUANTITY_QUANTUM: Final[Decimal] = Decimal(str(FRACTIONAL_QUANTITY_UNIT))


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
    scale = _broker_scale(supports_fractional)
    return math.floor(x * scale + EPS) / scale


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


def _fractional_residue_note(symbol: str, quantity: float) -> Note:
    return Note(
        "warning",
        f"{symbol}: {quantity:.4f} shares can't be sold through the API "
        f"(fractional). Sell them manually in TWS or the IBKR app.",
    )


def _priority_order(candidates: Sequence[_BuyCandidate]) -> list[_BuyCandidate]:
    """Fractional-path priority: lowest weight, then descending symbol."""
    by_descending_symbol = sorted(candidates, key=lambda row: row.symbol, reverse=True)
    return sorted(by_descending_symbol, key=lambda row: row.weight)


def _broker_scale(supports_fractional: bool) -> int:
    return _FRACTIONAL_QUANTITY_SCALE if supports_fractional else 1


def _format_fractional_quantity(quantity: float) -> str:
    value = Decimal(str(quantity)).quantize(_FRACTIONAL_QUANTITY_QUANTUM)
    return format(value, "f").rstrip("0").rstrip(".")


def _as_units(quantity: float, scale: int) -> int:
    return int(round(quantity * scale))


def _as_quantity(units: int, scale: int) -> float:
    return units / scale


def _affordable_units(available: float, price: float, scale: int) -> int:
    """Maximum broker units affordable with `available`, without per-unit loops."""
    if available <= 0:
        return 0
    units = int(
        (Decimal(str(available)) * scale / Decimal(str(price))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return max(0, units)


def _fit_buys(
    candidates: Sequence[_BuyCandidate], budget: float, supports_fractional: bool
) -> None:
    """Preserve the fractional path's deterministic affordability behavior."""
    scale = _broker_scale(supports_fractional)
    priority = _priority_order(candidates)
    cost = sum(row.quantity * row.limit_price for row in candidates)

    # Remove low-priority quantities until one partial reduction makes the plan fit.
    for row in priority:
        if cost <= budget + EPS:
            break
        cost_without_row = cost - row.quantity * row.limit_price
        if cost_without_row > budget + EPS:
            row.quantity = 0.0
            cost = cost_without_row
            continue
        retained_units = min(
            _as_units(row.quantity, scale),
            _affordable_units(budget + EPS - cost_without_row, row.limit_price, scale),
        )
        row.quantity = _as_quantity(retained_units, scale)
        cost = cost_without_row + row.quantity * row.limit_price
        if cost > budget + EPS and retained_units > 0:
            row.quantity = _as_quantity(retained_units - 1, scale)
            cost = cost_without_row + row.quantity * row.limit_price
        break

    # Restore each reduced candidate once, using the same priority order.
    for row in priority:
        removed_units = _as_units(row.raw_quantity - row.quantity, scale)
        if removed_units <= 0:
            continue
        restore_units = min(
            removed_units,
            _affordable_units(budget + EPS - cost, row.limit_price, scale),
        )
        if restore_units <= 0:
            continue
        row.quantity = _as_quantity(_as_units(row.quantity, scale) + restore_units, scale)
        cost += _as_quantity(restore_units, scale) * row.limit_price
        if cost > budget + EPS:
            row.quantity = _as_quantity(_as_units(row.quantity, scale) - 1, scale)
            cost -= _as_quantity(1, scale) * row.limit_price


def plan_sells(
    positions: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash: float,
    reserve: float,
    slippage_pct: float,
    supports_fractional: bool,
    optimization_timeout_seconds: float,
) -> SellPlan:
    """Build the one sell wave against `max(0, V0 - reserve)`.

    `reserve` is the fixed dollar reserve calculated by the caller from V0.
    """
    marks = _marks_held(positions, quotes)
    total_value = cash + sum(p.quantity * marks[p.symbol] for p in positions)

    if not supports_fractional:
        optimized = _optimize_whole_shares(
            positions=positions,
            targets=targets,
            quotes=quotes,
            cash=cash,
            reserve=reserve,
            slippage_pct=slippage_pct,
            buy_only=False,
            optimization_timeout_seconds=optimization_timeout_seconds,
        )
        orders = tuple(
            PlannedOrder(row.symbol, Side.SELL, float(row.sell), None)
            for row in optimized.symbols
            if row.sell > 0
        )
        notes = tuple(
            _fractional_residue_note(row.symbol, row.final_quantity)
            for row in optimized.symbols
            if targets.get(row.symbol, 0.0) == 0 and row.final_quantity > EPS
        )
        return SellPlan(total_value=total_value, orders=orders, notes=notes)

    ordered = sorted(positions, key=lambda p: p.symbol)
    target_base = max(0.0, total_value - reserve)
    orders: list[PlannedOrder] = []
    notes: list[Note] = []

    for p in ordered:
        symbol = p.symbol
        w = targets.get(symbol, 0.0)
        if w == 0:  # not a target: sell all of it
            qty = round_quantity(p.quantity, supports_fractional)
            leftover = p.quantity - qty
            if leftover > EPS:
                notes.append(_fractional_residue_note(symbol, leftover))
        else:  # a target that is over its weight
            excess_value = p.quantity * marks[symbol] - w * target_base
            if excess_value <= 0:
                qty = 0
            else:
                qty = min(
                    round_quantity(p.quantity, supports_fractional),
                    round_quantity(excess_value / marks[symbol], supports_fractional),
                )
        if qty > 0:
            orders.append(PlannedOrder(symbol, Side.SELL, qty, None))

    return SellPlan(
        total_value=total_value,
        orders=tuple(orders),
        notes=tuple(notes),
    )


def _cash_at_or_below_reserve_plan(
    total_value: float,
    cash_base: float,
    reserve: float,
    budget: float,
    prior_notes: tuple[Note, ...] = (),
) -> BuyPlan:
    warning = Note(
        "warning",
        f"Cash ${cash_base:,.2f} is at or below the minimum reserve "
        f"${reserve:,.2f}; no buys placed.",
    )
    return BuyPlan(
        total_value=total_value,
        orders=(),
        notes=(*prior_notes, warning),
        cash_base=cash_base,
        reserve=reserve,
        budget=budget,
    )


def plan_buys(
    positions_after: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash_base: float,
    reserve: float,
    slippage_pct: float,
    supports_fractional: bool,
    optimization_timeout_seconds: float,
) -> BuyPlan:
    """Build buys against strategy capital `max(0, V1 - reserve)`.

    Whole-share accounts use the portfolio optimizer. Fractional accounts keep
    the existing six-decimal sizing and deterministic affordability behavior.
    """
    marks = _marks_held(positions_after, quotes)
    held = _held_quantities(positions_after)
    total_value_1 = cash_base + sum(p.quantity * marks[p.symbol] for p in positions_after)
    target_base = max(0.0, total_value_1 - reserve)
    budget = cash_base - reserve

    if not supports_fractional:
        optimized = _optimize_whole_shares(
            positions=positions_after,
            targets=targets,
            quotes=quotes,
            cash=cash_base,
            reserve=reserve,
            slippage_pct=slippage_pct,
            buy_only=True,
            optimization_timeout_seconds=optimization_timeout_seconds,
        )
        if budget <= 0:
            return _cash_at_or_below_reserve_plan(
                total_value_1, cash_base, reserve, budget
            )
        orders = tuple(
            PlannedOrder(
                row.symbol,
                Side.BUY,
                float(row.buy),
                limit_price(quotes[row.symbol].buy_reference()[0], slippage_pct),
            )
            for row in optimized.symbols
            if row.buy > 0
        )
        trimmed = tuple(
            row
            for row in optimized.symbols
            if targets.get(row.symbol, 0.0) > 0
            and row.final_whole < row.baseline_final_whole
        )
        notes: list[Note] = []
        for row in optimized.symbols:
            if (
                targets.get(row.symbol, 0.0) > 0
                and held.get(row.symbol, 0.0) == 0
                and row.buy == 0
            ):
                reference = quotes[row.symbol].buy_reference()[0]
                desired_value = targets[row.symbol] * target_base
                if 0 < desired_value < reference:
                    notes.append(Note(
                        "info",
                        f"{row.symbol}: target ${desired_value:,.2f} would buy less than "
                        f"one share at ${reference:,.2f}; not bought.",
                    ))
        if trimmed:
            parts: list[str] = []
            for row in trimmed:
                difference = row.baseline_final_whole - row.final_whole
                if row.final_whole == 0:
                    parts.append(
                        f"{row.symbol} skipped ({targets[row.symbol] * 100:.1f}% target)"
                    )
                else:
                    plural = "share" if difference == 1 else "shares"
                    parts.append(f"{row.symbol} reduced by {difference} {plural}")
            notes.append(Note(
                "warning",
                "Not enough cash for all buys: " + "; ".join(parts) + ".",
            ))
        return BuyPlan(
            total_value=total_value_1,
            orders=orders,
            notes=tuple(notes),
            trimmed_symbols=tuple(row.symbol for row in trimmed),
            cash_base=cash_base,
            reserve=reserve,
            budget=budget,
        )

    raw_buys: list[_BuyCandidate] = []
    notes: list[Note] = []
    for symbol in sorted(targets):
        w = targets[symbol]
        held_value = held.get(symbol, 0.0) * marks.get(symbol, 0.0)
        # marks only covers held symbols; an unheld target's held_value is 0.
        desired_value = w * target_base
        gap = desired_value - held_value
        if gap <= 0:
            continue
        quote = quotes[symbol]
        ref = quote.buy_reference()[0]
        limit = limit_price(ref, slippage_pct)
        qty = round_quantity(gap / ref, supports_fractional)
        if qty == 0 and held.get(symbol, 0.0) == 0:
            minimum_quantity = (
                "the broker minimum of "
                f"{_format_fractional_quantity(FRACTIONAL_QUANTITY_UNIT)} share"
                if supports_fractional else "one share"
            )
            notes.append(Note(
                "info",
                f"{symbol}: target ${desired_value:,.2f} would buy less than "
                f"{minimum_quantity} at ${ref:,.2f}; not bought.",
            ))
            continue
        if qty > 0:
            raw_buys.append(_BuyCandidate(symbol, w, qty, qty, limit))

    if budget <= 0:
        # CASH-3: no buys placed; return with just this warning note.
        return _cash_at_or_below_reserve_plan(
            total_value_1, cash_base, reserve, budget, tuple(notes)
        )

    raw_cost = sum(row.quantity * row.limit_price for row in raw_buys)
    if raw_cost > budget + EPS:
        _fit_buys(raw_buys, budget, supports_fractional)

    final_cost = sum(row.quantity * row.limit_price for row in raw_buys)
    if final_cost > budget + EPS:
        raise RuntimeError(
            f"Buy plan cost ${final_cost:.10f} exceeds budget ${budget:.10f}."
        )

    scale = _broker_scale(supports_fractional)
    for row in raw_buys:
        units = _as_units(row.quantity, scale)
        if row.quantity < -EPS or row.quantity > row.raw_quantity + EPS:
            raise RuntimeError(f"Buy quantity for {row.symbol} is outside its raw range.")
        if abs(row.quantity - _as_quantity(units, scale)) > EPS:
            raise RuntimeError(f"Buy quantity for {row.symbol} violates broker granularity.")
        if row.quantity < row.raw_quantity - EPS:
            unit_cost = row.limit_price / scale
            if final_cost + unit_cost <= budget + EPS:
                raise RuntimeError(
                    f"Buy plan left enough budget for one more broker unit of {row.symbol}."
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
                    _format_fractional_quantity(diff)
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
