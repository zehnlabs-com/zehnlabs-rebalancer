"""Trade calculation logic with round-and-scale algorithm"""

from typing import List, Optional
import logging
import math
from app.models import AccountSnapshot, AllocationItem, AccountConfig, Trade

class TradeCalculator:
    """Calculate trades needed for rebalancing"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def calculate_trades(self, snapshot: AccountSnapshot, allocations: List[AllocationItem],
                        market_prices: dict[str, float], account_config: AccountConfig,
                        phase: str = 'all') -> List[Trade]:
        """Calculate required trades based on target allocations"""

        trades = []
        total_value = snapshot.total_value
        cash_reserve = account_config.cash_reserve_percent / 100.0

        # Always calculate target values based on total account value
        # Cash constraints are handled in the scaling phase
        available_value = total_value * (1 - cash_reserve)

        if phase == 'buy':
            self.logger.debug(f"Buy phase: Target values based on total account ${available_value:,.2f}")

        position_map = {pos.symbol: pos for pos in snapshot.positions}

        # Track total value allocated
        total_allocated_value = 0
        total_value_left_on_table = 0

        target_symbols = {alloc.symbol for alloc in allocations}
        for symbol, position in position_map.items():
            if symbol not in target_symbols:
                current_shares = position.quantity
                if current_shares > 0:
                    current_price = market_prices.get(symbol)
                    if current_price and not math.isnan(current_price) and current_price > 0:
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
            target_percent = allocation.allocation / 100.0
            target_value = available_value * target_percent

            current_position = position_map.get(symbol)
            current_shares = current_position.quantity if current_position else 0
            current_price = market_prices.get(symbol)

            if not current_price or math.isnan(current_price) or current_price == 0.0:
                self.logger.error(f"Invalid price for {symbol}: {current_price} - rebalance cannot proceed")
                raise ValueError(f"Invalid price for {symbol}: {current_price}. Rebalance aborted.")

            current_value = current_shares * current_price
            value_difference = target_value - current_value

            # Calculate shares to trade using rounding
            exact_shares = value_difference / current_price
            shares_to_trade = round(exact_shares)

            # Skip sell orders if difference is less than 0.5%
            if shares_to_trade < 0:
                current_percent = (current_value / total_value * 100) if total_value > 0 else 0
                target_percent_display = target_percent * 100
                allocation_diff = abs(target_percent_display - current_percent)
                if allocation_diff < 0.5:
                    self.logger.debug(f"Skipping sell for {symbol}: {allocation_diff:.2f}% difference < 0.5% threshold (target={target_percent_display:.2f}%, current={current_percent:.2f}%)")
                    continue

            # Apply phase filter
            if phase == 'sell' and shares_to_trade >= 0:
                continue
            elif phase == 'buy' and shares_to_trade <= 0:
                continue


            if shares_to_trade != 0:
                trade_value = shares_to_trade * current_price
                total_allocated_value += abs(trade_value)
                total_value_left_on_table += abs((exact_shares - shares_to_trade) * current_price)

                trades.append(Trade(
                    symbol=symbol,
                    quantity=shares_to_trade,
                    current_shares=current_shares,
                    target_value=target_value,
                    current_value=current_value,
                    price=current_price,
                    order_type='MARKET'
                ))


        trades.sort(key=lambda x: x.quantity)

        # Apply cash constraint scaling for buy phase
        if phase in ['buy', 'all']:
            trades = self._apply_cash_constraint_scaling(
                trades=trades,
                snapshot=snapshot,
                available_value=available_value
            )

        return trades

    def _apply_cash_constraint_scaling(self, trades: List[Trade], snapshot: AccountSnapshot, available_value: float) -> List[Trade]:
        """Apply cash constraint scaling to ensure trades fit within available cash"""

        buy_trades = [t for t in trades if t.quantity > 0]
        total_buy_cost = sum(t.quantity * t.price for t in buy_trades)
        available_cash = snapshot.cash_balance

        # Only show detailed scaling logs if there's actually a constraint issue
        if total_buy_cost > available_cash * 1.1:  # More than 10% over budget
            self.logger.info(f"Cash Constraint Analysis (Pre-Sell Planning):")
            self.logger.info(f"  Planned buy cost: ${total_buy_cost:,.2f}")
            self.logger.info(f"  Current cash: ${available_cash:,.2f}")
        else:
            self.logger.debug(f"Cash check: ${total_buy_cost:,.2f} cost vs ${available_cash:,.2f} available")

        total_account_value = snapshot.total_value

        # Safety check - don't exceed reasonable limits
        if total_buy_cost > total_account_value * 0.99:
            self.logger.warning(f"  Buy cost ${total_buy_cost:,.2f} exceeds safe limit of account value ${total_account_value:,.2f}")
            # Use account value as the constraint instead of cash balance
            available_cash = min(available_cash, total_account_value * 0.99)

        if abs(total_buy_cost - available_cash) < 100:  # Within $100, no scaling needed
            self.logger.info(f"  Minimal adjustment needed (${abs(total_buy_cost - available_cash):.2f} difference)")
            return trades

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