"""Simplified rebalancer without account locking"""

from typing import List, Optional
from datetime import datetime
import logging
from app.models import AccountConfig, AccountSnapshot, AllocationItem, Trade, RebalanceResult, CalculateRebalanceResult

class Rebalancer:
    """Simplified rebalancer without account locking"""

    def __init__(self, ibkr_client, logger: Optional[logging.Logger] = None):
        self.ibkr = ibkr_client
        self.logger = logger or logging.getLogger(__name__)

    async def rebalance_account(self, account: AccountConfig) -> RebalanceResult:
        """Execute rebalancing for account"""
        from .allocation_service import AllocationService
        from .replacement_service import ReplacementService
        from .trade_calculator import TradeCalculator

        account_id = account.account_id
        self.logger.info(f"Starting rebalance for account {account_id}")

        try:
            # Get target allocations
            allocation_service = AllocationService(logger=self.logger)
            allocations = await allocation_service.get_allocations(account)

            if account.replacement_set:
                replacement_service = ReplacementService(logger=self.logger)
                allocations = replacement_service.apply_replacements_with_scaling(
                    allocations=allocations,
                    replacement_set_name=account.replacement_set
                )

            self._log_target_allocations(allocations)

            snapshot = await self.ibkr.get_account_snapshot(account_id)

            self._log_account_snapshot("INITIAL", snapshot)

            all_symbols = list(set([a.symbol for a in allocations] +
                                  [p.symbol for p in snapshot.positions]))
            market_prices = await self.ibkr.get_multiple_market_prices(all_symbols)

            # Calculate required trades
            calculator = TradeCalculator(logger=self.logger)
            result = calculator.calculate_trades(
                snapshot=snapshot,
                allocations=allocations,
                market_prices=market_prices,
                account_config=account
            )
            trades = result.trades
            warnings = result.warnings

            # Cancel any pending orders first
            await self._cancel_pending_orders(account_id)

            # Log all planned orders
            self._log_planned_orders(trades)

            sell_orders = [t for t in trades if t.quantity < 0]
            if sell_orders:
                self.logger.info(f"Executing {len(sell_orders)} sell orders")
                for trade in sell_orders:
                    order_result = await self.ibkr.place_order(
                        account_id=account_id,
                        symbol=trade.symbol,
                        quantity=trade.quantity,
                        order_type=trade.order_type,
                        price=trade.price
                    )
                    trade.order_id = order_result.order_id

                await self._wait_for_orders_complete(sell_orders)

            snapshot = await self.ibkr.get_account_snapshot(account_id)
            self.logger.info(f"Cash balance after sells: ${snapshot.cash_balance:,.2f}")

            buy_result = calculator.calculate_trades(
                snapshot=snapshot,
                allocations=allocations,
                market_prices=market_prices,
                account_config=account,
                phase='buy'
            )
            warnings.extend(buy_result.warnings)

            buy_orders = [t for t in buy_result.trades if t.quantity > 0]
            if buy_orders:
                # Calculate available cash for buys (same logic as trade_calculator)
                reserved_amount = min(100, snapshot.cash_balance * 0.05) if snapshot.cash_balance < 2000 else 100
                available_cash = max(0, (snapshot.cash_balance - reserved_amount) / 1.01)

                self.logger.info(f"Executing {len(buy_orders)} buy orders with ${available_cash:.2f} available cash")

                # Build position map for quick lookup
                position_map = {pos.symbol: pos for pos in snapshot.positions}

                orders_to_execute = []
                for trade in buy_orders:
                    estimated_cost = trade.quantity * trade.price

                    if estimated_cost > available_cash:
                        # Check if symbol already exists in portfolio
                        current_position = position_map.get(trade.symbol)
                        if current_position and current_position.quantity > 0:
                            # Symbol exists - skip this buy due to insufficient cash
                            # This is normal operation, not a warning-worthy event
                            self.logger.info(
                                f"Skipped buy of {trade.symbol}: Insufficient cash "
                                f"(${available_cash:.2f} available, ${estimated_cost:.2f} needed). "
                                f"Symbol already held at {current_position.quantity} shares."
                            )
                            continue
                        else:
                            # Symbol missing - this is critical, must fail
                            error_msg = (
                                f"Cannot buy required symbol {trade.symbol}: Insufficient cash "
                                f"(${available_cash:.2f} available, ${estimated_cost:.2f} needed). "
                                f"All target symbols must be present in portfolio."
                            )
                            self.logger.error(error_msg)
                            raise ValueError(error_msg)

                    orders_to_execute.append(trade)
                    available_cash -= estimated_cost  # Track remaining cash for subsequent orders

                # Execute orders that passed the cash check
                for trade in orders_to_execute:
                    order_result = await self.ibkr.place_order(
                        account_id=account_id,
                        symbol=trade.symbol,
                        quantity=trade.quantity,
                        order_type=trade.order_type,
                        price=trade.price
                    )
                    trade.order_id = order_result.order_id

                if orders_to_execute:
                    await self._wait_for_orders_complete(orders_to_execute)

            # Get final snapshot using cached prices to avoid rate limiting
            final_snapshot = await self.ibkr.get_account_snapshot(account_id, use_cached_prices=True)
            self._log_account_snapshot("FINAL", final_snapshot)

            self.logger.info(f"Rebalance completed successfully for account {account_id}")
            # Combine executed orders (some buy orders may have been skipped due to cash constraints)
            executed_orders = sell_orders + (orders_to_execute if buy_orders else [])
            return RebalanceResult(
                orders=executed_orders,
                total_value=final_snapshot.total_value,
                cash_balance=final_snapshot.cash_balance,
                success=True,
                warnings=warnings
            )

        except Exception as e:
            self.logger.error(f"Rebalance failed for account {account_id}: {str(e)}")
            return RebalanceResult(
                orders=[],
                total_value=0,
                success=False,
                error=str(e)
            )

    async def calculate_rebalance(self, account: AccountConfig) -> CalculateRebalanceResult:
        """Calculate rebalance without executing (print-rebalance)"""
        from .allocation_service import AllocationService
        from .replacement_service import ReplacementService
        from .trade_calculator import TradeCalculator

        account_id = account.account_id
        self.logger.info(f"Calculating rebalance for account {account_id}")

        allocation_service = AllocationService(logger=self.logger)
        allocations = await allocation_service.get_allocations(account)

        if account.replacement_set:
            replacement_service = ReplacementService(logger=self.logger)
            allocations = replacement_service.apply_replacements_with_scaling(
                allocations=allocations,
                replacement_set_name=account.replacement_set
            )

        self._log_target_allocations(allocations)

        snapshot = await self.ibkr.get_account_snapshot(account_id)

        self._log_account_snapshot("CURRENT", snapshot)

        all_symbols = list(set([a.symbol for a in allocations] +
                              [p.symbol for p in snapshot.positions]))
        market_prices = await self.ibkr.get_multiple_market_prices(all_symbols)

        calculator = TradeCalculator(logger=self.logger)
        result = calculator.calculate_trades(
            snapshot=snapshot,
            allocations=allocations,
            market_prices=market_prices,
            account_config=account
        )

        self._log_planned_orders(result.trades, is_preview=True)

        return CalculateRebalanceResult(
            proposed_trades=result.trades,
            current_value=snapshot.total_value,
            success=True,
            warnings=result.warnings
        )

    async def _cancel_pending_orders(self, account_id: str):
        """Cancel any pending orders for the account"""
        try:
            open_orders = await self.ibkr.get_open_orders(account_id)
            for order in open_orders:
                self.logger.info(f"Cancelling order {order.order_id}")
                await self.ibkr.cancel_order(order.order_id)
        except Exception as e:
            self.logger.warning(f"Error cancelling pending orders: {e}")

    async def _wait_for_orders_complete(self, orders: List[Trade], timeout: int = 300):
        """Wait for orders to complete or fail"""
        import asyncio

        if not orders:
            return

        # TWS API terminal states (DoneStates)
        TERMINAL_STATES = ['FILLED', 'CANCELLED', 'APICANCELLED', 'INACTIVE']
        FAILED_STATES = ['CANCELLED', 'APICANCELLED', 'INACTIVE']

        self.logger.info(f"Waiting for {len(orders)} orders to complete")
        start_time = datetime.now()
        failed_orders = []

        while (datetime.now() - start_time).total_seconds() < timeout:
            all_complete = True
            failed_orders = []

            for order in orders:
                status = await self.ibkr.get_order_status(order.order_id)
                self.logger.debug(f"Order {order.order_id} ({order.symbol} x{order.quantity}) status: '{status}'")

                if status and status.upper() not in TERMINAL_STATES:
                    all_complete = False
                elif status and status.upper() in FAILED_STATES:
                    failed_orders.append(order)

            if all_complete:
                if failed_orders:
                    failed_details = [f"{o.symbol} x{o.quantity}" for o in failed_orders]
                    error_msg = f"Orders failed: {', '.join(failed_details)}"
                    self.logger.error(error_msg)
                    raise Exception(error_msg)
                else:
                    self.logger.info("All orders completed successfully")
                    await asyncio.sleep(1)
                    return

            await asyncio.sleep(2)

        self.logger.error(f"CRITICAL: Orders timed out after {timeout} seconds")
        raise Exception(f"Order execution timeout after {timeout} seconds")

    def _log_account_snapshot(self, stage: str, snapshot: AccountSnapshot):
        """Log detailed account snapshot"""
        account_id = snapshot.account_id
        total_value = snapshot.total_value
        positions = snapshot.positions

        self.logger.info(f"====== {stage} ACCOUNT SNAPSHOT ======")
        self.logger.info(f"Account ID: {account_id}")
        self.logger.info(f"Total Account Value: ${total_value:,.2f}")

        if positions:
            self.logger.info(f"Positions ({len(positions)}):")
            sorted_positions = sorted(positions, key=lambda x: x.symbol)
            for pos in sorted_positions:
                symbol = pos.symbol
                quantity = pos.quantity
                market_price = pos.market_price
                market_value = pos.market_value
                percent_of_account = (market_value / total_value * 100) if total_value > 0 else 0

                self.logger.info(f"  {symbol}: {quantity:,} shares @ ${market_price:.2f} "
                               f"= ${market_value:,.2f} ({percent_of_account:.2f}%)")
        else:
            self.logger.info("No positions held")

        cash_balance = snapshot.cash_balance
        settled_cash = snapshot.settled_cash
        self.logger.info(f"Cash Balance: ${cash_balance:,.2f}")
        self.logger.info(f"Settled Cash: ${settled_cash:,.2f}")
        self.logger.info("=" * 40)

    def _log_target_allocations(self, allocations: List[AllocationItem]):
        """Log target allocation percentages"""
        self.logger.info(f"====== TARGET ALLOCATIONS ({len(allocations)}) ======")
        total_allocation = sum(alloc.allocation for alloc in allocations)

        sorted_allocations = sorted(allocations, key=lambda x: x.symbol)
        for alloc in sorted_allocations:
            symbol = alloc.symbol
            percentage = alloc.allocation * 100  # Convert fraction to percentage for display
            self.logger.info(f"  {symbol}: {percentage:.2f}%")

        self.logger.info(f"Total Allocation: {total_allocation * 100:.2f}%")
        self.logger.info("=" * 35)

    def _log_planned_orders(self, trades: List[Trade], is_preview: bool = False):
        """Log planned orders/trades"""
        stage = "PROPOSED TRADES (PREVIEW)" if is_preview else "PLANNED ORDERS"
        self.logger.info(f"====== {stage} ======")

        if not trades:
            self.logger.info("No trades required - portfolio is already balanced")
            self.logger.info("=" * (len(stage) + 14))
            return

        sell_orders = [t for t in trades if t.quantity < 0]
        buy_orders = [t for t in trades if t.quantity > 0]

        total_sell_value = sum(abs(t.quantity * t.price) for t in sell_orders)
        total_buy_value = sum(t.quantity * t.price for t in buy_orders)

        self.logger.info(f"Total Orders: {len(trades)} ({len(sell_orders)} sells, {len(buy_orders)} buys)")
        self.logger.info(f"Total Sell Value: ${total_sell_value:,.2f}")
        self.logger.info(f"Total Buy Value: ${total_buy_value:,.2f}")
        self.logger.info("")

        if sell_orders:
            self.logger.info("SELL Orders:")
            for trade in sell_orders:
                symbol = trade.symbol
                quantity = abs(trade.quantity)
                price = trade.price
                current_shares = trade.current_shares
                trade_value = quantity * price
                self.logger.info(f"  SELL {quantity:,} shares of {symbol} @ ${price:.2f} "
                               f"= ${trade_value:,.2f} (from {current_shares:,} shares)")

        if buy_orders:
            self.logger.info("BUY Orders:")
            for trade in buy_orders:
                symbol = trade.symbol
                quantity = trade.quantity
                price = trade.price
                current_shares = trade.current_shares
                trade_value = quantity * price
                self.logger.info(f"  BUY {quantity:,} shares of {symbol} @ ${price:.2f} "
                               f"= ${trade_value:,.2f} (to {current_shares + quantity:,} shares)")

        self.logger.info("=" * (len(stage) + 14))