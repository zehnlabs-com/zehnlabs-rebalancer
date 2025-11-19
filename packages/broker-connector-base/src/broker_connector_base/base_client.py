from abc import ABC, abstractmethod
from typing import List, Optional
from .models import AccountSnapshot, ContractPrice, OrderResult, OpenOrder

class BrokerClient(ABC):
    """Abstract base class for broker API clients"""

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to broker"""
        pass

    @abstractmethod
    async def disconnect(self):
        """Close connection to broker"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to broker"""
        pass

    @abstractmethod
    async def get_account_snapshot(
        self,
        account_id: str,
        use_cached_prices: bool = False
    ) -> AccountSnapshot:
        """Get account positions, balances, and values"""
        pass

    @abstractmethod
    async def get_multiple_market_prices(
        self,
        symbols: List[str],
        use_cache: bool = False
    ) -> List[ContractPrice]:
        """Get current market prices for multiple symbols"""
        pass

    @abstractmethod
    async def place_order(
        self,
        account_id: str,
        symbol: str,
        quantity: int,
        order_type: str = 'MARKET',
        price: Optional[float] = None
    ) -> OrderResult:
        """Place a trade order"""
        pass

    @abstractmethod
    async def get_open_orders(self, account_id: str) -> List[OpenOrder]:
        """Get all open orders for account"""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str):
        """Cancel an order (order_id is string to support both int and UUID)"""
        pass

    @abstractmethod
    async def get_order_status(self, order_id: str) -> str:
        """Get current status of an order (order_id is string)"""
        pass
