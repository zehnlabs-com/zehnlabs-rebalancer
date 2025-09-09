"""
IBKR Data Models for strongly typed returns from IBKR Client
"""
from pydantic import BaseModel
from typing import Dict, Optional, Any
from datetime import datetime


class PnLData(BaseModel):
    """P&L data returned from IBKR's reqPnL method"""
    daily_pnl: float
    unrealized_pnl: float
    realized_pnl: float


class ContractDetailsData(BaseModel):
    """Contract details for a specific symbol including trading hours"""
    trading_hours: str
    liquid_hours: str
    time_zone: str
    contract_details: Any  # ib_async ContractDetails object


class TradingHoursResult(BaseModel):
    """Result of trading hours check for multiple symbols"""
    all_within_hours: bool
    next_start_time: Optional[datetime]
    symbol_status: Dict[str, bool]


class MarketPrices(BaseModel):
    """Wrapper for market prices with validation and convenience methods"""
    prices: Dict[str, float]
    
    def get(self, symbol: str, default: float = 0.0) -> float:
        """Get price for symbol with default fallback"""
        return self.prices.get(symbol, default)
    
    def __getitem__(self, symbol: str) -> float:
        """Allow dict-like access"""
        return self.prices[symbol]
    
    def __contains__(self, symbol: str) -> bool:
        """Allow 'in' operator"""
        return symbol in self.prices
    
    def items(self):
        """Allow iteration over items"""
        return self.prices.items()
    
    def keys(self):
        """Allow iteration over keys"""
        return self.prices.keys()
    
    def values(self):
        """Allow iteration over values"""
        return self.prices.values()