"""WP3 acceptance tests: planner (PRD §7.4–7.7, Appendix A.2–A.4)."""

import math
import random
import unittest
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from broker_connector_base import Position, Quote, Side
from rebalance_calculator import (
    BuyPlan,
    FRACTIONAL_QUANTITY_UNIT,
    SellPlan,
    plan_sells,
    plan_buys,
    round_quantity,
    limit_price,
)

EPS = 1e-9


def quote(bid=None, ask=None, last=None, close=None):
    return Quote("x", bid=bid, ask=ask, last=last, close=close)


@dataclass(frozen=True)
class RawBuyCandidate:
    symbol: str
    weight: float
    quantity: float
    limit_price: float


def raw_buy_candidates(
    positions: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash_base: float,
    reserve: float,
    slippage_pct: float,
    supports_fractional: bool,
) -> list[RawBuyCandidate]:
    """Independent oracle, intentionally separate from the offline validator."""
    held: dict[str, float] = {}
    for position in positions:
        held[position.symbol] = held.get(position.symbol, 0.0) + position.quantity
    marks = {position.symbol: quotes[position.symbol].mark()[0] for position in positions}
    value = cash_base + sum(
        position.quantity * marks[position.symbol] for position in positions
    )
    target_base = max(0.0, value - reserve)
    candidates: list[RawBuyCandidate] = []
    for symbol in sorted(targets):
        reference = quotes[symbol].buy_reference()[0]
        gap = targets[symbol] * target_base - held.get(symbol, 0.0) * marks.get(symbol, 0.0)
        quantity = round_quantity(gap / reference, supports_fractional)
        if quantity > 0:
            candidates.append(RawBuyCandidate(
                symbol, targets[symbol], quantity,
                limit_price(reference, slippage_pct),
            ))
    return candidates


def literal_fit(
    candidates: Sequence[RawBuyCandidate],
    budget: float,
    supports_fractional: bool,
) -> dict[str, float]:
    """Independent unit-step oracle; sharing fitter code would hide common defects."""
    scale = int(round(1.0 / FRACTIONAL_QUANTITY_UNIT)) if supports_fractional else 1
    raw_units = {row.symbol: int(round(row.quantity * scale)) for row in candidates}
    units = dict(raw_units)
    prices = {row.symbol: row.limit_price for row in candidates}
    priority = sorted(candidates, key=lambda row: row.symbol, reverse=True)
    priority.sort(key=lambda row: row.weight)

    def cost() -> float:
        return sum(units[symbol] / scale * prices[symbol] for symbol in units)

    for row in priority:
        while cost() > budget + EPS and units[row.symbol] > 0:
            units[row.symbol] -= 1
        if cost() <= budget + EPS:
            break
    for row in priority:
        while units[row.symbol] < raw_units[row.symbol]:
            units[row.symbol] += 1
            if cost() > budget + EPS:
                units[row.symbol] -= 1
                break
    return {symbol: quantity / scale for symbol, quantity in units.items() if quantity > 0}


class RoundQuantityTests(unittest.TestCase):
    def test_edges(self):
        # NOTE: PRD §16.2 lists `99.9999999999 -> 99`, but the §7 formula
        # `floor(x + EPS)` with EPS=1e-9 yields 100 for that input (see impl log;
        # the fractional deviation 1e-10 is inside the EPS margin, and such a value
        # only arises from a division result that truly rounds up to 100). The §7
        # formula is authoritative; the §16.2 expectation is recorded as a deviation.
        self.assertEqual(round_quantity(99.9999999999, False), 100)
        self.assertEqual(round_quantity(100.0, False), 100)
        self.assertEqual(round_quantity(100.0 - 1e-14, False), 100)  # float artifact corrected
        self.assertEqual(round_quantity(0.9999, False), 0)
        self.assertEqual(round_quantity(0, False), 0)
        self.assertEqual(round_quantity(-5, False), 0)

    def test_fractional_floor_6_decimals(self):
        self.assertEqual(round_quantity(10.123456789, True), 10.123456)


class LimitPriceTests(unittest.TestCase):
    def test_rounds_half_up_to_2_decimals_above_one(self):
        # 90.05 * 1.005 = 90.50025 -> 90.50 ; 250.10*1.005 -> 251.35
        self.assertEqual(limit_price(90.05, 0.5), 90.50)
        self.assertEqual(limit_price(250.10, 0.5), 251.35)
        self.assertEqual(limit_price(600.10, 0.5), 603.10)
        self.assertEqual(limit_price(10.01, 0.5), 10.06)

    def test_4_decimals_below_one(self):
        p = limit_price(0.67, 0.5)
        self.assertAlmostEqual(p, 0.6734, places=4)


