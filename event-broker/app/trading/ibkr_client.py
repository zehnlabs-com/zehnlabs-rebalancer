"""Simplified IBKR client for direct trading operations"""

import asyncio
import os
import logging
from typing import Dict, List, Optional, Any
from ib_async import IB, Stock, MarketOrder, Contract

class IBKRClient:
    """Simplified IBKR client with dedicated connection per account"""

    def __init__(self, client_id: int, logger: Optional[logging.Logger] = None):
        self.ib = IB()
        self.ib.RequestTimeout = 10.0
        self.client_id = client_id
        self.logger = logger or logging.getLogger(__name__)
        self.host = os.getenv('IB_HOST', 'ibkr-gateway')

        # Automatically determine port based on trading mode
        self.port = self._determine_port()

    def _determine_port(self) -> int:
        """Determine the correct IBKR Gateway port based on trading mode"""
        trading_mode = os.getenv('TRADING_MODE', 'paper').lower()

        # Debug logging
        self.logger.info(f"TRADING_MODE detected: '{trading_mode}'")

        # Allow manual override via IB_PORT if explicitly set
        manual_port = os.getenv('IB_PORT')
        self.logger.info(f"IB_PORT environment variable: {manual_port}")

        if manual_port:
            port = int(manual_port)
            self.logger.info(f"Using manually configured IB_PORT: {port}")
            return port

        # Automatic port determination based on trading mode
        if trading_mode == 'live':
            port = 4003  # Live trading port
            self.logger.info(f"Auto-detected port {port} for LIVE trading mode")
        else:
            port = 4004  # Paper trading port (default)
            self.logger.info(f"Auto-detected port {port} for PAPER trading mode")

        return port

    async def connect(self) -> bool:
        """Connect to IBKR Gateway"""
        if self.ib.isConnected():
            return True

        try:
            self.logger.info(f"Connecting to IBKR Gateway at {self.host}:{self.port} with client ID {self.client_id}")
            await self.ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=10
            )

            # Wait a bit for connection to stabilize
            await asyncio.sleep(0.5)

            # Check if still connected
            if not self.ib.isConnected():
                self.logger.error("Connection dropped immediately after connecting")
                return False

            # Set market data type for delayed data
            self.ib.reqMarketDataType(3)
            self.logger.info("Successfully connected to IBKR Gateway")
            return True

        except Exception as e:
            self.logger.error(f"Failed to connect to IBKR Gateway: {e}")
            return False

    async def disconnect(self):
        """Disconnect from IBKR Gateway"""
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
                self.logger.info("Disconnected from IBKR Gateway")
        except Exception as e:
            self.logger.error(f"Error disconnecting: {e}")

    def is_connected(self) -> bool:
        """Check if connected to IBKR Gateway"""
        return self.ib.isConnected()

    async def get_account_snapshot(self, account_id: str) -> Dict[str, Any]:
        """Get account snapshot with positions and total value"""
        try:
            # Get portfolio positions
            portfolio_items = self.ib.portfolio(account=account_id)
            await asyncio.sleep(1)  # Allow data to populate

            positions = []

            for item in portfolio_items:
                position = {
                    'symbol': item.contract.symbol,
                    'quantity': item.position,
                    'market_price': item.marketPrice,
                    'market_value': item.marketValue,
                    'avg_cost': item.averageCost
                }
                positions.append(position)

            # Get account values - use NetLiquidation as the definitive account value
            account_values = self.ib.accountValues(account=account_id)
            total_value = 0.0

            for value in account_values:
                if value.tag == 'NetLiquidation' and value.currency == 'USD':
                    total_value = float(value.value)
                    break

            return {
                'account_id': account_id,
                'positions': positions,
                'total_value': total_value,
                'timestamp': asyncio.get_event_loop().time()
            }

        except Exception as e:
            self.logger.error(f"Failed to get account snapshot: {e}")
            raise

    async def get_multiple_market_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get market prices for multiple symbols with fallback exchanges"""
        prices = {}

        try:
            for symbol in symbols:
                price = await self._get_single_market_price(symbol)
                prices[symbol] = price

            return prices

        except Exception as e:
            self.logger.error(f"Failed to get market prices: {e}")
            return {symbol: 0.0 for symbol in symbols}

    async def _get_single_market_price(self, symbol: str) -> float:
        """Get market price for a single symbol with exchange fallback"""
        # Common exchanges to try in order of preference
        exchanges = ['SMART', 'ARCA', 'NASDAQ', 'NYSE', 'NYSEARCA']

        for exchange in exchanges:
            try:
                contract = Stock(symbol, exchange, 'USD')
                qualified = await self.ib.qualifyContractsAsync(contract)

                if qualified:
                    contract = qualified[0]
                    ticker = self.ib.reqMktData(contract, '', False, False)

                    # Wait longer for data to populate
                    await asyncio.sleep(2.0)

                    if ticker.last and ticker.last > 0:
                        price = ticker.last
                        self.logger.info(f"Got price for {symbol} from {exchange}: ${price}")
                        self.ib.cancelMktData(contract)
                        return price
                    elif ticker.close and ticker.close > 0:
                        price = ticker.close
                        self.logger.info(f"Got close price for {symbol} from {exchange}: ${price}")
                        self.ib.cancelMktData(contract)
                        return price

                    self.ib.cancelMktData(contract)
                    self.logger.debug(f"No valid price data for {symbol} on {exchange}")

            except Exception as e:
                self.logger.debug(f"Failed to get price for {symbol} on {exchange}: {e}")
                continue

        # If all exchanges fail, log error and return 0
        self.logger.error(f"Failed to get price for {symbol} on any exchange")
        return 0.0

    async def place_order(self, account_id: str, symbol: str, quantity: int, order_type: str = 'MARKET') -> Dict[str, Any]:
        """Place an order"""
        try:
            # Create contract
            contract = Stock(symbol, 'SMART', 'USD')
            qualified = await self.ib.qualifyContractsAsync(contract)

            if not qualified:
                raise ValueError(f"Could not qualify contract for {symbol}")

            contract = qualified[0]

            # Create order
            order = MarketOrder('BUY' if quantity > 0 else 'SELL', abs(quantity))
            order.account = account_id

            # Place order
            trade = self.ib.placeOrder(contract, order)
            await asyncio.sleep(1)  # Allow order to be processed

            self.logger.info(f"Placed order: {order.action} {order.totalQuantity} {symbol}")

            return {
                'order_id': trade.order.orderId,
                'symbol': symbol,
                'quantity': quantity,
                'status': trade.orderStatus.status
            }

        except Exception as e:
            self.logger.error(f"Failed to place order for {symbol}: {e}")
            raise

    async def get_open_orders(self, account_id: str) -> List[Dict[str, Any]]:
        """Get open orders for account"""
        try:
            trades = self.ib.trades()
            open_orders = []

            for trade in trades:
                if (trade.order.account == account_id and
                    trade.orderStatus.status not in ['Filled', 'Cancelled']):
                    open_orders.append({
                        'order_id': trade.order.orderId,
                        'symbol': trade.contract.symbol,
                        'quantity': trade.order.totalQuantity,
                        'status': trade.orderStatus.status
                    })

            return open_orders

        except Exception as e:
            self.logger.error(f"Failed to get open orders: {e}")
            return []

    async def cancel_order(self, order_id: int):
        """Cancel an order"""
        try:
            trades = self.ib.trades()
            for trade in trades:
                if trade.order.orderId == order_id:
                    self.ib.cancelOrder(trade.order)
                    self.logger.info(f"Cancelled order {order_id}")
                    return

            self.logger.warning(f"Order {order_id} not found")

        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")

    async def get_order_status(self, order_id: int) -> str:
        """Get status of an order"""
        try:
            trades = self.ib.trades()
            for trade in trades:
                if trade.order.orderId == order_id:
                    return trade.orderStatus.status
            return 'NOT_FOUND'

        except Exception as e:
            self.logger.error(f"Failed to get order status for {order_id}: {e}")
            return 'ERROR'