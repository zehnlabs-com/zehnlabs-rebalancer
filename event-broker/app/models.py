from typing import Optional, List, Literal
from pydantic import BaseModel, Field


class AccountPosition(BaseModel):
    symbol: str
    quantity: float
    market_price: float
    market_value: float


class AccountSnapshot(BaseModel):
    account_id: str
    total_value: float
    cash_balance: float
    settled_cash: float
    positions: List[AccountPosition]


class AllocationItem(BaseModel):
    symbol: str
    allocation: float


class Trade(BaseModel):
    symbol: str
    quantity: int
    current_shares: float
    target_value: float
    current_value: float
    price: float
    order_type: str = 'MARKET'
    order_id: Optional[int] = None


class AccountConfig(BaseModel):
    account_id: str
    type: Literal['paper', 'live']
    enabled: bool = True
    strategy_name: str
    cash_reserve_percent: float = 1.0
    replacement_set: Optional[str] = None


class EventData(BaseModel):
    exec: Literal['rebalance', 'print-rebalance']
    account_id: Optional[str] = None
    event_id: Optional[str] = None
    times_queued: Optional[int] = None
    created_at: Optional[str] = None
    data: Optional[dict] = None


class AccountExecutionResult(BaseModel):
    account_id: str
    success: bool
    error: Optional[str] = None
    details: Optional[dict] = None


class StrategyExecutionResult(BaseModel):
    strategy: str
    timestamp: str
    event: str
    accounts_processed: int
    results: List[AccountExecutionResult]
    status: Optional[str] = None


class RebalanceResult(BaseModel):
    orders: List[Trade]
    total_value: float
    success: bool


class CalculateRebalanceResult(BaseModel):
    proposed_trades: List[Trade]
    current_value: float
    success: bool


class OrderResult(BaseModel):
    order_id: int
    symbol: str
    quantity: int
    status: str


class OpenOrder(BaseModel):
    order_id: int
    symbol: str
    quantity: int
    status: str
    order_type: str