class AppendixA2Tests(unittest.TestCase):
    """Appendix A.2 — the two-leg run."""

    def setUp(self):
        self.positions = [
            Position("SPY", 70), Position("TLT", 400),
            Position("GLD", 60), Position("XLE", 100.37),
        ]
        self.targets = {"SPY": 0.45, "TLT": 0.35, "GLD": 0.20}
        self.marks = {"SPY": 600.0, "TLT": 90.0, "GLD": 250.0, "XLE": 85.0}
        self.cash = 2500.0
        self.quotes = {
            "SPY": Quote("SPY", bid=599.90, ask=600.10),
            "TLT": Quote("TLT", bid=89.95, ask=90.05),
            "GLD": Quote("GLD", bid=249.90, ask=250.10),
            "XLE": Quote("XLE", bid=84.95, ask=85.05),
        }

    def test_V0_and_sell_plan(self):
        reserve = 0.0025 * (
            self.cash + sum(p.quantity * self.marks[p.symbol] for p in self.positions)
        )
        plan = plan_sells(
            self.positions, self.targets, self.marks, self.cash, reserve, False
        )
        self.assertAlmostEqual(plan.total_value, 104031.45, places=2)
        self.assertEqual(len(plan.orders), 1)
        self.assertEqual(plan.orders[0].symbol, "XLE")
        self.assertEqual(plan.orders[0].side, Side.SELL)
        self.assertEqual(plan.orders[0].quantity, 100)
        self.assertIsNone(plan.orders[0].limit_price)
        self.assertTrue(any("0.3700" in n.text and "can't be sold" in n.text for n in plan.notes))

    def test_reserve_and_buys(self):
        # Simulation: XLE sells at 84.95 -> proceeds 8495 -> cash_base 10995
        cash_base = 2500 + 100 * 84.95
        positions_after = [
            Position("SPY", 70), Position("TLT", 400),
            Position("GLD", 60), Position("XLE", 0.37),
        ]
        v0 = 104031.45
        reserve = 0.0025 * v0  # 0.25%
        self.assertAlmostEqual(reserve, 260.078625, places=4)
        bplan = plan_buys(positions_after, self.targets, self.quotes, cash_base, reserve, 0.5, False)
        self.assertAlmostEqual(bplan.total_value, 104026.45, places=2)
        buys = {o.symbol: (o.quantity, o.limit_price) for o in bplan.orders}
        self.assertEqual(buys["GLD"][0], 23)
        self.assertEqual(buys["GLD"][1], 251.35)
        self.assertEqual(buys["SPY"][0], 7)
        self.assertEqual(buys["SPY"][1], 603.10)
        self.assertEqual(buys["TLT"][0], 3)
        self.assertEqual(buys["TLT"][1], 90.50)
        cost = sum(o.quantity * o.limit_price for o in bplan.orders)
        self.assertAlmostEqual(cost, 10274.25, places=2)
        self.assertEqual(bplan.notes, ())


class AppendixA3Tests(unittest.TestCase):
    def test_cash_shortfall_trims_lowest_weight(self):
        cash = 10000.0
        targets = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
        quotes = {
            "AAA": Quote("AAA", bid=9.99, ask=10.01),
            "BBB": Quote("BBB", bid=19.99, ask=20.01),
            "CCC": Quote("CCC", bid=24.99, ask=25.01),
        }
        reserve = 0.0025 * cash
        self.assertAlmostEqual(reserve, 25.0, places=6)
        bplan = plan_buys([], targets, quotes, cash, reserve, 0.5, False)
        buys = {o.symbol: o.quantity for o in bplan.orders}
        self.assertEqual(buys["AAA"], 498)
        self.assertEqual(buys["BBB"], 149)
        self.assertEqual(buys["CCC"], 78)
        cost = sum(o.quantity * o.limit_price for o in bplan.orders)
        self.assertAlmostEqual(cost, 9967.19, places=2)
        self.assertTrue(any("CCC reduced by 1 share" in n.text for n in bplan.notes))


