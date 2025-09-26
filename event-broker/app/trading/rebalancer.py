"""Simplified rebalancer without account locking"""

from typing import List, Dict, Optional
from datetime import datetime
import logging

class Rebalancer:
    """Simplified rebalancer without account locking"""

    def __init__(self, ibkr_client, logger: Optional[logging.Logger] = None):
        self.ibkr = ibkr_client
        self.logger = logger or logging.getLogger(__name__)

    async def rebalance_account(self, account: dict):
        """Execute rebalancing for account"""
        from .allocation_service import AllocationService
        from .replacement_service import ReplacementService
        from .trade_calculator import TradeCalculator

        account_id = account['account_id']
        self.logger.info(f"Starting rebalance for account {account_id}")

        # Get target allocations
        allocation_service = AllocationService(logger=self.logger)
        allocations = await allocation_service.get_allocations(account)

        # Apply replacements if needed
        if account.get('replacement_set'):
            replacement_service = ReplacementService(logger=self.logger)
            allocations = replacement_service.apply_replacements_with_scaling(
                allocations=allocations,
                replacement_set_name=account['replacement_set']
            )

        # Log target allocations
        self._log_target_allocations(allocations)

        # Get current positions
        snapshot = await self.ibkr.get_account_snapshot(account_id)

        # Log initial account snapshot
        self._log_account_snapshot("INITIAL", snapshot)

        # Get market prices for all symbols
        all_symbols = list(set([a['symbol'] for a in allocations] +
                              [p['symbol'] for p in snapshot['positions']]))
        market_prices = await self.ibkr.get_multiple_market_prices(all_symbols)

        # Calculate required trades
        calculator = TradeCalculator(logger=self.logger)
        trades = calculator.calculate_trades(
            snapshot=snapshot,
            allocations=allocations,
            market_prices=market_prices,
            account_config=account
        )

        # Cancel any pending orders first
        await self._cancel_pending_orders(account_id)

        # Log all planned orders
        self._log_planned_orders(trades)

        # Execute sell orders first
        sell_orders = [t for t in trades if t['quantity'] < 0]
        if sell_orders:
            self.logger.info(f"Executing {len(sell_orders)} sell orders")
            for trade in sell_orders:
                order_result = await self.ibkr.place_order(
                    account_id=account_id,
                    symbol=trade['symbol'],
                    quantity=trade['quantity'],
                    order_type=trade.get('order_type', 'MARKET')
                )
                # Store the order ID in the trade object for tracking
                trade['order_id'] = order_result['order_id']

            # Wait for sells to complete
            await self._wait_for_orders_complete(sell_orders)

        # Get updated snapshot after sells
        snapshot = await self.ibkr.get_account_snapshot(account_id)

        # Recalculate buy orders with updated cash
        trades = calculator.calculate_trades(
            snapshot=snapshot,
            allocations=allocations,
            market_prices=market_prices,
            account_config=account,
            phase='buy'
        )

        # Execute buy orders
        buy_orders = [t for t in trades if t['quantity'] > 0]
        if buy_orders:
            self.logger.info(f"Executing {len(buy_orders)} buy orders")
            for trade in buy_orders:
                order_result = await self.ibkr.place_order(
                    account_id=account_id,
                    symbol=trade['symbol'],
                    quantity=trade['quantity'],
                    order_type=trade.get('order_type', 'MARKET')
                )
                # Store the order ID in the trade object for tracking
                trade['order_id'] = order_result['order_id']

            # Wait for buys to complete
            await self._wait_for_orders_complete(buy_orders)

        # Get final account snapshot after all trades
        final_snapshot = await self.ibkr.get_account_snapshot(account_id)
        self._log_account_snapshot("FINAL", final_snapshot)

        return {
            'orders': sell_orders + buy_orders,
            'total_value': final_snapshot['total_value'],
            'success': True
        }

    async def calculate_rebalance(self, account: dict):
        """Calculate rebalance without executing (print-rebalance)"""
        from .allocation_service import AllocationService
        from .replacement_service import ReplacementService
        from .trade_calculator import TradeCalculator

        account_id = account['account_id']
        self.logger.info(f"Calculating rebalance for account {account_id}")

        # Get target allocations
        allocation_service = AllocationService(logger=self.logger)
        allocations = await allocation_service.get_allocations(account)

        # Apply replacements if needed
        if account.get('replacement_set'):
            replacement_service = ReplacementService(logger=self.logger)
            allocations = replacement_service.apply_replacements_with_scaling(
                allocations=allocations,
                replacement_set_name=account['replacement_set']
            )

        # Log target allocations
        self._log_target_allocations(allocations)

        # Get current positions
        snapshot = await self.ibkr.get_account_snapshot(account_id)

        # Log initial account snapshot
        self._log_account_snapshot("CURRENT", snapshot)

        # Get market prices
        all_symbols = list(set([a['symbol'] for a in allocations] +
                              [p['symbol'] for p in snapshot['positions']]))
        market_prices = await self.ibkr.get_multiple_market_prices(all_symbols)

        # Calculate required trades
        calculator = TradeCalculator(logger=self.logger)
        trades = calculator.calculate_trades(
            snapshot=snapshot,
            allocations=allocations,
            market_prices=market_prices,
            account_config=account
        )

        # Log proposed trades
        self._log_planned_orders(trades, is_preview=True)

        return {
            'proposed_trades': trades,
            'current_value': snapshot['total_value'],
            'success': True
        }

    async def _cancel_pending_orders(self, account_id: str):
        """Cancel any pending orders for the account"""
        try:
            open_orders = await self.ibkr.get_open_orders(account_id)
            for order in open_orders:
                self.logger.info(f"Cancelling order {order['order_id']}")
                await self.ibkr.cancel_order(order['order_id'])
        except Exception as e:
            self.logger.warning(f"Error cancelling pending orders: {e}")

    async def _wait_for_orders_complete(self, orders: List[dict], timeout: int = 60):
        """Wait for orders to complete"""
        import asyncio

        if not orders:
            return

        self.logger.info(f"Waiting for {len(orders)} orders to complete")
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < timeout:
            # Check order status
            all_complete = True
            for order in orders:
                status = await self.ibkr.get_order_status(order.get('order_id'))
                self.logger.debug(f"Order {order.get('order_id')} status: '{status}'")
                if status and status.upper() not in ['FILLED', 'CANCELLED']:
                    all_complete = False
                    break

            if all_complete:
                self.logger.info("All orders completed")
                await asyncio.sleep(1)
                return

            await asyncio.sleep(2)

        self.logger.warning(f"Timeout waiting for orders after {timeout} seconds")

    def _log_account_snapshot(self, stage: str, snapshot: dict):
        """Log detailed account snapshot"""
        account_id = snapshot['account_id']
        total_value = snapshot['total_value']
        positions = snapshot.get('positions', [])

        self.logger.info(f"====== {stage} ACCOUNT SNAPSHOT ======")
        self.logger.info(f"Account ID: {account_id}")
        self.logger.info(f"Total Account Value: ${total_value:,.2f}")

        if positions:
            self.logger.info(f"Positions ({len(positions)}):")
            sorted_positions = sorted(positions, key=lambda x: x['symbol'])
            for pos in sorted_positions:
                symbol = pos['symbol']
                quantity = pos['quantity']
                market_price = pos.get('market_price', 0)
                market_value = pos.get('market_value', 0)
                percent_of_account = (market_value / total_value * 100) if total_value > 0 else 0

                self.logger.info(f"  {symbol}: {quantity:,} shares @ ${market_price:.2f} "
                               f"= ${market_value:,.2f} ({percent_of_account:.2f}%)")
        else:
            self.logger.info("No positions held")

        # Log actual cash balances from IBKR API
        cash_balance = snapshot.get('cash_balance', 0)
        settled_cash = snapshot.get('settled_cash', 0)
        self.logger.info(f"Cash Balance: ${cash_balance:,.2f}")
        self.logger.info(f"Settled Cash: ${settled_cash:,.2f}")
        self.logger.info("=" * 40)

    def _log_target_allocations(self, allocations: list):
        """Log target allocation percentages"""
        self.logger.info(f"====== TARGET ALLOCATIONS ({len(allocations)}) ======")
        total_allocation = sum(alloc['allocation'] for alloc in allocations)

        sorted_allocations = sorted(allocations, key=lambda x: x['symbol'])
        for alloc in sorted_allocations:
            symbol = alloc['symbol']
            percentage = alloc['allocation']
            self.logger.info(f"  {symbol}: {percentage:.2f}%")

        self.logger.info(f"Total Allocation: {total_allocation:.2f}%")
        self.logger.info("=" * 35)

    def _log_planned_orders(self, trades: list, is_preview: bool = False):
        """Log planned orders/trades"""
        stage = "PROPOSED TRADES (PREVIEW)" if is_preview else "PLANNED ORDERS"
        self.logger.info(f"====== {stage} ======")

        if not trades:
            self.logger.info("No trades required - portfolio is already balanced")
            self.logger.info("=" * (len(stage) + 14))
            return

        sell_orders = [t for t in trades if t['quantity'] < 0]
        buy_orders = [t for t in trades if t['quantity'] > 0]

        total_sell_value = sum(abs(t['quantity'] * t['price']) for t in sell_orders)
        total_buy_value = sum(t['quantity'] * t['price'] for t in buy_orders)

        self.logger.info(f"Total Orders: {len(trades)} ({len(sell_orders)} sells, {len(buy_orders)} buys)")
        self.logger.info(f"Total Sell Value: ${total_sell_value:,.2f}")
        self.logger.info(f"Total Buy Value: ${total_buy_value:,.2f}")
        self.logger.info("")

        if sell_orders:
            self.logger.info("SELL Orders:")
            for trade in sell_orders:
                symbol = trade['symbol']
                quantity = abs(trade['quantity'])
                price = trade['price']
                current_shares = trade['current_shares']
                trade_value = quantity * price
                self.logger.info(f"  SELL {quantity:,} shares of {symbol} @ ${price:.2f} "
                               f"= ${trade_value:,.2f} (from {current_shares:,} shares)")

        if buy_orders:
            self.logger.info("BUY Orders:")
            for trade in buy_orders:
                symbol = trade['symbol']
                quantity = trade['quantity']
                price = trade['price']
                current_shares = trade['current_shares']
                trade_value = quantity * price
                self.logger.info(f"  BUY {quantity:,} shares of {symbol} @ ${price:.2f} "
                               f"= ${trade_value:,.2f} (to {current_shares + quantity:,} shares)")

        self.logger.info("=" * (len(stage) + 14))