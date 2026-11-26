"""Planner regression tests for shared pricing and the unchanged fractional path."""

import random
import unittest
from dataclasses import dataclass
from typing import Mapping, Sequence
from unittest.mock import patch

from broker_connector_base import Position, Quote, Side
from rebalance_calculator import (
    FRACTIONAL_QUANTITY_UNIT,
    limit_price,
    plan_buys,
    plan_sells,
    round_quantity,
)


TIMEOUT = 10.0
EPS = 1e-9
FRACTIONAL_SCALE = int(round(1.0 / FRACTIONAL_QUANTITY_UNIT))


@dataclass
class _FractionalBuyCandidate:
    symbol: str
    weight: float
    raw_units: int
    units: int
    limit_price: float


def _fractional_expected_buys(
    positions: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash_base: float,
    reserve: float,
    slippage_pct: float,
) -> dict[str, float]:
    """Independent unit-step oracle for the preserved fractional behavior."""
    held = {}
    for position in positions:
        held[position.symbol] = held.get(position.symbol, 0.0) + position.quantity
    marks = {
        position.symbol: quotes[position.symbol].mark()[0]
        for position in positions
    }
    total_value = cash_base + sum(
        position.quantity * marks[position.symbol] for position in positions
    )
    target_base = max(0.0, total_value - reserve)
    rows: list[_FractionalBuyCandidate] = []
    for symbol in sorted(targets):
        reference = quotes[symbol].buy_reference()[0]
        gap = targets[symbol] * target_base - held.get(symbol, 0.0) * marks.get(
            symbol, 0.0
        )
        quantity = round_quantity(gap / reference, True)
        if quantity > 0:
            units = int(round(quantity * FRACTIONAL_SCALE))
            rows.append(_FractionalBuyCandidate(
                symbol=symbol,
                weight=targets[symbol],
                raw_units=units,
                units=units,
                limit_price=limit_price(reference, slippage_pct),
            ))

    budget = cash_base - reserve

    def cost() -> float:
        return sum(
            row.units / FRACTIONAL_SCALE * row.limit_price for row in rows
        )

    priority = sorted(rows, key=lambda row: row.symbol, reverse=True)
    priority.sort(key=lambda row: row.weight)
    for row in priority:
        while cost() > budget + EPS and row.units > 0:
            row.units -= 1
        if cost() <= budget + EPS:
            break
    for row in priority:
        while row.units < row.raw_units:
            row.units += 1
            if cost() > budget + EPS:
                row.units -= 1
                break
    return {
        row.symbol: row.units / FRACTIONAL_SCALE
        for row in rows
        if row.units > 0
    }


class RoundQuantityTests(unittest.TestCase):
    def test_whole_share_edges(self):
        self.assertEqual(round_quantity(99.9999999999, False), 100)
        self.assertEqual(round_quantity(0.9999, False), 0)
        self.assertEqual(round_quantity(0, False), 0)
        self.assertEqual(round_quantity(-5, False), 0)

    def test_fractional_floor_uses_six_decimals(self):
        self.assertEqual(round_quantity(10.123456789, True), 10.123456)


class LimitPriceTests(unittest.TestCase):
    def test_rounds_half_up_to_two_decimals_at_or_above_one(self):
        self.assertEqual(limit_price(90.05, 0.5), 90.50)
        self.assertEqual(limit_price(250.10, 0.5), 251.35)
        self.assertEqual(limit_price(600.10, 0.5), 603.10)

    def test_rounds_half_up_to_four_decimals_below_one(self):
        self.assertEqual(limit_price(0.67, 0.5), 0.6734)


