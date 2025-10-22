"""Trade calculation logic with round-and-scale algorithm"""

from typing import List, Optional
import logging
import math
from app.models import AccountSnapshot, AllocationItem, AccountConfig, Trade, ContractPrice, TradeCalculationResult

class TradeCalculator:
    """Calculate trades needed for rebalancing"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def calculate_trades(self, snapshot: AccountSnapshot, allocations: List[AllocationItem],
                        market_prices: List[ContractPrice], account_config: AccountConfig,
                        phase: str = 'all') -> TradeCalculationResult:
        """
        Calculate required trades based on target allocations.
        Returns TradeCalculationResult containing trades and warnings
        """

        trades = []
        warnings = []
        total_value = snapshot.total_value
        cash_reserve = account_config.cash_reserve_percent / 100.0

        # Always calculate target values based on total account value
        # Cash constraints are handled in the scaling phase
        available_value = total_value * (1 - cash_reserve)

        if phase == 'buy':
            self.logger.debug(f"Buy phase: Target values based on total account ${available_value:,.2f}")

        position_map = {pos.symbol: pos for pos in snapshot.positions}
        price_map = {mp.symbol: mp for mp in market_prices}

        # Track total value allocated
        total_allocated_value = 0
        total_value_left_on_table = 0

        target_symbols = {alloc.symbol for alloc in allocations}
        for symbol, position in position_map.items():
            if symbol not in target_symbols:
                current_shares = position.quantity
                if current_shares > 0:
                    price_data = price_map.get(symbol)

                    # Validate price data exists
                    if not price_data:
                        self.logger.error(f"No price data for {symbol} to liquidate - rebalance cannot proceed")
                        raise ValueError(f"No price data for {symbol}. Cannot liquidate position without valid price.")

                    # Validate bid price is valid (get_multiple_market_prices already validated this)
                    if not price_data.bid or price_data.bid <= 0 or math.isnan(price_data.bid):
                        self.logger.error(f"Invalid bid price for {symbol}: {price_data.bid} - cannot liquidate")
                        raise ValueError(f"Invalid bid price for {symbol}: {price_data.bid}. Cannot liquidate position.")

                    # For sells, use bid price (what you receive)
                    current_price = price_data.bid

                    # Check if fractional position that cannot be liquidated via API
                    if 0 < current_shares < 1:
                        # Only warn during first phase (not during buy-only recalculation)
                        if phase != 'buy':
                            market_value = current_shares * current_price
                            warning_message = (
                                f"Position {symbol} ({current_shares:.4f} shares, ${market_value:.2f}) "
                                f"cannot be liquidated via API.\n\n"
                                f"IBKR API does not support liquidating fractional positions programmatically.\n\n"
                                f"Please close this position manually using TWS desktop or IBKR Mobile app."
                            )
                            warnings.append(warning_message)
                            self.logger.warning(
                                f"Cannot liquidate fractional position via API: "
                                f"{symbol} ({current_shares:.4f} shares). Manual intervention required."
                            )
                        continue  # Skip - cannot liquidate via API

                    self.logger.info(f"Liquidating: {symbol} ({current_shares:,} shares @ ${current_price:.2f})")
                    trades.append(Trade(
                        symbol=symbol,
                        quantity=int(-current_shares),
                        current_shares=current_shares,
                        target_value=0,
                        current_value=current_shares * current_price,
                        price=current_price,
                        order_type='MARKET'
                    ))

        for allocation in allocations:
            symbol = allocation.symbol
            target_percent = allocation.allocation
            target_value = available_value * target_percent

            current_position = position_map.get(symbol)
            current_shares = current_position.quantity if current_position else 0
            price_data = price_map.get(symbol)

            if not price_data:
                self.logger.error(f"No price data for {symbol} - rebalance cannot proceed")
                raise ValueError(f"No price data for {symbol}. Rebalance aborted.")

            # Validate that we have valid bid and ask prices
            # The ibkr_client should have already validated this, but we double-check
            if not price_data.bid or price_data.bid <= 0 or math.isnan(price_data.bid):
                self.logger.error(f"Invalid bid price for {symbol}: {price_data.bid} - rebalance cannot proceed")
                raise ValueError(f"Invalid bid price for {symbol}: {price_data.bid}. Rebalance aborted.")

            if not price_data.ask or price_data.ask <= 0 or math.isnan(price_data.ask):
                self.logger.error(f"Invalid ask price for {symbol}: {price_data.ask} - rebalance cannot proceed")
                raise ValueError(f"Invalid ask price for {symbol}: {price_data.ask}. Rebalance aborted.")

            # Use midpoint for current value calculation (bid and ask are guaranteed valid)
            current_price = (price_data.bid + price_data.ask) / 2

            current_value = current_shares * current_price
            value_difference = target_value - current_value

            # Determine if this will be a buy or sell, and use appropriate price
            # For buy: use ask + 0.5% slippage (what you actually pay with limit order)
            # For sell: use bid (what you receive)
            if value_difference > 0:
                # This will be a buy - use ask price with 0.5% slippage adjustment
                # This ensures we calculate quantities based on the actual limit price we'll use
                trade_price = price_data.ask * 1.005
            else:
                # This will be a sell - use bid price (guaranteed valid)
                trade_price = price_data.bid

            # Calculate shares to trade using the appropriate price
            exact_shares = value_difference / trade_price
            shares_to_trade = round(exact_shares)

            # Skip orders (buy or sell) if allocation difference is less than 0.5%
            current_percent = (current_value / total_value * 100) if total_value > 0 else 0
            target_percent_display = target_percent * 100
            allocation_diff = abs(target_percent_display - current_percent)

            if allocation_diff < 0.5:
                action = "sell" if shares_to_trade < 0 else "buy"
                self.logger.debug(f"Skipping {action} for {symbol}: {allocation_diff:.2f}% difference < 0.5% threshold (target={target_percent_display:.2f}%, current={current_percent:.2f}%)")
                continue

            # Apply phase filter
            if phase == 'sell' and shares_to_trade >= 0:
                continue
            elif phase == 'buy' and shares_to_trade <= 0:
                continue

            if shares_to_trade != 0:
                trade_value = shares_to_trade * trade_price
                total_allocated_value += abs(trade_value)
                total_value_left_on_table += abs((exact_shares - shares_to_trade) * trade_price)

                # For buys, use LIMIT orders with 0.5% slippage protection
                # For sells, use MARKET orders (already getting bid price)
                if shares_to_trade > 0:
                    order_type = 'LIMIT'
                else:
                    order_type = 'MARKET'

                trades.append(Trade(
                    symbol=symbol,
                    quantity=shares_to_trade,
                    current_shares=current_shares,
                    target_value=target_value,
                    current_value=current_value,
                    price=round(trade_price, 2),
                    order_type=order_type
                ))


        # Sort trades by priority: sells first, then buys by allocation % (highest first)
        # Build allocation map for priority sorting
        allocation_map = {alloc.symbol: alloc.allocation for alloc in allocations}

        def sort_key(trade):
            if trade.quantity < 0:
                # Sells: sort by quantity (most negative first)
                return (0, trade.quantity)
            else:
                # Buys: sort by allocation priority (highest % first)
                allocation_pct = allocation_map.get(trade.symbol, 0)
                return (1, -allocation_pct)  # Negative to sort descending

        trades.sort(key=sort_key)

        # Apply cash constraint scaling for buy phase
        if phase in ['buy', 'all']:
            trades = self._apply_cash_constraint_scaling(
                trades=trades,
                snapshot=snapshot,
                allocations=allocations,
                phase=phase
            )

        return TradeCalculationResult(trades=trades, warnings=warnings)

    def _apply_cash_constraint_scaling(self, trades: List[Trade], snapshot: AccountSnapshot,
                                        allocations: List[AllocationItem], phase: str) -> List[Trade]:
        """Apply cash constraint scaling to ensure trades fit within available cash"""

        buy_trades = [t for t in trades if t.quantity > 0]
        total_buy_cost = sum(t.quantity * t.price for t in buy_trades)

        # Slippage is already included in t.price (ask * 1.005 for buys)
        # Account for 1% commission on all trades and reserve $100 for subscription fees
        # If cash balance is less than $100, available cash is 0 (can't make any trades)
        if snapshot.cash_balance < 100:
            available_cash = 0
        else:
            available_cash = (snapshot.cash_balance - 100) / 1.01

        # Check if any target allocation symbols are missing from the account
        target_symbols = {alloc.symbol for alloc in allocations}
        current_symbols = {pos.symbol for pos in snapshot.positions if pos.quantity > 0}
        missing_symbols = target_symbols - current_symbols

        # If available cash is 0 and all symbols are present, skip rebalance entirely
        if available_cash <= 0 and not missing_symbols:
            self.logger.info(f"  All target symbols already present in account.")
            self.logger.info(f"  No available cash for optimization (${snapshot.cash_balance:.2f} balance < $100 minimum).")
            self.logger.info(f"  Skipping rebalance - minimum requirements already met.")
            # Return only sell trades (keep liquidations)
            return [t for t in trades if t.quantity <= 0]


        # Only show detailed scaling logs if there's actually a constraint issue
        if total_buy_cost > available_cash * 1.1:  # More than 10% over budget
            self.logger.info(f"Cash Constraint Analysis (Pre-Sell Planning):")
            self.logger.info(f"  Planned buy cost: ${total_buy_cost:,.2f}")
            self.logger.info(f"  Current cash: ${available_cash:,.2f}")
        else:
            self.logger.debug(f"Cash check: ${total_buy_cost:,.2f} cost vs ${available_cash:,.2f} available")

        total_account_value = snapshot.total_value

        # Safety check - don't exceed reasonable limits
        if total_buy_cost > total_account_value * 0.995:
            self.logger.warning(f"  Buy cost ${total_buy_cost:,.2f} exceeds safe limit of account value ${total_account_value:,.2f}")
            # Use account value as the constraint instead of cash balance
            available_cash = min(available_cash, total_account_value * 0.995)

        # Apply scaling logic to fully utilize available cash

        fixed_trades = [t for t in trades if t.quantity <= 0 or t.quantity == 1]
        scaleable_trades = [t for t in buy_trades if t.quantity > 1]

        fixed_cost = sum(t.quantity * t.price for t in fixed_trades if t.quantity > 0)
        scaleable_cost = sum(t.quantity * t.price for t in scaleable_trades)

        # Calculate scaling factor for scaleable trades to use available cash
        target_scaleable_cost = available_cash - fixed_cost
        scaling_factor = target_scaleable_cost / scaleable_cost if scaleable_cost > 0 else 1.0

        action = "scaling up" if scaling_factor > 1 else "scaling down"
        self.logger.info(f"  Cash utilization scaling: {action} by factor {scaling_factor:.4f}")
        self.logger.info(f"  Target deployment: ${available_cash:,.2f} (vs original ${total_buy_cost:,.2f})")

        self.logger.debug(f"  Fixed cost: ${fixed_cost:,.2f}, Scaleable cost: ${scaleable_cost:,.2f}, Factor: {scaling_factor:.4f}")

        scaled_trades = []
        total_scaled_cost = 0

        for trade in trades:
            if trade.quantity <= 0 or trade.quantity == 1:
                scaled_trades.append(trade)
                if trade.quantity > 0:
                    total_scaled_cost += trade.quantity * trade.price
            else:
                original_quantity = trade.quantity
                scaled_quantity = max(1, int(1 + (original_quantity - 1) * scaling_factor))

                scaled_trade = trade.model_copy()
                scaled_trade.quantity = scaled_quantity
                scaled_trades.append(scaled_trade)
                total_scaled_cost += scaled_quantity * trade.price

                if scaled_quantity != original_quantity:
                    self.logger.debug(f"  Scaled {trade.symbol}: {original_quantity} → {scaled_quantity} shares")

        if total_scaled_cost > available_cash:
            remaining_overage = total_scaled_cost - available_cash
            self.logger.debug(f"  Fine-tuning required: ${remaining_overage:.2f} overage remaining")

            scaleable_scaled = [t for t in scaled_trades if t.quantity > 1]
            scaleable_scaled.sort(key=lambda x: x.quantity * x.price, reverse=True)

            for trade in scaleable_scaled:
                if total_scaled_cost <= available_cash:
                    break

                if trade.quantity > 1:
                    reduction_value = trade.price
                    trade.quantity -= 1
                    total_scaled_cost -= reduction_value
                    self.logger.debug(f"  Fine-tuned {trade.symbol}: reduced by 1 share")

        final_cost = sum(t.quantity * t.price for t in scaled_trades if t.quantity > 0)
        remaining_cash = available_cash - final_cost

        # Only show summary if we actually did meaningful scaling
        if scaling_factor < 0.9:  # If we scaled down by more than 10%
            self.logger.info(f"  Trades adjusted to fit available cash of ${available_cash:,.2f}")

        self.logger.debug(f"  Final cost: ${final_cost:,.2f}, Remaining: ${remaining_cash:.2f}")

        return scaled_trades