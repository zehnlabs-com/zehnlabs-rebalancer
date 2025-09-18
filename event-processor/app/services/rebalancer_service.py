import asyncio
from typing import List, Dict, Optional
from collections import defaultdict
from datetime import datetime
from app.config import config
from app.models.account_config import EventAccountConfig
from app.models.rebalance_data import TargetAllocation, AccountSnapshot, CurrentPosition, RebalanceOrder
from app.services.ibkr_client import IBKRClient
from app.services.allocation_service import AllocationService
from app.logger import AppLogger

app_logger = AppLogger(__name__)

class TradingHoursException(Exception):
    """Exception raised when symbols are outside trading hours"""
    
    def __init__(self, message: str, next_start_time: Optional[datetime] = None, symbol_status: Optional[Dict[str, bool]] = None):
        super().__init__(message)
        self.message = message
        self.next_start_time = next_start_time
        self.symbol_status = symbol_status or {}


class RebalanceResult:
    def __init__(self, orders, equity_info, cancelled_orders=None):
        self.orders = orders
        self.equity_info = equity_info
        self.cancelled_orders = cancelled_orders or []

class RebalancerService:
    # Class-level locks shared across all instances
    _account_locks = defaultdict(asyncio.Lock)
    
    def __init__(self, ibkr_client: IBKRClient):
        self.ibkr_client = ibkr_client
        self.allocation_service = AllocationService()
    
    async def rebalance_account(self, account_config: EventAccountConfig):
        # Log queue position
        waiting_accounts = [acc_id for acc_id, lock in self._account_locks.items() if lock.locked()]
        if waiting_accounts:
            app_logger.log_debug(f"Account {account_config.account_id} waiting for {len(waiting_accounts)} accounts: {waiting_accounts}")
        
        async with self._account_locks[account_config.account_id]:
            app_logger.log_debug(f"Account {account_config.account_id} acquired lock, starting rebalance")
            try:
                app_logger.log_info(f"Starting LIVE rebalance for account {account_config.account_id}")
                
                # Step 1: Get target allocations from API
                target_allocations = await self.allocation_service.get_allocations(account_config)
                
                # Step 2: Validate trading hours for all symbols before proceeding with any operations
                await self._validate_trading_hours(target_allocations, account_config)
                
                # Step 3: Cancel any pending orders before starting phases
                cancelled_orders = await self._cancel_pending_orders(account_config.account_id)
                
                # PHASE 1: SELL ORDERS
                app_logger.log_info(f"PHASE 1: Processing sell orders for account {account_config.account_id}")
                
                # Get fresh account snapshot for sell phase
                account_snapshot = await self.ibkr_client.get_account_snapshot(account_config.account_id)
                
                # Get fresh market prices for all symbols (including current positions)
                sell_symbols = list(set([allocation.symbol for allocation in target_allocations] + 
                                      [pos.symbol for pos in account_snapshot.positions]))
                market_prices = await self.ibkr_client.get_multiple_market_prices(sell_symbols)
                
                # Calculate sell orders based on original allocations (no replacements for sells)
                sell_orders = await self._calculate_phase_orders(
                    target_allocations=target_allocations,
                    current_positions=account_snapshot.positions,
                    account_value=account_snapshot.total_value,
                    account_config=account_config,
                    market_prices=market_prices,
                    phase='sell'                )
                
                # Execute sell orders and wait for completion
                await self._execute_sell_orders(account_config.account_id, sell_orders, dry_run=False)
                
                # PHASE 2: BUY ORDERS
                app_logger.log_info(f"PHASE 2: Processing buy orders for account {account_config.account_id}")
                
                # Get fresh account snapshot after sells complete
                account_snapshot = await self.ibkr_client.get_account_snapshot(account_config.account_id)
                
                # Apply ETF replacements for buy orders if configured
                buy_target_allocations = target_allocations
                if account_config.replacement_set:
                    from app.services.replacement_service import ReplacementService
                    replacement_service = ReplacementService()
                    app_logger.log_info(f"Applying replacement set '{account_config.replacement_set}' for buy orders")
                    buy_target_allocations = replacement_service.apply_replacements_with_scaling(
                        allocations=target_allocations,
                        replacement_set_name=account_config.replacement_set                    )
                
                # Get fresh market prices including replacement symbols
                buy_symbols = list(set([allocation.symbol for allocation in buy_target_allocations] + 
                                      [pos.symbol for pos in account_snapshot.positions]))
                market_prices = await self.ibkr_client.get_multiple_market_prices(buy_symbols)
                
                # Calculate buy orders with fresh data and replacements
                buy_orders = await self._calculate_phase_orders(
                    target_allocations=buy_target_allocations,
                    current_positions=account_snapshot.positions,
                    account_value=account_snapshot.total_value,
                    account_config=account_config,
                    market_prices=market_prices,
                    phase='buy'                )
                
                # Execute buy orders
                await self._execute_buy_orders(account_config.account_id, buy_orders, dry_run=False)
                
                app_logger.log_info(f"Completed LIVE rebalance for account {account_config.account_id}")
                
                # Create result with all orders (use final account snapshot for equity info)
                all_orders = sell_orders + buy_orders
                equity_info = {
                    'total_equity': account_snapshot.total_value,
                    'cash_reserve_percent': account_config.cash_reserve_percent,
                    'reserve_amount': account_snapshot.total_value * (account_config.cash_reserve_percent / 100.0),
                    'available_for_trading': account_snapshot.total_value * (1.0 - account_config.cash_reserve_percent / 100.0)
                }
                
                return RebalanceResult(all_orders, equity_info, cancelled_orders)
                
            except Exception as e:
                app_logger.log_error(f"Error in LIVE rebalance for account {account_config.account_id}: {e}")
                raise
    
    async def _calculate_phase_orders(
        self,
        target_allocations: List[TargetAllocation],
        current_positions: List[CurrentPosition],
        account_value: float,
        account_config: EventAccountConfig,
        market_prices: Dict[str, float],
        phase: str  # 'sell' or 'buy'
    ) -> List[RebalanceOrder]:
        """
        Calculate orders for a specific phase (sell or buy) based on fresh account data.
        Uses total account value as the base for all calculations.
        """
        # Calculate cash reserve
        cash_reserve_percent = account_config.cash_reserve_percent / 100.0
        investable_value = account_value * (1.0 - cash_reserve_percent)
        
        app_logger.log_info(f"Phase {phase.upper()} - Account value: ${account_value:.2f}, Investable: ${investable_value:.2f}")
        
        # Calculate target positions based on total account value
        target_positions = {}
        for allocation in target_allocations:
            symbol = allocation.symbol
            target_value = investable_value * allocation.allocation_percent
            if symbol in market_prices:
                target_shares = int(target_value / market_prices[symbol])
                target_positions[symbol] = target_shares
                app_logger.log_debug(f"Target for {symbol}: {target_shares} shares (${target_value:.2f})")
        
        # Build current positions map
        current_positions_map = {}
        for pos in current_positions:
            current_positions_map[pos.symbol] = pos.shares
        
        # Generate orders for this phase
        orders = []
        
        # Process symbols in target allocations
        for symbol, target_shares in target_positions.items():
            current_shares = current_positions_map.get(symbol, 0)
            shares_diff = target_shares - current_shares
            
            if shares_diff == 0:
                continue
            
            action = 'BUY' if shares_diff > 0 else 'SELL'
            
            # Filter by phase
            if phase == 'sell' and action != 'SELL':
                continue
            if phase == 'buy' and action != 'BUY':
                continue
            
            orders.append(RebalanceOrder(
                symbol=symbol,
                action=action,
                shares=abs(shares_diff),
                estimated_value=abs(shares_diff) * market_prices[symbol]
            ))
            app_logger.log_info(f"Order: {action} {abs(shares_diff)} shares of {symbol} (${abs(shares_diff) * market_prices[symbol]:.2f})")
        
        # Handle positions to liquidate (not in target allocations) - only in sell phase
        if phase == 'sell':
            for symbol, current_shares in current_positions_map.items():
                if symbol not in target_positions and current_shares > 0:
                    if symbol in market_prices:
                        orders.append(RebalanceOrder(
                            symbol=symbol,
                            action='SELL',
                            shares=current_shares,
                            estimated_value=current_shares * market_prices[symbol]
                        ))
                        app_logger.log_info(f"Liquidation: SELL {current_shares} shares of {symbol} (not in target)")
        
        app_logger.log_info(f"Generated {len(orders)} {phase} orders")
        return orders
    
    async def _validate_trading_hours(
        self,
        target_allocations: List[TargetAllocation],
        account_config: EventAccountConfig
    ) -> None:
        """
        Validate that all symbols (current positions + target allocations) are within trading hours.
        Raises TradingHoursException if any symbols are outside trading hours.
        """
        try:
            # Get account snapshot to include current positions in validation
            account_snapshot = await self.ibkr_client.get_account_snapshot(account_config.account_id)
            
            # Collect all symbols that will be involved in rebalancing
            target_symbols = {allocation.symbol for allocation in target_allocations}
            current_symbols = {pos.symbol for pos in account_snapshot.positions if pos.shares != 0}
            
            # Also include replacement symbols if replacement set is configured
            replacement_symbols = set()
            if account_config.replacement_set:
                from app.services.replacement_service import ReplacementService
                replacement_service = ReplacementService()
                buy_target_allocations = replacement_service.apply_replacements_with_scaling(
                    allocations=target_allocations,
                    replacement_set_name=account_config.replacement_set
                )
                replacement_symbols = {allocation.symbol for allocation in buy_target_allocations}
                app_logger.log_debug(f"Including {len(replacement_symbols)} replacement symbols in trading hours validation")
            
            all_symbols = list(target_symbols.union(current_symbols).union(replacement_symbols))
            
            app_logger.log_debug(f"Validating trading hours for {len(all_symbols)} symbols: {all_symbols}")
            
            # Check trading hours for all symbols
            trading_hours_result = await self.ibkr_client.check_trading_hours(all_symbols)
            all_within_hours = trading_hours_result.all_within_hours
            next_start_time = trading_hours_result.next_start_time
            symbol_status = trading_hours_result.symbol_status
            
            if not all_within_hours:
                # Some symbols are outside trading hours
                outside_symbols = [symbol for symbol, is_open in symbol_status.items() if not is_open]
                app_logger.log_info(f"Symbols outside trading hours: {outside_symbols}")
                
                raise TradingHoursException(
                    message=f"One or more symbols are outside trading hours: {', '.join(outside_symbols)}",
                    next_start_time=next_start_time,
                    symbol_status=symbol_status
                )
            
            app_logger.log_debug(f"All {len(all_symbols)} symbols are within trading hours")
            
        except TradingHoursException:
            # Re-raise TradingHoursException as-is
            raise
        except Exception as e:
            app_logger.log_error(f"Failed to validate trading hours: {e}")
            raise TradingHoursException(
                message=f"Failed to validate trading hours",
                next_start_time=None,
                symbol_status={}
            )

    
    
    
    async def _cancel_pending_orders(self, account_id: str):
        """Cancel all pending orders for the account before rebalancing"""
        try:
            cancelled_orders = await self.ibkr_client.cancel_all_orders(account_id)
            if cancelled_orders:
                app_logger.log_info(f"Cancelled {len(cancelled_orders)} pending orders for account {account_id}")
            return cancelled_orders
        except Exception as e:
            app_logger.log_error(f"Failed to cancel pending orders for account {account_id}: {e}")
            raise

    async def _execute_sell_orders(self, account_id: str, orders: List[RebalanceOrder], dry_run: bool = False):
        """Execute sell orders with concurrent placement and concurrent waiting - fail fast on any rejection"""
        sell_orders = [order for order in orders if order.action == 'SELL']
        
        if not sell_orders:
            app_logger.log_info("No sell orders to execute")
            return
        
        mode_text = "DRY RUN" if dry_run else "LIVE"
        app_logger.log_info(f"{mode_text} - Executing {len(sell_orders)} sell orders for account {account_id}")
        
        if dry_run:
            return
        
        # Place ALL sell orders concurrently (like simple algorithm)
        sell_tasks = []
        for order in sell_orders:
            quantity = -order.shares  # Negative for sell
            
            trade = await self.ibkr_client.place_order(
                account_id=account_id,
                symbol=order.symbol,
                quantity=quantity,
                order_type="MKT",
                time_in_force="DAY"
            )
            
            sell_tasks.append(trade)
            app_logger.log_info(f"SELL order placed: {order} - Order ID: {trade.order.orderId}")
        
        # Wait for ALL sells to complete concurrently - any failure will fail immediately
        try:
            await asyncio.gather(*[self._wait_for_order_completion(trade) for trade in sell_tasks])
            app_logger.log_info("All SELL orders executed successfully")
        except Exception as e:
            # Any single order failure will cause this exception immediately
            app_logger.log_error(f"SELL order execution failed: {e}")
            raise
    
    async def _execute_buy_orders(self, account_id: str, orders: List[RebalanceOrder], dry_run: bool = False):
        """Execute buy orders with concurrent placement and concurrent waiting - fail fast on any rejection"""
        buy_orders = [order for order in orders if order.action == 'BUY']
        
        if not buy_orders:
            app_logger.log_info("No buy orders to execute")
            return
        
        mode_text = "DRY RUN" if dry_run else "LIVE"
        app_logger.log_info(f"{mode_text} - Executing {len(buy_orders)} buy orders for account {account_id}")
        
        if dry_run:
            return
        
        # Place ALL buy orders concurrently (like simple algorithm)
        buy_tasks = []
        for order in buy_orders:
            trade = await self.ibkr_client.place_order(
                account_id=account_id,
                symbol=order.symbol,
                quantity=order.shares,
                order_type="MKT",
                time_in_force="DAY"
            )
            
            buy_tasks.append(trade)
            app_logger.log_info(f"BUY order placed: {order} - Order ID: {trade.order.orderId}")
        
        # Wait for ALL buys to complete concurrently - any failure will fail immediately
        try:
            await asyncio.gather(*[self._wait_for_order_completion(trade) for trade in buy_tasks])
            app_logger.log_info("All BUY orders executed successfully")
        except Exception as e:
            # Any single order failure will cause this exception immediately
            app_logger.log_error(f"BUY order execution failed: {e}")
            raise
    
    async def _wait_for_order_completion(self, trade):
        """Wait for order to complete and fail immediately if not filled"""
        if trade.isDone():
            # Check status immediately for already completed orders
            if trade.orderStatus.status != 'Filled':
                error_message = await self.ibkr_client.get_order_failure_message(trade)
                raise Exception(error_message)
            return
        
        # Poll for completion instead of relying on events
        timeout = config.ibkr.order_completion_timeout
        start_time = asyncio.get_event_loop().time()
        
        while True:
            # Check if order is done
            if trade.isDone():
                if trade.orderStatus.status != 'Filled':
                    error_message = await self.ibkr_client.get_order_failure_message(trade)
                    raise Exception(error_message)
                return
            
            # Check timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                app_logger.log_error(f"Order {trade.order.orderId} timed out after {timeout}s - Status: {trade.orderStatus.status}")
                raise Exception(f"Order {trade.order.orderId} timed out after {timeout}s")
            
            # Wait a short time before checking again
            await asyncio.sleep(0.1)
    
    async def dry_run_rebalance(self, account_config: EventAccountConfig) -> RebalanceResult:
        # Log queue position
        waiting_accounts = [acc_id for acc_id, lock in self._account_locks.items() if lock.locked()]
        if waiting_accounts:
            app_logger.log_debug(f"Account {account_config.account_id} waiting for {len(waiting_accounts)} accounts: {waiting_accounts}")
        
        async with self._account_locks[account_config.account_id]:
            app_logger.log_debug(f"Account {account_config.account_id} acquired lock, starting dry run rebalance")
            try:
                app_logger.log_info(f"Starting DRY RUN rebalance for account {account_config.account_id}")
                
                # Step 1: Get target allocations from API
                target_allocations = await self.allocation_service.get_allocations(account_config)
                
                # PHASE 1: SELL ORDERS (simulation)
                app_logger.log_info(f"DRY RUN PHASE 1: Simulating sell orders for account {account_config.account_id}")
                
                # Get account snapshot for sell phase
                account_snapshot = await self.ibkr_client.get_account_snapshot(account_config.account_id)
                
                # Get market prices for all symbols
                sell_symbols = list(set([allocation.symbol for allocation in target_allocations] + 
                                      [pos.symbol for pos in account_snapshot.positions]))
                market_prices = await self.ibkr_client.get_multiple_market_prices(sell_symbols)
                
                # Calculate sell orders
                sell_orders = await self._calculate_phase_orders(
                    target_allocations=target_allocations,
                    current_positions=account_snapshot.positions,
                    account_value=account_snapshot.total_value,
                    account_config=account_config,
                    market_prices=market_prices,
                    phase='sell'                )
                
                # Simulate sell orders
                await self._execute_sell_orders(account_config.account_id, sell_orders, dry_run=True)
                
                # PHASE 2: BUY ORDERS (simulation)
                app_logger.log_info(f"DRY RUN PHASE 2: Simulating buy orders for account {account_config.account_id}")
                
                # Note: In dry run, we use the same snapshot since we didn't actually execute sells
                # In production, this would be a fresh snapshot after sells complete
                
                # Apply ETF replacements for buy orders if configured
                buy_target_allocations = target_allocations
                if account_config.replacement_set:
                    from app.services.replacement_service import ReplacementService
                    replacement_service = ReplacementService()
                    app_logger.log_info(f"Applying replacement set '{account_config.replacement_set}' for buy orders")
                    buy_target_allocations = replacement_service.apply_replacements_with_scaling(
                        allocations=target_allocations,
                        replacement_set_name=account_config.replacement_set                    )
                
                # Get market prices including replacement symbols
                buy_symbols = list(set([allocation.symbol for allocation in buy_target_allocations] + 
                                      [pos.symbol for pos in account_snapshot.positions]))
                market_prices = await self.ibkr_client.get_multiple_market_prices(buy_symbols)
                
                # Simulate positions after sells (remove sold positions)
                simulated_positions = []
                current_pos_map = {pos.symbol: pos for pos in account_snapshot.positions}
                for order in sell_orders:
                    if order.symbol in current_pos_map:
                        pos = current_pos_map[order.symbol]
                        remaining_shares = pos.shares - order.shares
                        if remaining_shares > 0:
                            simulated_positions.append(CurrentPosition(
                                symbol=order.symbol,
                                shares=remaining_shares,
                                market_value=remaining_shares * market_prices[order.symbol],
                                average_cost=pos.average_cost
                            ))
                # Add positions that weren't sold
                for symbol, pos in current_pos_map.items():
                    if not any(order.symbol == symbol for order in sell_orders):
                        simulated_positions.append(pos)
                
                # Calculate buy orders with simulated positions
                buy_orders = await self._calculate_phase_orders(
                    target_allocations=buy_target_allocations,
                    current_positions=simulated_positions,
                    account_value=account_snapshot.total_value,
                    account_config=account_config,
                    market_prices=market_prices,
                    phase='buy'                )
                
                # Simulate buy orders
                await self._execute_buy_orders(account_config.account_id, buy_orders, dry_run=True)
                
                app_logger.log_info(f"Completed DRY RUN rebalance for account {account_config.account_id}")
                
                # Create result with all orders
                all_orders = sell_orders + buy_orders
                equity_info = {
                    'total_equity': account_snapshot.total_value,
                    'cash_reserve_percent': account_config.cash_reserve_percent,
                    'reserve_amount': account_snapshot.total_value * (account_config.cash_reserve_percent / 100.0),
                    'available_for_trading': account_snapshot.total_value * (1.0 - account_config.cash_reserve_percent / 100.0)
                }
                
                return RebalanceResult(all_orders, equity_info, [])
                
            except Exception as e:
                app_logger.log_error(f"Error in dry run rebalance for account {account_config.account_id}: {e}")
                raise