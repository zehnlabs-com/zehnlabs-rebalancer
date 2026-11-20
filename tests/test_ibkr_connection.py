"""WP4 acceptance tests: IBKRConnection against a mocked ib_async.IB (PRD §9)."""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from app_config import IBKRConfig
from broker_connector_base import (
    OrderRequest,
    OrderStatus,
    Side,
    AccountDataError,
    CancelNotConfirmedError,
    ConnectionFailedError,
    NoPriceError,
    OrderPlacementError,
    PricingError,
)
import ibkr_connector.connection as module
from ibkr_connector.connection import IBKRConnection

SETTINGS = NS(ib_host="gateway", ib_port=None, trading_mode="paper")


def av(tag, value, currency="USD"):
    return NS(tag=tag, value=str(value), currency=currency)


def pos(sec_type, symbol, qty):
    return NS(contract=NS(secType=sec_type, symbol=symbol), position=qty)


def ticker(bid=0.0, ask=0.0, last=0.0, close=0.0):
    return NS(bid=bid, ask=ask, last=last, close=close)


def bar(close):
    return NS(close=close)


def logentry(message):
    return NS(message=message)


class FakeIB:
    """Scripted stand-in for ib_async.IB."""

    def __init__(self):
        self.account_values = {}
        self.raw_positions = []
        self.managed = ["U123456"]
        self.connected = False
        self.market_data_type = None
        self.qualify_map = None          # None = qualify all; else symbol->Contract/None
        self.tickers = {}
        self.bars = {}
        self.place_calls = []
        self.trades = {}                  # orderId -> Trade
        self.open_order_trades = []       # for reqAllOpenOrdersAsync
        self.cancelled = set()
        self.connected_kwargs = None

    def isConnected(self):
        return self.connected

    async def connectAsync(self, **kwargs):
        self.connected_kwargs = kwargs
        self.connected = True
        return None

    def disconnect(self):
        self.connected = False

    def managedAccounts(self):
        return self.managed

    def reqMarketDataType(self, mdt):
        self.market_data_type = mdt

    def accountValues(self, account):
        return self.account_values.get(account, [])

    def positions(self, account):
        return self.raw_positions

    async def qualifyContractsAsync(self, *contracts):
        if self.qualify_map is None:
            return list(contracts)
        return [self.qualify_map.get(getattr(c, "symbol", ""), c) for c in contracts]

    async def reqTickersAsync(self, *contracts):
        return [self.tickers.get(getattr(c, "symbol", ""), ticker()) for c in contracts]

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.last_bar_kwargs = kwargs
        return self.bars.get(getattr(contract, "symbol", ""), [])

    def placeOrder(self, contract, order):
        order_id = len(self.trades) + 1
        order.orderId = order_id
        trade = self._new_trade(contract, order, status="Submitted", filled=0.0)
        self.trades[str(order_id)] = trade
        self.place_calls.append((contract, order))
        return trade

    def _new_trade(self, contract, order, status, filled, avg=0.0, message=None):
        status_obj = NS(status=status, filled=filled, avgFillPrice=avg)
        lt = [logentry(message)] if message else [logentry(""), logentry(message or "")]
        return NS(
            order=order,
            orderStatus=status_obj,
            contract=contract,
            isDone=lambda: status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"),
            log=[logentry("")] + ([logentry(message)] if message else []),
        )

    def openTrades(self):
        return [t for t in self.trades.values()
                if t.order.clientId == 42 and not t.isDone()]

    async def reqAllOpenOrdersAsync(self):
        return self.open_order_trades

    def cancelOrder(self, order):
        self.cancelled.add(getattr(order, "orderId", None))
        self.trades[str(order.orderId)].orderStatus.status = "ApiCancelled"


TEST_CFG = IBKRConfig(
    connection_stabilization_delay_seconds=0.0,
    order_placement_delay_seconds=0.1,
    market_data_retry_delay_seconds=0.5,
    market_data_max_retries=1,
)


