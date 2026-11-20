"""Broker connection abstract base class (PRD §6.2).

One connection object serves one account. The account ID is given to the
constructor. `async with` support is inherited: `__aenter__` connects and
returns self; `__aexit__` closes (never raises, idempotent).
"""

from abc import ABC, abstractmethod
from typing import Collection, Sequence

from .models import AccountState, OrderRequest, OrderState, Quote, WorkingOrder


class BrokerConnection(ABC):
    """Abstract broker connection for a single account."""

    @property
    @abstractmethod
    def supports_fractional(self) -> bool:
        """True if this broker accepts fractional share quantities."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection. Raises ConnectionFailedError."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection. Never raises; idempotent."""

    @abstractmethod
    async def account_state(self) -> AccountState:
        """Current cash, net liquidation and positions. Raises AccountDataError."""

    @abstractmethod
    async def quotes(self, symbols: Collection[str]) -> dict[str, Quote]:
        """Current quotes for the requested symbols. Raises PricingError."""

    @abstractmethod
    async def working_orders(self) -> list[WorkingOrder]:
        """Own + foreign working orders, this account only."""

    @abstractmethod
    async def cancel_own_orders(self, timeout_seconds: float) -> int:
        """Cancel every own working order, wait until each is done; return the count.

        Raises CancelNotConfirmedError if any is still working at the timeout.
        """

    @abstractmethod
    async def place(self, order: OrderRequest) -> str:
        """Place an order and return an opaque handle. Raises OrderPlacementError."""

    @abstractmethod
    async def order_states(self, handles: Sequence[str]) -> list[OrderState]:
        """Current state of the specified order handles."""

    async def __aenter__(self) -> "BrokerConnection":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()