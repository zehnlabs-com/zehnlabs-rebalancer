from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field

# Import models from packages
from broker_connector_base import (
    Trade,
    AllocationItem,
    AccountConfig,
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    OrderResult,
    OpenOrder,
    RebalanceResult,
    CalculateRebalanceResult,
)
from rebalance_calculator import TradeCalculationResult


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


class PDTCheckResult(BaseModel):
    """Result of PDT protection check"""
    allowed: bool
    next_allowed_time: Optional[str] = None


class PDTExecutionInfo(BaseModel):
    """PDT execution tracking information"""
    last_executed: str
    next_execution: str