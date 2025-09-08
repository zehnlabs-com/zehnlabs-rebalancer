"""
Rebalance data models as specified in new-rebalance-logic.md
"""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TargetAllocation:
    symbol: str
    allocation_percent: float  # 0.0 to 1.0


@dataclass
class CurrentPosition:
    symbol: str
    shares: int
    market_value: float
    average_cost: float


@dataclass
class MarketPrice:
    symbol: str
    price: float


@dataclass
class RebalanceOrder:
    symbol: str
    action: str  # 'BUY' or 'SELL'
    shares: int
    estimated_value: float


@dataclass
class AccountSnapshot:
    account_id: str
    total_value: float  # NetLiquidation
    positions: List[CurrentPosition]
    cash_balance: float  # TotalCashValue


@dataclass
class PortfolioPosition:
    symbol: str
    shares: float  # Can be negative for shorts
    market_price: float
    market_value: float
    average_cost: float
    unrealized_pnl: float


@dataclass
class PortfolioSnapshot:
    account_id: str
    total_value: float  # NetLiquidation
    cash_balance: float  # TotalCashValue
    positions: List[PortfolioPosition]