class FractionalPlannerRegressionTests(unittest.TestCase):
    def test_sell_quantities_keep_six_decimal_granularity(self):
        plan = plan_sells(
            positions=(Position("A", 10.123456789),),
            targets={},
            quotes={"A": Quote("A", bid=100.0, ask=100.0)},
            cash=0.0,
            reserve=0.0,
            slippage_pct=0.5,
            supports_fractional=True,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual(
            [(order.symbol, order.side, order.quantity) for order in plan.orders],
            [("A", Side.SELL, 10.123456)],
        )

    def test_sub_dollar_buy_preserves_affordability_and_warning(self):
        plan = plan_buys(
            positions_after=(),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=0.67, ask=0.67)},
            cash_base=1.0,
            reserve=0.5,
            slippage_pct=0.5,
            supports_fractional=True,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("A", 0.7425, 0.6734)],
        )
        self.assertEqual(
            [note.text for note in plan.notes if note.level == "warning"],
            ["Not enough cash for all buys: A reduced by 0.003768 shares."],
        )

    def test_target_below_fractional_minimum_has_accurate_note(self):
        plan = plan_buys(
            positions_after=(),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=1.0, ask=1.0)},
            cash_base=0.0000005,
            reserve=0.0,
            slippage_pct=0.0,
            supports_fractional=True,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual(plan.orders, ())
        self.assertEqual(
            [note.text for note in plan.notes],
            [
                "A: target $0.00 would buy less than the broker minimum of "
                "0.000001 share at $1.00; not bought."
            ],
        )

    def test_cash_at_or_below_reserve_places_no_buys(self):
        plan = plan_buys(
            positions_after=(Position("A", 1.0),),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=10.0, ask=10.0)},
            cash_base=5.0,
            reserve=5.0,
            slippage_pct=0.5,
            supports_fractional=True,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual(plan.orders, ())
        self.assertEqual(plan.trimmed_symbols, ())
        self.assertTrue(any("no buys placed" in note.text for note in plan.notes))

    def test_exact_budget_tolerance_is_preserved(self):
        positions = (Position("X", 1.0),)
        two_units = 2 * FRACTIONAL_QUANTITY_UNIT
        quotes = {
            "A": Quote("A", ask=1.0),
            "X": Quote("X", last=two_units),
        }
        for budget, expected in (
            (FRACTIONAL_QUANTITY_UNIT, FRACTIONAL_QUANTITY_UNIT),
            (FRACTIONAL_QUANTITY_UNIT - EPS, FRACTIONAL_QUANTITY_UNIT),
            (FRACTIONAL_QUANTITY_UNIT - 2 * EPS, 0.0),
        ):
            with self.subTest(budget=budget):
                plan = plan_buys(
                    positions,
                    {"A": 1.0},
                    quotes,
                    two_units,
                    two_units - budget,
                    0.0,
                    True,
                    TIMEOUT,
                )
                quantity = plan.orders[0].quantity if plan.orders else 0.0
                self.assertEqual(quantity, expected)

    def test_repeated_fractional_plan_is_deterministic(self):
        args = (
            (Position("X", 1.0),),
            {"A": 0.6, "B": 0.4},
            {
                "A": Quote("A", bid=10.0, ask=10.0),
                "B": Quote("B", bid=20.0, ask=20.0),
                "X": Quote("X", last=100.0),
            },
            50.0,
            5.0,
            0.5,
            True,
            TIMEOUT,
        )
        self.assertEqual(plan_buys(*args), plan_buys(*args))

    def test_randomized_fractional_plans_match_independent_oracle(self):
        rng = random.Random(20261124)
        with patch(
            "rebalance_calculator.planner._optimize_whole_shares",
            side_effect=AssertionError("whole-share optimizer invoked"),
        ):
            for _ in range(500):
                count = rng.randint(1, 5)
                raw_weights = [rng.uniform(0.1, 1.0) for _ in range(count)]
                weight_sum = sum(raw_weights)
                targets = {
                    f"S{index}": weight / weight_sum
                    for index, weight in enumerate(raw_weights)
                }
                target_base = rng.uniform(
                    10 * FRACTIONAL_QUANTITY_UNIT,
                    300 * FRACTIONAL_QUANTITY_UNIT,
                )
                cash_base = rng.uniform(target_base * 0.05, target_base * 0.95)
                positions = (Position("X", 1.0),)
                quotes = {"X": Quote("X", last=target_base - cash_base)}
                for symbol in targets:
                    reference = rng.uniform(1.0, 20.0)
                    quotes[symbol] = Quote(symbol, bid=reference, ask=reference)
                slippage = rng.choice((0.0, 0.5))

                expected = _fractional_expected_buys(
                    positions,
                    targets,
                    quotes,
                    cash_base,
                    0.0,
                    slippage,
                )
                plan = plan_buys(
                    positions,
                    targets,
                    quotes,
                    cash_base,
                    0.0,
                    slippage,
                    True,
                    TIMEOUT,
                )
                actual = {order.symbol: order.quantity for order in plan.orders}
                self.assertEqual(actual, expected)
                self.assertLessEqual(
                    sum(order.quantity * order.limit_price for order in plan.orders),
                    plan.budget + EPS,
                )


if __name__ == "__main__":
    unittest.main()