def make_conn(fake, account_values=None, raw_positions=None, manage=False):
    conn = IBKRConnection(
        "U123456", 42, TEST_CFG, SETTINGS,
        logger=None,
    )
    if account_values is not None:
        fake.account_values["U123456"] = account_values
    if raw_positions is not None:
        fake.raw_positions = raw_positions
    if manage:
        conn._ib = fake
        fake.connected = True
    return conn


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_args_and_fetchfields(self):
        fake = FakeIB()
        with patch.object(module, "IB", return_value=fake):
            conn = make_conn(fake)
            await conn.connect()
        kw = fake.connected_kwargs
        self.assertEqual(kw["account"], "U123456")
        self.assertIs(kw["raiseSyncErrors"], True)
        self.assertEqual(kw["clientId"], 42)
        # POSITIONS | ORDERS_OPEN | ACCOUNT_UPDATES; asserts ORDERS_OPEN is present
        # and that EXECUTIONS / ORDERS_COMPLETE / SUB_ACCOUNT_UPDATES are absent.
        from ib_async import StartupFetch
        combo = kw["fetchFields"]
        self.assertEqual(combo,
                         StartupFetch.POSITIONS | StartupFetch.ORDERS_OPEN
                         | StartupFetch.ACCOUNT_UPDATES)
        for excluded in (StartupFetch.EXECUTIONS, StartupFetch.ORDERS_COMPLETE,
                         StartupFetch.SUB_ACCOUNT_UPDATES):
            self.assertEqual(combo.value & excluded.value, 0)
        self.assertEqual(fake.market_data_type, 1)

    async def test_unmanaged_account_fails(self):
        fake = FakeIB()
        fake.managed = ["UOTHER"]
        with patch.object(module, "IB", return_value=fake):
            conn = make_conn(fake)
            with self.assertRaises(ConnectionFailedError):
                await conn.connect()
        self.assertFalse(fake.connected)


class AccountStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cash_ledger_preferred_when_both_present(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "100.0"), av("$LEDGER-CashBalance", "2500.0"),
            av("NetLiquidation", "100000.0"), av("SomeTag", "1.0", "EUR"),
        ], raw_positions=[pos("STK", "SPY", 70)], manage=True)
        st = await conn.account_state()
        self.assertEqual(st.cash, 2500.0)
        self.assertEqual(st.net_liquidation, 100000.0)
        self.assertEqual(st.positions[0].symbol, "SPY")
        self.assertEqual(st.positions[0].quantity, 70)

    async def test_cash_falls_back_to_cashbalance(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "88.0"), av("NetLiquidation", "10.0"),
        ], manage=True)
        st = await conn.account_state()
        self.assertEqual(st.cash, 88.0)

    async def test_no_cash_reported_is_error(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[av("NetLiquidation", "10.0")], manage=True)
        with self.assertRaises(AccountDataError):
            await conn.account_state()

    async def test_missing_nlv_is_error(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[av("CashBalance", "10.0")], manage=True)
        with self.assertRaises(AccountDataError):
            await conn.account_state()

    async def test_non_stk_position_is_error(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "1.0"), av("NetLiquidation", "5.0"),
        ], raw_positions=[pos("OPT", "AAPL", -3)], manage=True)
        with self.assertRaises(AccountDataError):
            await conn.account_state()

    async def test_negative_stk_position_is_error(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "1.0"), av("NetLiquidation", "5.0"),
        ], raw_positions=[pos("STK", "SPY", -10)], manage=True)
        with self.assertRaises(AccountDataError):
            await conn.account_state()

    async def test_zero_position_skipped(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "1.0"), av("NetLiquidation", "5.0"),
        ], raw_positions=[pos("STK", "SPY", 0), pos("STK", "TLT", 5)], manage=True)
        st = await conn.account_state()
        self.assertEqual(len(st.positions), 1)
        self.assertEqual(st.positions[0].symbol, "TLT")

    async def test_non_finite_cash_is_error(self):
        fake = FakeIB()
        conn = make_conn(fake, account_values=[
            av("CashBalance", "not-a-number"), av("NetLiquidation", "5.0"),
        ], manage=True)
        with self.assertRaises(AccountDataError):
            await conn.account_state()


class _Order:
    def __init__(self, action, qty, client_id=42):
        self.action = action
        self.totalQuantity = qty
        self.clientId = client_id
        self.account = "U123456"
        self.orderId = 1
        self.tif = ""
        self.orderRef = ""
        self.outsideRth = None


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_fallback_chain_including_bar(self):
        fake = FakeIB()
        fake.tickers = {
            "SPY": ticker(bid=599.9, ask=600.1),
            "GOOG": ticker(),  # nothing live
        }
        fake.bars = {"GOOG": [bar(1234.5)]}
        conn = make_conn(fake, manage=True)
        conn._contracts = {"SPY": NS(symbol="SPY"), "GOOG": NS(symbol="GOOG")}
        qs = await conn.quotes(["SPY", "GOOG"])
        self.assertAlmostEqual(qs["SPY"].mark()[0], 600.0)
        self.assertIsNotNone(qs["GOOG"].bar_close)
        # bar request used useRTH=False and TRADES as required
        self.assertEqual(fake.last_bar_kwargs["whatToShow"], "TRADES")
        self.assertIs(fake.last_bar_kwargs["useRTH"], False)

    async def test_no_price_at_all_raises(self):
        fake = FakeIB()
        fake.tickers = {"GOOG": ticker()}
        fake.bars = {"GOOG": []}
        conn = make_conn(fake, manage=True)
        conn._contracts = {"GOOG": NS(symbol="GOOG")}
        with self.assertRaises(NoPriceError):
            await conn.quotes(["GOOG"])


class OrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_market_day_and_ref(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)
        conn._contracts = {"SPY": NS(symbol="SPY")}
        h = await conn.place(OrderRequest("SPY", Side.SELL, 100.0, None, "run1"))
        self.assertEqual(len(fake.place_calls), 1)
        cb, ob = fake.place_calls[0]
        self.assertEqual(ob.action, "SELL")
        self.assertEqual(ob.totalQuantity, 100)
        self.assertEqual(ob.tif, "DAY")
        self.assertEqual(ob.account, "U123456")
        self.assertEqual(ob.orderRef, "zl-rebalancer:run1")
        self.assertIs(ob.outsideRth, False)

    async def test_buy_is_limit(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)
        conn._contracts = {"SPY": NS(symbol="SPY")}
        await conn.place(OrderRequest("SPY", Side.BUY, 8, 603.10, "r"))
        cb, ob = fake.place_calls[0]
        self.assertEqual(ob.action, "BUY")
        self.assertEqual(ob.lmtPrice, 603.10)

    async def test_fractional_quantity_refused(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)
        conn._contracts = {"SPY": NS(symbol="SPY")}
        with self.assertRaises(OrderPlacementError):
            await conn.place(OrderRequest("SPY", Side.SELL, 0.5, None, "r"))
        self.assertEqual(len(fake.place_calls), 0)

    async def test_status_mapping_unknown_is_working(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)
        conn._contracts = {"SPY": NS(symbol="SPY")}
        h = await conn.place(OrderRequest("SPY", Side.SELL, 10, None, "r"))
        # push a status change on the stored trade
        trade = fake.trades[str(1)]
        for status in ("Filled", "Cancelled", "ApiCancelled", "Inactive", "Submitted", ""):
            trade.orderStatus.status = status
            trade.orderStatus.filled = 0.0
            st = await conn.order_states([h])
            if status in ("Filled",):
                self.assertIs(st[0].status, OrderStatus.FILLED)
            elif status in ("Cancelled", "ApiCancelled"):
                self.assertIs(st[0].status, OrderStatus.CANCELLED)
            elif status == "Inactive":
                self.assertIs(st[0].status, OrderStatus.REJECTED)
            else:
                self.assertIs(st[0].status, OrderStatus.WORKING)

    async def test_working_orders_own_vs_foreign(self):
        fake = FakeIB()
        foreign = NS(contract=NS(symbol="QQQ"), order=_Order("BUY", 50, client_id=7),
                     isDone=lambda: False)
        own = NS(contract=NS(symbol="SPY"), order=_Order("SELL", 20, client_id=42),
                 isDone=lambda: False)
        done_other = NS(contract=NS(symbol="GLD"), order=_Order("BUY", 5),
                        isDone=lambda: True)
        fake.open_order_trades = [foreign, own, done_other]
        conn = make_conn(fake, manage=True)
        working = await conn.working_orders()
        self.assertEqual(len(working), 2)
        by_sym = {w.symbol: w for w in working}
        self.assertFalse(by_sym["QQQ"].own)
        self.assertTrue(by_sym["SPY"].own)


class CancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_unconfirmed_raises(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)

        def never_done():
            return False
        trading = NS(contract=NS(symbol="SPY"), order=_Order("SELL", 10),
                     isDone=never_done, orderStatus=NS(status="Submitted"))
        fake.trades = {"1": trading}
        with self.assertRaises(CancelNotConfirmedError):
            await conn.cancel_own_orders(0.01)
        self.assertIn(1, fake.cancelled)

    async def test_cancel_confirmed_returns_count(self):
        fake = FakeIB()
        conn = make_conn(fake, manage=True)
        # a working trade that becomes done once cancelOrder fires
        state = {"done": False}
        trade = NS(contract=NS(symbol="SPY"), order=_Order("SELL", 5),
                   isDone=lambda: state["done"])

        def _cancel(order):
            state["done"] = True
        fake.trades = {"1": trade}
        fake.cancelOrder = _cancel
        n = await conn.cancel_own_orders(0.5)
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()