class BuyPriorityAllocationTests(unittest.TestCase):
    def test_unrestorable_expensive_candidate_does_not_block_cheaper_candidate(self):
        positions = [Position("X", 1)]
        targets = {"LEXP": 0.10, "LCHEAP": 0.20, "H": 0.70}
        quotes = {
            "LEXP": Quote("LEXP", bid=100.0, ask=100.0),
            "LCHEAP": Quote("LCHEAP", bid=10.0, ask=10.0),
            "H": Quote("H", bid=100.0, ask=100.0),
            "X": Quote("X", last=650.0),
        }

        plan = plan_buys(
            positions, targets, quotes, cash_base=350.0, reserve=0.0,
            slippage_pct=0.0, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("H", 3, 100.0), ("LCHEAP", 5, 10.0)],
        )
        self.assertEqual(plan.trimmed_symbols, ("H", "LCHEAP", "LEXP"))
        self.assertTrue(any(
            "LCHEAP reduced by 15 shares" in note.text for note in plan.notes
        ))

    def test_fully_restored_candidate_disappears_from_trim_reporting(self):
        positions = [Position("X", 1)]
        targets = {"LOW": 0.05, "HIGH": 0.95}
        quotes = {
            "LOW": Quote("LOW", bid=10.0, ask=10.0),
            "HIGH": Quote("HIGH", bid=90.0, ask=90.0),
            "X": Quote("X", last=100.0),
        }

        plan = plan_buys(
            positions, targets, quotes, cash_base=110.0, reserve=0.0,
            slippage_pct=0.0, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("HIGH", 1, 90.0), ("LOW", 1, 10.0)],
        )
        self.assertEqual(
            [note.text for note in plan.notes if note.level == "warning"],
            ["Not enough cash for all buys: HIGH reduced by 1 share."],
        )
        self.assertEqual(plan.trimmed_symbols, ("HIGH",))

    def test_partially_restores_lower_priority_target(self):
        targets = {"H": 0.80, "L": 0.20}
        quotes = {
            "H": Quote("H", bid=100.0, ask=100.0),
            "L": Quote("L", bid=10.0, ask=10.0),
        }

        plan = plan_buys(
            [], targets, quotes, cash_base=1000.0, reserve=250.0,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("H", 6), ("L", 14)],
        )
        self.assertTrue(any("L reduced by 1 share" in note.text for note in plan.notes))

    def test_equal_weights_fund_reverse_alphabetically_and_emit_symbol_order(self):
        targets = {"A": 0.50, "B": 0.50}
        quotes = {
            "A": Quote("A", bid=100.0, ask=100.0),
            "B": Quote("B", bid=100.0, ask=100.0),
        }

        positions = [Position("X", 0.5)]
        quotes["X"] = Quote("X", last=200.0)
        plan = plan_buys(
            positions, targets, quotes, cash_base=400.0, reserve=98.5,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("A", 2), ("B", 1)],
        )

    def test_unaffordable_expensive_target_does_not_block_cheaper_target(self):
        targets = {"EXPENSIVE": 0.80, "CHEAP": 0.20}
        quotes = {
            "EXPENSIVE": Quote("EXPENSIVE", bid=200.0, ask=200.0),
            "CHEAP": Quote("CHEAP", bid=10.0, ask=10.0),
            "X": Quote("X", last=700.0),
        }

        plan = plan_buys(
            [Position("X", 1)], targets, quotes, cash_base=300.0, reserve=0.0,
            slippage_pct=0.0, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("CHEAP", 10), ("EXPENSIVE", 1)],
        )

    def test_budget_boundaries(self):
        targets = {"A": 1.0}
        quotes = {"A": Quote("A", bid=10.0, ask=10.0)}
        cash_base = 10.10
        cases = [
            ("exact", 10.05, 1),
            ("one cent below", 10.04, 0),
            ("one cent above", 10.06, 1),
            ("inside EPS", 10.05 - EPS, 1),
            ("outside EPS", 10.05 - 2 * EPS, 0),
        ]

        for name, budget, expected_quantity in cases:
            with self.subTest(name=name):
                plan = plan_buys(
                    [], targets, quotes, cash_base=cash_base,
                    reserve=cash_base - budget, slippage_pct=0.5,
                    supports_fractional=False,
                )
                quantity = plan.orders[0].quantity if plan.orders else 0
                self.assertEqual(quantity, expected_quantity)

    def test_zero_negative_empty_and_no_shortfall_behavior(self):
        targets = {"A": 1.0}
        quotes = {"A": Quote("A", bid=10.0, ask=10.0)}
        for reserve in (100.0, 101.0):
            with self.subTest(reserve=reserve):
                plan = plan_buys([], targets, quotes, 100.0, reserve, 0.5, False)
                self.assertEqual(plan.orders, ())
                self.assertEqual(len(plan.notes), 1)
                self.assertIn("no buys placed", plan.notes[0].text)

        empty = plan_buys([], {}, {}, 100.0, 25.0, 0.5, False)
        self.assertEqual(empty.orders, ())
        self.assertEqual(empty.notes, ())

        no_shortfall = plan_buys([], targets, quotes, 10.10, 0.04, 0.5, False)
        self.assertEqual([(o.symbol, o.quantity) for o in no_shortfall.orders], [("A", 1)])
        self.assertEqual(no_shortfall.notes, ())

    def test_fractional_sub_dollar_buy_uses_six_decimals_in_order_and_warning(self):
        targets = {"A": 1.0}
        quotes = {"A": Quote("A", bid=0.67, ask=0.67)}

        plan = plan_buys(
            [], targets, quotes, cash_base=1.0, reserve=0.5,
            slippage_pct=0.5, supports_fractional=True,
        )

        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("A", 0.7425, 0.6734)],
        )
        self.assertEqual(
            [note.text for note in plan.notes if note.level == "warning"],
            ["Not enough cash for all buys: A reduced by 0.003768 shares."],
        )

    def test_fractional_target_below_broker_minimum_has_accurate_note(self):
        plan = plan_buys(
            [], {"A": 1.0}, {"A": Quote("A", bid=1.0, ask=1.0)},
            cash_base=0.0000005, reserve=0.0,
            slippage_pct=0.0, supports_fractional=True,
        )

        self.assertEqual(plan.orders, ())
        self.assertEqual(
            [note.text for note in plan.notes],
            [
                "A: target $0.00 would buy less than the broker minimum of "
                f"{format(Decimal(str(FRACTIONAL_QUANTITY_UNIT)), 'f')} share "
                "at $1.00; not bought."
            ],
        )


