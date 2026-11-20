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

from enum import StrEnum
from dataclasses import dataclass, field
from typing import Tuple, Optional
from .errors import NoPriceError


class OrderStatus(StrEnum):
    """PRD §6.1. Replaces the old string-constant class (replaced by the enum)."""
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

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


# --- PRD §6.1 domain models (the new broker-connector-base models) -------- #
# Plain frozen value types. They never cross a process boundary. Prices are
# float, quantities are float (whole numbers for a whole-share broker).


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PriceSource(StrEnum):
    MID = "mid"
    ASK = "ask"
    BID = "bid"
    LAST = "last"
    CLOSE = "close"
    BAR = "bar"


def _finite(value):
    """Return a finite price > 0 as float, else None (PRD §6.1)."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0 or value in (float("inf"), float("-inf")):
        return None
    return value


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    close: Optional[float] = None
    bar_close: Optional[float] = None

    def _clean(self):
        return {
            "bid": _finite(self.bid),
            "ask": _finite(self.ask),
            "last": _finite(self.last),
            "close": _finite(self.close),
            "bar_close": _finite(self.bar_close),
        }

    def mark(self) -> Tuple[float, PriceSource]:
        """PRD PRC-1. mid; else last; else close; else bar_close."""
        c = self._clean()
        mid = None
        if c["bid"] is not None and c["ask"] is not None and c["ask"] >= c["bid"]:
            mid = (c["bid"] + c["ask"]) / 2.0
        for value, source in ((mid, PriceSource.MID), (c["last"], PriceSource.LAST),
                              (c["close"], PriceSource.CLOSE), (c["bar_close"], PriceSource.BAR)):
            if value is not None:
                return value, source
        raise NoPriceError(f"No mark price available for {self.symbol}")

    def buy_reference(self) -> Tuple[float, PriceSource]:
        """PRD PRC-2. ask; else last; else close; else bar_close."""
        c = self._clean()
        for value, source in ((c["ask"], PriceSource.ASK), (c["last"], PriceSource.LAST),
                              (c["close"], PriceSource.CLOSE), (c["bar_close"], PriceSource.BAR)):
            if value is not None:
                return value, source
        raise NoPriceError(f"No buy reference price available for {self.symbol}")

    def sell_reference(self) -> Tuple[float, PriceSource]:
        """PRD PRC-3. bid; else last; else close; else bar_close."""
        c = self._clean()
        for value, source in ((c["bid"], PriceSource.BID), (c["last"], PriceSource.LAST),
                              (c["close"], PriceSource.CLOSE), (c["bar_close"], PriceSource.BAR)):
            if value is not None:
                return value, source
        raise NoPriceError(f"No sell reference price available for {self.symbol}")


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float


@dataclass(frozen=True)
class AccountState:
    account_id: str
    cash: float
    """USD cash, may be negative (margin debt)."""
    net_liquidation: float
    positions: Tuple[Position, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    limit_price: Optional[float] = None
    run_id: str = ""


@dataclass(frozen=True)
class OrderState:
    handle: str
    symbol: str
    side: Side
    quantity: float
    status: OrderStatus
    filled_quantity: float
    average_fill_price: Optional[float] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class WorkingOrder:
    symbol: str
    side: Side
    quantity: float
    own: bool
