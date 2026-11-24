"""WP3 acceptance tests: planner (PRD §7.4–7.7, Appendix A.2–A.4)."""

import math
import random
import unittest

from broker_connector_base import Position, Quote, Side
from rebalance_calculator import (
    plan_sells,
    plan_buys,
    round_quantity,
    limit_price,
)

EPS = 1e-9


def quote(bid=None, ask=None, last=None, close=None):
    return Quote("x", bid=bid, ask=ask, last=last, close=close)


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
        plan = plan_sells(self.positions, self.targets, self.marks, self.cash, False)
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
        self.assertEqual(buys["GLD"][0], 22)
        self.assertEqual(buys["GLD"][1], 251.35)
        self.assertEqual(buys["SPY"][0], 8)
        self.assertEqual(buys["SPY"][1], 603.10)
        self.assertEqual(buys["TLT"][0], 4)
        self.assertEqual(buys["TLT"][1], 90.50)
        cost = sum(o.quantity * o.limit_price for o in bplan.orders)
        self.assertAlmostEqual(cost, 10716.50, places=2)
        self.assertTrue(any("GLD reduced by 1 share" in n.text for n in bplan.notes))


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
        self.assertEqual(buys["AAA"], 499)
        self.assertEqual(buys["BBB"], 149)
        self.assertEqual(buys["CCC"], 77)
        cost = sum(o.quantity * o.limit_price for o in bplan.orders)
        self.assertAlmostEqual(cost, 9952.11, places=2)
        self.assertTrue(any("CCC reduced by 2" in n.text for n in bplan.notes))


class BuyPriorityAllocationTests(unittest.TestCase):
    def test_restores_cheaper_buy_after_expensive_share_absorbs_shortfall(self):
        positions = [Position("A", 1990), Position("B", 300)]
        targets = {"A": 0.20, "B": 0.30, "C": 0.50}
        quotes = {
            "A": Quote("A", bid=10.0, ask=10.0),
            "B": Quote("B", bid=100.0, ask=100.0),
            "C": Quote("C", bid=710.0, ask=710.0),
        }

        plan = plan_buys(
            positions, targets, quotes, cash_base=50100.0, reserve=250.0,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("A", 10, 10.05), ("C", 69, 713.55)],
        )

    def test_restored_production_shaped_buy_is_not_reported_as_trimmed(self):
        targets = {"C": 0.95, "PDBC": 0.05}
        quotes = {
            "C": Quote("C", bid=100.0, ask=100.0),
            "PDBC": Quote("PDBC", bid=17.38, ask=17.38),
        }

        plan = plan_buys(
            [], targets, quotes, cash_base=400.0, reserve=100.0,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity, order.limit_price) for order in plan.orders],
            [("C", 2, 100.50), ("PDBC", 1, 17.47)],
        )
        self.assertEqual(
            [note.text for note in plan.notes if note.level == "warning"],
            ["Not enough cash for all buys: C reduced by 1 share."],
        )
        self.assertEqual(plan.trimmed_symbols, ("C",))

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
            [("H", 7), ("L", 4)],
        )
        self.assertTrue(any("L reduced by 16 shares" in note.text for note in plan.notes))

    def test_equal_weights_fund_reverse_alphabetically_and_emit_symbol_order(self):
        targets = {"A": 0.50, "B": 0.50}
        quotes = {
            "A": Quote("A", bid=100.0, ask=100.0),
            "B": Quote("B", bid=100.0, ask=100.0),
        }

        plan = plan_buys(
            [], targets, quotes, cash_base=400.0, reserve=98.5,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("A", 1), ("B", 2)],
        )

    def test_unaffordable_expensive_target_does_not_block_cheaper_target(self):
        targets = {"EXPENSIVE": 0.80, "CHEAP": 0.20}
        quotes = {
            "EXPENSIVE": Quote("EXPENSIVE", bid=200.0, ask=200.0),
            "CHEAP": Quote("CHEAP", bid=10.0, ask=10.0),
        }

        plan = plan_buys(
            [], targets, quotes, cash_base=300.0, reserve=200.0,
            slippage_pct=0.5, supports_fractional=False,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("CHEAP", 6)],
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
            ["Not enough cash for all buys: A reduced by 0.750037 shares."],
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
                "0.000001 share at $1.00; not bought."
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

            sell_plan = plan_sells(
                positions, targets, marks_for_positions, cash, supports_fractional
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

            reserve = 0.0025 * sell_plan.total_value
            bplan = plan_buys(
                next_positions, targets, quotes, cash, reserve, 0.5,
                supports_fractional,
            )
            repeated = plan_buys(
                next_positions, targets, quotes, cash, reserve, 0.5,
                supports_fractional,
            )
            self.assertEqual(bplan, repeated)

            raw_plan = plan_buys(
                next_positions, targets, quotes, cash, -1_000_000_000.0, 0.5,
                supports_fractional,
            )
            raw_by_symbol = {order.symbol: order for order in raw_plan.orders}
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
            unit = 0.000001 if supports_fractional else 1.0
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
        a = plan_sells(positions, targets, marks, 2500.0, False)
        b = plan_sells(positions, targets, marks, 2500.0, False)
        self.assertEqual([(o.symbol, o.quantity) for o in a.orders],
                         [(o.symbol, o.quantity) for o in b.orders])
        pa = plan_buys([Position("GLD", 60), Position("SPY", 70), Position("TLT", 400)],
                       targets, quotes, 10000.0, 25.0, 0.5, False)
        pb = plan_buys([Position("GLD", 60), Position("SPY", 70), Position("TLT", 400)],
                       targets, quotes, 10000.0, 25.0, 0.5, False)
        self.assertEqual([(o.symbol, o.quantity) for o in pa.orders],
                         [(o.symbol, o.quantity) for o in pb.orders])


class PriceFallbackTests(unittest.TestCase):
    def test_weight_zero_target_sold_floor_not_ceil(self):
        positions = [Position("SPY", 10.7)]
        plan = plan_sells(positions, {}, {"SPY": 600.0}, 0.0, False)
        self.assertEqual(plan.orders[0].quantity, 10)

    def test_buy_without_bid_but_ask_bought(self):
        targets = {"A": 1.0}
        quotes = {"A": Quote("A", ask=100.0)}
        bplan = plan_buys([], targets, quotes, 10000.0, 25.0, 0.5, False)
        self.assertGreater(len(bplan.orders), 0)


if __name__ == "__main__":
    unittest.main()
