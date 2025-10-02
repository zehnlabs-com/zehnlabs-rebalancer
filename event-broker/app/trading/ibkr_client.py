"""Simplified IBKR client for direct trading operations"""

import asyncio
import os
import logging
import math
from typing import List, Optional
from ib_async import IB, Stock, MarketOrder, LimitOrder, Contract
from app.models import AccountSnapshot, AccountPosition, OrderResult, OpenOrder, ContractPrice

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

            # Set market data type for live data
            self.ib.reqMarketDataType(1)
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

    async def get_account_snapshot(self, account_id: str) -> AccountSnapshot:
        """Get account snapshot with positions and total value"""
        try:
            all_positions = self.ib.positions()
            account_positions = [p for p in all_positions if p.account == account_id]

            self.logger.info(f"Found {len(account_positions)} positions for account {account_id}")

            # Get market prices for all positions
            symbols = [pos.contract.symbol for pos in account_positions if pos.position != 0]
            market_prices_list = await self.get_multiple_market_prices(symbols)
            market_prices_map = {mp.symbol: mp for mp in market_prices_list}

            # Build positions list with market prices
            positions = []
            for pos in account_positions:
                if pos.position == 0:
                    continue  # Skip zero positions

                symbol = pos.contract.symbol
                price_data = market_prices_map.get(symbol)

                # Validate price data exists
                if not price_data:
                    self.logger.error(f"No price data for position {symbol} in account snapshot")
                    raise ValueError(f"No price data for {symbol}. Cannot generate account snapshot without valid prices.")

                # Validate bid price (get_multiple_market_prices already validated this)
                if not price_data.bid or price_data.bid <= 0:
                    self.logger.error(f"Invalid bid price for position {symbol}: {price_data.bid}")
                    raise ValueError(f"Invalid bid price for {symbol}: {price_data.bid}. Cannot generate account snapshot.")

                # For positions, use bid price (what you'd get if you sold)
                market_price = price_data.bid

                positions.append(
                    AccountPosition(
                        symbol=symbol,
                        quantity=pos.position,
                        market_price=market_price,
                        market_value=pos.position * market_price
                    )
                )

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

            return AccountSnapshot(
                account_id=account_id,
                positions=positions,
                total_value=total_value,
                cash_balance=cash_balance,
                settled_cash=settled_cash
            )

        except Exception as e:
            self.logger.error(f"Failed to get account snapshot: {e}")
            raise

    async def get_multiple_market_prices(self, symbols: List[str]) -> List[ContractPrice]:
        """Get market prices for multiple symbols using batch request"""
        prices = []

        try:
            # Create contracts for all symbols
            contracts = [Stock(symbol, 'SMART', 'USD') for symbol in symbols]

            # Qualify contracts
            qualified_contracts = []
            symbol_to_contract = {}

            failed_to_qualify = []
            for contract in contracts:
                try:
                    qualified = await self.ib.qualifyContractsAsync(contract)
                    if qualified:
                        qualified_contract = qualified[0]
                        qualified_contracts.append(qualified_contract)
                        symbol_to_contract[qualified_contract.symbol] = qualified_contract
                    else:
                        failed_to_qualify.append(contract.symbol)
                except Exception as e:
                    self.logger.debug(f"Failed to qualify contract for {contract.symbol}: {e}")
                    failed_to_qualify.append(contract.symbol)

            if not qualified_contracts:
                self.logger.error(f"Failed to qualify any contracts for symbols: {symbols}")
                raise ValueError(f"Contract qualification failed for all symbols: {symbols}. Cannot retrieve market prices without valid contracts.")

            # If some contracts failed to qualify, this is also a critical issue
            if failed_to_qualify:
                self.logger.error(f"Failed to qualify contracts for {len(failed_to_qualify)} symbols: {failed_to_qualify}")
                raise ValueError(f"Contract qualification failed for symbols: {failed_to_qualify}. All symbols must be qualified to proceed with rebalancing.")

            self.logger.info(f"Requesting batch prices for {len(qualified_contracts)} symbols...")

            # Batch request all tickers at once
            tickers = await self.ib.reqTickersAsync(*qualified_contracts)

            # Extract prices from tickers
            price_results = []
            failed_symbols = []

            for ticker in tickers:
                symbol = ticker.contract.symbol

                # Validate that BOTH bid and ask are available - required for trading
                # Check for all invalid cases: None, -1, 0, negative, or NaN
                if (ticker.bid is None or ticker.bid <= 0 or math.isnan(ticker.bid) or
                    ticker.ask is None or ticker.ask <= 0 or math.isnan(ticker.ask)):
                    failed_symbols.append(symbol)
                    self.logger.error(f"Missing bid/ask prices for {symbol} (bid={ticker.bid}, ask={ticker.ask}). Cannot execute trades without valid market quotes.")
                    continue

                # Extract valid prices (bid/ask are guaranteed valid at this point)
                # For last/close, default to 0.0 if invalid (these are optional for display purposes)
                last = ticker.last if (ticker.last and ticker.last > 0 and not math.isnan(ticker.last)) else 0.0
                close = ticker.close if (ticker.close and ticker.close > 0 and not math.isnan(ticker.close)) else 0.0

                # Store all available prices
                prices.append(ContractPrice(
                    symbol=symbol,
                    bid=ticker.bid,
                    ask=ticker.ask,
                    last=last,
                    close=close
                ))

                # Log successful price retrieval
                price_results.append(f"{symbol} -> ${ticker.ask:.2f}")

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

    async def place_order(self, account_id: str, symbol: str, quantity: int, order_type: str = 'MARKET', price: float = None) -> OrderResult:
        """Place an order"""
        try:
            # Create contract
            contract = Stock(symbol, 'SMART', 'USD')
            qualified = await self.ib.qualifyContractsAsync(contract)

            if not qualified:
                raise ValueError(f"Could not qualify contract for {symbol}")

            contract = qualified[0]

            # Create order based on type
            action = 'BUY' if quantity > 0 else 'SELL'
            if order_type == 'LIMIT' and price is not None:
                order = LimitOrder(action, abs(quantity), price)
            else:
                order = MarketOrder(action, abs(quantity))

            order.account = account_id

            # Place order
            trade = self.ib.placeOrder(contract, order)
            await asyncio.sleep(1)  # Allow order to be processed

            order_desc = f"{order.action} {order.totalQuantity} {symbol}"
            if order_type == 'LIMIT':
                order_desc += f" @ ${price}"
            self.logger.info(f"Placed order: {order_desc}")

            return OrderResult(
                order_id=trade.order.orderId,
                symbol=symbol,
                quantity=quantity,
                status=trade.orderStatus.status
            )

        except Exception as e:
            self.logger.error(f"Failed to place order for {symbol}: {e}")
            raise

    async def get_open_orders(self, account_id: str) -> List[OpenOrder]:
        """Get open orders for account"""
        try:
            trades = self.ib.trades()
            open_orders = []

            for trade in trades:
                if (trade.order.account == account_id and
                    trade.orderStatus.status not in ['Filled', 'Cancelled']):
                    open_orders.append(OpenOrder(
                        order_id=trade.order.orderId,
                        symbol=trade.contract.symbol,
                        quantity=trade.order.totalQuantity,
                        status=trade.orderStatus.status,
                        order_type='MARKET'
                    ))

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