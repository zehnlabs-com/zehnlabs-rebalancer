from typing import List, Optional, Literal
from pydantic import BaseModel, Field

# Core trading models
class Trade(BaseModel):
    """Standardized trade representation across all brokers"""
    symbol: str
    quantity: int
    current_shares: float
    target_value: float
    current_value: float
    price: float
    order_type: str = 'MARKET'
    order_id: Optional[str] = None  # String to support IBKR int and Schwab UUID

class AllocationItem(BaseModel):
    """Standardized allocation item"""
    symbol: str
    allocation: float

class AccountConfig(BaseModel):
    """Broker-agnostic account configuration"""
    account_id: str
    type: Literal['paper', 'live']
    enabled: bool = True
    strategy_name: str
    cash_reserve_percent: float = 1.0
    replacement_set: Optional[str] = None
    pdt_protection_enabled: bool = False
    broker: str = 'ibkr'  # Default to IBKR for backward compatibility

# Market data models
class ContractPrice(BaseModel):
    """Standardized price data"""
    symbol: str
    bid: float
    ask: float
    last: float
    close: float

class AccountPosition(BaseModel):
    """Standardized position data"""
    symbol: str
    quantity: float
    market_price: float
    market_value: float

class AccountSnapshot(BaseModel):
    """Standardized account snapshot"""
    account_id: str
    total_value: float
    cash_balance: float
    settled_cash: float
    positions: List[AccountPosition]

# Order models
class OrderResult(BaseModel):
    """Standardized order placement result"""
    order_id: str  # String to support both int (IBKR) and UUID (Schwab)
    symbol: str
    quantity: int
    status: str

class OpenOrder(BaseModel):
    """Standardized open order"""
    order_id: str
    symbol: str
    quantity: int
    status: str
    order_type: str

class OrderStatus:
    """Normalized order statuses across brokers"""
    FILLED = "FILLED"
    WORKING = "WORKING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"

# Rebalancing result models
class RebalanceResult(BaseModel):
    """Result of rebalance operation"""
    orders: List[Trade]
    total_value: float
    cash_balance: Optional[float] = None
    success: bool
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

class CalculateRebalanceResult(BaseModel):
    """Result of rebalance calculation (preview)"""
    proposed_trades: List[Trade]
    current_value: float
    success: bool
    warnings: List[str] = Field(default_factory=list)
