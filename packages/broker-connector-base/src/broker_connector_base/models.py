from enum import StrEnum
from dataclasses import dataclass, field
from typing import Optional, Tuple
from .errors import NoPriceError


class OrderStatus(StrEnum):
    """PRD §6.1. Replaces the old string-constant class (replaced by the enum)."""
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


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