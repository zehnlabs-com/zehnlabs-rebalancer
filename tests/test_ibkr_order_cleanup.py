import unittest
from unittest.mock import Mock, patch

from app_config import AppConfig
from ib_async import LimitOrder, OrderStatus, Stock, Trade
from ibkr_connector.client import IBKRClient


class IBKROrderCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_active_orders_for_the_account_are_eligible_for_cleanup(self):
        account = "DU100001"
        order_states = (
            (account, "Submitted"),
            (account, "PreSubmitted"),
            (account, "PendingCancel"),
            (account, "Inactive"),
            (account, ""),
            (account, "Filled"),
            (account, "Cancelled"),
            ("DU100002", "Submitted"),
        )
        ib = Mock()
        ib.trades.return_value = [
            Trade(
                Stock("TEST", "SMART", "USD"),
                LimitOrder("BUY", 1, 10, account=order_account, orderId=order_id),
                OrderStatus(status=status),
            )
            for order_id, (order_account, status) in enumerate(order_states, start=1)
        ]
        with patch("ibkr_connector.client.IB", return_value=ib), patch(
            "ibkr_connector.client.get_config", return_value=AppConfig()
        ):
            client = IBKRClient(client_id=100001)

        open_orders = await client.get_open_orders(account)

        self.assertEqual([order.order_id for order in open_orders], ["1", "2"])
