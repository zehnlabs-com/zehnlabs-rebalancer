"""Portfolio-wide whole-share optimization using PuLP's native HiGHS adapter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Final, Mapping, Sequence

import highspy
import pulp
from broker_connector_base import NoPriceError, Position, Quote

from .pricing import limit_price


MONEY_SCALE: Final[int] = 10_000
MAX_SAFE_INTEGER: Final[int] = 2**53 - 1
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.0001")
_INTEGRAL_TOLERANCE: Final[float] = 1e-6
_SOLVER_FEASIBILITY_TOLERANCE: Final[float] = 1e-7
_STAGE_OBJECTIVE_INTEGRAL_TOLERANCE: Final[float] = 0.25


class OptimizationError(Exception):
    """The whole-share model could not produce a proven safe optimum."""


@dataclass(frozen=True)
class _OptimizedSymbol:
    symbol: str
    final_quantity: float
    final_whole: int
    buy: int
    sell: int
    error_ticks: int
    baseline_final_whole: int


@dataclass(frozen=True)
class _OptimizationResult:
    symbols: tuple[_OptimizedSymbol, ...]
    projected_cash_ticks: int
    total_error_ticks: int


@dataclass(frozen=True)
class _SymbolInputs:
    symbol: str
    weight: Decimal
    held_whole: int
    residue: Decimal
    mark_ticks: int
    residue_value_ticks: int
    target_ticks: int
    final_upper_bound: int
    buy_upper_bound: int
    sell_reference_ticks: int
    buy_limit_ticks: int
    error_upper_bound: int
    buy_only_baseline: int


@dataclass(frozen=True)
class _SymbolVariables:
    final_whole: pulp.LpVariable
    buy: pulp.LpVariable
    sell: pulp.LpVariable
    error_ticks: pulp.LpVariable
    direction: pulp.LpVariable


def _money_ticks(value: Decimal | float | int, name: str) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize all invalid numeric input
        raise OptimizationError(f"{name} is not a finite money value.") from exc
    if not amount.is_finite():
        raise OptimizationError(f"{name} is not a finite money value.")
    try:
        ticks = int(
            (amount.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP) * MONEY_SCALE)
            .to_integral_value(rounding=ROUND_HALF_UP)
        )
    except Exception as exc:  # noqa: BLE001 - overflow/precision failures fail closed
        raise OptimizationError(f"{name} exceeds the safe integer bound.") from exc
    _ensure_safe(ticks, name)
    return ticks


def _required_price_ticks(value: float, name: str) -> int:
    ticks = _money_ticks(value, name)
    if ticks <= 0:
        raise OptimizationError(f"{name} must be positive.")
    return ticks


def _ensure_safe(value: int, name: str) -> None:
    if abs(value) > MAX_SAFE_INTEGER:
        raise OptimizationError(f"{name} exceeds the safe integer bound.")


def _aggregate_positions(positions: Sequence[Position]) -> dict[str, Decimal]:
    held: dict[str, Decimal] = {}
    for position in positions:
        quantity = Decimal(str(position.quantity))
        if not quantity.is_finite():
            raise OptimizationError(f"Holding quantity for {position.symbol} is not finite.")
        held[position.symbol] = held.get(position.symbol, Decimal(0)) + quantity
    for symbol, quantity in held.items():
        if quantity < 0:
            raise OptimizationError(f"Negative holding for {symbol} is not supported.")
    return held


def _target_weights(targets: Mapping[str, float]) -> dict[str, Decimal]:
    weights: dict[str, Decimal] = {}
    for symbol, value in targets.items():
        weight = Decimal(str(value))
        if not weight.is_finite() or weight < 0:
            raise OptimizationError(f"Target weight for {symbol} is invalid.")
        if weight != 0:
            weights[symbol] = weight
    return weights


def _quote(quotes: Mapping[str, Quote], symbol: str) -> Quote:
    quote = quotes.get(symbol)
    if quote is None:
        raise NoPriceError(f"No price available for {symbol}")
    return quote


def _round_ticks(value: Decimal, name: str) -> int:
    if not value.is_finite():
        raise OptimizationError(f"{name} is not finite.")
    ticks = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    _ensure_safe(ticks, name)
    return ticks


def _build_inputs(
    positions: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash: float,
    reserve: float,
    slippage_pct: float,
    buy_only: bool,
) -> tuple[tuple[_SymbolInputs, ...], int, int]:
    held = _aggregate_positions(positions)
    weights = _target_weights(targets)
    symbols = sorted(set(weights) | set(held))
    cash_ticks = _money_ticks(cash, "cash")
    reserve_ticks = _money_ticks(reserve, "reserve")
    if reserve_ticks < 0:
        raise OptimizationError("reserve must not be negative.")

    marks: dict[str, int] = {}
    holding_values: dict[str, int] = {}
    for symbol in symbols:
        mark, _ = _quote(quotes, symbol).mark()
        marks[symbol] = _required_price_ticks(mark, f"mark for {symbol}")
        holding_values[symbol] = _round_ticks(
            held.get(symbol, Decimal(0)) * marks[symbol],
            f"holding value for {symbol}",
        )

    total_value_ticks = cash_ticks + sum(holding_values.values())
    _ensure_safe(total_value_ticks, "total value")
    strategy_capital_ticks = max(0, total_value_ticks - reserve_ticks)
    _ensure_safe(strategy_capital_ticks, "strategy capital")

    rows: list[_SymbolInputs] = []
    total_error_bound = 0
    total_buy_cost_bound = 0
    total_sell_proceeds_bound = 0
    for symbol in symbols:
        held_quantity = held.get(symbol, Decimal(0))
        held_whole = int(held_quantity.to_integral_value(rounding=ROUND_FLOOR))
        residue = held_quantity - held_whole
        mark_ticks = marks[symbol]
        residue_value_ticks = _round_ticks(
            residue * mark_ticks, f"fractional residue value for {symbol}"
        )
        target_ticks = _round_ticks(
            weights.get(symbol, Decimal(0)) * strategy_capital_ticks,
            f"target dollars for {symbol}",
        )
        useful_numerator = target_ticks - residue_value_ticks
        useful_upper_bound = max(
            0,
            int(
                (Decimal(useful_numerator) / Decimal(mark_ticks)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        final_upper_bound = (
            max(held_whole, useful_upper_bound) if buy_only else useful_upper_bound
        )
        buy_upper_bound = max(0, final_upper_bound - held_whole)
        below_target = int(
            (Decimal(useful_numerator) / Decimal(mark_ticks)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        baseline_candidates = {
            max(held_whole, below_target),
            max(held_whole, useful_upper_bound),
        }
        buy_only_baseline = min(
            baseline_candidates,
            key=lambda quantity: (
                abs(residue_value_ticks + quantity * mark_ticks - target_ticks),
                quantity,
            ),
        )

        sell_reference_ticks = 0
        if held_whole > 0 and not buy_only:
            reference, _ = _quote(quotes, symbol).sell_reference()
            sell_reference_ticks = _required_price_ticks(
                reference, f"sell reference for {symbol}"
            )

        buy_limit_ticks = 0
        if buy_upper_bound > 0:
            reference, _ = _quote(quotes, symbol).buy_reference()
            buy_limit_ticks = _required_price_ticks(
                limit_price(reference, slippage_pct), f"buy limit for {symbol}"
            )

        final_value_upper = residue_value_ticks + final_upper_bound * mark_ticks
        error_upper_bound = max(
            abs(residue_value_ticks - target_ticks),
            abs(final_value_upper - target_ticks),
        )

        for value, name in (
            (held_whole, f"held whole-share bound for {symbol}"),
            (useful_upper_bound, f"useful quantity bound for {symbol}"),
            (final_upper_bound, f"final quantity bound for {symbol}"),
            (buy_upper_bound, f"buy quantity bound for {symbol}"),
            (held_whole * sell_reference_ticks, f"sell proceeds bound for {symbol}"),
            (buy_upper_bound * buy_limit_ticks, f"buy cost bound for {symbol}"),
            (final_upper_bound * mark_ticks, f"marked value bound for {symbol}"),
            (error_upper_bound, f"error bound for {symbol}"),
        ):
            _ensure_safe(value, name)

        total_error_bound += error_upper_bound
        total_buy_cost_bound += buy_upper_bound * buy_limit_ticks
        total_sell_proceeds_bound += held_whole * sell_reference_ticks
        rows.append(
            _SymbolInputs(
                symbol=symbol,
                weight=weights.get(symbol, Decimal(0)),
                held_whole=held_whole,
                residue=residue,
                mark_ticks=mark_ticks,
                residue_value_ticks=residue_value_ticks,
                target_ticks=target_ticks,
                final_upper_bound=final_upper_bound,
                buy_upper_bound=buy_upper_bound,
                sell_reference_ticks=sell_reference_ticks,
                buy_limit_ticks=buy_limit_ticks,
                error_upper_bound=error_upper_bound,
                buy_only_baseline=buy_only_baseline,
            )
        )

    _ensure_safe(total_error_bound, "total objective bound")
    _ensure_safe(total_buy_cost_bound, "total buy cost bound")
    _ensure_safe(total_sell_proceeds_bound, "total sell proceeds bound")
    _ensure_safe(
        abs(cash_ticks) + total_buy_cost_bound + total_sell_proceeds_bound,
        "projected cash bound",
    )
    return tuple(rows), cash_ticks, reserve_ticks


def _solve(
    problem: pulp.LpProblem, optimization_timeout_seconds: float
) -> pulp.LpSolveStats:
    """Solve one lexicographic stage and accept only a proven HiGHS optimum."""
    try:
        solver = pulp.HiGHS(
            mip=True,
            msg=False,
            gapRel=0.0,
            gapAbs=0.0,
            threads=1,
            timeLimit=optimization_timeout_seconds,
            parallel="off",
            random_seed=0,
            mip_feasibility_tolerance=_SOLVER_FEASIBILITY_TOLERANCE,
            primal_feasibility_tolerance=_SOLVER_FEASIBILITY_TOLERANCE,
        )
        if not solver.available():
            raise OptimizationError("HiGHS is unavailable.")
        stats = problem.solve(solver)

        if stats.status != pulp.LpSolveStatus.Optimal:
            raise OptimizationError(f"HiGHS did not prove an optimum: {stats.status.name}.")
        if not stats.has_solution:
            raise OptimizationError("HiGHS reported an optimum without a solution.")
        solver_model = getattr(problem, "solverModel", None)
        if (
            solver_model is None
            or solver_model.getModelStatus() != highspy.HighsModelStatus.kOptimal
        ):
            raise OptimizationError("HiGHS model status was not optimal.")
        return stats
    except OptimizationError:
        raise
    except Exception as exc:  # noqa: BLE001 - every solver failure fails closed
        raise OptimizationError(f"HiGHS solve failed: {type(exc).__name__}: {exc}") from exc


def _integer_value(variable: pulp.LpVariable, name: str) -> int:
    value = variable.varValue
    if value is None or not math.isfinite(value):
        raise OptimizationError(f"Solver value for {name} is not finite.")
    rounded = round(value)
    if abs(value - rounded) > _INTEGRAL_TOLERANCE:
        raise OptimizationError(f"Solver value for {name} is not integral.")
    return int(rounded)


def _solve_stage(
    problem: pulp.LpProblem,
    objective: pulp.LpAffineExpression | pulp.LpVariable,
    name: str,
    optimization_timeout_seconds: float,
) -> int:
    problem.setObjective(objective)
    _solve(problem, optimization_timeout_seconds)
    value = objective.value()
    if value is None or not math.isfinite(value):
        raise OptimizationError(f"Objective value for {name} is not finite.")
    optimum = round(value)
    if abs(value - optimum) > _STAGE_OBJECTIVE_INTEGRAL_TOLERANCE:
        raise OptimizationError(f"Objective value for {name} is not integral.")
    optimum = int(optimum)
    _ensure_safe(optimum, f"objective value for {name}")
    problem.add(objective <= optimum)
    return optimum


def _optimize_whole_shares(
    *,
    positions: Sequence[Position],
    targets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    cash: float,
    reserve: float,
    slippage_pct: float,
    buy_only: bool,
    optimization_timeout_seconds: float,
) -> _OptimizationResult:
    """Return the proven whole-share optimum for one planning snapshot."""
    rows, cash_ticks, reserve_ticks = _build_inputs(
        positions, targets, quotes, cash, reserve, slippage_pct, buy_only
    )
    if not rows:
        if not buy_only and cash_ticks < reserve_ticks:
            raise OptimizationError("Projected cash is below the reserve.")
        return _OptimizationResult((), cash_ticks, 0)

    problem = pulp.LpProblem("whole_share_rebalance", pulp.LpMinimize)
    variables: dict[str, _SymbolVariables] = {}
    for row in rows:
        final_lower = row.held_whole if buy_only else 0
        final_whole = problem.add_variable(
            f"final_whole_{row.symbol}",
            lowBound=final_lower,
            upBound=row.final_upper_bound,
            cat=pulp.LpInteger,
        )
        buy = problem.add_variable(
            f"buy_{row.symbol}",
            lowBound=0,
            upBound=row.buy_upper_bound,
            cat=pulp.LpInteger,
        )
        sell = problem.add_variable(
            f"sell_{row.symbol}",
            lowBound=0,
            upBound=0 if buy_only else row.held_whole,
            cat=pulp.LpInteger,
        )
        error_ticks = problem.add_variable(
            f"error_ticks_{row.symbol}",
            lowBound=0,
            upBound=row.error_upper_bound,
            cat=pulp.LpContinuous,
        )
        direction = problem.add_variable(f"direction_{row.symbol}", cat=pulp.LpBinary)
        variables[row.symbol] = _SymbolVariables(
            final_whole, buy, sell, error_ticks, direction
        )

        problem.add(final_whole == row.held_whole + buy - sell)
        problem.add(sell <= row.held_whole)
        problem.add(buy <= row.buy_upper_bound * direction)
        problem.add(sell <= row.held_whole * (1 - direction))
        final_value = row.residue_value_ticks + final_whole * row.mark_ticks
        problem.add(error_ticks >= final_value - row.target_ticks)
        problem.add(error_ticks >= row.target_ticks - final_value)

    if buy_only:
        spendable_cash = max(0, cash_ticks - reserve_ticks)
        problem.add(
            pulp.lpSum(
                variables[row.symbol].buy * row.buy_limit_ticks for row in rows
            )
            <= spendable_cash
        )
    else:
        projected_cash = (
            cash_ticks
            + pulp.lpSum(
                variables[row.symbol].sell * row.sell_reference_ticks for row in rows
            )
            - pulp.lpSum(
                variables[row.symbol].buy * row.buy_limit_ticks for row in rows
            )
        )
        problem.add(projected_cash >= reserve_ticks)

    total_error = pulp.lpSum(variables[row.symbol].error_ticks for row in rows)
    _solve_stage(
        problem, total_error, "total error", optimization_timeout_seconds
    )
    priority_rows = sorted(rows, key=lambda row: (-row.weight, row.symbol))
    for row in priority_rows:
        _solve_stage(
            problem,
            variables[row.symbol].error_ticks,
            f"error for {row.symbol}",
            optimization_timeout_seconds,
        )
    for row in priority_rows:
        _solve_stage(
            problem,
            variables[row.symbol].final_whole,
            f"final quantity for {row.symbol}",
            optimization_timeout_seconds,
        )

    output: list[_OptimizedSymbol] = []
    projected_cash_ticks = cash_ticks
    total_error_ticks = 0
    for row in rows:
        symbol_vars = variables[row.symbol]
        final_whole = _integer_value(symbol_vars.final_whole, f"final whole {row.symbol}")
        buy = _integer_value(symbol_vars.buy, f"buy {row.symbol}")
        sell = _integer_value(symbol_vars.sell, f"sell {row.symbol}")
        if final_whole < 0 or buy < 0 or sell < 0:
            raise OptimizationError(f"Negative solver quantity for {row.symbol}.")
        if final_whole != row.held_whole + buy - sell:
            raise OptimizationError(f"Quantity identity failed for {row.symbol}.")
        if sell > row.held_whole:
            raise OptimizationError(f"Sell exceeds holdings for {row.symbol}.")
        if buy > 0 and sell > 0:
            raise OptimizationError(f"Both buy and sell selected for {row.symbol}.")
        error_ticks = abs(
            row.residue_value_ticks + final_whole * row.mark_ticks - row.target_ticks
        )
        _ensure_safe(error_ticks, f"result error for {row.symbol}")
        total_error_ticks += error_ticks
        projected_cash_ticks += sell * row.sell_reference_ticks - buy * row.buy_limit_ticks
        output.append(
            _OptimizedSymbol(
                symbol=row.symbol,
                final_quantity=float(row.residue + final_whole),
                final_whole=final_whole,
                buy=buy,
                sell=sell,
                error_ticks=error_ticks,
                baseline_final_whole=row.buy_only_baseline,
            )
        )

    _ensure_safe(total_error_ticks, "result total error")
    _ensure_safe(projected_cash_ticks, "result projected cash")
    if buy_only:
        spendable_cash = max(0, cash_ticks - reserve_ticks)
        buy_cost = cash_ticks - projected_cash_ticks
        if buy_cost > spendable_cash:
            raise OptimizationError("Buy-only cost exceeds spendable cash.")
    elif projected_cash_ticks < reserve_ticks:
        raise OptimizationError("Projected cash is below the reserve.")

    return _OptimizationResult(tuple(output), projected_cash_ticks, total_error_ticks)

