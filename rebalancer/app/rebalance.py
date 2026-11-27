"""The account run: `rebalance_account(...)` — the one operation (PRD §8)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from broker_connector_base import (
    BrokerConnection,
    CancelNotConfirmedError,
    NoPriceError,
    OrderRequest,
    OrderState,
    OrderStatus,
    Position,
    PricingError,
    Side,
    AccountDataError,
    ConnectionFailedError,
    OrderPlacementError,
)
from rebalance_calculator import (
    REPLACEMENT_SETS,
    BuyPlan,
    InvalidTargetsError,
    Note,
    OptimizationError,
    PlannedOrder,
    RawAllocation,
    SellPlan,
    Targets,
    apply_replacements,
    plan_buys,
    plan_sells,
    validate_and_scale,
)
from app_config import AppConfig

from .accounts import AccountConfig
from .allocations import AllocationsClient, AllocationsUnavailable
from .market_clock import MarketClock
from .results import (
    AccountRunResult,
    ExecutedOrder,
    FailureReason,
    INTERNAL_ERROR_MESSAGE,
    NoteOut,
    PriceEvidence,
    PlannedOrderOut,
    RecordedAccountState,
    RecordedPosition,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
)
from .settings import RuntimeSettings

ET = ZoneInfo("America/New_York")
EPS = 1e-9
UNEXPECTED_ORDER_PLACEMENT_MESSAGE = (
    "An unexpected broker error occurred while placing orders."
)

_SOURCE_PHRASE = {
    "last": "last trade",
    "close": "last close",
    "bar": "bar",
    "bid": "bid",
}


@dataclass
class RunDeps:
    app_config: AppConfig
    settings: RuntimeSettings
    allocations: AllocationsClient
    connection_factory: Callable[[AccountConfig], BrokerConnection]
    clock: MarketClock
    logger: logging.Logger


class _RunFailed(Exception):
    def __init__(self, reason: FailureReason, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _note(level: str, text: str) -> NoteOut:
    return NoteOut(level=level, text=text)  # type: ignore[arg-type]


def _plans_out(orders: Sequence[PlannedOrder]) -> list[PlannedOrderOut]:
    return [
        PlannedOrderOut(
            symbol=o.symbol, side=o.side.value, quantity=o.quantity,
            limit_price=o.limit_price,
        )
        for o in orders
    ]


async def _fills(conn: BrokerConnection, handles: Sequence[str]) -> list[ExecutedOrder]:
    if not handles:
        return []
    states = await conn.order_states(handles)
    return [
        ExecutedOrder(
            symbol=s.symbol, side=s.side.value, quantity=s.quantity,
            filled_quantity=s.filled_quantity, average_fill_price=s.average_fill_price,
            status=s.status.value,
        )
        for s in states
    ]


def _trimmed_symbols(plan: BuyPlan) -> list[str]:
    return list(plan.trimmed_symbols)


async def _wait_for_orders(
    conn: BrokerConnection, handles: Sequence[str], deps: RunDeps, logger: logging.Logger
) -> None:
    """PRD §8.5 WAIT-1..3. Raises `_RunFailed(ORDERS_FAILED/ORDERS_TIMED_OUT)`."""
    cfg = deps.app_config.trading
    interval = cfg.order_status_check_interval_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.order_timeout_seconds
    while True:
        states = await conn.order_states(handles)
        working = [s for s in states if s.status == OrderStatus.WORKING]
        if not working:
            bad = [s for s in states if s.status != OrderStatus.FILLED]
            if bad:
                listing = ", ".join(
                    f"{s.side.value} {int(s.quantity)} {s.symbol}: {s.status.value}"
                    for s in bad
                )
                raise _RunFailed(FailureReason.ORDERS_FAILED, listing)
            return
        if loop.time() >= deadline:
            listing = ", ".join(
                f"{s.side.value} {int(s.filled_quantity)} of {int(s.quantity)} {s.symbol}"
                for s in working
            )
            raise _RunFailed(FailureReason.ORDERS_TIMED_OUT, listing)
        await asyncio.sleep(interval)


def _subtract_sells(
    positions: Sequence[Position], fills: Sequence[ExecutedOrder]
) -> list[Position]:
    """§8.1 step 14: subtract the filled sell quantities (never re-read from IBKR)."""
    sold: dict[str, float] = {}
    for f in fills:
        if f.side == Side.SELL.value:
            sold[f.symbol] = sold.get(f.symbol, 0.0) + f.filled_quantity
    result: list[Position] = []
    for p in positions:
        remaining = p.quantity - sold.get(p.symbol, 0.0)
        if remaining > EPS:
            result.append(Position(p.symbol, remaining))
    return result


def new_run_id(now: datetime, account_id: str) -> str:
    """§8.1 step 1: `<stamp>-<account>-<4 hex>` (also used for parent-side results)."""
    stamp = now.astimezone(ET).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{account_id}-{secrets.token_hex(2)}"


async def rebalance_account(
    account: AccountConfig,
    mode: RunMode,
    source: RunSource,
    deps: RunDeps,
    *,
    run_id: str,
) -> AccountRunResult:
    """Run one rebalance attempt for one account. Never raises."""
    # LOG-1: one adapter carrying strategy/account_id (from the worker) + run_id.
    # LoggerAdapter.process replaces `extra` rather than merging, so wrapping the
    # worker's adapter would let it overwrite run_id. Copy its fields and wrap
    # the underlying logger instead.
    if isinstance(deps.logger, logging.LoggerAdapter):
        _extra = dict(deps.logger.extra or {})
        base_logger = deps.logger.logger
    else:
        _extra = {}
        base_logger = deps.logger
    _extra["run_id"] = run_id
    adapter = logging.LoggerAdapter(base_logger, _extra)
    started_at = _utc_timestamp(deps.clock.now())

    record = RunRecord(
        run_id=run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=mode,
        source=source,
        started_at=started_at,
        replacement_set=account.replacement_set,
    )

    result = AccountRunResult(
        run_id=run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=mode,
        source=source,
        outcome=RunOutcome.FAILED,
        record=record,
    )
    executed: list[ExecutedOrder] = []
    notes: list[NoteOut] = []
    planned_sells: list[PlannedOrderOut] = []
    planned_buys: list[PlannedOrderOut] = []
    conn: Optional[BrokerConnection] = None
    placed: list[str] = []

    def finish(
        outcome: RunOutcome,
        *,
        reason: Optional[FailureReason] = None,
        error_message: Optional[str] = None,
        value_end: Optional[float] = None,
        cash_end: Optional[float] = None,
    ) -> AccountRunResult:
        result.outcome = outcome
        result.failure_reason = reason
        result.error_message = error_message
        result.executed = executed
        result.planned_sells = planned_sells
        result.planned_buys = planned_buys
        result.notes = notes
        result.value_end = value_end
        result.cash_end = cash_end
        record.finished_at = _utc_timestamp(deps.clock.now())
        record.outcome = outcome
        record.failure_reason = reason
        record.error_message = error_message
        record.planned_sells = list(planned_sells)
        record.planned_buys = list(planned_buys)
        record.executed = list(executed)
        record.notes = list(notes)
        result.record = record
        # LOG-1: one outcome line for every run (success, preview, failure, closed).
        parts = [f"outcome: {outcome.value}"]
        if reason is not None:
            parts.append(f"reason={reason.value}")
        if value_end is not None:
            parts.append(f"value_end={value_end:,.2f}")
        if cash_end is not None:
            parts.append(f"cash_end={cash_end:,.2f}")
        if error_message:
            parts.append(f"error={error_message}")
        adapter.info(" ".join(parts))
        return result

    async def fail(reason: FailureReason, message: str) -> AccountRunResult:
        if conn is not None and placed:
            try:
                executed[:] = await _fills(conn, placed)
            except Exception:  # noqa: BLE001 - never raise out of fail()
                executed[:] = executed
        return finish(RunOutcome.FAILED, reason=reason, error_message=message)

    try:
        # ---- 2. allocations ------------------------------------------------ #
        try:
            raw_allocs: list[RawAllocation] = await deps.allocations.fetch(account.strategy_name)
        except AllocationsUnavailable as exc:
            return await fail(FailureReason.ALLOCATIONS_UNAVAILABLE, str(exc))

        # ---- 3. validate + scale + replacements --------------------------- #
        try:
            targets: Targets = validate_and_scale(raw_allocs)
        except InvalidTargetsError as exc:
            return await fail(FailureReason.TARGETS_INVALID, str(exc))

        for n in targets.notes:
            notes.append(_note(n.level, n.text))

        final_targets: dict[str, float] = targets.weights
        if account.replacement_set is not None:
            final_targets, repl_notes = apply_replacements(
                targets.weights, REPLACEMENT_SETS[account.replacement_set]
            )
            for n in repl_notes:
                notes.append(_note(n.level, n.text))

        adapter.info("raw weights: %s", targets.raw)
        adapter.info("scaled weights: %s", targets.scaled)
        adapter.info("final targets: %s", final_targets)
        record.raw_weights = targets.raw
        record.scaled_weights = targets.scaled
        record.targets = final_targets

        # ---- 4. market closed (live only, before any connection) ---------- #
        if mode == "live" and not deps.clock.is_open(deps.clock.now()):
            return finish(RunOutcome.MARKET_CLOSED, error_message="The market was closed.")

        # ---- 5-18. inside the connection ----------------------------------- #
        cfg = deps.app_config
        slippage = cfg.trading.buy_slippage_percent
        optimization_timeout = cfg.trading.optimization_timeout_seconds
        conn = deps.connection_factory(account)
        async with conn:
                # ---- 6. cancel own orders (live only) ---------------------- #
                if mode == "live":
                    try:
                        cancelled = await conn.cancel_own_orders(
                            cfg.trading.cancel_confirm_timeout_seconds
                        )
                    except CancelNotConfirmedError as exc:
                        return await fail(FailureReason.CANCEL_NOT_CONFIRMED, str(exc))
                    if cancelled > 0:
                        notes.append(_note(
                            "info", f"Cancelled {cancelled} earlier orders left by this app."
                        ))

                # ---- 7. working orders -------------------------------------- #
                working = await conn.working_orders()
                foreign = [w for w in working if not w.own]
                if foreign:
                    listing = ", ".join(
                        f"{w.side.value} {int(w.quantity)} {w.symbol}" for w in foreign
                    )
                    if mode == "live":
                        return await fail(FailureReason.FOREIGN_ORDERS, listing)
                    notes.append(_note("info", f"Foreign working orders: {listing}"))

                # ---- 8. state0 ---------------------------------------------- #
                try:
                    state0 = await conn.account_state()
                except AccountDataError as exc:
                    return await fail(FailureReason.ACCOUNT_DATA_UNAVAILABLE, str(exc))
                adapter.info("state0: cash=%s nlv=%s positions=%s",
                             state0.cash, state0.net_liquidation,
                             [(p.symbol, p.quantity) for p in state0.positions])

                # ---- 9. quotes0 + marks -------------------------------------- #
                needed0 = sorted(
                    set(final_targets) | {p.symbol for p in state0.positions}
                )
                try:
                    q0 = await conn.quotes(needed0)
                except PricingError as exc:
                    return await fail(FailureReason.PRICING_FAILED, str(exc))
                marks: dict[str, float] = {}
                for symbol in needed0:
                    quote = q0.get(symbol)
                    if quote is None:
                        return await fail(FailureReason.PRICING_FAILED,
                                          f"No price available for {symbol}")
                    try:
                        price, source = quote.mark()
                    except NoPriceError:
                        return await fail(FailureReason.PRICING_FAILED,
                                          f"No price available for {symbol}")
                    marks[symbol] = price
                    if source.value != "mid":
                        notes.append(_note(
                            "info",
                            f"Priced {symbol} from {_SOURCE_PHRASE.get(source.value, source.value)} "
                            f"(no live quote).",
                        ))

                # ---- 10. fixed reserve + sell plan --------------------------- #
                V0 = state0.cash + sum(
                    p.quantity * marks[p.symbol] for p in state0.positions
                )
                reserve = (cfg.trading.minimum_cash_reserve_percent / 100.0) * V0
                record.V0 = V0
                record.reserve = reserve
                record.state0 = RecordedAccountState(
                    cash=state0.cash,
                    net_liquidation=state0.net_liquidation,
                    positions=[
                        RecordedPosition(symbol=p.symbol, quantity=p.quantity)
                        for p in state0.positions
                    ],
                )
                record.prices = _prices_record(q0, marks)
                sell_plan = plan_sells(
                    state0.positions,
                    final_targets,
                    q0,
                    state0.cash,
                    reserve,
                    slippage,
                    conn.supports_fractional,
                    optimization_timeout,
                )
                for n in sell_plan.notes:
                    notes.append(_note(n.level, n.text))
                nlv = state0.net_liquidation
                nlv_mismatch = (nlv > 0 and abs(V0 - nlv) / nlv > 0.02) or (nlv <= 0 and V0 > 0)
                if nlv_mismatch:
                    message = (f"Computed portfolio value ${V0:,.2f} differs from IBKR "
                               f"NetLiquidation ${nlv:,.2f} by more than 2%.")
                    if mode == "live":
                        return await fail(FailureReason.ACCOUNT_DATA_MISMATCH, message)
                    notes.append(_note("warning", message))

                # ---- 11. log + preview --------------------------------------- #
                adapter.info(
                    "PLAN sells: %s | V0 $%s reserve $%s",
                    ", ".join(f"{o.symbol} -{int(o.quantity)}" for o in sell_plan.orders),
                    f"{V0:,.2f}", f"{reserve:,.2f}",
                )
                # REC-2 / M2: record the planned sell orders as soon as the plan is
                # final, so a later failure still shows what the run was about to do.
                planned_sells.extend(_plans_out(sell_plan.orders))

                if mode == "preview":
                    # simulate sells at the sell reference, then plan buys
                    proceeds = 0.0
                    for order in sell_plan.orders:
                        try:
                            ref, _src = q0[order.symbol].sell_reference()
                        except NoPriceError:
                            return await fail(FailureReason.PRICING_FAILED,
                                              f"No sell reference for {order.symbol}")
                        proceeds += order.quantity * ref
                    cash_after = state0.cash + proceeds
                    record.cash_base = cash_after
                    pos_after = _subtract_sells(
                        state0.positions,
                        [ExecutedOrder(symbol=o.symbol, side=o.side.value,
                                       quantity=o.quantity, filled_quantity=o.quantity,
                                       average_fill_price=None, status="FILLED")
                         for o in sell_plan.orders],
                    )
                    buy_plan = plan_buys(
                        pos_after,
                        final_targets,
                        q0,
                        cash_after,
                        reserve,
                        slippage,
                        conn.supports_fractional,
                        optimization_timeout,
                    )
                    for n in buy_plan.notes:
                        notes.append(_note(n.level, n.text))
                    planned_buys.extend(_plans_out(buy_plan.orders))
                    record.planned_sells = list(planned_sells)
                    record.planned_buys = list(planned_buys)
                    record.trimmed = _trimmed_symbols(buy_plan)
                    record.V1 = buy_plan.total_value
                    return finish(RunOutcome.PREVIEWED,
                                  value_end=V0, cash_end=cash_after)

                # ---- 12. sell leg -------------------------------------------- #
                if sell_plan.orders:
                    if not deps.clock.is_open(deps.clock.now()):
                        return await fail(FailureReason.MARKET_CLOSED_DURING_RUN,
                                          "The market closed while the run was in progress.")
                    try:
                        for order in sell_plan.orders:
                            handle = await conn.place(OrderRequest(
                                order.symbol, order.side, order.quantity,
                                order.limit_price, run_id,
                            ))
                            placed.append(handle)
                    except OrderPlacementError as exc:
                        return await fail(FailureReason.ORDER_PLACEMENT_FAILED, str(exc))
                    except Exception as exc:  # noqa: BLE001 - any transport error = placement failure
                        adapter.error(
                            "unexpected sell order placement error (%s)",
                            type(exc).__name__,
                        )
                        return await fail(
                            FailureReason.ORDER_PLACEMENT_FAILED,
                            UNEXPECTED_ORDER_PLACEMENT_MESSAGE,
                        )
                    try:
                        await _wait_for_orders(conn, placed, deps, adapter)
                    except _RunFailed as rf:
                        return await fail(rf.reason, rf.message)

                # ---- 13. cash wait -------------------------------------------- #
                sell_fills = await _fills(conn, placed)
                gross = sum(
                    f.filled_quantity * (f.average_fill_price or 0.0) for f in sell_fills
                )
                reported_cash = state0.cash
                if gross > 0:
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + cfg.trading.cash_update_timeout_seconds
                    target = state0.cash + 0.98 * gross
                    while True:
                        try:
                            st = await conn.account_state()
                        except AccountDataError as exc:
                            return await fail(FailureReason.ACCOUNT_DATA_UNAVAILABLE, str(exc))
                        reported_cash = st.cash
                        if reported_cash >= target or loop.time() >= deadline:
                            break
                        await asyncio.sleep(1)
                    if reported_cash < target:
                        adapter.warning(
                            "cash still %s after waiting; using reported value",
                            f"{reported_cash:,.2f}",
                        )
                cash_base = (
                    min(reported_cash, state0.cash + gross) if sell_plan.orders else state0.cash
                )
                record.cash_base = cash_base

                # ---- 14. positions_after -------------------------------------- #
                positions_after = _subtract_sells(state0.positions, sell_fills)

                # ---- 15. quotes1 ---------------------------------------------- #
                needed1 = sorted(set(final_targets) | {p.symbol for p in positions_after})
                try:
                    q1 = await conn.quotes(needed1)
                except PricingError as exc:
                    return await fail(FailureReason.PRICING_FAILED, str(exc))
                for symbol in needed1:
                    if symbol not in q1:
                        return await fail(FailureReason.PRICING_FAILED,
                                          f"No price available for {symbol}")
                    try:
                        _ref, bsource = q1[symbol].buy_reference()
                    except NoPriceError:
                        return await fail(FailureReason.PRICING_FAILED,
                                          f"No buy reference for {symbol}")
                    if bsource.value != "ask":
                        notes.append(_note(
                            "info",
                            f"Priced {symbol} from {_SOURCE_PHRASE.get(bsource.value, bsource.value)} "
                            f"(no live quote).",
                        ))

                # ---- 16. buy plan --------------------------------------------- #
                record.prices = _prices_record(q1, marks)
                buy_plan = plan_buys(
                    positions_after,
                    final_targets,
                    q1,
                    cash_base,
                    reserve,
                    slippage,
                    conn.supports_fractional,
                    optimization_timeout,
                )
                for n in buy_plan.notes:
                    notes.append(_note(n.level, n.text))
                cost = sum(o.quantity * (o.limit_price or 0.0) for o in buy_plan.orders)
                adapter.info(
                    "PLAN buys: %s | cash_base $%s budget $%s cost<= $%s",
                    ", ".join(
                        f"{o.symbol} +{int(o.quantity)} "
                        f"{'' if o.limit_price is None else f'≤${o.limit_price:,.2f}'}"
                        for o in buy_plan.orders
                    ),
                    f"{cash_base:,.2f}", f"{buy_plan.budget:,.2f}", f"{cost:,.2f}",
                )
                # REC-2 / M2: record the planned buys + prices/V1/trimmed now so a
                # buy-leg failure keeps them (and the notification can say "BUY orders
                # did not complete.").
                planned_buys.extend(_plans_out(buy_plan.orders))
                record.V1 = buy_plan.total_value
                record.trimmed = _trimmed_symbols(buy_plan)

                # ---- 17. buy leg ---------------------------------------------- #
                if buy_plan.orders:
                    if not deps.clock.is_open(deps.clock.now()):
                        return await fail(FailureReason.MARKET_CLOSED_DURING_RUN,
                                          "The market closed while the run was in progress.")
                    buy_handles: list[str] = []
                    try:
                        for order in buy_plan.orders:
                            handle = await conn.place(OrderRequest(
                                order.symbol, order.side, order.quantity,
                                order.limit_price, run_id,
                            ))
                            buy_handles.append(handle)
                            placed.append(handle)
                    except OrderPlacementError as exc:
                        return await fail(FailureReason.ORDER_PLACEMENT_FAILED, str(exc))
                    except Exception as exc:  # noqa: BLE001 - any transport error = placement failure
                        adapter.error(
                            "unexpected buy order placement error (%s)",
                            type(exc).__name__,
                        )
                        return await fail(
                            FailureReason.ORDER_PLACEMENT_FAILED,
                            UNEXPECTED_ORDER_PLACEMENT_MESSAGE,
                        )
                    try:
                        await _wait_for_orders(conn, placed, deps, adapter)
                    except _RunFailed as rf:
                        return await fail(rf.reason, rf.message)

                # ---- 18. success ---------------------------------------------- #
                executed[:] = await _fills(conn, placed)
                buy_fills = [f for f in executed if f.side == Side.BUY.value]
                cash_end = cash_base - sum(
                    f.filled_quantity * (f.average_fill_price or 0.0) for f in buy_fills
                )
                record.planned_sells = list(planned_sells)
                record.planned_buys = list(planned_buys)
                record.trimmed = _trimmed_symbols(buy_plan)
                record.V1 = buy_plan.total_value
                record.executed = list(executed)
                for order in executed:
                    adapter.info(
                        "order %s %s qty=%s filled=%s avg=%s status=%s",
                        order.side, order.symbol, order.quantity,
                        order.filled_quantity, order.average_fill_price, order.status,
                    )
                return finish(RunOutcome.SUCCEEDED,
                              value_end=buy_plan.total_value, cash_end=cash_end)
    except ConnectionFailedError as exc:
        return await fail(FailureReason.CONNECTION_FAILED, str(exc))
    except NoPriceError as exc:
        return await fail(FailureReason.PRICING_FAILED, str(exc))
    except OptimizationError as exc:
        return await fail(FailureReason.OPTIMIZATION_FAILED, str(exc))
    except _RunFailed as rf:
        return await fail(rf.reason, rf.message)
    except BaseException as exc:  # noqa: BLE001 - unexpected -> INTERNAL_ERROR
        type_name = type(exc).__name__
        adapter.error("internal error during run (%s)", type_name)
        return await fail(FailureReason.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE)


def _prices_record(q: dict, marks: dict[str, float]) -> dict[str, PriceEvidence]:
    """REC-2 `prices` block from a quote map: raw fields + used mark/buy refs."""
    out: dict[str, PriceEvidence] = {}
    for symbol, quote in q.items():
        entry = PriceEvidence(
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            close=quote.close,
            bar_close=quote.bar_close,
            used_mark=marks.get(symbol),
        )
        try:
            _m, m_src = quote.mark()
            entry.mark_source = m_src.value
        except NoPriceError:
            pass
        try:
            _b, b_src = quote.buy_reference()
            entry.buy_reference = _b
            entry.buy_source = b_src.value
        except NoPriceError:
            pass
        out[symbol] = entry
    return out


def _utc_timestamp(value: datetime) -> str:
    """Return a durable RFC 3339 UTC timestamp ending in ``Z``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ET)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
