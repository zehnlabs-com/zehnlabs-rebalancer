import asyncio
import math
import random
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple, Any
from ib_async import IB, Stock, MarketOrder, Contract
from ib_async.contract import ContractDetails
from app.config import config
from app.logger import AppLogger
from app.models.rebalance_data import AccountSnapshot, CurrentPosition, PortfolioSnapshot, PortfolioPosition
from app.models.ibkr_data import PnLData, ContractDetailsData, TradingHoursResult, MarketPrices

app_logger = AppLogger(__name__)
class IBKRClient:
    def __init__(self, service_container=None, client_id_range=None):
        self.ib = IB()
        self.ib.RequestTimeout = 10.0  # Match rebalancer-api timeout
        
        # Use client ID range to avoid conflicts between services
        if client_id_range:
            self.client_id = random.randint(client_id_range[0], client_id_range[1])
        else:
            # Default range for backward compatibility
            self.client_id = random.randint(1000, 2999)
        
        # Add synchronization locks
        self._connection_lock = asyncio.Lock()
        self._order_lock = asyncio.Lock()
        
        # Get Redis data service from DI container
        self.service_container = service_container
        self.redis_data_service = None
        self._redis_initialized = False
    
    async def connect(self) -> bool:
        if self.ib.isConnected():  # This method is synchronous and safe to use
            return True
        
        try:
            # Direct connection like the old working code
            app_logger.log_info(f"Attempting to connect to IB Gateway at {config.ibkr.host}:{config.ibkr.port} with client ID {self.client_id}")
            await self.ib.connectAsync(
                host=config.ibkr.host,
                port=config.ibkr.port,
                clientId=self.client_id,
                timeout=10  # Use same timeout as old working code
            )
            app_logger.log_info(f"Successfully connected to IB Gateway at {config.ibkr.host}:{config.ibkr.port} with client ID {self.client_id}")
            
            # Set market data type for proper price data (1=real-time, 3=delayed, 4=frozen)
            self.ib.reqMarketDataType(3)  # Use delayed data (should work without special permissions)
            
            # Subscribe to error events
            self.ib.errorEvent += self._on_error_event
            
            # Initialize Redis data service from DI container if available
            if not self._redis_initialized and self.service_container:
                self.redis_data_service = self.service_container.redis_account_service()
                self._redis_initialized = True
            
            return True
        except TimeoutError as e:
            app_logger.log_error(f"Connection timeout to IB Gateway at {config.ibkr.host}:{config.ibkr.port}: {e}")
            return False
        except ConnectionRefusedError as e:
            app_logger.log_error(f"Connection refused to IB Gateway at {config.ibkr.host}:{config.ibkr.port}: {e}")
            return False
        except Exception as e:
            app_logger.log_error(f"Failed to connect to IB Gateway at {config.ibkr.host}:{config.ibkr.port}: {type(e).__name__}: {e}")
            return False    
    
    # Removed _init_redis method as RedisDataService is now injected
    
    def _on_error_event(self, reqId, errorCode, errorString, advancedOrderRejectJson):
        """Event handler for IB errors - stores detailed error information in Redis"""
        if self._redis_initialized and errorCode:
            # Store error details with reqId as key
            error_data = {
                'error_code': errorCode,
                'error_string': errorString,
                'timestamp': datetime.now().isoformat(),
                'advanced_order_reject_json': advancedOrderRejectJson
            }
            
            # Run Redis operation in background to avoid blocking
            asyncio.create_task(self.redis_data_service.store_ibkr_error(reqId, error_data))
    
    
    async def _store_order_mapping(self, reqId, orderId):
        """Store reqId -> orderId mapping for error correlation"""
        await self.redis_data_service.store_order_mapping(reqId, orderId)
    
    async def getOrderErrors(self, orderId: int) -> Optional[Dict]:
        """Get detailed error information for an order"""
        if not self._redis_initialized:
            return None
        return await self.redis_data_service.get_ibkr_error(orderId)
    
    
    async def get_account_value(self, account_id: str, tag: str = "NetLiquidation", event=None) -> float:
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        try:
            # Use accountSummaryAsync with timeout to prevent hanging
            account_summary = await asyncio.wait_for(
                self.ib.accountSummaryAsync(),
                timeout=30.0
            )
            for av in account_summary:
                if av.tag == tag and av.currency == "USD" and av.account == account_id:
                    return float(av.value)
            
            # If not found in summary, raise error like simple algorithm
            if tag == "NetLiquidation":
                raise Exception(f"Could not retrieve {tag} value for account {account_id} from IB.")
            return 0.0
        except Exception as e:
            app_logger.log_error(f"Failed to get account value: {e}", event)
            raise
    
    async def get_cash_balance(self, account_id: str) -> float:
        """Get available cash balance for the account"""
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        try:
            account_values = self.ib.accountValues(account_id)
            # Try TotalCashValue first, fall back to AvailableFunds
            for av in account_values:
                if av.tag == "TotalCashValue" and av.currency == "USD":
                    return float(av.value)
            
            # Fallback to AvailableFunds if TotalCashValue not found
            for av in account_values:
                if av.tag == "AvailableFunds" and av.currency == "USD":
                    return float(av.value)
            
            return 0.0
        except Exception as e:
            app_logger.log_error(f"Failed to get cash balance: {e}")
            raise
    
    async def get_account_pnl(self, account_id: str) -> PnLData:
        """Get P&L data for the account using IBKR's reqPnL method"""
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        try:
            app_logger.log_debug(f"Requesting P&L for account {account_id}")
            
            # Request P&L subscription - this returns a PnL object that gets updated
            pnl_obj = self.ib.reqPnL(account_id)
            
            # Wait a moment for the PnL data to be populated
            await asyncio.sleep(2)  # Give it time to get data
            
            # Extract P&L values from the object
            daily_pnl = float(pnl_obj.dailyPnL) if pnl_obj.dailyPnL else 0.0
            unrealized_pnl = float(pnl_obj.unrealizedPnL) if pnl_obj.unrealizedPnL else 0.0
            realized_pnl = float(pnl_obj.realizedPnL) if pnl_obj.realizedPnL else 0.0
            
            # Cancel the subscription using the account
            self.ib.cancelPnL(account_id)
            
            app_logger.log_debug(f"P&L for account {account_id}: daily={daily_pnl}, unrealized={unrealized_pnl}, realized={realized_pnl}")
            
            return PnLData(
                daily_pnl=daily_pnl,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl
            )
                
        except Exception as e:
            app_logger.log_error(f"Failed to get P&L for account {account_id}: {e}")
            # Return zeros if P&L request fails
            return PnLData(
                daily_pnl=0.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0
            )
    
    async def get_account_snapshot(self, account_id: str, event=None) -> AccountSnapshot:
        """Get complete account snapshot with positions and account value"""
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        try:
            app_logger.log_debug(f"Getting account snapshot for {account_id}", event)
            
            # Get account value
            total_value = await self.get_account_value(account_id, event=event)
            
            # Get positions
            positions_data = await self.ib.reqPositionsAsync()
            positions = []
            for position in positions_data:
                if position.account == account_id and position.position != 0:
                    # Calculate market value if not available
                    market_value = getattr(position, 'marketValue', position.position * position.avgCost)
                    
                    positions.append(CurrentPosition(
                        symbol=position.contract.symbol,
                        shares=int(position.position),
                        market_value=float(market_value),
                        average_cost=float(position.avgCost)
                    ))
            
            # Get cash balance
            cash_balance = await self.get_cash_balance(account_id)
            
            app_logger.log_debug(f"Account snapshot: {len(positions)} positions, total value: ${total_value:.2f}", event)
            
            return AccountSnapshot(
                account_id=account_id,
                total_value=total_value,
                positions=positions,
                cash_balance=cash_balance
            )
            
        except Exception as e:
            app_logger.log_error(f"Failed to get account snapshot: {e}", event)
            raise

    async def get_portfolio_snapshot(self, account_id: str, event=None) -> PortfolioSnapshot:
        """Get complete portfolio snapshot with current market prices using optimal 3-call strategy"""
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        try:
            app_logger.log_debug(f"Getting portfolio snapshot for {account_id}", event)
            
            # Use working methods for account data - prioritize reliability over call count optimization
            total_value = await self.get_account_value(account_id, event=event)
            cash_balance = await self.get_cash_balance(account_id)
            
            # CALL 2: Get all positions for the account
            positions_data = await self.ib.reqPositionsAsync()
            account_positions = [p for p in positions_data if p.account == account_id and p.position != 0]
            
            if not account_positions:
                return PortfolioSnapshot(
                    account_id=account_id,
                    total_value=total_value,
                    cash_balance=cash_balance,
                    positions=[]
                )
            
            # CALL 3: Get current market prices for all position symbols
            symbols = [pos.contract.symbol for pos in account_positions]
            market_prices = await self.get_multiple_market_prices(symbols, event)
            
            # Build portfolio positions with current market data
            positions = []
            for position in account_positions:
                symbol = position.contract.symbol
                shares = float(position.position)
                average_cost = float(position.avgCost)
                market_price = market_prices.get(symbol, 0.0)
                
                # Calculate market value and P&L with current prices
                market_value = abs(shares) * market_price
                cost_basis = abs(shares) * average_cost
                unrealized_pnl = market_value - cost_basis
                
                positions.append(PortfolioPosition(
                    symbol=symbol,
                    shares=shares,
                    market_price=market_price,
                    market_value=market_value,
                    average_cost=average_cost,
                    unrealized_pnl=unrealized_pnl
                ))
            
            app_logger.log_debug(f"Portfolio snapshot: {len(positions)} positions, total value: ${total_value:.2f}", event)
            
            return PortfolioSnapshot(
                account_id=account_id,
                total_value=total_value,
                cash_balance=cash_balance,
                positions=positions
            )
            
        except Exception as e:
            app_logger.log_error(f"Failed to get portfolio snapshot: {e}", event)
            raise

    
    async def _fetch_single_snapshot_price(self, contract: 'Contract') -> Optional[Tuple[str, float]]:
        """
        Phase 1 helper: Fetches a price for one contract using a snapshot.
        Improved error handling to avoid Error 300 issues.
        """
        ticker = None
        try:
            # Ensure contract is properly qualified to avoid Error 300
            if not hasattr(contract, 'conId') or not contract.conId:
                app_logger.log_warning(f"Contract {contract.symbol} not properly qualified, skipping snapshot")
                return None
                
            # Request market data snapshot
            ticker = self.ib.reqMktData(
                contract, genericTickList="", snapshot=True, regulatorySnapshot=False
            )
            
            if not ticker:
                app_logger.log_warning(f"Failed to create ticker for {contract.symbol}")
                return None
            
            price = float('nan')
            
            # Wait for ticker data with more generous timeout during market hours
            max_wait_time = 30  # 3 seconds total (30 * 0.1s)
            for i in range(max_wait_time):
                await asyncio.sleep(0.1)
                
                # Check for valid market data in priority order
                market_p = ticker.marketPrice()
                last_p = ticker.last
                close_p = ticker.close
                bid_p = ticker.bid
                ask_p = ticker.ask
                
                # Prefer live market price, then last trade, then mid-point of bid/ask, then close
                if not math.isnan(market_p) and market_p > 0:
                    price = market_p
                    break
                elif last_p and not math.isnan(last_p) and last_p > 0:
                    price = last_p
                    break
                elif (bid_p and ask_p and not math.isnan(bid_p) and not math.isnan(ask_p) 
                      and bid_p > 0 and ask_p > 0):
                    price = (bid_p + ask_p) / 2
                    break
                elif close_p and not math.isnan(close_p) and close_p > 0:
                    price = close_p
                    break
            
            if math.isnan(price) or price <= 0:
                return None
                
            return (contract.symbol, price)
            
        except Exception as e:
            app_logger.log_debug(f"Snapshot request failed for {contract.symbol}: {e}")
            return None
        finally:
            # Properly cancel market data subscription to avoid Error 300
            if ticker:
                try:
                    # Only attempt to cancel if ticker has a valid reqId
                    if hasattr(ticker, 'reqId') and ticker.reqId is not None:
                        self.ib.cancelMktData(ticker)
                    else:
                        app_logger.log_debug(f"Ticker for {contract.symbol} has no reqId, skipping cancelMktData")
                except Exception as e:
                    # Log but don't re-raise cancellation errors
                    app_logger.log_debug(f"Error cancelling market data for {contract.symbol}: {e}")

    async def _fetch_single_historical_price(self, contract: 'Contract') -> Optional[Tuple[str, float]]:
        """
        Phase 2 helper: Fetches the last closing price for one contract from historical data.
        """
        try:
            # Request the last 1 day of data to get the most recent close
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="2 D",  # Request 2 days to ensure we get at least one bar
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1
            )
            if bars:
                # Return the close of the most recent bar
                return (contract.symbol, bars[-1].close)
            return None
        except Exception as e:
            app_logger.log_warning(f"Historical data fetch failed for {contract.symbol}: {e}")
            return None

    async def get_multiple_market_prices(self, symbols: List[str], event=None) -> MarketPrices:
        """
        Gets market prices using a robust, two-phase concurrent strategy.
        Phase 1: Concurrent snapshot requests for all symbols during market hours.
        Phase 2: Historical data fallback for any symbols that failed Phase 1.
        """
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        if not symbols:
            return MarketPrices(prices={})
        
        # Qualify all contracts first to ensure proper contract specifications
        contracts = [Stock(s, 'SMART', 'USD') for s in symbols]
        try:
            qualified_contracts = await self.ib.qualifyContractsAsync(*contracts)
            # Filter out any contracts that failed qualification
            qualified_contracts = [c for c in qualified_contracts if hasattr(c, 'conId') and c.conId]
            
            if len(qualified_contracts) != len(symbols):
                failed_symbols = [s for s in symbols if s not in [c.symbol for c in qualified_contracts]]
                app_logger.log_warning(f"Failed to qualify contracts for: {failed_symbols}", event)
                
        except Exception as e:
            app_logger.log_error(f"Failed to qualify contracts for symbols {symbols}: {e}", event)
            raise RuntimeError(f"Could not qualify contracts for: {symbols}. Cannot proceed.")

        prices: Dict[str, float] = {}
        contracts_map = {c.symbol: c for c in qualified_contracts}

        # --- Phase 1: Concurrent Snapshot Requests ---
        # Use gather with return_exceptions=True to handle individual failures gracefully
        snapshot_tasks = [self._fetch_single_snapshot_price(c) for c in qualified_contracts]
        snapshot_results = await asyncio.gather(*snapshot_tasks, return_exceptions=True)
        
        successful_snapshots = 0
        for i, result in enumerate(snapshot_results):
            if isinstance(result, Exception):
                app_logger.log_debug(f"Snapshot exception for {qualified_contracts[i].symbol}: {result}")
                continue
            if result:
                symbol, price = result
                prices[symbol] = price
                successful_snapshots += 1

        # --- Phase 2: Concurrent Historical Fallback ---
        successful_historical = 0
        remaining_symbols = [s for s in symbols if s not in prices]
        if remaining_symbols:
            remaining_contracts = [contracts_map[s] for s in remaining_symbols if s in contracts_map]
            
            if remaining_contracts:
                historical_tasks = [self._fetch_single_historical_price(c) for c in remaining_contracts]
                historical_results = await asyncio.gather(*historical_tasks, return_exceptions=True)

                for i, result in enumerate(historical_results):
                    if isinstance(result, Exception):
                        app_logger.log_debug(f"Historical exception for {remaining_contracts[i].symbol}: {result}")
                        continue
                    if result:
                        symbol, price = result
                        prices[symbol] = price
                        successful_historical += 1

        # --- Final Check ---
        final_missing = [s for s in symbols if s not in prices]
        if final_missing:
            app_logger.log_error(f"Could not fetch prices for: {final_missing} after all fallbacks", event)
            raise RuntimeError(f"Could not fetch price for: {final_missing} after all fallbacks.")
        
        # Single consolidated completion log
        phase2_msg = f", Phase 2 (Historical): {successful_historical}/{len(remaining_symbols) if remaining_symbols else 0}" if remaining_symbols else ""
        app_logger.log_debug(f"Market prices retrieved for {len(symbols)} symbols - Phase 1 (Snapshot): {successful_snapshots}/{len(qualified_contracts)}{phase2_msg}")
        return MarketPrices(prices=prices)
    
    
    async def place_order(self, account_id: str, symbol: str, quantity: int, order_type: str = "MKT", event=None, 
                        time_in_force: str = "DAY"):
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        contract = Stock(symbol, 'SMART', 'USD')
        
        # Use the async version for consistency
        try:
            qualified_contracts = await self.ib.qualifyContractsAsync(contract)
            if not qualified_contracts:
                raise Exception(f"Could not qualify contract for {symbol}")
            contract = qualified_contracts[0]
        except Exception as e:
            app_logger.log_error(f"Failed to qualify contract for {symbol}: {e}", event)
            raise RuntimeError(f"Could not qualify contract for: {symbol}. Cannot proceed.")

        action = "BUY" if quantity > 0 else "SELL"        
        
        order = MarketOrder(action, abs(quantity))
        order.account = account_id
        
        trade = self.ib.placeOrder(contract, order)
        app_logger.log_info(f"Order placed: ID={trade.order.orderId}; {action} {abs(quantity)} shares of {symbol}", event)
        
        # Store reqId -> orderId mapping for error correlation
        if self._redis_initialized and hasattr(trade, 'order') and hasattr(trade.order, 'orderId'):
            # The reqId for order placement is typically the orderId
            if self._redis_initialized:
                asyncio.create_task(self._store_order_mapping(trade.order.orderId, trade.order.orderId))
        
        return trade
    
    async def get_order_failure_message(self, trade) -> str:
        """
        Extract detailed error message from a failed trade.
        Returns formatted error message with IBKR error code if available,
        otherwise returns generic status-based message.
        """
        order_id = trade.order.orderId
        
        # First try to get detailed error from Redis
        error_details = await self.getOrderErrors(order_id)
        if error_details:
            error_code = error_details.get('error_code', 'Unknown')
            error_string = error_details.get('error_string', 'Unknown error')
            return f"Order {order_id} failed - Error {error_code}: {error_string}"
        
        # Check trade log for detailed error information as fallback
        
        # Check trade log for detailed error information
        if hasattr(trade, 'log') and trade.log:
            for log_entry in trade.log:
                if hasattr(log_entry, 'errorCode') and log_entry.errorCode:
                    return f"Order {order_id} failed - Error {log_entry.errorCode}: {log_entry.message}"
        
        # Fallback to generic status message
        return f"Order {order_id} failed with status: {trade.orderStatus.status}"
    
    async def cancel_all_orders(self, account_id: str, event=None) -> List[Dict]:
        """Cancel all pending orders for the given account.
        
        This method cancels all pending orders and waits up to 60 seconds for 
        confirmation from the brokerage. If any orders remain pending after 
        the timeout, an exception is raised to prevent conflicting orders 
        during rebalancing.
        
        Returns:
            List[Dict]: Details of orders that were cancelled
            
        Raises:
            Exception: If orders cannot be cancelled within 60 seconds
        """
        async with self._order_lock:
            if not await self.ensure_connected():
                raise Exception("Unable to establish IBKR connection")
            
            try:
                open_orders = self.ib.openOrders()
                cancelled_orders = []
                
                for order in open_orders:
                    if order.account == account_id:
                            
                        # Get contract symbol
                        symbol = 'Unknown'
                        if hasattr(order, 'contract') and order.contract:
                            symbol = getattr(order.contract, 'symbol', 'Unknown')
                        
                        order_details = {
                            'order_id': str(order.orderId),
                            'symbol': symbol,
                            'quantity': abs(order.totalQuantity),
                            'action': order.action,
                            'order_type': order.orderType,
                            'status': 'OpenOrder'
                        }
                        cancelled_orders.append(order_details)
                        
                        self.ib.cancelOrder(order)
                        app_logger.log_debug(f"Cancelled order {order.orderId} for {account_id}: {order.action} {abs(order.totalQuantity)} {symbol}", event)
                
                if cancelled_orders:
                    # Wait for all cancellations to be confirmed
                    await self._wait_for_orders_cancelled(account_id, max_wait_seconds=60)
                
                app_logger.log_info(f"Cancelled {len(cancelled_orders)} pending orders for account {account_id}", event)
                return cancelled_orders
                
            except Exception as e:
                app_logger.log_error(f"Failed to cancel orders for account {account_id}: {e}", event)
                raise
    
    async def _wait_for_orders_cancelled(self, account_id: str, max_wait_seconds: int = 60):
        """Wait for all pending orders to be cancelled for the account"""
        start_time = asyncio.get_event_loop().time()
        
        while True:
            trades = self.ib.trades()
            pending_orders = [
                trade for trade in trades 
                if (trade.order.account == account_id and 
                    trade.orderStatus.status in ['PreSubmitted', 'Submitted', 'PendingSubmit'])
            ]
            
            if not pending_orders:
                app_logger.log_debug(f"All orders successfully cancelled for account {account_id}")
                return
            
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= max_wait_seconds:
                pending_ids = [trade.order.orderId for trade in pending_orders]
                error_msg = f"Timeout waiting for order cancellations for account {account_id}. Still pending: {pending_ids}"
                app_logger.log_error(error_msg)
                raise Exception(error_msg)
            
            await asyncio.sleep(10)  # Check every 10 seconds
    
    
    async def ensure_connected(self) -> bool:
        async with self._connection_lock:
            if not self.ib.isConnected():
                return await self.connect()
            
            # Active validation with timeout to detect stale connections
            try:
                await asyncio.wait_for(
                    self.ib.reqCurrentTimeAsync(), 
                    timeout=5.0
                )
                return True
            except (asyncio.TimeoutError, Exception) as e:
                app_logger.log_debug(f"Reconnecting to IBKR gateway...")
                return await self.connect()
    
    async def get_contract_details(self, symbols: List[str], event=None) -> Dict[str, ContractDetailsData]:
        """
        Get contract details for multiple symbols including trading hours information
        
        Returns:
            Dict mapping symbol to contract details containing tradingHours, liquidHours, timeZone
        """
        if not await self.ensure_connected():
            raise Exception("Unable to establish IBKR connection")
        
        if not symbols:
            return {}
        
        try:
            contracts = [Stock(symbol, 'SMART', 'USD') for symbol in symbols]
            contract_details = {}
            
            for contract in contracts:
                try:
                    # Get contract details
                    details_list = await self.ib.reqContractDetailsAsync(contract)
                    
                    if details_list:
                        # Take the first matching contract details
                        details = details_list[0]
                        
                        contract_details[contract.symbol] = ContractDetailsData(
                            trading_hours=details.tradingHours,
                            liquid_hours=details.liquidHours,
                            time_zone=details.timeZoneId,
                            contract_details=details
                        )
                        
                        app_logger.log_debug(f"Got contract details for {contract.symbol}: timeZone={details.timeZoneId}", event)
                    else:
                        app_logger.log_warning(f"No contract details found for {contract.symbol}", event)
                        
                except Exception as e:
                    app_logger.log_error(f"Failed to get contract details for {contract.symbol}: {e}", event)
                    # Continue with other symbols
                    
            return contract_details
            
        except Exception as e:
            app_logger.log_error(f"Failed to get contract details: {e}", event)
            raise
    
    
    async def check_trading_hours(self, symbols: List[str], event=None) -> TradingHoursResult:
        """
        Check if all symbols are currently within their trading hours.
        
        Args:
            symbols: List of symbols to check
            event: Event context for logging
            
        Returns:
            Tuple of (all_within_hours, earliest_next_start, symbol_status_dict)
        """
        if not symbols:
            return TradingHoursResult(
                all_within_hours=True,
                next_start_time=None,
                symbol_status={}
            )
        
        try:
            # Get contract details for all symbols
            contract_details = await self.get_contract_details(symbols, event)
            
            # Get current time with timezone awareness for comparison with ib_async sessions
            current_time = datetime.now(ZoneInfo('America/New_York'))
            all_within_hours = True
            earliest_next_start = None
            symbol_status = {}
            
            for symbol in symbols:
                details = contract_details.get(symbol)
                if not details:
                    app_logger.log_error(f"No contract details available for {symbol}", event)
                    symbol_status[symbol] = False
                    all_within_hours = False
                    continue
                
                # Get the ContractDetails object
                contract_detail_obj = details.contract_details
                if not contract_detail_obj:
                    app_logger.log_warning(f"No contract details object for {symbol}", event)
                    symbol_status[symbol] = False
                    all_within_hours = False
                    continue
                
                # Always use liquid hours only (regular market hours)
                sessions = contract_detail_obj.liquidSessions()
                
                # Check if current time is within any session
                is_within = False
                next_start = None
                
                for session in sessions:
                    # ib_async returns sessions with timezone-aware datetimes
                    # Convert current_time to be timezone-aware if needed
                    if session.start <= current_time <= session.end:
                        is_within = True
                        break
                    elif session.start > current_time:
                        # This is a future session
                        if next_start is None or session.start < next_start:
                            next_start = session.start
                
                symbol_status[symbol] = is_within
                
                if not is_within:
                    all_within_hours = False
                    app_logger.log_info(f"Symbol {symbol} is outside trading hours", event)
                    
                    if next_start and (earliest_next_start is None or next_start < earliest_next_start):
                        earliest_next_start = next_start
                else:
                    app_logger.log_debug(f"Symbol {symbol} is within trading hours", event)
            
            if all_within_hours:
                app_logger.log_info(f"All symbols are within liquid hours", event)
            else:
                next_str = earliest_next_start.strftime("%Y-%m-%d %H:%M:%S") if earliest_next_start else "unknown"
                app_logger.log_info(f"Some symbols outside {hours_type}, earliest next start: {next_str}", event)
            
            return TradingHoursResult(
                all_within_hours=all_within_hours,
                next_start_time=earliest_next_start,
                symbol_status=symbol_status
            )
            
        except Exception as e:
            app_logger.log_error(f"Failed to check trading hours: {e}", event)
            raise
    
    
