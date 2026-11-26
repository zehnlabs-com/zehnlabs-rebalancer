"""Independent exhaustive differential tests for the whole-share optimizer."""

from __future__ import annotations

import itertools
import math
import random
import unittest
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from broker_connector_base import Position, Quote
from rebalance_calculator.optimizer import _optimize_whole_shares


MONEY_SCALE = 10_000
MONEY_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class PortfolioCase:
    positions: tuple[Position, ...]
    targets: dict[str, float]
    quotes: dict[str, Quote]
    cash: float
    reserve: float
    slippage_pct: float


@dataclass(frozen=True)
class OracleSymbol:
    final_whole: int
    buy: int
    sell: int
    error_ticks: int
    residue: Decimal


@dataclass(frozen=True)
class OracleResult:
    symbols: dict[str, OracleSymbol]
    projected_cash_ticks: int
    total_error_ticks: int


@dataclass(frozen=True)
class _OracleInputs:
    symbols: tuple[str, ...]
    weights: dict[str, Decimal]
    cash_ticks: int
    reserve_ticks: int
    held_whole: dict[str, int]
    residues: dict[str, Decimal]
    mark_ticks: dict[str, int]
    residue_value_ticks: dict[str, int]
    target_ticks: dict[str, int]
    sell_reference_ticks: dict[str, int]
    buy_limit_ticks: dict[str, int]
    ranges: tuple[range, ...]


def money_ticks(value: Decimal | float | int) -> int:
    amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return int((amount * MONEY_SCALE).to_integral_value(rounding=ROUND_HALF_UP))