class AppendixA4Tests(unittest.TestCase):
    def test_cash_below_reserve_no_buys(self):
        positions_after = [Position("SPY", 10)]
        targets = {"SPY": 0.5, "TLT": 0.5}
        quotes = {
            "SPY": Quote("SPY", bid=599.90, ask=600.10),
            "TLT": Quote("TLT", bid=90.00, ask=90.05),
        }
        cash_base = -50.0
        reserve = 14.875
        bplan = plan_buys(positions_after, targets, quotes, cash_base, reserve, 0.5, False)
        self.assertEqual(bplan.orders, ())
        self.assertLessEqual(bplan.budget, 0)
        self.assertTrue(any("no buys placed" in n.text for n in bplan.notes))


class ReserveAwareAcceptanceTests(unittest.TestCase):
    targets = {"H": 0.80, "M": 0.15, "L": 0.05}
    dollar_quotes = {
        "H": Quote("H", bid=1.0, ask=1.0),
        "M": Quote("M", bid=1.0, ask=1.0),
        "L": Quote("L", bid=1.0, ask=1.0),
    }

    def test_examples_1_and_2_use_desired_value_minus_existing_holdings(self):
        all_cash = plan_buys(
            [], self.targets, self.dollar_quotes, 1000.0, 100.0, 0.0, False
        )
        with_h = plan_buys(
            [Position("H", 700)], self.targets, self.dollar_quotes,
            300.0, 100.0, 0.0, False,
        )

        self.assertEqual(all_cash.total_value - all_cash.reserve, 900.0)
        self.assertEqual(
            {order.symbol: order.quantity for order in all_cash.orders},
            {"H": 720, "L": 45, "M": 135},
        )
        self.assertEqual(
            {order.symbol: order.quantity for order in with_h.orders},
            {"H": 20, "L": 45, "M": 135},
        )
        self.assertEqual(1000.0 - sum(o.quantity for o in all_cash.orders), 100.0)
        self.assertEqual(300.0 - sum(o.quantity for o in with_h.orders), 100.0)

    def test_examples_3_and_4_sell_to_the_net_of_reserve_targets(self):
        fully_invested = [Position("H", 800), Position("M", 150), Position("L", 50)]
        plan3 = plan_sells(
            fully_invested, self.targets,
            {symbol: 1.0 for symbol in self.targets}, 0.0, 100.0, False,
        )
        plan4 = plan_sells(
            [Position("H", 850), Position("X", 100)], self.targets,
            {"H": 1.0, "X": 1.0}, 50.0, 100.0, False,
        )

        self.assertEqual(plan3.total_value - 100.0, 900.0)
        self.assertEqual(
            {order.symbol: order.quantity for order in plan3.orders},
            {"H": 80, "L": 5, "M": 15},
        )
        self.assertEqual(
            {order.symbol: order.quantity for order in plan4.orders},
            {"H": 130, "X": 100},
        )

        after4 = plan_buys(
            [Position("H", 720)], self.targets, self.dollar_quotes,
            280.0, 100.0, 0.0, False,
        )
        self.assertEqual(
            {order.symbol: order.quantity for order in after4.orders},
            {"L": 45, "M": 135},
        )
        self.assertEqual(280.0 - sum(o.quantity for o in after4.orders), 100.0)

    def test_example_5_raw_plan_that_fits_is_unchanged(self):
        quotes = {
            "H": Quote("H", ask=100.0),
            "M": Quote("M", ask=20.0),
            "L": Quote("L", ask=10.0),
        }
        plan = plan_buys([], self.targets, quotes, 1000.0, 100.0, 0.5, False)

        self.assertEqual(
            [(o.symbol, o.quantity, o.limit_price) for o in plan.orders],
            [("H", 7, 100.50), ("L", 4, 10.05), ("M", 6, 20.10)],
        )
        self.assertAlmostEqual(
            sum(o.quantity * o.limit_price for o in plan.orders), 864.30, places=2
        )
        self.assertEqual(plan.trimmed_symbols, ())

    def test_example_6_limit_price_fitting_removes_45_low_weight_shares(self):
        quotes = {
            "H": Quote("H", ask=100.0),
            "M": Quote("M", ask=20.0),
            "L": Quote("L", ask=10.0),
        }
        plan = plan_buys([], self.targets, quotes, 100000.0, 10000.0, 0.5, False)

        self.assertEqual(
            {order.symbol: order.quantity for order in plan.orders},
            {"H": 720, "L": 405, "M": 675},
        )
        self.assertAlmostEqual(
            sum(o.quantity * o.limit_price for o in plan.orders), 89997.75, places=2
        )
        self.assertEqual(plan.trimmed_symbols, ("L",))

    def test_example_7_cascading_removal_and_same_order_add_back(self):
        positions = [Position("M", 4), Position("L", 2), Position("X", 0.5)]
        targets = {"H": 0.50, "M": 0.30, "L": 0.20}
        marks = {"H": 199.005, "M": 49.75, "L": 50.75, "X": 799.0}
        sell_plan = plan_sells(positions, targets, marks, 350.0, 50.0, False)
        self.assertEqual(sell_plan.orders, ())
        self.assertTrue(any("0.5000" in note.text for note in sell_plan.notes))

        quotes = {
            "H": Quote("H", ask=199.005, last=199.005),
            "M": Quote("M", ask=49.75, last=49.75),
            "L": Quote("L", ask=50.75, last=50.75),
            "X": Quote("X", last=799.0),
        }
        buy_plan = plan_buys(
            positions, targets, quotes, 350.0, 50.0, 0.5, False
        )

        self.assertEqual(buy_plan.total_value - buy_plan.reserve, 1000.0)
        self.assertEqual(
            [(o.symbol, o.quantity, o.limit_price) for o in buy_plan.orders],
            [("H", 1, 200.0), ("L", 1, 51.0)],
        )
        self.assertAlmostEqual(
            sum(o.quantity * o.limit_price for o in buy_plan.orders), 251.0, places=2
        )
        self.assertEqual(buy_plan.trimmed_symbols, ("H", "M"))

    def test_reserve_edge_cases_and_fractional_sell_granularity(self):
        zero_reserve = plan_sells(
            [Position("H", 10)], {"H": 1.0}, {"H": 100.0},
            0.0, 0.0, False,
        )
        smaller_than_share = plan_sells(
            [Position("H", 10)], {"H": 1.0}, {"H": 100.0},
            0.0, 50.0, False,
        )
        fractional = plan_sells(
            [Position("H", 10)], {"H": 1.0}, {"H": 100.0},
            0.0, 50.0, True,
        )

        self.assertEqual(zero_reserve.orders, ())
        self.assertEqual(smaller_than_share.orders, ())
        self.assertEqual([(o.symbol, o.quantity) for o in fractional.orders], [("H", 0.5)])

    def test_cash_at_reserve_and_v1_below_reserve_place_no_buys(self):
        at_reserve = plan_buys(
            [Position("H", 720), Position("M", 135), Position("L", 45)],
            self.targets, self.dollar_quotes, 100.0, 100.0, 0.0, False,
        )
        below_reserve = plan_buys(
            [Position("H", 10)], {"H": 1.0}, {"H": Quote("H", last=1.0)},
            5.0, 100.0, 0.0, False,
        )

        self.assertEqual(at_reserve.orders, ())
        self.assertLess(below_reserve.total_value, below_reserve.reserve)
        self.assertEqual(below_reserve.orders, ())

    def test_equal_weight_add_back_uses_descending_symbol_order(self):
        targets = {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
        positions = [Position("X", 1)]
        quotes = {
            "A": Quote("A", ask=100.0),
            "B": Quote("B", ask=50.0),
            "C": Quote("C", ask=20.0),
            "X": Quote("X", last=180.0),
        }
        plan = plan_buys(positions, targets, quotes, 120.0, 0.0, 0.0, False)

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("A", 1), ("C", 1)],
        )
        self.assertEqual(plan.trimmed_symbols, ("B", "C"))

    def test_fractional_exact_budget_and_eps_boundaries(self):
        positions = [Position("X", 1.0)]
        two_units = 2 * FRACTIONAL_QUANTITY_UNIT
        quotes = {"A": Quote("A", ask=1.0), "X": Quote("X", last=two_units)}
        cash_base = two_units
        cases = [
            ("exact", FRACTIONAL_QUANTITY_UNIT, FRACTIONAL_QUANTITY_UNIT),
            (
                "inside EPS",
                FRACTIONAL_QUANTITY_UNIT - EPS,
                FRACTIONAL_QUANTITY_UNIT,
            ),
            ("outside EPS", FRACTIONAL_QUANTITY_UNIT - 2 * EPS, 0.0),
        ]
        for name, budget, expected in cases:
            with self.subTest(name=name):
                plan = plan_buys(
                    positions, {"A": 1.0}, quotes, cash_base, cash_base - budget,
                    0.0, True,
                )
                quantity = plan.orders[0].quantity if plan.orders else 0.0
                self.assertEqual(quantity, expected)


