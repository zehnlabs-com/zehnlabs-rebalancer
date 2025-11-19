from typing import List
from pydantic import BaseModel, Field
from broker_connector_base import Trade

class TradeCalculationResult(BaseModel):
    """Result of trade calculation with warnings"""
    trades: List[Trade]
    warnings: List[str] = Field(default_factory=list)