def round_ticks(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def buy_limit(reference: float, slippage_pct: float) -> float:
    raw = reference * (1.0 + slippage_pct / 100.0)
    quantum = Decimal("0.01") if reference >= 1.0 else Decimal("0.0001")
    return float(Decimal(str(raw)).quantize(quantum, rounding=ROUND_HALF_UP))


def aggregate_positions(positions: Sequence[Position]) -> dict[str, Decimal]:
    held: dict[str, Decimal] = {}
    for position in positions:
        held[position.symbol] = held.get(position.symbol, Decimal(0)) + Decimal(
            str(position.quantity)
        )
    return held


def _build_oracle_inputs(case: PortfolioCase, *, buy_only: bool) -> _OracleInputs:
    held = aggregate_positions(case.positions)
    weights = {
        symbol: Decimal(str(weight))
        for symbol, weight in case.targets.items()
        if weight != 0
    }
    symbols = tuple(sorted(set(held) | set(weights)))
    cash_ticks = money_ticks(case.cash)
    reserve_ticks = money_ticks(case.reserve)

    mark_ticks: dict[str, int] = {}
    holding_value_ticks: dict[str, int] = {}
    for symbol in symbols:
        mark = case.quotes[symbol].mark()[0]
        mark_ticks[symbol] = money_ticks(mark)
        holding_value_ticks[symbol] = round_ticks(
            held.get(symbol, Decimal(0)) * mark_ticks[symbol]
        )
    total_value_ticks = cash_ticks + sum(holding_value_ticks.values())
    strategy_capital_ticks = max(0, total_value_ticks - reserve_ticks)

    held_whole: dict[str, int] = {}
    residues: dict[str, Decimal] = {}
    residue_value_ticks: dict[str, int] = {}
    target_ticks: dict[str, int] = {}
    sell_reference_ticks: dict[str, int] = {}
    buy_limit_ticks: dict[str, int] = {}
    ranges: list[range] = []
    for symbol in symbols:
        quantity = held.get(symbol, Decimal(0))
        whole = int(quantity.to_integral_value(rounding=ROUND_FLOOR))
        residue = quantity - whole
        held_whole[symbol] = whole
        residues[symbol] = residue
        residue_value_ticks[symbol] = round_ticks(residue * mark_ticks[symbol])
        target_ticks[symbol] = round_ticks(
            weights.get(symbol, Decimal(0)) * strategy_capital_ticks
        )
        useful = max(
            0,
            int(
                (
                    Decimal(target_ticks[symbol] - residue_value_ticks[symbol])
                    / Decimal(mark_ticks[symbol])
                ).to_integral_value(rounding=ROUND_CEILING)
            ),
        )
        lower = whole if buy_only else 0
        upper = max(whole, useful) if buy_only else useful
        ranges.append(range(lower, upper + 1))
        sell_reference_ticks[symbol] = money_ticks(
            case.quotes[symbol].sell_reference()[0]
        )
        buy_limit_ticks[symbol] = money_ticks(
            buy_limit(case.quotes[symbol].buy_reference()[0], case.slippage_pct)
        )

    return _OracleInputs(
        symbols=symbols,
        weights=weights,
        cash_ticks=cash_ticks,
        reserve_ticks=reserve_ticks,
        held_whole=held_whole,
        residues=residues,
        mark_ticks=mark_ticks,
        residue_value_ticks=residue_value_ticks,
        target_ticks=target_ticks,
        sell_reference_ticks=sell_reference_ticks,
        buy_limit_ticks=buy_limit_ticks,
        ranges=tuple(ranges),
    )


def exhaustive_oracle(case: PortfolioCase, *, buy_only: bool) -> OracleResult:
    inputs = _build_oracle_inputs(case, buy_only=buy_only)
    symbols = inputs.symbols

    priority = sorted(
        symbols,
        key=lambda symbol: (-inputs.weights.get(symbol, Decimal(0)), symbol),
    )
    best_rank: tuple[int, ...] | None = None
    best: OracleResult | None = None
    spendable = max(0, inputs.cash_ticks - inputs.reserve_ticks)
    for quantities in itertools.product(*inputs.ranges):
        final = dict(zip(symbols, quantities, strict=True))
        buys = {
            symbol: max(0, final[symbol] - inputs.held_whole[symbol])
            for symbol in symbols
        }
        sells = {
            symbol: max(0, inputs.held_whole[symbol] - final[symbol])
            for symbol in symbols
        }
        buy_cost = sum(
            buys[symbol] * inputs.buy_limit_ticks[symbol] for symbol in symbols
        )
        sell_proceeds = sum(
            sells[symbol] * inputs.sell_reference_ticks[symbol] for symbol in symbols
        )
        projected_cash = inputs.cash_ticks + sell_proceeds - buy_cost
        if buy_only:
            if any(sells.values()) or buy_cost > spendable:
                continue
        elif projected_cash < inputs.reserve_ticks:
            continue

        errors = {
            symbol: abs(
                inputs.residue_value_ticks[symbol]
                + final[symbol] * inputs.mark_ticks[symbol]
                - inputs.target_ticks[symbol]
            )
            for symbol in symbols
        }
        total_error = sum(errors.values())
        rank = (
            total_error,
            *(errors[symbol] for symbol in priority),
            *(final[symbol] for symbol in priority),
        )
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = OracleResult(
                symbols={
                    symbol: OracleSymbol(
                        final_whole=final[symbol],
                        buy=buys[symbol],
                        sell=sells[symbol],
                        error_ticks=errors[symbol],
                        residue=inputs.residues[symbol],
                    )
                    for symbol in symbols
                },
                projected_cash_ticks=projected_cash,
                total_error_ticks=total_error,
            )

    if best is None:
        raise AssertionError("generated oracle case was unexpectedly infeasible")
    return best


def state_count(case: PortfolioCase, *, buy_only: bool) -> int:
    inputs = _build_oracle_inputs(case, buy_only=buy_only)
    return math.prod(len(values) for values in inputs.ranges)


def generated_cases() -> list[PortfolioCase]:
    cases = [
        PortfolioCase(
            positions=(),
            targets={"A": 0.60, "B": 0.40},
            quotes={"A": Quote("A", bid=80, ask=80), "B": Quote("B", bid=40, ask=40)},
            cash=100,
            reserve=0,
            slippage_pct=0,
        ),
        PortfolioCase(
            positions=(),
            targets={"A": 0.50, "B": 0.50},
            quotes={"A": Quote("A", bid=60, ask=60), "B": Quote("B", bid=60, ask=60)},
            cash=100,
            reserve=0,
            slippage_pct=0,
        ),
        PortfolioCase(
            positions=(),
            targets={"A": 0.50, "B": 0.50},
            quotes={
                "A": Quote("A", bid=100, ask=100),
                "B": Quote("B", bid=1000, ask=1000),
            },
            cash=100,
            reserve=0,
            slippage_pct=0,
        ),
    ]
    rng = random.Random(20261125)
    quantity_choices = (0.0, 0.25, 0.5, 1.0, 1.25, 1.5, 2.25)
    mark_choices = (10.0, 20.0, 25.0, 40.0, 80.0)
    spread_choices = (0.0, 0.20, 1.0)
    cash_choices = (0.0, 5.0, 10.0, 20.0, 40.0)

    while len(cases) < 500:
        symbol_count = rng.randint(1, 4)
        symbols = [f"S{index}" for index in range(symbol_count)]
        empty_targets = len(cases) % 29 == 0
        target_count = 0 if empty_targets else rng.randint(1, symbol_count)
        target_symbols = symbols[:target_count]
        if target_symbols:
            raw_weights = [rng.randint(1, 4) for _ in target_symbols]
            weight_sum = sum(raw_weights)
            targets = {
                symbol: weight / weight_sum
                for symbol, weight in zip(target_symbols, raw_weights, strict=True)
            }
        else:
            targets = {}

        quotes: dict[str, Quote] = {}
        quantities: dict[str, float] = {}
        for symbol in symbols:
            mark = rng.choice(mark_choices)
            spread = rng.choice(spread_choices)
            quotes[symbol] = Quote(
                symbol,
                bid=mark - spread / 2,
                ask=mark + spread / 2,
            )
            quantities[symbol] = rng.choice(quantity_choices)

        # Force regular coverage of held non-target symbols and empty targets.
        if target_count < symbol_count and (len(cases) % 5 == 0 or not targets):
            quantities[symbols[-1]] = rng.choice((1.25, 1.5, 2.25))

        positions: list[Position] = []
        for index, symbol in enumerate(symbols):
            quantity = quantities[symbol]
            if quantity == 0:
                continue
            if index == 0 and len(cases) % 11 == 0 and quantity >= 1:
                # Duplicate rows exercise aggregation before whole/residue splitting.
                positions.append(Position(symbol, 0.5))
                positions.append(Position(symbol, quantity - 0.5))
            else:
                positions.append(Position(symbol, quantity))

        cash = rng.choice(cash_choices)
        max_reserve_cash = cash + sum(
            math.floor(quantity) * quotes[symbol].sell_reference()[0]
            for symbol, quantity in quantities.items()
        )
        reserve_choices = [
            value for value in (0.0, 5.0, 10.0, 15.0) if value <= max_reserve_cash
        ]
        reserve = rng.choice(reserve_choices)
        case = PortfolioCase(
            positions=tuple(positions),
            targets=targets,
            quotes=quotes,
            cash=cash,
            reserve=reserve,
            slippage_pct=rng.choice((0.0, 0.5)),
        )
        if (
            state_count(case, buy_only=False) <= 512
            and state_count(case, buy_only=True) <= 512
        ):
            cases.append(case)
    return cases


class ExhaustiveDifferentialTests(unittest.TestCase):
    def test_500_generated_portfolios_match_the_complete_enumeration(self):
        cases = generated_cases()
        self.assertEqual(len(cases), 500)

        for case_index, case in enumerate(cases):
            for buy_only in (False, True):
                with self.subTest(case=case_index, buy_only=buy_only):
                    expected = exhaustive_oracle(case, buy_only=buy_only)
                    actual = _optimize_whole_shares(
                        positions=case.positions,
                        targets=case.targets,
                        quotes=case.quotes,
                        cash=case.cash,
                        reserve=case.reserve,
                        slippage_pct=case.slippage_pct,
                        buy_only=buy_only,
                        optimization_timeout_seconds=10.0,
                    )
                    actual_symbols = {row.symbol: row for row in actual.symbols}

                    self.assertEqual(set(actual_symbols), set(expected.symbols))
                    for symbol, expected_symbol in expected.symbols.items():
                        actual_symbol = actual_symbols[symbol]
                        self.assertEqual(actual_symbol.final_whole, expected_symbol.final_whole)
                        self.assertEqual(actual_symbol.buy, expected_symbol.buy)
                        self.assertEqual(actual_symbol.sell, expected_symbol.sell)
                        self.assertEqual(actual_symbol.error_ticks, expected_symbol.error_ticks)
                        self.assertEqual(
                            actual_symbol.final_quantity,
                            float(expected_symbol.residue + expected_symbol.final_whole),
                        )
                    self.assertEqual(
                        actual.projected_cash_ticks, expected.projected_cash_ticks
                    )
                    self.assertEqual(actual.total_error_ticks, expected.total_error_ticks)

                    cash_ticks = money_ticks(case.cash)
                    reserve_ticks = money_ticks(case.reserve)
                    if buy_only:
                        self.assertLessEqual(
                            cash_ticks - actual.projected_cash_ticks,
                            max(0, cash_ticks - reserve_ticks),
                        )
                        self.assertTrue(all(row.sell == 0 for row in actual.symbols))
                    else:
                        self.assertGreaterEqual(actual.projected_cash_ticks, reserve_ticks)


if __name__ == "__main__":
    unittest.main()