class CapturedSnapshotRegressionTests(unittest.TestCase):
    def test_psq_gld_btal_spxu_counterfactual_fixture(self):
        positions = [
            Position("GLD", 42), Position("RWM", 255),
            Position("BTAL", 555), Position("PSQ", 766),
        ]
        targets = {"PSQ": 0.425, "GLD": 0.275, "BTAL": 0.15, "SPXU": 0.15}
        marks = {"GLD": 325.57, "RWM": 29.95, "BTAL": 13.805, "PSQ": 28.485}
        reserve = 127.8871395
        sells = plan_sells(positions, targets, marks, 362.3808, reserve, False)

        self.assertAlmostEqual(sells.total_value, 51154.8558, places=6)
        self.assertAlmostEqual(sells.total_value - reserve, 51026.9686605, places=6)
        self.assertEqual(
            [(order.symbol, order.quantity) for order in sells.orders],
            [("PSQ", 4), ("RWM", 255)],
        )

        positions_after = [
            Position("GLD", 42), Position("BTAL", 555), Position("PSQ", 762)
        ]
        quotes = {
            "GLD": Quote("GLD", ask=325.66, last=325.57),
            "BTAL": Quote("BTAL", ask=13.81, last=13.805),
            "PSQ": Quote("PSQ", ask=28.48, last=28.485),
            "SPXU": Quote("SPXU", close=40.4000015258789),
        }
        buys = plan_buys(
            positions_after, targets, quotes, 8110.9608, reserve, 0.5, False
        )

        self.assertAlmostEqual(buys.total_value, 51152.2458, places=6)
        self.assertAlmostEqual(buys.total_value - buys.reserve, 51024.3586605, places=6)
        self.assertAlmostEqual(buys.budget, 7983.0736605, places=6)
        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in buys.orders],
            [("GLD", 1, 327.29), ("SPXU", 188, 40.60)],
        )
        self.assertAlmostEqual(
            sum(o.quantity * o.limit_price for o in buys.orders), 7960.09, places=2
        )
        self.assertEqual(buys.trimmed_symbols, ("SPXU",))
        self.assertEqual(
            [note.text for note in buys.notes if note.level == "warning"],
            ["Not enough cash for all buys: SPXU reduced by 1 share."],
        )


