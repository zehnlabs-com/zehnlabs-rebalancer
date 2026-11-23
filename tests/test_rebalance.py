"""WP6 acceptance tests: rebalance_account with fakes (PRD §8)."""

import logging
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import sys as _sys

REPO = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(REPO / "event-broker"))

from app_config import AppConfig  # noqa: E402
from broker_connector_base import (  # noqa: E402
    AccountState,
    Position,
    Quote,
    Side,
    WorkingOrder,
)

from app.accounts import AccountConfig  # noqa: E402
from app.rebalance import rebalance_account, RunDeps  # noqa: E402
from app.results import FailureReason, RunOutcome  # noqa: E402

from fakes import FakeAllocations, FakeBrokerConnection, FakeClock  # noqa: E402

ACCOUNT = AccountConfig(account_id="U123456", type="paper", enabled=True,
                        strategy_name="strategy-one")

ALLOCS = {"strategy-one": {"SPY": 0.45, "TLT": 0.35, "GLD": 0.20}}
QUOTES = {
    "SPY": Quote("SPY", bid=599.90, ask=600.10),
    "TLT": Quote("TLT", bid=89.95, ask=90.05),
    "GLD": Quote("GLD", bid=249.90, ask=250.10),
    "XLE": Quote("XLE", bid=84.95, ask=85.05),
}
STATE = AccountState(
    account_id="U123456", cash=2500.0, net_liquidation=104000.0,
    positions=(
        Position("SPY", 70), Position("TLT", 400),
        Position("GLD", 60), Position("XLE", 100.37),
    ),
)
FILLS = {
    "SPY": ("fill", 600.0),
    "TLT": ("fill", 90.0),
    "GLD": ("fill", 250.0),
    "XLE": ("fill", 84.95),
}


def cfg() -> AppConfig:
    c = AppConfig(
        trading={
            "minimum_cash_reserve_percent": 0.25,
            "buy_slippage_percent": 0.5,
            "order_timeout_seconds": 10,
            "order_status_check_interval_seconds": 0.1,
            "cancel_confirm_timeout_seconds": 1,
            "cash_update_timeout_seconds": 0,
        }
    )
    # speed up the never-fill timeout test without model-side validation
    c.trading.order_timeout_seconds = 0
    return c


def make_deps(conn, clock, allocs=None, app_config=None):
    return RunDeps(
        app_config=app_config or cfg(),
        settings=SimpleNamespace(trading_mode="paper", ib_port=None),
        allocations=allocs or FakeAllocations(dict(ALLOCS)),
        connection_factory=lambda account: conn,
        clock=clock,
        logger=logging.getLogger("test_rebalance"),
    )


def open_clock(hour=10):
    return FakeClock(datetime(2026, 11, 20, hour, 0))


class HappyPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_happy_path(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.outcome, RunOutcome.SUCCEEDED)
        self.assertEqual(conn.cancel_calls, 1)
        self.assertEqual(conn.closes, 1)
        self.assertEqual(len(result.executed), 4)  # 1 sell + 3 buys
        sell = next(e for e in result.executed if e.symbol == "XLE")
        self.assertEqual(sell.filled_quantity, 100)
        self.assertEqual(sell.average_fill_price, 84.95)
        self.assertEqual(result.record["cash_base"], 10995.0)
        self.assertAlmostEqual(result.cash_end, 335.0, places=2)
        self.assertAlmostEqual(result.value_end, 104026.45, places=2)
        self.assertEqual([o.symbol for o in result.planned_sells], ["XLE"])
        self.assertEqual(len(result.planned_buys), 3)


