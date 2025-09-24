"""Trade calculation logic"""

from typing import List, Dict, Optional
import logging
import math

class TradeCalculator:
    """Calculate trades needed for rebalancing"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def calculate_trades(self, snapshot: dict, allocations: List[dict],
                        market_prices: Dict[str, float], account_config: dict,
                        phase: str = 'all') -> List[dict]:
        """Calculate required trades based on target allocations"""

        trades = []
        total_value = snapshot['total_value']
        cash_reserve = account_config.get('cash_reserve_percent', 1.0) / 100.0
        available_value = total_value * (1 - cash_reserve)

        # Create position map
        position_map = {pos['symbol']: pos for pos in snapshot.get('positions', [])}

        # Track total value allocated
        total_allocated_value = 0
        total_value_left_on_table = 0

        # First, handle positions not in target allocations (sell them completely)
        target_symbols = {alloc['symbol'] for alloc in allocations}
        for symbol, position in position_map.items():
            if symbol not in target_symbols:
                current_shares = position.get('quantity', 0)
                if current_shares > 0:
                    current_price = market_prices.get(symbol)
                    if current_price and not math.isnan(current_price) and current_price > 0:
                        self.logger.info(f"Liquidating orphaned position: {symbol} ({current_shares:,} shares @ ${current_price:.2f})")
                        trades.append({
                            'symbol': symbol,
                            'quantity': -current_shares,
                            'current_shares': current_shares,
                            'target_value': 0,
                            'current_value': current_shares * current_price,
                            'price': current_price,
                            'order_type': 'MARKET'
                        })

        # Then process target allocations
        for allocation in allocations:
            symbol = allocation['symbol']
            target_percent = allocation['allocation'] / 100.0
            target_value = available_value * target_percent

            # Get current position
            current_position = position_map.get(symbol, {})
            current_shares = current_position.get('quantity', 0)
            current_price = market_prices.get(symbol)

            if not current_price or math.isnan(current_price) or current_price == 0.0:
                self.logger.error(f"Invalid price for {symbol}: {current_price} - rebalance cannot proceed")
                raise ValueError(f"Invalid price for {symbol}: {current_price}. Rebalance aborted.")

            current_value = current_shares * current_price
            value_difference = target_value - current_value

            # Calculate shares to trade
            exact_shares = value_difference / current_price
            shares_to_trade = int(exact_shares)

            # Apply phase filter
            if phase == 'sell' and shares_to_trade >= 0:
                continue
            elif phase == 'buy' and shares_to_trade <= 0:
                continue

            # Apply minimum trade threshold
            if abs(value_difference) < 100:  # Skip trades under $100
                continue

            if shares_to_trade != 0:
                trade_value = shares_to_trade * current_price
                total_allocated_value += abs(trade_value)
                total_value_left_on_table += abs((exact_shares - shares_to_trade) * current_price)

                trades.append({
                    'symbol': symbol,
                    'quantity': shares_to_trade,
                    'current_shares': current_shares,
                    'target_value': target_value,
                    'current_value': current_value,
                    'price': current_price,
                    'order_type': 'MARKET'
                })


        # Sort trades - sells first (negative quantities), then buys
        trades.sort(key=lambda x: x['quantity'])

        return trades