class PlannerPropertyTests(unittest.TestCase):
    """P1–P7 over >=5,000 random accounts with a fixed seed (PRD §7.7)."""

    def test_properties_hold_over_random_accounts(self):
        rng = random.Random(424242)
        for _ in range(5000):
            supports_fractional = bool(rng.getrandbits(1))
            n_targets = rng.randint(1, 12)
            # targets sum to 1
            raw = [rng.uniform(0.0, 1.0) for _ in range(n_targets)]
            s = sum(raw)
            targets = {f"SYM{i}": w / s for i, w in enumerate(raw)}
            target_syms = list(targets)

            # prices $2..$1200, spreads 0.01%..1%
            quotes = {}
            for sym in target_syms:
                ref = rng.uniform(2.0, 1200.0)
                spread = ref * rng.uniform(0.0001, 0.01)
                bid, ask = max(ref - spread / 2, 0.01), ref + spread / 2
                quotes[sym] = Quote(sym, bid=bid, ask=ask)

            # holdings: some fractional, some non-target
            held_syms = target_syms + ["OTHER"]
            n_held = rng.randint(0, len(held_syms))
            positions = []
            for sym in rng.sample(held_syms, n_held):
                qty = rng.uniform(0, 1000) + rng.random()  # often fractional
                positions.append(Position(sym, qty))

            # quotes must cover held symbols too (PRD §8.1 step 9/15)
            for p in positions:
                if p.symbol not in quotes:
                    quotes[p.symbol] = Quote(p.symbol, bid=100.0, ask=101.0)

            marks_for_positions = {s: q.mark()[0] for s, q in quotes.items()}
            cash = rng.uniform(0.0, 200000.0)

            # P6 targets sum to 1 within 1e-9
            self.assertAlmostEqual(sum(targets.values()), 1.0, places=9)

            v0 = cash + sum(
                p.quantity * marks_for_positions[p.symbol] for p in positions
            )
            reserve = 0.0025 * v0
            sell_plan = plan_sells(
                positions, targets, marks_for_positions, cash, reserve,
                supports_fractional,
            )
            # P1 no sell exceeds held shares; P3 valid broker granularity & >0
            held = {}
            for p in positions:
                held[p.symbol] = held.get(p.symbol, 0.0) + p.quantity
            for o in sell_plan.orders:
                self.assertGreater(o.quantity, 0)
                if supports_fractional:
                    self.assertAlmostEqual(o.quantity, round(o.quantity, 6), places=9)
                else:
                    self.assertEqual(o.quantity, math.floor(o.quantity))
                self.assertLessEqual(o.quantity, held[o.symbol] + EPS)
                self.assertEqual(o.side, Side.SELL)
            # P5 sell plan no buys
            self.assertTrue(all(o.side == Side.SELL for o in sell_plan.orders))

            # simulate the post-sell positions
            next_positions = []
            for p in positions:
                sell_qty = 0.0
                for o in sell_plan.orders:
                    if o.symbol == p.symbol and o.side == Side.SELL:
                        sell_qty = o.quantity
                remain = p.quantity - sell_qty
                if remain > EPS:
                    next_positions.append(Position(p.symbol, remain))

            bplan = plan_buys(
                next_positions, targets, quotes, cash, reserve, 0.5,
                supports_fractional,
            )
            repeated = plan_buys(
                next_positions, targets, quotes, cash, reserve, 0.5,
                supports_fractional,
            )
            self.assertEqual(bplan, repeated)

            raw_by_symbol = {
                candidate.symbol: candidate
                for candidate in raw_buy_candidates(
                    next_positions, targets, quotes, cash, reserve, 0.5,
                    supports_fractional,
                )
            }
            final_by_symbol = {order.symbol: order for order in bplan.orders}

            # P5 buy plan no sells
            self.assertTrue(all(o.side == Side.BUY for o in bplan.orders))
            self.assertEqual(
                [o.symbol for o in bplan.orders],
                sorted(o.symbol for o in bplan.orders),
            )
            # P3 broker granularity and positivity on buys.
            for o in bplan.orders:
                self.assertGreater(o.quantity, 0)
                if supports_fractional:
                    self.assertAlmostEqual(o.quantity, round(o.quantity, 6), places=9)
                else:
                    self.assertEqual(o.quantity, math.floor(o.quantity))

            # P2 no position goes negative: sells never exceed held quantities (P1)
            # and buys never spend more than available cash (P4), so positions stay >= 0.
            cost = sum(o.quantity * o.limit_price for o in bplan.orders)
            if bplan.budget <= 0:
                self.assertEqual(bplan.orders, ())
                continue

            self.assertLessEqual(cost, bplan.budget + EPS)
            remainder = bplan.budget - cost
            unit = FRACTIONAL_QUANTITY_UNIT if supports_fractional else 1.0
            for symbol, raw_order in raw_by_symbol.items():
                final_quantity = final_by_symbol.get(symbol)
                final_quantity = final_quantity.quantity if final_quantity else 0.0
                self.assertGreaterEqual(final_quantity, 0.0)
                self.assertLessEqual(final_quantity, raw_order.quantity + EPS)
                if final_quantity < raw_order.quantity - EPS:
                    self.assertGreater(unit * raw_order.limit_price, remainder + EPS)

    def test_p7_identical_inputs_identical_plan(self):
        positions = [Position("GLD", 60), Position("SPY", 70), Position("TLT", 400)]
        targets = {"SPY": 0.45, "GLD": 0.20, "TLT": 0.35}
        marks = {"GLD": 250.0, "SPY": 600.0, "TLT": 90.0}
        quotes = {
            "GLD": Quote("GLD", bid=249.90, ask=250.10),
            "SPY": Quote("SPY", bid=599.90, ask=600.10),
            "TLT": Quote("TLT", bid=89.95, ask=90.05),
        }
        a = plan_sells(positions, targets, marks, 2500.0, 0.0, False)
        b = plan_sells(positions, targets, marks, 2500.0, 0.0, False)
        self.assertEqual([(o.symbol, o.quantity) for o in a.orders],
                         [(o.symbol, o.quantity) for o in b.orders])
        pa = plan_buys([Position("GLD", 60), Position("SPY", 70), Position("TLT", 400)],
                       targets, quotes, 10000.0, 25.0, 0.5, False)
        pb = plan_buys([Position("GLD", 60), Position("SPY", 70), Position("TLT", 400)],
                       targets, quotes, 10000.0, 25.0, 0.5, False)
        self.assertEqual([(o.symbol, o.quantity) for o in pa.orders],
                         [(o.symbol, o.quantity) for o in pb.orders])


