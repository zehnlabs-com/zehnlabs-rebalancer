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
            await self.ib.reqAccountUpdatesAsync(account_id)

            await asyncio.sleep(0.5)

            portfolio_items = self.ib.portfolio(account=account_id)

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

            account_values = self.ib.accountValues(account=account_id)
            total_value = 0.0
            cash_balance = 0.0
            settled_cash = 0.0

            for value in account_values:
                if value.currency == 'USD':
                    if value.tag == 'NetLiquidation':
                        total_value = float(value.value)
                    elif value.tag == 'CashBalance':
                        cash_balance = float(value.value)
                    elif value.tag == 'SettledCash':
                        settled_cash = float(value.value)

            return {
                'account_id': account_id,
                'positions': positions,
                'total_value': total_value,
                'cash_balance': cash_balance,
                'settled_cash': settled_cash,
                'timestamp': asyncio.get_event_loop().time()
            }

        except Exception as e:
            self.logger.error(f"Failed to get account snapshot: {e}")
            raise

    async def get_multiple_market_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Get market prices for multiple symbols using batch request"""
        prices = {}

        try:
            # Create contracts for all symbols
            contracts = [Stock(symbol, 'SMART', 'USD') for symbol in symbols]

            # Qualify contracts
            qualified_contracts = []
            symbol_to_contract = {}

            for contract in contracts:
                try:
                    qualified = await self.ib.qualifyContractsAsync(contract)
                    if qualified:
                        qualified_contract = qualified[0]
                        qualified_contracts.append(qualified_contract)
                        symbol_to_contract[qualified_contract.symbol] = qualified_contract
                except Exception as e:
                    self.logger.debug(f"Failed to qualify contract for {contract.symbol}: {e}")

            if not qualified_contracts:
                self.logger.error("No contracts could be qualified")
                return {symbol: 0.0 for symbol in symbols}

            self.logger.info(f"Requesting batch prices for {len(qualified_contracts)} symbols...")

            # Batch request all tickers at once
            tickers = await self.ib.reqTickersAsync(*qualified_contracts)

            # Extract prices from tickers
            price_results = []
            failed_symbols = []

            for ticker in tickers:
                symbol = ticker.contract.symbol
                price = None

                # Try to get the best available price
                if ticker.last and ticker.last > 0:
                    price = ticker.last
                elif ticker.close and ticker.close > 0:
                    price = ticker.close
                elif ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    price = (ticker.bid + ticker.ask) / 2  # Mid-point

                if price and price > 0:
                    prices[symbol] = price
                    price_results.append(f"{symbol} -> ${price}")
                else:
                    failed_symbols.append(symbol)
                    self.logger.error(f"No valid price received for {symbol} from batch request")

            # Log all successful prices in one concise line
            if price_results:
                self.logger.info(f"Retrieved prices: {', '.join(price_results)}")

            # If any symbols failed, this is a critical system issue - fail immediately
            if failed_symbols:
                self.logger.error(f"Batch pricing failed for {len(failed_symbols)} symbols: {failed_symbols}")
                raise ValueError(f"Batch pricing API failed for symbols: {failed_symbols}. This indicates a system issue that must be resolved.")

            return prices

        except Exception as e:
            self.logger.error(f"Batch price request failed: {e}")
            raise ValueError(f"Batch pricing system failure. This indicates a serious system issue that must be resolved.")

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

        # If all exchanges fail, this is a critical error - we cannot proceed
        self.logger.error(f"Failed to get price for {symbol} on any exchange")
        raise ValueError(f"Cannot obtain valid price for {symbol} from any exchange. Trading operations cannot proceed safely.")

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