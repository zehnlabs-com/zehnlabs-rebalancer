"""Whole-share portfolio optimizer acceptance tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import highspy
import pulp
from broker_connector_base import Position, Quote
from rebalance_calculator import OptimizationError, plan_buys, plan_sells
from rebalance_calculator.optimizer import _optimize_whole_shares, _solve


TIMEOUT = 10.0


class ExactWholeShareExamples(unittest.TestCase):
    def optimize(self, *, cash, reserve, targets, prices, positions=()):
        return _optimize_whole_shares(
            positions=positions,
            targets=targets,
            quotes={
                symbol: Quote(symbol, bid=price, ask=price)
                for symbol, price in prices.items()
            },
            cash=cash,
            reserve=reserve,
            slippage_pct=0.0,
            buy_only=False,
            optimization_timeout_seconds=TIMEOUT,
        )

    def test_tie_breaks_preserve_feasible_large_tick_portfolios(self):
        # Expected allocations were independently enumerated in integer ticks.
        cases = (
            ((1170, 66), (55 / 117, 62 / 117), (769.71, 769.72, 282.13, 282.43),
             1008635.96, 2521.5899, 0.5, (1175, 3597), 55351151),
            ((1303, 1630), (32 / 55, 23 / 55), (1272.46, 1272.49, 852.96, 852.99),
             10001113.84, 50005.5692, 0.5, (5919, 6352), 496157208),
            ((1578, 1463), (84 / 149, 65 / 149), (84.30, 84.33, 556.12, 556.15),
             100001113.84, 500005.5692, 0.5, (665908, 78752), 5050063058),
            ((1757, 961), (0.025, 0.975), (477.85, 478.01, 540.76, 540.85),
             10014053.57, 25035.133925, 0.2, (579, 20432), 220058211),
        )
        for held, weights, prices, cash, reserve, slippage, quantities, drift in cases:
            with self.subTest(cash=cash):
                result = _optimize_whole_shares(
                    positions=(Position("A", held[0]), Position("B", held[1])),
                    targets={"A": weights[0], "B": weights[1]},
                    quotes={
                        "A": Quote("A", bid=prices[0], ask=prices[1]),
                        "B": Quote("B", bid=prices[2], ask=prices[3]),
                    },
                    cash=cash, reserve=reserve, slippage_pct=slippage, buy_only=False,
                    optimization_timeout_seconds=TIMEOUT,
                )
                self.assertEqual(tuple(row.final_whole for row in result.symbols), quantities)
                self.assertEqual(result.total_error_ticks, drift)

    def test_global_sell_decision_minimizes_portfolio_drift(self):
        result = _optimize_whole_shares(
            positions=(Position("A", 1), Position("B", 90)),
            targets={"A": 0.06, "B": 0.94},
            quotes={
                "A": Quote("A", bid=100.0, ask=100.0),
                "B": Quote("B", bid=10.0, ask=10.0),
            },
            cash=0.0,
            reserve=0.0,
            slippage_pct=0.0,
            buy_only=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        symbols = {row.symbol: row for row in result.symbols}
        self.assertEqual(
            {
                symbol: (row.final_quantity, row.sell, row.buy, row.error_ticks)
                for symbol, row in symbols.items()
            },
            {
                "A": (0.0, 1, 0, 600_000),
                "B": (94.0, 0, 4, 0),
            },
        )
        self.assertEqual(result.projected_cash_ticks, 600_000)
        self.assertEqual(result.total_error_ticks, 600_000)

    def test_unconstrained_nearest_quantities_choose_lower_half_share_tie(self):
        result = self.optimize(
            cash=1_000.0,
            reserve=100.0,
            targets={"H": 0.80, "M": 0.15, "L": 0.05},
            prices={"H": 100.0, "M": 20.0, "L": 10.0},
        )

        symbols = {row.symbol: row for row in result.symbols}
        self.assertEqual(
            {
                symbol: (row.final_quantity, row.sell, row.buy, row.error_ticks)
                for symbol, row in symbols.items()
            },
            {
                "H": (7.0, 0, 7, 200_000),
                "L": (4.0, 0, 4, 50_000),
                "M": (7.0, 0, 7, 50_000),
            },
        )
        self.assertEqual(result.projected_cash_ticks, 1_200_000)
        self.assertEqual(result.total_error_ticks, 300_000)

    def test_equal_total_drift_prefers_the_higher_weight_symbol(self):
        result = self.optimize(
            cash=100.0,
            reserve=0.0,
            targets={"A": 0.60, "B": 0.40},
            prices={"A": 80.0, "B": 40.0},
        )

        self.assertEqual(
            {row.symbol: row.final_whole for row in result.symbols},
            {"A": 1, "B": 0},
        )
        self.assertEqual(result.total_error_ticks, 600_000)

    def test_nearest_quantities_that_exceed_cash_choose_the_closest_fit(self):
        result = _optimize_whole_shares(
            positions=(),
            targets={"H": 0.80, "M": 0.15, "L": 0.05},
            quotes={
                "H": Quote("H", bid=99.50, ask=100.50),
                "M": Quote("M", bid=19.90, ask=20.10),
                "L": Quote("L", bid=9.95, ask=10.05),
            },
            cash=1_000.0,
            reserve=250.0,
            slippage_pct=0.0,
            buy_only=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        symbols = {row.symbol: row for row in result.symbols}
        self.assertEqual(
            {
                symbol: (row.final_quantity, row.sell, row.buy, row.error_ticks)
                for symbol, row in symbols.items()
            },
            {
                "H": (6.0, 0, 6, 0),
                "L": (4.0, 0, 4, 25_000),
                "M": (5.0, 0, 5, 125_000),
            },
        )
        self.assertEqual(result.projected_cash_ticks, 2_563_000)
        self.assertEqual(result.total_error_ticks, 150_000)

    def test_closeness_overrides_the_old_removal_priority(self):
        result = self.optimize(
            cash=1_000.0,
            reserve=0.0,
            targets={"H": 0.90, "L": 0.10},
            prices={"H": 20.0, "L": 111.11},
        )

        symbols = {row.symbol: row for row in result.symbols}
        self.assertEqual(
            {
                symbol: (row.final_quantity, row.sell, row.buy, row.error_ticks)
                for symbol, row in symbols.items()
            },
            {
                "H": (44.0, 0, 44, 200_000),
                "L": (1.0, 0, 1, 111_100),
            },
        )
        self.assertEqual(result.projected_cash_ticks, 88_900)
        self.assertEqual(result.total_error_ticks, 311_100)

    def test_multiple_shares_are_removed_when_limit_exceeds_mark(self):
        result = _optimize_whole_shares(
            positions=(),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=0.99, ask=1.01)},
            cash=1_000.0,
            reserve=0.0,
            slippage_pct=0.0,
            buy_only=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        row = result.symbols[0]
        self.assertEqual(
            (row.final_quantity, row.sell, row.buy, row.error_ticks),
            (990.0, 0, 990, 100_000),
        )
        self.assertEqual(result.projected_cash_ticks, 1_000)
        self.assertEqual(result.total_error_ticks, 100_000)

    def test_buy_only_recalculation_uses_actual_sell_proceeds(self):
        result = _optimize_whole_shares(
            positions=(Position("B", 90),),
            targets={"A": 0.06, "B": 0.94},
            quotes={
                "A": Quote("A", bid=100.0, ask=100.0),
                "B": Quote("B", bid=10.0, ask=10.0),
            },
            cash=80.0,
            reserve=0.0,
            slippage_pct=0.0,
            buy_only=True,
            optimization_timeout_seconds=TIMEOUT,
        )

        symbols = {row.symbol: row for row in result.symbols}
        self.assertEqual(
            {
                symbol: (row.final_quantity, row.sell, row.buy, row.error_ticks)
                for symbol, row in symbols.items()
            },
            {
                "A": (0.0, 0, 0, 588_000),
                "B": (92.0, 0, 2, 12_000),
            },
        )
        self.assertEqual(result.projected_cash_ticks, 600_000)
        self.assertEqual(result.total_error_ticks, 600_000)

    def test_fractional_residue_is_immutable_for_a_whole_share_broker(self):
        result = self.optimize(
            cash=0.0,
            reserve=80.0,
            targets={"A": 1.0},
            prices={"A": 100.0},
            positions=(Position("A", 10.4),),
        )

        row = result.symbols[0]
        self.assertEqual(
            (row.final_quantity, row.final_whole, row.sell, row.buy, row.error_ticks),
            (9.4, 9, 1, 0, 200_000),
        )
        self.assertEqual(result.projected_cash_ticks, 1_000_000)
        self.assertEqual(result.total_error_ticks, 200_000)

    def test_empty_targets_sell_every_tradable_whole_share(self):
        result = self.optimize(
            cash=0.0,
            reserve=0.0,
            targets={},
            prices={"A": 100.0},
            positions=(Position("A", 2.4),),
        )

        row = result.symbols[0]
        self.assertEqual(
            (row.final_quantity, row.final_whole, row.sell, row.buy, row.error_ticks),
            (0.4, 0, 2, 0, 400_000),
        )
        self.assertEqual(result.projected_cash_ticks, 2_000_000)
        self.assertEqual(result.total_error_ticks, 400_000)

    def test_buy_only_cash_below_reserve_is_a_feasible_no_trade_result(self):
        result = _optimize_whole_shares(
            positions=(Position("A", 1),),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=100.0, ask=100.0)},
            cash=40.0,
            reserve=50.0,
            slippage_pct=0.0,
            buy_only=True,
            optimization_timeout_seconds=TIMEOUT,
        )

        row = result.symbols[0]
        self.assertEqual(
            (row.final_quantity, row.final_whole, row.sell, row.buy, row.error_ticks),
            (1.0, 1, 0, 0, 100_000),
        )
        self.assertEqual(result.projected_cash_ticks, 400_000)
        self.assertEqual(result.total_error_ticks, 100_000)


class PlannerWholeShareBehavior(unittest.TestCase):
    def test_initial_plan_exposes_only_the_optimized_sell_wave(self):
        plan = plan_sells(
            positions=(Position("A", 1), Position("B", 90)),
            targets={"A": 0.06, "B": 0.94},
            quotes={
                "A": Quote("A", bid=100.0, ask=100.0),
                "B": Quote("B", bid=10.0, ask=10.0),
            },
            cash=0.0,
            reserve=0.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        self.assertEqual(plan.total_value, 1_000.0)
        self.assertEqual(
            [(order.symbol, order.side.value, order.quantity) for order in plan.orders],
            [("A", "SELL", 1)],
        )

    def test_non_target_fractional_residue_keeps_the_manual_sale_warning(self):
        plan = plan_sells(
            positions=(Position("A", 2.4),),
            targets={},
            quotes={"A": Quote("A", bid=100.0, ask=100.0)},
            cash=0.0,
            reserve=0.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual([(order.symbol, order.quantity) for order in plan.orders], [("A", 2)])
        self.assertEqual(
            [note.text for note in plan.notes],
            [
                "A: 0.4000 shares can't be sold through the API (fractional). "
                "Sell them manually in TWS or the IBKR app."
            ],
        )

    def test_buy_only_plan_uses_the_post_sell_snapshot(self):
        plan = plan_buys(
            positions_after=(Position("B", 90),),
            targets={"A": 0.06, "B": 0.94},
            quotes={
                "A": Quote("A", bid=100.0, ask=100.0),
                "B": Quote("B", bid=10.0, ask=10.0),
            },
            cash_base=80.0,
            reserve=0.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        self.assertEqual(
            [
                (order.symbol, order.side.value, order.quantity, order.limit_price)
                for order in plan.orders
            ],
            [("B", "BUY", 2, 10.0)],
        )
        self.assertEqual(plan.total_value, 980.0)

    def test_trimmed_symbols_compare_against_the_independently_closest_baseline(self):
        plan = plan_buys(
            positions_after=(),
            targets={"H": 0.80, "M": 0.15, "L": 0.05},
            quotes={
                "H": Quote("H", bid=99.50, ask=100.50),
                "M": Quote("M", bid=19.90, ask=20.10),
                "L": Quote("L", bid=9.95, ask=10.05),
            },
            cash_base=1_000.0,
            reserve=250.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        self.assertEqual(
            [(order.symbol, order.quantity) for order in plan.orders],
            [("H", 6), ("L", 4), ("M", 5)],
        )
        self.assertEqual(plan.trimmed_symbols, ("M",))
        warnings = [note.text for note in plan.notes if note.level == "warning"]
        self.assertEqual(
            warnings,
            ["Not enough cash for all buys: M reduced by 1 share."],
        )

    def test_cash_at_or_below_reserve_is_a_feasible_no_buy_plan(self):
        plan = plan_buys(
            positions_after=(Position("A", 1),),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=100.0, ask=100.0)},
            cash_base=50.0,
            reserve=50.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )

        self.assertEqual(plan.orders, ())
        self.assertEqual(plan.trimmed_symbols, ())
        self.assertEqual(
            [note.text for note in plan.notes],
            [
                "Cash $50.00 is at or below the minimum reserve $50.00; "
                "no buys placed."
            ],
        )

    def test_cash_at_reserve_still_validates_whole_share_inputs(self):
        with self.assertRaisesRegex(OptimizationError, "Negative holding"):
            plan_buys(
                positions_after=(Position("A", -1),),
                targets={"A": 1.0},
                quotes={"A": Quote("A", bid=100.0, ask=100.0)},
                cash_base=50.0,
                reserve=50.0,
                slippage_pct=0.0,
                supports_fractional=False,
                optimization_timeout_seconds=TIMEOUT,
            )

    def test_cash_at_reserve_still_requires_an_available_solver(self):
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = False
            with self.assertRaisesRegex(OptimizationError, "HiGHS is unavailable"):
                plan_buys(
                    positions_after=(Position("A", 1),),
                    targets={"A": 1.0},
                    quotes={"A": Quote("A", bid=100.0, ask=100.0)},
                    cash_base=50.0,
                    reserve=50.0,
                    slippage_pct=0.0,
                    supports_fractional=False,
                    optimization_timeout_seconds=TIMEOUT,
                )

    def test_unheld_target_smaller_than_one_share_keeps_the_information_note(self):
        plan = plan_buys(
            positions_after=(),
            targets={"A": 1.0},
            quotes={"A": Quote("A", bid=100.0, ask=100.0)},
            cash_base=40.0,
            reserve=0.0,
            slippage_pct=0.0,
            supports_fractional=False,
            optimization_timeout_seconds=TIMEOUT,
        )
        self.assertEqual(plan.orders, ())
        self.assertEqual(
            [note.text for note in plan.notes],
            [
                "A: target $40.00 would buy less than one share at $100.00; "
                "not bought."
            ],
        )


class FractionalPlannerIsolation(unittest.TestCase):
    def test_fractional_plans_do_not_invoke_the_whole_share_optimizer(self):
        quote = Quote("A", bid=99.0, ask=101.0)
        with patch(
            "rebalance_calculator.planner._optimize_whole_shares",
            side_effect=AssertionError("whole-share optimizer invoked"),
        ):
            sell_plan = plan_sells(
                positions=(Position("A", 1.5),),
                targets={},
                quotes={"A": quote},
                cash=0.0,
                reserve=0.0,
                slippage_pct=0.5,
                supports_fractional=True,
                optimization_timeout_seconds=TIMEOUT,
            )
            buy_plan = plan_buys(
                positions_after=(),
                targets={"A": 1.0},
                quotes={"A": quote},
                cash_base=100.0,
                reserve=0.0,
                slippage_pct=0.5,
                supports_fractional=True,
                optimization_timeout_seconds=TIMEOUT,
            )

        self.assertEqual(sell_plan.orders[0].quantity, 1.5)
        self.assertEqual(buy_plan.orders[0].quantity, 0.985124)


class TieAndDeterminismTests(unittest.TestCase):
    def optimize(self, targets, quotes):
        return _optimize_whole_shares(
            positions=(),
            targets=targets,
            quotes=quotes,
            cash=100.0,
            reserve=0.0,
            slippage_pct=0.0,
            buy_only=False,
            optimization_timeout_seconds=TIMEOUT,
        )

    def test_equal_weight_tie_prefers_symbol_ascending(self):
        result = self.optimize(
            {"A": 0.5, "B": 0.5},
            {
                "A": Quote("A", bid=60.0, ask=60.0),
                "B": Quote("B", bid=60.0, ask=60.0),
            },
        )
        self.assertEqual(
            {row.symbol: row.final_whole for row in result.symbols},
            {"A": 1, "B": 0},
        )

    def test_exact_half_share_tie_chooses_the_lower_quantity(self):
        result = self.optimize(
            {"A": 0.5, "B": 0.5},
            {
                "A": Quote("A", bid=100.0, ask=100.0),
                "B": Quote("B", bid=1_000.0, ask=1_000.0),
            },
        )
        self.assertEqual(
            {row.symbol: row.final_whole for row in result.symbols},
            {"A": 0, "B": 0},
        )

    def test_input_insertion_order_does_not_change_the_plan(self):
        targets = {"A": 0.6, "B": 0.4}
        quotes = {
            "A": Quote("A", bid=80.0, ask=80.0),
            "B": Quote("B", bid=40.0, ask=40.0),
        }
        forward = self.optimize(targets, quotes)
        reverse = self.optimize(
            dict(reversed(list(targets.items()))),
            dict(reversed(list(quotes.items()))),
        )
        self.assertEqual(forward, reverse)

    def test_tie_break_solver_receives_preceding_solution(self):
        with patch("rebalance_calculator.optimizer.pulp.HiGHS", wraps=pulp.HiGHS) as solver:
            self.optimize({"A": 1.0}, {"A": Quote("A", bid=40.0, ask=40.0)})
        self.assertEqual(
            [call.kwargs.get("warmStart", False) for call in solver.call_args_list],
            [False, True, True],
        )

    def test_repeated_model_is_deterministic(self):
        targets = {"A": 0.5, "B": 0.5}
        quotes = {
            "A": Quote("A", bid=60.0, ask=60.0),
            "B": Quote("B", bid=60.0, ask=60.0),
        }
        expected = self.optimize(targets, quotes)
        for _ in range(100):
            self.assertEqual(self.optimize(targets, quotes), expected)


class SolverFailureTests(unittest.TestCase):
    def fake_problem(self, stats, model_status=highspy.HighsModelStatus.kOptimal):
        return SimpleNamespace(
            solve=Mock(return_value=stats),
            solverModel=SimpleNamespace(getModelStatus=lambda: model_status),
        )

    def test_unavailable_solver_fails_closed(self):
        problem = self.fake_problem(
            SimpleNamespace(status=pulp.LpSolveStatus.Optimal, has_solution=True)
        )
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = False
            with self.assertRaises(OptimizationError):
                _solve(problem, TIMEOUT)
        problem.solve.assert_not_called()

    def test_nonoptimal_solver_statuses_fail_closed_even_with_an_incumbent(self):
        statuses = (
            pulp.LpSolveStatus.Infeasible,
            pulp.LpSolveStatus.TimeLimit,
            pulp.LpSolveStatus.NodeLimit,
            pulp.LpSolveStatus.GapLimit,
            pulp.LpSolveStatus.NumericalError,
        )
        for status in statuses:
            with self.subTest(status=status.name):
                problem = self.fake_problem(
                    SimpleNamespace(status=status, has_solution=True)
                )
                with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
                    highs.return_value.available.return_value = True
                    with self.assertRaises(OptimizationError):
                        _solve(problem, TIMEOUT)

    def test_optimal_without_solution_fails_closed(self):
        problem = self.fake_problem(
            SimpleNamespace(status=pulp.LpSolveStatus.Optimal, has_solution=False)
        )
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = True
            with self.assertRaises(OptimizationError):
                _solve(problem, TIMEOUT)

    def test_pulp_optimal_with_nonoptimal_highs_status_fails_closed(self):
        problem = self.fake_problem(
            SimpleNamespace(status=pulp.LpSolveStatus.Optimal, has_solution=True),
            highspy.HighsModelStatus.kTimeLimit,
        )
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = True
            with self.assertRaises(OptimizationError):
                _solve(problem, TIMEOUT)

    def test_solver_exception_is_an_optimization_error(self):
        problem = self.fake_problem(
            SimpleNamespace(status=pulp.LpSolveStatus.Optimal, has_solution=True)
        )
        problem.solve.side_effect = RuntimeError("solver exploded")
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = True
            with self.assertRaisesRegex(OptimizationError, "solver exploded"):
                _solve(problem, TIMEOUT)

    def test_solver_status_inspection_exception_is_an_optimization_error(self):
        problem = self.fake_problem(
            SimpleNamespace(status=pulp.LpSolveStatus.Optimal, has_solution=True)
        )
        problem.solverModel.getModelStatus = Mock(
            side_effect=RuntimeError("status read failed")
        )
        with patch("rebalance_calculator.optimizer.pulp.HiGHS") as highs:
            highs.return_value.available.return_value = True
            with self.assertRaisesRegex(OptimizationError, "status read failed"):
                _solve(problem, TIMEOUT)


class ResultSafetyTests(unittest.TestCase):
    def optimize_with_corruption(self, corrupt, *, buy_only=False):
        from rebalance_calculator import optimizer

        real_solve = optimizer._solve
        solve_count = 0

        def solve_and_corrupt(problem, timeout):
            nonlocal solve_count
            real_solve(problem, timeout)
            solve_count += 1
            if solve_count == 5:  # total + two error stages + two quantity stages
                corrupt({variable.name: variable for variable in problem.variables()})

        with patch("rebalance_calculator.optimizer._solve", side_effect=solve_and_corrupt):
            return _optimize_whole_shares(
                positions=(Position("A", 1),),
                targets={"A": 0.5, "B": 0.5},
                quotes={
                    "A": Quote("A", bid=60.0, ask=60.0),
                    "B": Quote("B", bid=60.0, ask=60.0),
                },
                cash=100.0,
                reserve=0.0,
                slippage_pct=0.0,
                buy_only=buy_only,
                optimization_timeout_seconds=TIMEOUT,
            )

    def test_nonfinite_solver_result_is_rejected(self):
        with self.assertRaises(OptimizationError):
            self.optimize_with_corruption(
                lambda variables: setattr(variables["buy_A"], "varValue", float("nan"))
            )

    def test_nonintegral_solver_result_is_rejected(self):
        with self.assertRaises(OptimizationError):
            self.optimize_with_corruption(
                lambda variables: setattr(variables["buy_A"], "varValue", 0.5)
            )

    def test_buy_and_sell_on_the_same_symbol_is_rejected(self):
        def corrupt(variables):
            variables["final_whole_A"].varValue = 1.0
            variables["buy_A"].varValue = 1.0
            variables["sell_A"].varValue = 1.0

        with self.assertRaisesRegex(OptimizationError, "Both buy and sell"):
            self.optimize_with_corruption(corrupt)

    def test_sell_exceeding_holdings_is_rejected(self):
        def corrupt(variables):
            variables["final_whole_A"].varValue = 0.0
            variables["buy_A"].varValue = 1.0
            variables["sell_A"].varValue = 2.0

        with self.assertRaisesRegex(OptimizationError, "Sell exceeds holdings"):
            self.optimize_with_corruption(corrupt)

    def test_initial_projected_cash_below_reserve_is_rejected(self):
        def corrupt(variables):
            variables["final_whole_A"].varValue = 4.0
            variables["buy_A"].varValue = 3.0
            variables["sell_A"].varValue = 0.0

        with self.assertRaisesRegex(OptimizationError, "below the reserve"):
            self.optimize_with_corruption(corrupt)

    def test_empty_full_model_still_enforces_the_reserve(self):
        with self.assertRaisesRegex(OptimizationError, "below the reserve"):
            _optimize_whole_shares(
                positions=(),
                targets={},
                quotes={},
                cash=0.0,
                reserve=1.0,
                slippage_pct=0.0,
                buy_only=False,
                optimization_timeout_seconds=TIMEOUT,
            )

    def test_buy_only_cost_above_spendable_cash_is_rejected(self):
        def corrupt(variables):
            variables["final_whole_A"].varValue = 4.0
            variables["buy_A"].varValue = 3.0
            variables["sell_A"].varValue = 0.0

        with self.assertRaisesRegex(OptimizationError, "exceeds spendable cash"):
            self.optimize_with_corruption(corrupt, buy_only=True)

    def test_unsafe_scaled_numeric_bounds_are_rejected_before_solving(self):
        for cash in (1e20, 1e100):
            with self.subTest(cash=cash):
                with patch("rebalance_calculator.optimizer._solve") as solve:
                    with self.assertRaises(OptimizationError):
                        _optimize_whole_shares(
                            positions=(),
                            targets={},
                            quotes={},
                            cash=cash,
                            reserve=0.0,
                            slippage_pct=0.0,
                            buy_only=False,
                            optimization_timeout_seconds=TIMEOUT,
                        )
                solve.assert_not_called()

    def test_negative_holdings_are_rejected_before_solving(self):
        with patch("rebalance_calculator.optimizer._solve") as solve:
            with self.assertRaisesRegex(OptimizationError, "Negative holding"):
                _optimize_whole_shares(
                    positions=(Position("A", -1),),
                    targets={},
                    quotes={"A": Quote("A", bid=10.0, ask=10.0)},
                    cash=0.0,
                    reserve=0.0,
                    slippage_pct=0.0,
                    buy_only=False,
                    optimization_timeout_seconds=TIMEOUT,
                )
        solve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