class BulkVsLiteralRandomizedTests(unittest.TestCase):
    def test_bulk_fitter_matches_literal_reference_for_both_broker_types(self):
        rng = random.Random(20261124)
        for supports_fractional in (False, True):
            for _ in range(500):
                count = rng.randint(1, 5)
                raw_weights = [rng.uniform(0.1, 1.0) for _ in range(count)]
                weight_sum = sum(raw_weights)
                targets = {
                    f"S{index}": weight / weight_sum
                    for index, weight in enumerate(raw_weights)
                }
                if supports_fractional:
                    target_base = rng.uniform(
                        10 * FRACTIONAL_QUANTITY_UNIT,
                        300 * FRACTIONAL_QUANTITY_UNIT,
                    )
                else:
                    target_base = rng.uniform(20.0, 200.0)
                cash_base = rng.uniform(target_base * 0.05, target_base * 0.95)
                positions = [Position("X", 1.0)]
                quotes = {"X": Quote("X", last=target_base - cash_base)}
                for symbol in targets:
                    reference = rng.uniform(1.0, 20.0)
                    quotes[symbol] = Quote(symbol, bid=reference, ask=reference)
                slippage = rng.choice((0.0, 0.5))

                candidates = raw_buy_candidates(
                    positions, targets, quotes, cash_base, 0.0, slippage,
                    supports_fractional,
                )
                expected = literal_fit(candidates, cash_base, supports_fractional)
                plan = plan_buys(
                    positions, targets, quotes, cash_base, 0.0, slippage,
                    supports_fractional,
                )
                actual = {order.symbol: order.quantity for order in plan.orders}

                self.assertEqual(actual, expected)
                self.assertLessEqual(
                    sum(order.quantity * order.limit_price for order in plan.orders),
                    plan.budget + EPS,
                )
                self.assertEqual(
                    [order.symbol for order in plan.orders],
                    sorted(order.symbol for order in plan.orders),
                )


class PriceFallbackTests(unittest.TestCase):
    def test_weight_zero_target_sold_floor_not_ceil(self):
        positions = [Position("SPY", 10.7)]
        plan = plan_sells(positions, {}, {"SPY": 600.0}, 0.0, 0.0, False)
        self.assertEqual(plan.orders[0].quantity, 10)

    def test_buy_without_bid_but_ask_bought(self):
        targets = {"A": 1.0}
        quotes = {"A": Quote("A", ask=100.0)}
        bplan = plan_buys([], targets, quotes, 10000.0, 25.0, 0.5, False)
        self.assertGreater(len(bplan.orders), 0)


if __name__ == "__main__":
    unittest.main()
