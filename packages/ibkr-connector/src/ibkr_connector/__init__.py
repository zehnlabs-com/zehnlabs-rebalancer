from .connection import IBKRConnection
from .discovery import (
    DISCOVERY_CLIENT_ID,
    AccountDiscoveryError,
    AccountDiscoveryResponseError,
    AccountDiscoveryResult,
    discover_managed_accounts,
)

__version__ = "1.0.0"

__all__ = [
    "IBKRConnection",
    "DISCOVERY_CLIENT_ID",
    "AccountDiscoveryError",
    "AccountDiscoveryResponseError",
    "AccountDiscoveryResult",
    "discover_managed_accounts",
    "__version__",
]
