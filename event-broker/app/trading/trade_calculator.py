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

        # Auto-Deploy Remaining Cash: Deploy remaining cash into the position that can best absorb it
        # This runs only for buy phase or 'all' phase to avoid double-processing
        if phase in ['buy', 'all']:
            trades = self._auto_deploy_remaining_cash(
                trades=trades,
                snapshot=snapshot,
                allocations=allocations,
                market_prices=market_prices,
                available_value=available_value,
                position_map=position_map,
                account_config=account_config
            )

        return trades

    def _auto_deploy_remaining_cash(self, trades: List[dict], snapshot: dict, allocations: List[dict],
                                   market_prices: Dict[str, float], available_value: float,
                                   position_map: Dict[str, dict], account_config: dict) -> List[dict]:
        """
        Auto-Deploy Remaining Cash: Deploy remaining cash into the position that can best absorb it.

        Purpose: After regular rebalancing trades are calculated, there's often cash left over
        due to fractional share constraints. This opt-in mechanism deploys remaining cash into
        the position that can best absorb it (largest positions with lowest over-allocation impact).

        Algorithm:
        1. Check if auto_deploy_remaining_cash is enabled for this account
        2. Calculate projected cash after executing all planned trades
        3. Find positions that can absorb cash (can buy at least 1 share)
        4. Rank by: largest position size first, then lowest over-allocation percentage
        5. Deploy all remaining cash into the best absorber position

        Trade-off: Accepts slight over-allocation in one position to minimize uninvested cash.

        Args:
            trades: List of already calculated trades
            snapshot: Current account snapshot with positions and cash
            allocations: Target allocations from API
            market_prices: Current market prices for all symbols
            available_value: Total account value available for investment
            position_map: Dictionary mapping symbol to current position info
            account_config: Account configuration including auto_deploy_remaining_cash setting

        Returns:
            Updated trades list with optional auto-deploy trade
        """

        # Step 1: Check if auto-deploy is enabled for this account
        auto_deploy_enabled = account_config.get('auto_deploy_remaining_cash', False)
        if not auto_deploy_enabled:
            return trades

        # Step 2: Calculate projected cash after executing planned trades
        current_cash = snapshot.get('cash_balance', 0)

        # Calculate net cash change from planned trades
        net_cash_change = 0
        for trade in trades:
            trade_value = trade['quantity'] * trade['price']
            net_cash_change -= trade_value  # Negative for buys, positive for sells

        projected_cash = current_cash + net_cash_change

        self.logger.info(f"Auto-Deploy Remaining Cash:")
        self.logger.info(f"  Feature enabled: {auto_deploy_enabled}")
        self.logger.info(f"  Current cash: ${current_cash:,.2f}")
        self.logger.info(f"  Net cash change from trades: ${net_cash_change:,.2f}")
        self.logger.info(f"  Projected cash after trades: ${projected_cash:,.2f}")

        # Skip auto-deploy if insufficient cash (less than $50 to avoid tiny over-allocations)
        if projected_cash < 50:
            self.logger.info(f"  Skipping auto-deploy: insufficient cash (${projected_cash:.2f})")
            return trades

        # Step 3: Calculate projected position values after planned trades
        projected_positions = {}

        for allocation in allocations:
            symbol = allocation['symbol']
            current_position = position_map.get(symbol, {})
            current_shares = current_position.get('quantity', 0)
            current_price = market_prices.get(symbol, 0)

            # Add shares from planned trades
            planned_shares = 0
            for trade in trades:
                if trade['symbol'] == symbol:
                    planned_shares += trade['quantity']

            total_shares = current_shares + planned_shares
            projected_value = total_shares * current_price
            target_percent = allocation['allocation'] / 100.0
            target_value = available_value * target_percent

            projected_positions[symbol] = {
                'current_shares': current_shares,
                'planned_shares': planned_shares,
                'total_shares': total_shares,
                'projected_value': projected_value,
                'target_value': target_value,
                'price': current_price
            }

        # Step 4: Find best cash absorber
        candidates = []

        for symbol, pos_info in projected_positions.items():
            price = pos_info['price']
            if price <= 0:
                continue

            shares_affordable = int(projected_cash / price)
            if shares_affordable == 0:
                continue  # Can't afford even 1 share

            absorption_value = shares_affordable * price
            new_value = pos_info['projected_value'] + absorption_value
            target_value = pos_info['target_value']

            # Calculate over-allocation percentage
            over_allocation_pct = ((new_value - target_value) / target_value) * 100 if target_value > 0 else float('inf')

            candidates.append({
                'symbol': symbol,
                'shares_to_buy': shares_affordable,
                'absorption_value': absorption_value,
                'projected_value': pos_info['projected_value'],
                'new_value': new_value,
                'target_value': target_value,
                'over_allocation_pct': over_allocation_pct,
                'position_size': pos_info['projected_value'],  # For ranking (larger is better)
                'price': price,
                'current_shares': pos_info['current_shares'],
                'planned_shares': pos_info['planned_shares']
            })

        if not candidates:
            self.logger.info(f"  No positions can absorb remaining cash")
            return trades

        # Sort by: largest position size first, then lowest over-allocation %
        candidates.sort(key=lambda x: (-x['position_size'], x['over_allocation_pct']))

        # Step 5: Deploy cash into best absorber
        best_absorber = candidates[0]

        self.logger.debug(f"  Best cash absorber candidates:")
        for i, candidate in enumerate(candidates[:3]):  # Show top 3
            self.logger.debug(f"    {i+1}. {candidate['symbol']}: {candidate['shares_to_buy']} shares "
                           f"(${candidate['absorption_value']:.0f}), over-alloc: {candidate['over_allocation_pct']:.1f}%")

        # Create auto-deploy trade
        auto_deploy_trade = {
            'symbol': best_absorber['symbol'],
            'quantity': best_absorber['shares_to_buy'],
            'current_shares': best_absorber['current_shares'] + best_absorber['planned_shares'],
            'target_value': best_absorber['target_value'],
            'current_value': best_absorber['projected_value'],
            'price': best_absorber['price'],
            'order_type': 'MARKET',
            'auto_deploy': True  # Flag to identify auto-deploy trades in logs
        }

        remaining_cash = projected_cash - best_absorber['absorption_value']

        self.logger.info(f"  Auto-Deploy Remaining Cash: BUY {best_absorber['shares_to_buy']} shares of {best_absorber['symbol']} "
                       f"@ ${best_absorber['price']:.2f} = ${best_absorber['absorption_value']:.2f}")
        self.logger.debug(f"  Over-allocation: {best_absorber['over_allocation_pct']:.2f}% "
                       f"(${best_absorber['new_value']:.0f} vs ${best_absorber['target_value']:.0f} target)")
        self.logger.info(f"  Potential Cash deployed: ${best_absorber['absorption_value']:.2f}")
        self.logger.info(f"  Remaining cash: ${remaining_cash:.2f}")

        # Combine original trades with auto-deploy trade
        all_trades = trades + [auto_deploy_trade]

        return all_trades