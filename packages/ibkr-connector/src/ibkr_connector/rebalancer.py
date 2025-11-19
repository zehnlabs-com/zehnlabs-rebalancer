"""Simplified rebalancer without account locking"""

from typing import List, Optional
from datetime import datetime
import logging

try:
    from broker_connector_base import (
        BaseRebalancer,
        AccountSnapshot,
        RebalanceResult,
        CalculateRebalanceResult,
        Trade,
        AllocationItem,
        AccountConfig,
    )
    from app_config import get_config
    from rebalance_calculator import TradeCalculator
    from .allocation_service import AllocationService
    from .replacement_service import ReplacementService
except ImportError as e:
    raise ImportError(
        f"Failed to import required packages: {e}. "
        "Ensure packages are installed."
    )


class IBKRRebalancer(BaseRebalancer):
    """Simplified rebalancer without account locking"""

    def __init__(self, broker_client, logger: Optional[logging.Logger] = None):
        self.config = get_config()
        super().__init__(broker_client, logger)
        self.ibkr = broker_client  # Keep self.ibkr for compatibility

    async def rebalance_account(self, account: AccountConfig) -> RebalanceResult:
        """Execute rebalancing for account"""
        # Imports already at top of file

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

            snapshot = await self.ibkr.get_account_snapshot(account_id, use_cached_prices=True)
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
                # Calculate available cash for buys (unified with trade_calculator formula)
                min_reserve = self.config.trading.minimum_cash_reserve_usd
                commission_divisor = self.config.trading.commission_divisor

                if snapshot.cash_balance < min_reserve:
                    available_cash = 0
                else:
                    available_cash = (snapshot.cash_balance - min_reserve) / commission_divisor

                self.logger.info(f"Executing {len(buy_orders)} buy orders with ${available_cash:.2f} available cash")

                # Build position map and allocation map for tracking skipped symbols
                position_map = {pos.symbol: pos for pos in snapshot.positions}
                allocation_map = {alloc.symbol: alloc.allocation for alloc in allocations}

                # Track execution and skips
                orders_to_execute = []
                skipped_insufficient_cash = []

                for trade in buy_orders:
                    estimated_cost = trade.quantity * trade.price

                    if estimated_cost > available_cash:
                        # Cannot afford this buy - skip it
                        current_position = position_map.get(trade.symbol)
                        is_missing = not (current_position and current_position.quantity > 0)
                        allocation_pct = allocation_map.get(trade.symbol, 0) * 100

                        skipped_insufficient_cash.append({
                            'trade': trade,
                            'shortfall': estimated_cost - available_cash,
                            'is_missing': is_missing,
                            'allocation_pct': allocation_pct
                        })

                        self.logger.info(
                            f"Skipped buy of {trade.symbol} ({allocation_pct:.2f}% allocation): "
                            f"Insufficient cash (${available_cash:.2f} available, ${estimated_cost:.2f} needed). "
                            f"{'Missing from portfolio' if is_missing else 'Already held'}."
                        )
                        continue

                    # Can afford - add to execution list
                    orders_to_execute.append(trade)
                    available_cash -= estimated_cost  # Track remaining cash for subsequent orders

                # Generate warnings for skipped items
                skipped_missing = [s for s in skipped_insufficient_cash if s['is_missing']]
                skipped_existing = [s for s in skipped_insufficient_cash if not s['is_missing']]

                if skipped_missing:
                    for skip in skipped_missing:
                        t = skip['trade']
                        warning_msg = (
                            f"Missing symbol {t.symbol} ({skip['allocation_pct']:.2f}% target allocation) "
                            f"could not be purchased. Shortfall: ${skip['shortfall']:.2f}"
                        )
                        warnings.append(warning_msg)
                        self.logger.warning(warning_msg)

                if skipped_existing:
                    # Less urgent - just info
                    symbols_info = [f"{s['trade'].symbol} ({s['allocation_pct']:.2f}%)" for s in skipped_existing]
                    info_msg = f"Portfolio optimization incomplete for: {', '.join(symbols_info)}"
                    warnings.append(info_msg)
                    self.logger.info(info_msg)

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
        # Imports already at top of file

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

    async def _wait_for_orders_complete(self, orders: List[Trade], timeout: Optional[int] = None):
        """Wait for orders to complete or fail"""
        import asyncio

        if not orders:
            return

        if timeout is None:
            timeout = self.config.trading.order_timeout_seconds

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
                    await asyncio.sleep(self.config.trading.post_completion_delay_seconds)
                    return

            await asyncio.sleep(self.config.trading.order_status_check_interval_seconds)

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
