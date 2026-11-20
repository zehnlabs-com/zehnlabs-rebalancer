from .base_client import BrokerClient
from .base_rebalancer import BaseRebalancer
from .connection import BrokerConnection
from .errors import (
    BrokerError,
    ConnectionFailedError,
    AccountDataError,
    PricingError,
    NoPriceError,
    CancelNotConfirmedError,
    OrderPlacementError,
)
from .models import (
    # Legacy models (kept until their consumers are replaced in later work packages)
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
    # PRD §6.1 domain models
    Side,
    OrderStatus,
    PriceSource,
    Quote,
    Position,
    AccountState,
    OrderRequest,
    OrderState,
    WorkingOrder,
)

__version__ = "1.0.0"

__all__ = [
    "BrokerClient",
    "BaseRebalancer",
    "BrokerConnection",
    "BrokerError",
    "ConnectionFailedError",
    "AccountDataError",
    "PricingError",
    "NoPriceError",
    "CancelNotConfirmedError",
    "OrderPlacementError",
    "Trade",
    "AllocationItem",
    "AccountConfig",
    "AccountSnapshot",
    "AccountPosition",
    "ContractPrice",
    "OrderResult",
    "OpenOrder",
    "RebalanceResult",
    "CalculateRebalanceResult",
    "Side",
    "OrderStatus",
    "PriceSource",
    "Quote",
    "Position",
    "AccountState",
    "OrderRequest",
    "OrderState",
    "WorkingOrder",
    "__version__",
]