class WorkerLoggingTests(unittest.IsolatedAsyncioTestCase):
    """LOG-1 / WRK-2: run lines reach the worker handler with all three fields."""

    async def _run_captured(self, conn, mode):
        import io
        from app.services import trading_executor as te

        handler = te._worker_logger.handlers[0]
        buf = io.StringIO()
        old_stream = handler.setStream(buf)
        old_level = te._worker_logger.level
        # In production the level comes from the parent's basicConfig (LOG_LEVEL,
        # default INFO), inherited by the forked worker; tests have no basicConfig.
        te._worker_logger.setLevel(logging.INFO)
        try:
            adapter = logging.LoggerAdapter(
                te._worker_logger,
                {"strategy": "strategy-one", "account_id": ACCOUNT.account_id},
            )
            deps = make_deps(conn, open_clock())
            deps.logger = adapter  # exactly how trading_executor wires it
            result = await rebalance_account(ACCOUNT, mode, "signal", deps)
        finally:
            handler.setStream(old_stream)
            te._worker_logger.setLevel(old_level)
        out = buf.getvalue()
        self.assertNotIn(" -] ", out)  # no line fell back to a missing run_id
        return result, out, f"[strategy-one {ACCOUNT.account_id} {result.run_id}]"

    async def test_run_lines_carry_strategy_account_and_run_id(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result, out, tag = await self._run_captured(conn, "live")
        self.assertIn(f"{tag} PLAN sells: XLE -100", out)
        self.assertIn(f"{tag} PLAN buys: GLD +22", out)
        self.assertIn(f"{tag} outcome: SUCCEEDED value_end=104,026.45 cash_end=335.00", out)
        self.assertEqual(out.count("outcome:"), 1)

    async def test_preview_and_failure_log_their_outcome(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result, out, tag = await self._run_captured(conn, "preview")
        self.assertEqual(result.outcome, RunOutcome.PREVIEWED)
        self.assertIn(f"{tag} outcome: PREVIEWED value_end=104,031.45", out)

        foreign = [WorkingOrder("QQQ", Side.BUY, 50, own=False)]
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS, working=foreign)
        result, out, tag = await self._run_captured(conn, "live")
        self.assertIn(f"{tag} outcome: FAILED reason=FOREIGN_ORDERS error=BUY 50 QQQ", out)

    def test_worker_logger_does_not_propagate_to_root(self):
        from app.services import trading_executor as te

        self.assertFalse(te._worker_logger.propagate)


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_places_nothing_and_does_not_cancel(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result = await rebalance_account(ACCOUNT, "preview", "manual", make_deps(conn, open_clock()))
        self.assertEqual(result.outcome, RunOutcome.PREVIEWED)
        self.assertEqual(conn.placed, [])
        self.assertEqual(conn.cancel_calls, 0)
        self.assertEqual(len(result.planned_sells), 1)
        self.assertGreater(len(result.planned_buys), 0)
        self.assertAlmostEqual(result.value_end, 104031.45, places=2)

    async def test_preview_runs_when_market_is_closed(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result = await rebalance_account(ACCOUNT, "preview", "manual",
                                         make_deps(conn, open_clock(hour=8)))
        self.assertEqual(result.outcome, RunOutcome.PREVIEWED)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_closed_never_connects(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)
        result = await rebalance_account(ACCOUNT, "live", "signal",
                                         make_deps(conn, open_clock(hour=8)))
        self.assertEqual(result.outcome, RunOutcome.MARKET_CLOSED)
        self.assertEqual(result.error_message, "The market was closed.")
        self.assertEqual(conn.connects, 0)

    async def test_foreign_order_stops_before_orders(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS,
                                    working=[WorkingOrder("QQQ", Side.BUY, 50, own=False)])
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.outcome, RunOutcome.FAILED)
        self.assertEqual(result.failure_reason, FailureReason.FOREIGN_ORDERS)
        self.assertIn("BUY 50 QQQ", result.error_message)
        self.assertEqual(conn.placed, [])

    async def test_cancel_not_confirmed_fails_before_snapshot(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS,
                                    cancel_confirmed=False)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.failure_reason, FailureReason.CANCEL_NOT_CONFIRMED)
        self.assertEqual(conn.account_state_calls, 0)

    async def test_nlv_mismatch_fails_live(self):
        state = AccountState(
            account_id="U123456", cash=2500.0, net_liquidation=100000.0,
            positions=(Position("SPY", 70), Position("TLT", 400),
                       Position("GLD", 60), Position("XLE", 100.37)),
        )
        conn = FakeBrokerConnection(state=state, quotes=QUOTES, fill=FILLS)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.failure_reason, FailureReason.ACCOUNT_DATA_MISMATCH)
        # REC-2: the failed run's record keeps the snapshot it was judged on.
        self.assertAlmostEqual(result.record["V0"], 104031.45, places=6)
        self.assertEqual(result.record["state0"]["cash"], 2500.0)
        self.assertEqual(result.record["state0"]["net_liquidation"], 100000.0)
        self.assertEqual(
            result.record["state0"]["positions"],
            [{"symbol": "SPY", "quantity": 70}, {"symbol": "TLT", "quantity": 400},
             {"symbol": "GLD", "quantity": 60}, {"symbol": "XLE", "quantity": 100.37}],
        )


class OrderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_rejected_means_no_buys_and_what_happened_lists_fills(self):
        fills = dict(FILLS)
        fills["XLE"] = ("reject", "why not")
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=fills)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.failure_reason, FailureReason.ORDERS_FAILED)
        self.assertEqual(len(conn.placed), 1)  # only the sell was placed
        self.assertEqual(len(result.executed), 1)
        self.assertEqual(result.executed[0].status, "REJECTED")
        self.assertEqual(result.executed[0].message if hasattr(result.executed[0], "message") else None, None)

    async def test_buy_timeout_does_not_cancel(self):
        fills = dict(FILLS)
        fills["TLT"] = ("never", None)
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=fills)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.failure_reason, FailureReason.ORDERS_TIMED_OUT)
        # RUN-2: no cancel on failure; the only cancel was the step-6 own-order cancel.
        self.assertEqual(conn.cancel_calls, 1)
        tlt = next(e for e in result.executed if e.symbol == "TLT")
        self.assertEqual(tlt.status, "WORKING")


class CashTests(unittest.IsolatedAsyncioTestCase):
    async def test_cash_lag_uses_lower_number(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS, cash_factor=0.0)
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.outcome, RunOutcome.SUCCEEDED)
        # reported cash stayed at 2500 (sells invisible); state0.cash + gross = 10995
        self.assertEqual(result.record["cash_base"], 2500.0)


class InternalErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_exception_becomes_internal_error_and_never_raises(self):
        conn = FakeBrokerConnection(state=STATE, quotes=QUOTES, fill=FILLS)

        async def boom(symbols):
            raise ValueError("synthetic boom")
        conn.quotes = boom
        result = await rebalance_account(ACCOUNT, "live", "signal", make_deps(conn, open_clock()))
        self.assertEqual(result.outcome, RunOutcome.FAILED)
        self.assertEqual(result.failure_reason, FailureReason.INTERNAL_ERROR)
        self.assertTrue(result.error_message.startswith("Internal error: ValueError: synthetic boom"))
        self.assertLessEqual(len(result.error_message), 300)


if __name__ == "__main__":
    unittest.main()