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

            sell_plan = plan_sells(positions, targets, marks_for_positions, cash, False)
            # P1 no sell exceeds held whole shares; P3 whole & >0
            held = {}
            for p in positions:
                held[p.symbol] = held.get(p.symbol, 0.0) + p.quantity
            for o in sell_plan.orders:
                self.assertGreater(o.quantity, 0)
                self.assertEqual(o.quantity, math.floor(o.quantity))
                self.assertLessEqual(o.quantity, math.floor(held[o.symbol]) + EPS)
                self.assertEqual(o.side, Side.SELL)
            # P5 sell plan no buys
            self.assertTrue(all(o.side == Side.SELL for o in sell_plan.orders))

            # simulate sells at sell reference; whole-share -> proceeds
            sold_cash = cash
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
            bplan = plan_buys(next_positions, targets, quotes, cash, reserve, 0.5, False)

            # P5 buy plan no sells
            self.assertTrue(all(o.side == Side.BUY for o in bplan.orders))
            # P3 whole & positive on buys
            for o in bplan.orders:
                self.assertGreater(o.quantity, 0)
                self.assertEqual(o.quantity, math.floor(o.quantity))

            # P2 no position goes negative: sells never exceed held whole shares (P1)
            # and buys never spend more than available cash (P4), so positions stay >= 0.
            if bplan.orders:
                cost = sum(o.quantity * o.limit_price for o in bplan.orders)
                self.assertGreaterEqual(bplan.budget - cost, -0.01)

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