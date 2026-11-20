"""WP2 acceptance tests for broker-connector-base (§6)."""

import unittest
from dataclasses import FrozenInstanceError

from broker_connector_base import (
    BrokerConnection,
    BrokerError,
    ConnectionFailedError,
    AccountDataError,
    PricingError,
    NoPriceError,
    CancelNotConfirmedError,
    OrderPlacementError,
    OrderStatus,
    Side,
    PriceSource,
    Quote,
    Position,
    AccountState,
    OrderRequest,
    OrderState,
    WorkingOrder,
)


class ErrorHierarchyTests(unittest.TestCase):
    def test_all_errors_are_broker_errors_and_public_subclasses_exist(self):
        for cls in (ConnectionFailedError, AccountDataError, PricingError,
                    CancelNotConfirmedError, OrderPlacementError):
            self.assertTrue(issubclass(cls, BrokerError))

    def test_noprice_is_a_pricing_error(self):
        self.assertTrue(issubclass(NoPriceError, PricingError))


class EnumTests(unittest.TestCase):
    def test_order_status_values_equal_names(self):
        for member in OrderStatus:
            self.assertEqual(member.value, member.name)

    def test_side_and_price_source_values(self):
        self.assertEqual(Side.BUY.value, "BUY")
        self.assertEqual(Side.SELL.value, "SELL")
        self.assertEqual(PriceSource.MID.value, "mid")
        self.assertEqual(PriceSource.ASK.value, "ask")
        self.assertEqual(PriceSource.CLOSE.value, "close")
        self.assertEqual(PriceSource.BAR.value, "bar")


class QuotePriceTests(unittest.TestCase):
    def test_mark_uses_mid_when_bid_and_ask(self):
        q = Quote("SPY", bid=599.9, ask=600.1)
        price, source = q.mark()
        self.assertAlmostEqual(price, 600.0)
        self.assertIs(source, PriceSource.MID)

    def test_mark_falls_back_last_then_close_then_bar(self):
        q = Quote("A", last=10.0)
        self.assertEqual(q.mark(), (10.0, PriceSource.LAST))
        q = Quote("B", close=11.0)
        self.assertEqual(q.mark(), (11.0, PriceSource.CLOSE))
        q = Quote("C", bar_close=12.0)
        self.assertEqual(q.mark(), (12.0, PriceSource.BAR))

    def test_mark_no_price_raises(self):
        with self.assertRaises(NoPriceError):
            Quote("X").mark()
        with self.assertRaises(NoPriceError):
            Quote("X", bid=5.0).mark()  # bid without ask is not a mid

    def test_buy_reference_prefers_ask_without_bid(self):
        q = Quote("A", ask=600.1)
        self.assertEqual(q.buy_reference(), (600.1, PriceSource.ASK))
        q = Quote("B", last=9.0)
        self.assertEqual(q.buy_reference(), (9.0, PriceSource.LAST))
        with self.assertRaises(NoPriceError):
            Quote("C").buy_reference()

    def test_sell_reference_prefers_bid(self):
        q = Quote("A", bid=599.9, last=600.0)
        self.assertEqual(q.sell_reference(), (599.9, PriceSource.BID))
        with self.assertRaises(NoPriceError):
            Quote("X", ask=1.0).sell_reference()  # ask only is not a sell reference

    def test_non_finite_and_negatives_cleaned_to_none(self):
        q = Quote("A", bid=float("nan"), ask=float("inf"), last=-5.0, close=0.0,
                  bar_close=float("inf"))
        self.assertIsNone(q._clean()["bid"])
        self.assertIsNone(q._clean()["ask"])
        self.assertIsNone(q._clean()["last"])
        self.assertIsNone(q._clean()["close"])
        self.assertIsNone(q._clean()["bar_close"])


class ModelTests(unittest.TestCase):
    def test_plain_value_types_are_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            Position("SPY", 10.0).quantity = 12.0

    def test_account_state_holds_positions(self):
        pos = Position("SPY", 70)
        st = AccountState("U1", cash=2500.0, net_liquidation=104000.0, positions=(pos,))
        self.assertEqual(st.positions[0].symbol, "SPY")
        self.assertEqual(st.cash, 2500.0)

    def test_order_request_defaults_to_market(self):
        o = OrderRequest("SPY", Side.SELL, 100)
        self.assertIsNone(o.limit_price)

    def test_working_order_carries_own_flag(self):
        w = WorkingOrder("SPY", Side.BUY, 8, own=True)
        self.assertTrue(w.own)


class _Concrete(BrokerConnection):
    supports_fractional = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def account_state(self):
        raise NotImplementedError

    async def quotes(self, symbols):
        raise NotImplementedError

    async def working_orders(self):
        raise NotImplementedError

    async def cancel_own_orders(self, timeout_seconds):
        raise NotImplementedError

    async def place(self, order):
        raise NotImplementedError

    async def order_states(self, handles):
        raise NotImplementedError


class BrokerConnectionTests(unittest.IsolatedAsyncioTestCase):
    def test_abc_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            BrokerConnection()

    async def test_async_with_connects_and_closes(self):
        c = _Concrete()
        async with c as obj:
            self.assertIs(obj, c)
            self.assertTrue(c.connected)
        self.assertTrue(c.closed)


if __name__ == "__main__":
    unittest.main()