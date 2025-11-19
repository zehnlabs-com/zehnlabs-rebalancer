# Portfolio Rebalancer Refactor Plan

## Executive Summary

This document outlines the refactoring plan to split the monolithic `ibkr-portfolio-rebalancer` into a modular, broker-agnostic architecture using Python local packages. The refactor will extract core calculation logic and IBKR-specific code into separate packages while maintaining 100% backward compatibility with production deployments.

**Timeline:** 5-7 days
**Risk Level:** Low (with proper testing)
**Breaking Changes:** None (transparent refactor)

**Latest Updates** (Based on Technical Review - November 2025):
- ✅ Resolved circular dependency between broker-connector-base and rebalance-calculator
- ✅ Moved `Trade`, `AllocationItem`, and `AccountConfig` to broker-connector-base (foundation models)
- ✅ Complete order_id string conversion implementation in all IBKRClient methods (CRITICAL FIX)
- ✅ Added .dockerignore creation with explicit packages/ inclusion (CRITICAL FIX)
- ✅ Clarified sys.path.append usage (only for app.* imports, not packages - CRITICAL FIX)
- ✅ Added `broker` field to AccountConfig for future multi-broker support
- ✅ Added Appendix B with implementation guidelines (BrokerConnectionConfig, exception mapping, rollback validation, local dev setup, versioning, unit tests)
- ✅ Enhanced Dockerfile with proper package installation steps (CRITICAL FIX)
- ✅ Added baseline testing validation before starting refactor
- ✅ Added package version logging for better debugging
- ✅ Added enhanced error handling for import failures

**Latest Updates** (Round 2 - Final Refinements):
- ✅ **Task 1.0 Added**: Pre-scaffolding setup with .dockerignore creation FIRST (prevents silent Docker build failures)
- ✅ **Progressive CLAUDE.md Updates**: Document architecture changes incrementally after each phase (not all at end)
- ✅ **Design Decision Documented**: Rationale for keeping AllocationService/ReplacementService in ibkr-connector (not main app)
- ✅ **Enhanced Timeline**: Added CLAUDE.md update column showing documentation happens throughout refactor
- ✅ All critical suggestions from technical review incorporated

---

## Goals

1. ✅ **Extract broker-agnostic calculation logic** into `rebalance-calculator`
2. ✅ **Create broker abstraction layer** with `broker-connector-base`
3. ✅ **Isolate IBKR-specific code** into `ibkr-connector`
4. ✅ **Maintain zero breaking changes** to existing production system
5. ✅ **Enable future Schwab connector** with minimal effort
6. ✅ **Use Python local packages** (no PyPI publishing yet)

---

## Current Architecture

```
ibkr-portfolio-rebalancer/
├── docker-compose.yaml
├── accounts.yaml
├── replacement-sets.yaml
├── event-broker/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py
│       ├── models.py
│       ├── services/
│       │   ├── ably_service.py
│       │   ├── strategy_executor.py
│       │   ├── trading_executor.py
│       │   ├── notification_service.py
│       │   └── pdt_protection_service.py
│       └── trading/
│           ├── ibkr_client.py           ← Extract to ibkr-connector
│           ├── rebalancer.py            ← Extract to ibkr-connector
│           ├── trade_calculator.py      ← Extract to rebalance-calculator
│           ├── allocation_service.py    ← Extract to ibkr-connector
│           └── replacement_service.py   ← Extract to ibkr-connector
```

---

## Target Architecture

```
ibkr-portfolio-rebalancer/
├── docker-compose.yaml
├── accounts.yaml
├── replacement-sets.yaml
├── packages/                                    ← NEW: Local packages
│   ├── broker-connector-base/                  ← NEW: Abstract interface
│   │   ├── setup.py
│   │   └── src/
│   │       └── broker_connector_base/
│   │           ├── __init__.py
│   │           ├── py.typed
│   │           ├── base_client.py
│   │           ├── base_rebalancer.py
│   │           ├── models.py
│   │           └── exceptions.py
│   ├── rebalance-calculator/                   ← NEW: Pure calculation
│   │   ├── setup.py
│   │   └── src/
│   │       └── rebalance_calculator/
│   │           ├── __init__.py
│   │           ├── py.typed
│   │           ├── calculator.py
│   │           └── models.py
│   └── ibkr-connector/                         ← NEW: IBKR implementation
│       ├── setup.py
│       └── src/
│           └── ibkr_connector/
│               ├── __init__.py
│               ├── py.typed
│               ├── client.py
│               ├── rebalancer.py
│               ├── allocation_service.py
│               ├── replacement_service.py
│               └── models.py
└── event-broker/
    ├── Dockerfile                              ← Updated: Install local packages
    ├── requirements.txt                        ← Updated: Remove ib-async
    └── app/
        ├── main.py
        ├── models.py                           ← Updated: Import from packages
        ├── services/
        │   ├── ably_service.py
        │   ├── strategy_executor.py
        │   ├── trading_executor.py            ← Updated: Use broker factory
        │   ├── notification_service.py
        │   └── pdt_protection_service.py
        └── trading/
            └── broker_factory.py               ← NEW: Broker client factory
```

---

## Phase 1: Create Package Scaffolding (Day 1)

### Task 1.0: Pre-Scaffolding Setup (CRITICAL - DO FIRST)

**⚠️ CRITICAL: Complete these steps BEFORE creating any packages**

#### Step 1: Create .dockerignore file

```bash
# ⚠️ DO THIS FIRST - Critical to prevent Docker build failures
cat > .dockerignore << 'EOF'
# .dockerignore - Place in project root

# Python cache and build artifacts
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.so
*.egg
*.egg-info/
dist/
build/
.pytest_cache/
.coverage

# Environment and secrets
.env
.env.local

# Version control
.git/
.gitignore

# Documentation (except CLAUDE.md which is needed in container)
*.md
!CLAUDE.md

# IDE and editor files
.vscode/
.idea/
.DS_Store

# Node modules (if any)
node_modules/

# ⚠️ CRITICAL: Do NOT exclude packages/ - we need it in Docker build
# The following line should NOT exist:
# packages/  # ← DO NOT ADD THIS LINE

EOF

# Verify .dockerignore was created correctly
cat .dockerignore
echo ""
echo "✅ .dockerignore created - verify packages/ is NOT in the list above"
```

**Validation:**
```bash
# Ensure packages/ is NOT excluded
grep -i "^packages/" .dockerignore
# Should return nothing (exit code 1)

if [ $? -eq 0 ]; then
    echo "❌ ERROR: .dockerignore excludes packages/ - remove that line!"
    exit 1
else
    echo "✅ GOOD: packages/ is not excluded"
fi
```

#### Step 2: Create packages directory structure

```bash
# Create package directories
mkdir -p packages/broker-connector-base/src/broker_connector_base
mkdir -p packages/rebalance-calculator/src/rebalance_calculator
mkdir -p packages/ibkr-connector/src/ibkr_connector

# Verify structure
tree packages/ -L 3
```

#### Step 3: Create baseline test results (before refactor)

```bash
# Save current behavior for comparison
./tools/rebalance.sh -account DUM959247 > baseline_preview.log 2>&1
./tools/rebalance.sh -account DUM959247 -exec rebalance > baseline_execute.log 2>&1
pip list > baseline_packages.txt

echo "✅ Baseline tests saved"
```

---

### Task 1.1: Create broker-connector-base package

**Location:** `packages/broker-connector-base/`

**Files to create:**

```python
# packages/broker-connector-base/setup.py
from setuptools import setup, find_packages

setup(
    name="broker-connector-base",
    version="1.0.0",
    author="IBKR Portfolio Rebalancer Team",
    description="Abstract base classes for broker connectors",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "broker_connector_base": ["py.typed"],
    },
    install_requires=[
        "pydantic==2.11.7",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/broker-connector-base/src/broker_connector_base/__init__.py
from .base_client import BrokerClient
from .base_rebalancer import BaseRebalancer
from .models import (
    # Core trading models
    Trade,
    AllocationItem,
    AccountConfig,
    # Market data models
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    # Order models
    OrderResult,
    OpenOrder,
    OrderStatus,
    # Rebalancing result models
    RebalanceResult,
    CalculateRebalanceResult,
)
from .exceptions import (
    BrokerConnectionError,
    BrokerAPIError,
    OrderExecutionError,
)

__version__ = "1.0.0"  # Package version for logging and debugging

__all__ = [
    "BrokerClient",
    "BaseRebalancer",
    # Core trading models
    "Trade",
    "AllocationItem",
    "AccountConfig",
    # Market data models
    "AccountSnapshot",
    "AccountPosition",
    "ContractPrice",
    # Order models
    "OrderResult",
    "OpenOrder",
    "OrderStatus",
    # Rebalancing result models
    "RebalanceResult",
    "CalculateRebalanceResult",
    # Exceptions
    "BrokerConnectionError",
    "BrokerAPIError",
    "OrderExecutionError",
    # Version
    "__version__",
]
```

```python
# packages/broker-connector-base/src/broker_connector_base/base_client.py
from abc import ABC, abstractmethod
from typing import List, Optional
from .models import AccountSnapshot, ContractPrice, OrderResult, OpenOrder

class BrokerClient(ABC):
    """Abstract base class for broker API clients"""

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to broker.

        Returns:
            True if connection successful, False otherwise
        """
        pass

    @abstractmethod
    async def disconnect(self):
        """Close connection to broker"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if connected to broker.

        Returns:
            True if connected, False otherwise
        """
        pass

    @abstractmethod
    async def get_account_snapshot(
        self,
        account_id: str,
        use_cached_prices: bool = False
    ) -> AccountSnapshot:
        """
        Get account positions, balances, and values.

        Args:
            account_id: Broker account ID
            use_cached_prices: Use cached prices if available (within TTL)

        Returns:
            AccountSnapshot with positions and balances
        """
        pass

    @abstractmethod
    async def get_multiple_market_prices(
        self,
        symbols: List[str],
        use_cache: bool = False
    ) -> List[ContractPrice]:
        """
        Get current market prices for multiple symbols.

        Args:
            symbols: List of ticker symbols
            use_cache: Use cached prices if available (within TTL)

        Returns:
            List of ContractPrice objects
        """
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
        """
        Place a trade order.

        Args:
            account_id: Broker account ID
            symbol: Ticker symbol
            quantity: Number of shares (negative for sell)
            order_type: 'MARKET' or 'LIMIT'
            price: Limit price (required for LIMIT orders)

        Returns:
            OrderResult with order ID and status
        """
        pass

    @abstractmethod
    async def get_open_orders(self, account_id: str) -> List[OpenOrder]:
        """
        Get all open orders for account.

        Args:
            account_id: Broker account ID

        Returns:
            List of OpenOrder objects
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str):
        """
        Cancel an order.

        Args:
            order_id: Order identifier (string to support both int and UUID)
        """
        pass

    @abstractmethod
    async def get_order_status(self, order_id: str) -> str:
        """
        Get current status of an order.

        Args:
            order_id: Order identifier (string to support both int and UUID)

        Returns:
            Normalized order status string (see OrderStatus constants)
        """
        pass
```

```python
# packages/broker-connector-base/src/broker_connector_base/models.py
from typing import List, Optional, Literal
from pydantic import BaseModel, Field

# ============================================================================
# CORE TRADING MODELS (broker-agnostic)
# ============================================================================

class Trade(BaseModel):
    """
    Standardized trade representation across all brokers.
    ⚠️ CRITICAL: This is in broker-connector-base to avoid circular dependencies.
    ⚠️ CRITICAL: order_id is str (not int) to support both IBKR integers and future UUID-based brokers.
    """
    symbol: str
    quantity: int
    current_shares: float
    target_value: float
    current_value: float
    price: float  # Execution price: ask*1.005 (rounded) for buys, bid for sells
    order_type: str = 'MARKET'
    order_id: Optional[str] = None  # ⚠️ STRING type: IBKR converts int↔str, Schwab uses UUID

class AllocationItem(BaseModel):
    """Standardized allocation item across all brokers"""
    symbol: str
    allocation: float

class AccountConfig(BaseModel):
    """
    Broker-agnostic account configuration.
    ⚠️ CRITICAL: This is in broker-connector-base to avoid circular dependencies.
    """
    account_id: str
    type: Literal['paper', 'live']
    enabled: bool = True
    strategy_name: str
    cash_reserve_percent: float = 1.0
    replacement_set: Optional[str] = None
    pdt_protection_enabled: bool = False
    broker: str = 'ibkr'  # Default to IBKR for backward compatibility

# ============================================================================
# MARKET DATA MODELS
# ============================================================================

class ContractPrice(BaseModel):
    """Standardized price data across brokers"""
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

# ============================================================================
# ORDER MODELS
# ============================================================================

class OrderResult(BaseModel):
    """Standardized order placement result"""
    order_id: str  # String to support both int (IBKR) and UUID (Schwab)
    symbol: str
    quantity: int
    status: str

class OpenOrder(BaseModel):
    """Standardized open order"""
    order_id: str  # String to support both int (IBKR) and UUID (Schwab)
    symbol: str
    quantity: int
    status: str
    order_type: str

class OrderStatus:
    """Normalized order statuses across brokers"""
    FILLED = "FILLED"
    WORKING = "WORKING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"

# ============================================================================
# REBALANCING RESULT MODELS
# ============================================================================

class RebalanceResult(BaseModel):
    """Result of rebalance operation - broker-agnostic interface"""
    orders: List[Trade]  # List of Trade objects
    total_value: float
    cash_balance: Optional[float] = None
    success: bool
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

class CalculateRebalanceResult(BaseModel):
    """Result of rebalance calculation (preview) - broker-agnostic interface"""
    proposed_trades: List[Trade]  # List of Trade objects
    current_value: float
    success: bool
    warnings: List[str] = Field(default_factory=list)
```

```python
# packages/broker-connector-base/src/broker_connector_base/exceptions.py
class BrokerConnectionError(Exception):
    """Raised when broker connection fails"""
    pass

class BrokerAPIError(Exception):
    """Raised when broker API returns an error"""
    pass

class OrderExecutionError(Exception):
    """Raised when order execution fails"""
    pass
```

```python
# packages/broker-connector-base/src/broker_connector_base/base_rebalancer.py
from abc import ABC, abstractmethod
from typing import Optional
import logging
from .base_client import BrokerClient
from .models import RebalanceResult, CalculateRebalanceResult

class BaseRebalancer(ABC):
    """Base rebalancer class with common functionality and interface contract"""

    def __init__(self, broker_client: BrokerClient, logger: Optional[logging.Logger] = None):
        self.broker = broker_client
        self.logger = logger or logging.getLogger(__name__)

    @abstractmethod
    async def rebalance_account(self, account_config) -> RebalanceResult:
        """
        Execute live rebalancing for account.

        Args:
            account_config: AccountConfig object with account settings

        Returns:
            RebalanceResult with executed trades and status
        """
        pass

    @abstractmethod
    async def calculate_rebalance(self, account_config) -> CalculateRebalanceResult:
        """
        Calculate rebalance without executing (preview mode).

        Args:
            account_config: AccountConfig object with account settings

        Returns:
            CalculateRebalanceResult with proposed trades
        """
        pass
```

```
# packages/broker-connector-base/src/broker_connector_base/py.typed
# Marker file for PEP 561 type checking support
```

---

### Task 1.2: Create rebalance-calculator package

**Location:** `packages/rebalance-calculator/`

**Files to create:**

```python
# packages/rebalance-calculator/setup.py
from setuptools import setup, find_packages

setup(
    name="rebalance-calculator",
    version="1.0.0",
    author="IBKR Portfolio Rebalancer Team",
    description="Broker-agnostic rebalancing calculation engine",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "rebalance_calculator": ["py.typed"],
    },
    install_requires=[
        "pydantic==2.11.7",
        "broker-connector-base==1.0.0",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/rebalance-calculator/src/rebalance_calculator/__init__.py
from .calculator import TradeCalculator
from .models import TradeCalculationResult

# Re-export commonly used models from broker-connector-base for convenience
from broker_connector_base import Trade, AllocationItem, AccountConfig

__version__ = "1.0.0"  # Package version for logging and debugging

__all__ = [
    "TradeCalculator",
    "TradeCalculationResult",
    # Re-exported from broker-connector-base
    "Trade",
    "AllocationItem",
    "AccountConfig",
    # Version
    "__version__",
]
```

**Copy from existing:**
- Copy `event-broker/app/trading/trade_calculator.py` → `packages/rebalance-calculator/src/rebalance_calculator/calculator.py`
- Extract `TradeCalculationResult` from `event-broker/app/models.py` → `packages/rebalance-calculator/src/rebalance_calculator/models.py`

**⚠️ CRITICAL: Models are now organized to avoid circular dependencies:**
- `Trade`, `AllocationItem`, `AccountConfig` → **broker-connector-base** (shared foundation)
- `TradeCalculationResult` → **rebalance-calculator** (calculator-specific)

**Update imports in calculator.py:**
```python
# OLD
from app.models import AccountSnapshot, AllocationItem, AccountConfig, Trade, ContractPrice, TradeCalculationResult

# NEW
from broker_connector_base import AccountSnapshot, ContractPrice, Trade, AllocationItem, AccountConfig
from .models import TradeCalculationResult
```

**Models file:**
```python
# packages/rebalance-calculator/src/rebalance_calculator/models.py
from typing import List
from pydantic import BaseModel, Field
from broker_connector_base import Trade

class TradeCalculationResult(BaseModel):
    """Result of trade calculation with warnings"""
    trades: List[Trade]
    warnings: List[str] = Field(default_factory=list)
```

```
# packages/rebalance-calculator/src/rebalance_calculator/py.typed
# Marker file for PEP 561 type checking support
```

---

### Task 1.3: Create ibkr-connector package

**Location:** `packages/ibkr-connector/`

**Files to create:**

```python
# packages/ibkr-connector/setup.py
from setuptools import setup, find_packages

setup(
    name="ibkr-connector",
    version="1.0.0",
    author="IBKR Portfolio Rebalancer Team",
    description="Interactive Brokers connector implementation",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "ibkr_connector": ["py.typed"],
    },
    install_requires=[
        "broker-connector-base==1.0.0",
        "rebalance-calculator==1.0.0",
        "ib-async==2.0.1",
        "pydantic==2.11.7",
        "aiohttp==3.12.15",
        "PyYAML==6.0.2",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/ibkr-connector/src/ibkr_connector/__init__.py
from .client import IBKRClient
from .rebalancer import IBKRRebalancer
from .models import CachedPrice

__version__ = "1.0.0"  # Package version for logging and debugging

__all__ = [
    "IBKRClient",
    "IBKRRebalancer",
    "CachedPrice",
    "__version__",
]
```

**Copy from existing:**
- Copy `event-broker/app/trading/ibkr_client.py` → `packages/ibkr-connector/src/ibkr_connector/client.py`
- Copy `event-broker/app/trading/rebalancer.py` → `packages/ibkr-connector/src/ibkr_connector/rebalancer.py`
- Copy `event-broker/app/trading/allocation_service.py` → `packages/ibkr-connector/src/ibkr_connector/allocation_service.py`
- Copy `event-broker/app/trading/replacement_service.py` → `packages/ibkr-connector/src/ibkr_connector/replacement_service.py`

**Update imports:**

```python
# In client.py
# OLD
from app.models import AccountSnapshot, AccountPosition, OrderResult, OpenOrder, ContractPrice, CachedPrice

# NEW
import broker_connector_base
from broker_connector_base import (
    BrokerClient,
    AccountSnapshot,
    AccountPosition,
    OrderResult,
    OpenOrder,
    ContractPrice,
    BrokerConnectionError,
)
from .models import CachedPrice

# Make IBKRClient inherit from BrokerClient
class IBKRClient(BrokerClient):
    """IBKR implementation of BrokerClient interface"""

    def __init__(self, client_id: int, logger: Optional[logging.Logger] = None):
        # ... existing initialization ...
        
        # Log package versions for debugging
        self.logger.info(
            f"Initializing IBKRClient with broker-connector-base v{broker_connector_base.__version__}"
        )

    # ⚠️ CRITICAL: Complete order_id string conversion for all methods
    async def place_order(self, account_id: str, symbol: str, quantity: int,
                         order_type: str = 'MARKET', price: Optional[float] = None) -> OrderResult:
        """
        Place an order.
        
        Returns:
            OrderResult with order_id as STRING (converted from IBKR's int)
        """
        try:
            # ... existing implementation until return ...
            
            return OrderResult(
                order_id=str(trade.order.orderId),  # ⚠️ Convert int to string
                symbol=symbol,
                quantity=quantity,
                status=trade.orderStatus.status
            )
        except Exception as e:
            self.logger.error(f"Failed to place order for {symbol}: {e}")
            raise

    async def cancel_order(self, order_id: str):  # ⚠️ Changed from int
        """Cancel an order by order_id (string converted to int for IBKR API)"""
        try:
            order_id_int = int(order_id)  # ⚠️ Convert string back to int for IBKR API
        except ValueError:
            self.logger.error(f"Invalid order_id format for IBKR: {order_id}")
            raise ValueError(f"Order ID must be numeric for IBKR, got: {order_id}")

        try:
            # ... rest of implementation using order_id_int
            trades = self.ib.trades()
            for trade in trades:
                if trade.order.orderId == order_id_int:
                    self.ib.cancelOrder(trade.order)
                    self.logger.info(f"Cancelled order {order_id}")
                    return

            self.logger.warning(f"Order {order_id} not found")
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")
            raise

    async def get_order_status(self, order_id: str) -> str:  # ⚠️ Changed from int
        """Get status of an order by order_id (string converted to int for IBKR API)"""
        try:
            order_id_int = int(order_id)  # ⚠️ Convert string back to int for IBKR API
        except ValueError:
            self.logger.error(f"Invalid order_id format for IBKR: {order_id}")
            return 'ERROR'

        try:
            # ... rest of implementation using order_id_int
            trades = self.ib.trades()
            for trade in trades:
                if trade.order.orderId == order_id_int:
                    return trade.orderStatus.status
            return 'NOT_FOUND'
        except Exception as e:
            self.logger.error(f"Failed to get order status for {order_id}: {e}")
            return 'ERROR'
```

```python
# In rebalancer.py
# OLD
from app.models import AccountConfig, AccountSnapshot, AllocationItem, Trade, RebalanceResult, CalculateRebalanceResult

# NEW
try:
    from broker_connector_base import (
        BaseRebalancer,
        AccountSnapshot,
        RebalanceResult,
        CalculateRebalanceResult,
        Trade,
        AllocationItem,
        AccountConfig,
    )
    from rebalance_calculator import TradeCalculator
    from .allocation_service import AllocationService
    from .replacement_service import ReplacementService
except ImportError as e:
    raise ImportError(
        f"Failed to import required packages: {e}. "
        "Ensure packages are installed: pip install -e packages/broker-connector-base "
        "&& pip install -e packages/rebalance-calculator"
    )

class IBKRRebalancer(BaseRebalancer):
    # ... existing implementation - no changes needed beyond imports
```

```python
# In allocation_service.py
# OLD
from app.models import AllocationItem, AccountConfig

# NEW
from broker_connector_base import AllocationItem, AccountConfig

# Rest of file remains the same
```

```python
# In replacement_service.py
# OLD
from app.models import AllocationItem

# NEW
from broker_connector_base import AllocationItem

# Rest of file remains the same
```

```python
# packages/ibkr-connector/src/ibkr_connector/models.py
from pydantic import BaseModel
from datetime import datetime
from broker_connector_base import ContractPrice

class CachedPrice(BaseModel):
    """Cached price data with timestamp for TTL validation"""
    price: ContractPrice
    cached_at: datetime
```

```
# packages/ibkr-connector/src/ibkr_connector/py.typed
# Marker file for PEP 561 type checking support
```

---

## Phase 2: Update Main Application (Day 2-3)

### Task 2.1: Update Dockerfile to install local packages

**⚠️ CRITICAL CHANGE: This is the most important update to the Dockerfile**

```dockerfile
# event-broker/Dockerfile

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# ⚠️ CRITICAL: Copy local packages first (before app code)
COPY packages/broker-connector-base /tmp/broker-connector-base
COPY packages/rebalance-calculator /tmp/rebalance-calculator
COPY packages/ibkr-connector /tmp/ibkr-connector

# ⚠️ CRITICAL: Install local packages in dependency order
# Base → Calculator → Connector
RUN pip install --no-cache-dir /tmp/broker-connector-base && \
    pip install --no-cache-dir /tmp/rebalance-calculator && \
    pip install --no-cache-dir /tmp/ibkr-connector && \
    rm -rf /tmp/broker-connector-base /tmp/rebalance-calculator /tmp/ibkr-connector

# Verify package installation (helpful for debugging)
RUN pip list | grep -E "(broker|rebalance|ibkr)"

# Copy application code
COPY event-broker/app /app/app
COPY event-broker/requirements.txt /app/

# Install remaining application dependencies (ably, tzdata, etc.)
RUN pip install --no-cache-dir -r requirements.txt

# Set environment
ENV PYTHONPATH=/app
ENV TZ=America/New_York

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import asyncio; from app.main import app; print('healthy' if asyncio.run(app.get_status()).get('running', False) else 'unhealthy')" || exit 1

CMD ["python", "-m", "app.main"]
```

**⚠️ IMPORTANT: Ensure docker-compose.yaml volume mounts remain:**
```yaml
volumes:
  - ./accounts.yaml:/app/accounts.yaml:ro
  - ./replacement-sets.yaml:/app/replacement-sets.yaml:ro
  - ./data:/app/data
```

**⚠️ CRITICAL: Create .dockerignore file (currently missing):**
```bash
# .dockerignore - Place in project root
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.so
*.egg
*.egg-info/
dist/
build/
.pytest_cache/
.coverage
.env
.git/
.gitignore
*.md
!CLAUDE.md
node_modules/
.vscode/
.idea/
.DS_Store

# ⚠️ CRITICAL: Do NOT exclude packages/ - we need it in Docker build
# packages/  # ← DO NOT ADD THIS LINE
```

---

### Task 2.2: Update requirements.txt

```txt
# event-broker/requirements.txt

# ⚠️ IMPORTANT: Local packages (broker-connector-base, rebalance-calculator, 
# ibkr-connector) are installed via Dockerfile COPY + pip install steps.
# Only list event-broker-specific external dependencies here.

ably==2.0.12
tzdata==2025.2

# ⚠️ NOTE: The following are now installed via ibkr-connector package:
#   - pydantic==2.11.7
#   - aiohttp==3.12.15
#   - PyYAML==6.0.2
#   - ib-async==2.0.1
# Do NOT list them here to avoid version conflicts.
```

---

### Task 2.3: Create broker factory

```python
# event-broker/app/trading/broker_factory.py
"""Factory for creating broker clients"""

import os
import logging
from typing import Optional

try:
    from broker_connector_base import BrokerClient, AccountConfig
    from ibkr_connector import IBKRClient
except ImportError as e:
    raise ImportError(
        f"Failed to import broker packages: {e}. "
        "Ensure packages are installed via Dockerfile or locally: "
        "pip install -e packages/broker-connector-base && "
        "pip install -e packages/ibkr-connector"
    )

def create_broker_client(
    account_config: AccountConfig,
    client_id: int,
    logger: Optional[logging.Logger] = None
) -> BrokerClient:
    """
    Factory to create appropriate broker client based on account configuration.

    Args:
        account_config: Account configuration
        client_id: Unique client ID for this connection
        logger: Optional logger instance

    Returns:
        BrokerClient instance

    Raises:
        ValueError: If broker type is not supported
    """
    # Get broker type from account config (defaults to 'ibkr')
    broker = account_config.broker.lower()

    if logger:
        logger.debug(f"Creating {broker} broker client with client_id={client_id}")

    if broker == 'ibkr':
        return IBKRClient(client_id=client_id, logger=logger)
    else:
        raise ValueError(
            f"Unsupported broker: {broker}. "
            f"Supported brokers: ibkr. "
            f"To add {broker} support, implement a {broker}-connector package."
        )
```

---

### Task 2.4: Update trading_executor.py

```python
# event-broker/app/services/trading_executor.py

# OLD imports to REMOVE:
# from app.trading.ibkr_client import IBKRClient
# from app.trading.rebalancer import Rebalancer

# NEW imports to ADD:
from broker_connector_base import AccountConfig
from ibkr_connector import IBKRRebalancer
# Note: broker_factory imported inside context manager (see managed_broker_client)

# Update process_single_account function
async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process a single account with dedicated broker client"""

    account_config = AccountConfig(**account)
    account_id = account_config.account_id
    strategy_name = account_config.strategy_name

    async with account_logger_context(account_id, strategy_name) as logger:
        from app.services.pdt_protection_service import PDTProtectionService
        pdt_service = PDTProtectionService(logger=logger)

        # PDT Protection Pre-Check
        if account_config.pdt_protection_enabled and event_data.get('exec') == 'rebalance':
            check_result = pdt_service.is_execution_allowed(account_id)

            if not check_result.allowed:
                error_msg = (
                    f"PDT Protection: Account {account_id} was already rebalanced earlier today "
                    f"so it was skipped to protect against PDT. "
                    f"You can manually rebalance after {check_result.next_allowed_time}."
                )
                logger.warning(error_msg)
                raise Exception(error_msg)

        manager = SubprocessManager()

        # Create broker client using factory
        async with manager.managed_broker_client(account_config, client_id, logger) as broker_client:
            # ⚠️ NOTE: No sys.path.append needed for packages (installed via pip)
            # sys.path.append only needed for app.* imports (inside managed_broker_client)

            # Import rebalancer dynamically based on broker type
            try:
                if account_config.broker.lower() == 'ibkr':
                    from ibkr_connector import IBKRRebalancer
                    rebalancer = IBKRRebalancer(broker_client, logger=logger)
                else:
                    raise ValueError(f"Unsupported broker: {account_config.broker}")
            except ImportError as e:
                logger.error(f"Failed to import rebalancer: {e}")
                raise ImportError(
                    f"Failed to import {account_config.broker} rebalancer. "
                    f"Ensure ibkr-connector package is installed."
                )

            if event_data.get('exec') == 'rebalance':
                logger.info("Executing LIVE rebalance")
                result = await rebalancer.rebalance_account(account_config)

                if not result.success:
                    logger.error(f"Rebalance failed: {result.error}")
                    raise Exception(f"Rebalance failed: {result.error}")

                logger.info(f"Rebalance completed: {len(result.orders)} trades executed")

                # PDT Protection Post-Success
                pdt_service.record_execution(account_id)

                return {
                    'success': True,
                    'action': 'rebalance',
                    'trades_executed': len(result.orders),
                    'total_value': result.total_value,
                    'cash_balance': result.cash_balance,
                    'warnings': result.warnings
                }

            elif event_data.get('exec') == 'print-rebalance':
                logger.info("Calculating rebalance (preview mode)")
                result = await rebalancer.calculate_rebalance(account_config)

                proposed_trades = result.proposed_trades
                logger.info(f"Preview calculated: {len(proposed_trades)} proposed trades")

                if proposed_trades:
                    logger.info("=== PROPOSED TRADES ===")
                    for trade in proposed_trades:
                        action = "BUY" if trade.quantity > 0 else "SELL"
                        logger.info(f"{action} {abs(trade.quantity)} shares of {trade.symbol} @ ${trade.price}")
                    logger.info("=====================")
                else:
                    logger.info("No trades required - portfolio is already balanced")

                return {
                    'success': True,
                    'action': 'print-rebalance',
                    'proposed_trades': len(proposed_trades),
                    'current_value': result.current_value,
                    'trades_detail': [trade.model_dump() for trade in proposed_trades],
                    'warnings': result.warnings
                }
            else:
                raise ValueError(f"Unknown command: {event_data.get('exec')}")


# Update SubprocessManager.managed_broker_client
class SubprocessManager:
    """Manages subprocess lifecycle and cleanup"""

    def __init__(self):
        self.active_connections = {}

        # Register cleanup handlers
        atexit.register(self._cleanup_all)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    @asynccontextmanager
    async def managed_broker_client(self, account_config: AccountConfig, client_id: int, logger):
        """Context manager for broker client with guaranteed cleanup"""

        broker_client = None
        try:
            # ⚠️ sys.path.append ONLY needed for app.* imports (not for packages)
            # Packages (broker-connector-base, ibkr-connector, etc.) are installed via pip
            sys.path.append('/app')
            from app.trading.broker_factory import create_broker_client

            logger.info(f"Creating broker client with ID {client_id}")
            broker_client = create_broker_client(
                account_config=account_config,
                client_id=client_id,
                logger=logger
            )

            # Track active connection
            account_id = account_config.account_id
            self.active_connections[account_id] = broker_client

            # Connect
            logger.info("Establishing broker connection")
            connected = await broker_client.connect()

            if not connected:
                raise ConnectionError("Failed to connect to broker")

            logger.info("Broker connection established successfully")
            yield broker_client

        except Exception as e:
            logger.error(f"Broker client error: {e}")
            raise

        finally:
            # Guaranteed cleanup
            if broker_client:
                try:
                    if broker_client.is_connected():
                        logger.info("Cleaning up broker connection")
                        await broker_client.disconnect()
                        logger.info("Broker connection closed successfully")
                except Exception as e:
                    logger.error(f"Error during cleanup: {e}")
                finally:
                    # Remove from tracking
                    if account_config:
                        self.active_connections.pop(account_config.account_id, None)

    def _cleanup_all(self):
        """Emergency cleanup of all connections"""
        for account_id, client in self.active_connections.items():
            try:
                if client.is_connected():
                    # For IBKR, access underlying ib object
                    if hasattr(client, 'ib'):
                        client.ib.disconnect()
                    print(f"Emergency cleanup: Disconnected {account_id}")
            except Exception as e:
                print(f"Emergency cleanup failed for {account_id}: {e}")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"Received signal {signum}, initiating cleanup...")
        self._cleanup_all()
        sys.exit(0)
```

---

### Task 2.5: Update models.py

```python
# event-broker/app/models.py

# Remove models that are now in packages
# Keep only app-specific models

from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field

# Import from packages
from broker_connector_base import (
    # Core trading models
    Trade,
    AllocationItem,
    AccountConfig,
    # Market data models
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    # Order models
    OrderResult,
    OpenOrder,
    # Rebalancing result models
    RebalanceResult,
    CalculateRebalanceResult,
)
from rebalance_calculator import (
    TradeCalculationResult,
)

# Keep these app-specific models
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
```

---

### Task 2.6: Remove old files

After verifying everything works, delete these files:
- `event-broker/app/trading/ibkr_client.py` ❌
- `event-broker/app/trading/rebalancer.py` ❌
- `event-broker/app/trading/trade_calculator.py` ❌
- `event-broker/app/trading/allocation_service.py` ❌
- `event-broker/app/trading/replacement_service.py` ❌
- `event-broker/app/trading/__init__.py` (if now empty)

Keep these files:
- `event-broker/app/trading/broker_factory.py` ✅ (new)

---

## Phase 3: Testing & Validation (Day 4-5)

### Task 3.0: Baseline Testing (BEFORE starting refactor)

**⚠️ CRITICAL: Create baseline before making any changes**

```bash
# Save current behavior as baseline for comparison
./tools/rebalance.sh -account DUM959247 > baseline_preview.log 2>&1
./tools/rebalance.sh -account DUM959247 -exec rebalance > baseline_execute.log 2>&1

# Save package versions
pip list > baseline_packages.txt

# After refactor, compare outputs to ensure identical behavior
diff baseline_preview.log refactored_preview.log
diff baseline_execute.log refactored_execute.log
```

---

### Task 3.1: Local development testing

```bash
# Install packages in development mode (editable)
cd packages/broker-connector-base
pip install -e .

cd ../rebalance-calculator
pip install -e .

cd ../ibkr-connector
pip install -e .

# Verify installations
pip list | grep -E "(broker|rebalance|ibkr)"

# Expected output:
# broker-connector-base    1.0.0    /path/to/packages/broker-connector-base
# rebalance-calculator     1.0.0    /path/to/packages/rebalance-calculator
# ibkr-connector          1.0.0    /path/to/packages/ibkr-connector

# Run the application
cd ../../event-broker
python -m app.main
```

---

### Task 3.2: Docker build testing

```bash
# Build the new Docker image with verbose output
docker-compose build --progress=plain event-broker

# Check for build errors
docker-compose logs event-broker

# Verify packages are installed in container
docker-compose exec event-broker pip list | grep -E "(broker|rebalance|ibkr)"

# Verify package versions are accessible
docker-compose exec event-broker python -c "
import broker_connector_base
import rebalance_calculator
import ibkr_connector
print(f'broker-connector-base: v{broker_connector_base.__version__}')
print(f'rebalance-calculator: v{rebalance_calculator.__version__}')
print(f'ibkr-connector: v{ibkr_connector.__version__}')
"
```

Expected output:
```
broker-connector-base    1.0.0
rebalance-calculator     1.0.0
ibkr-connector          1.0.0

broker-connector-base: v1.0.0
rebalance-calculator: v1.0.0
ibkr-connector: v1.0.0
```

**⚠️ If packages are missing:**
```bash
# Check if .dockerignore is excluding packages/
cat .dockerignore

# Rebuild with --no-cache to ensure clean build
docker-compose build --no-cache event-broker
```

---

### Task 3.3: Integration testing checklist

#### Test 1: Paper Account Manual Rebalance (Preview)
```bash
./tools/rebalance.sh -account DUM959247
```

**Expected:**
- ✅ Connection to IBKR successful
- ✅ Account snapshot retrieved
- ✅ Target allocations fetched
- ✅ Proposed trades calculated
- ✅ Preview displayed in logs
- ✅ No errors

---

#### Test 2: Paper Account Manual Rebalance (Execute)
```bash
./tools/rebalance.sh -account DUM959247 -exec rebalance
```

**Expected:**
- ✅ Trades executed successfully
- ✅ Order status tracked
- ✅ Final snapshot shows updated positions
- ✅ No errors

---

#### Test 3: Ably Event Processing
**Trigger:** Wait for real-time strategy event from Ably

**Expected:**
- ✅ Event received and parsed
- ✅ Accounts grouped by strategy
- ✅ Subprocess spawned successfully
- ✅ All accounts in strategy rebalanced
- ✅ Notifications sent
- ✅ No errors

---

#### Test 4: PDT Protection
**Setup:** Manually trigger rebalance twice for same account

```bash
./tools/rebalance.sh -account DUM959247 -exec rebalance
# Wait a few seconds
./tools/rebalance.sh -account DUM959247 -exec rebalance
```

**Expected:**
- ✅ First rebalance succeeds
- ✅ Second rebalance blocked with PDT message
- ✅ Error logged correctly

---

#### Test 5: Error Handling
**Setup:** Use invalid account ID

```bash
./tools/rebalance.sh -account INVALID123
```

**Expected:**
- ✅ Error caught gracefully
- ✅ Helpful error message
- ✅ No crash

---

#### Test 6: Price Caching
**Setup:** Run preview twice quickly

```bash
./tools/rebalance.sh -account DUM959247
sleep 5
./tools/rebalance.sh -account DUM959247
```

**Expected:**
- ✅ Second run uses cached prices
- ✅ Log shows "Using cached price for..." messages
- ✅ Faster execution time

---

#### Test 7: Parallel Strategy Execution (NEW)
**Setup:** Trigger two accounts from different strategies simultaneously

```bash
# Start both rebalances in parallel
./tools/rebalance.sh -account DUM959247 &  # etf-blend-301-20
./tools/rebalance.sh -account DU789012 &   # etf-blend-100-20 (if enabled)
wait
```

**Expected:**
- ✅ Both execute in parallel without collision
- ✅ Distinct client IDs used (extracted from account IDs)
- ✅ No connection conflicts
- ✅ Both complete successfully

---

### Task 3.4: Regression testing

Compare before/after behavior:

| Test Case | Before | After | Status |
|-----------|--------|-------|--------|
| Preview trades | Works | Works | ✅ |
| Execute trades | Works | Works | ✅ |
| PDT protection | Works | Works | ✅ |
| Multi-account parallel | Works | Works | ✅ |
| Ably events | Works | Works | ✅ |
| Manual events | Works | Works | ✅ |
| Notifications | Works | Works | ✅ |
| Log format | Same | Same | ✅ |
| Order ID handling | int | str (transparent) | ✅ |

---

## Phase 4: Production Deployment (Day 6)

### Task 4.1: Pre-deployment checklist

- [ ] All tests passing
- [ ] Docker build successful
- [ ] No errors in logs
- [ ] Code review completed
- [ ] Git branch created and pushed
- [ ] Backup of current production state
- [ ] Config files (accounts.yaml, replacement-sets.yaml) volume mounts verified

---

### Task 4.2: Deployment steps

```bash
# 1. Stop current services
docker-compose down

# 2. Pull latest code
git pull origin feat/package-refactor

# 3. Rebuild images
docker-compose build

# 4. Start services
docker-compose up -d

# 5. Monitor logs
docker-compose logs -f event-broker

# 6. Verify packages installed
docker-compose exec event-broker pip list | grep -E "(broker|rebalance|ibkr)"

# 7. Verify connection
docker-compose exec event-broker python -c "
from ibkr_connector import IBKRClient
import asyncio
client = IBKRClient(client_id=9999)
print('Import successful')
"
```

---

### Task 4.3: Post-deployment validation

```bash
# Test with paper account first
./tools/rebalance.sh -account DUM959247

# Monitor logs for 10 minutes
docker-compose logs -f event-broker

# Check for errors
docker-compose logs event-broker | grep -i error

# Verify Ably connection
docker-compose logs event-broker | grep -i "ably"
```

**Success criteria:**
- ✅ No errors in logs
- ✅ Manual rebalance works
- ✅ Ably events processed
- ✅ Notifications sent
- ✅ All services healthy

---

## Phase 5: Documentation & Cleanup (Day 7)

### Task 5.0: Progressive CLAUDE.md Updates (Throughout Refactor)

**⚠️ NOTE: Update CLAUDE.md incrementally, not all at once at the end**

#### After Phase 1 (Package Creation):
Add to CLAUDE.md:
```markdown
## Package Architecture (In Progress)

The system is being refactored into modular packages:

### Local Packages (in `packages/`)

1. **broker-connector-base**: Abstract interface for broker connectors (✅ Created)
   - Status: Package scaffolding complete
   - Location: `packages/broker-connector-base/`

2. **rebalance-calculator**: Pure calculation engine (✅ Created)
   - Status: Package scaffolding complete
   - Location: `packages/rebalance-calculator/`

3. **ibkr-connector**: IBKR-specific implementation (✅ Created)
   - Status: Package scaffolding complete
   - Location: `packages/ibkr-connector/`
```

#### After Phase 2 (Main App Updates):
Update CLAUDE.md to add:
```markdown
### Package Integration Status

- ✅ Dockerfile updated to install local packages
- ✅ broker_factory.py created
- ✅ trading_executor.py updated to use packages
- ⚠️ Testing in progress

### Development Workflow (Updated)

**Working with Local Packages:**
```bash
# Install packages in editable mode
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector
```
```

#### After Phase 3 (Testing Complete):
Update CLAUDE.md to add:
```markdown
### Testing Status

- ✅ All 7 integration tests passing
- ✅ Docker build verified
- ✅ Package imports working correctly
- ✅ Order ID string conversion validated
```

#### After Phase 4 (Production Deployment):
Complete CLAUDE.md update (see Task 5.1 below)

---

### Task 5.1: Final CLAUDE.md Update

Add comprehensive architecture section:

```markdown
## Package Architecture

The system is now split into modular packages:

### Local Packages (in `packages/`)

1. **broker-connector-base**: Abstract interface for broker connectors
   - Defines `BrokerClient` base class with async methods
   - Defines `BaseRebalancer` with required abstract methods
   - Common models: `AccountSnapshot`, `ContractPrice`, `RebalanceResult`, etc.
   - Exception types
   - Broker-agnostic interface contracts

2. **rebalance-calculator**: Pure calculation engine (broker-agnostic)
   - `TradeCalculator`: Calculates trades based on allocations
   - Two-phase execution (sells, then buys)
   - Cash constraint handling
   - No broker-specific logic

3. **ibkr-connector**: IBKR-specific implementation
   - `IBKRClient`: Implements `BrokerClient` for IBKR
   - `IBKRRebalancer`: Rebalancing logic using IBKR client
   - `AllocationService`: Fetches strategy allocations from API
   - `ReplacementService`: Handles IRA ETF replacements
   - IBKR-specific models (CachedPrice)

### Main Application

The event-broker uses these packages via:
- `broker_factory.py`: Creates broker clients based on account config
- `trading_executor.py`: Uses rebalancer from connector packages
- Imports models from packages instead of local definitions

### Order ID Handling

Order IDs are now strings throughout the broker-agnostic interface to support:
- IBKR: integer order IDs (converted to/from string)
- Future brokers (e.g., Schwab): UUID-based order IDs

The conversion is transparent in the IBKR connector.
```

---

### Task 5.2: Update README (if exists)

Add development setup instructions:

```markdown
## Development Setup

### Installing Local Packages

```bash
# Install packages in editable mode for development
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector
```

### Adding a New Broker Connector

1. Create new package in `packages/your-broker-connector/`
2. Implement `BrokerClient` interface from `broker-connector-base`
3. Implement `BaseRebalancer` abstract methods
4. Update `broker_factory.py` to support new broker
5. Optionally add `broker` field to `AccountConfig` in accounts.yaml schema

Example structure:
```
packages/schwab-connector/
├── setup.py
└── src/
    └── schwab_connector/
        ├── __init__.py
        ├── client.py          # implements BrokerClient
        ├── rebalancer.py      # implements BaseRebalancer
        └── models.py          # Schwab-specific models
```
```

---

### Task 5.3: Create migration notes

```markdown
# Migration Notes - Package Refactor

## What Changed

- Codebase split into 3 local packages (broker-connector-base, rebalance-calculator, ibkr-connector)
- IBKR-specific code isolated in ibkr-connector package
- Calculation logic now broker-agnostic in rebalance-calculator
- Order IDs changed from `int` to `str` (transparent conversion in IBKR connector)
- AllocationService and ReplacementService moved to ibkr-connector package
- RebalanceResult and CalculateRebalanceResult moved to broker-connector-base (broker-agnostic interface)

## What Stayed the Same

- All API behavior
- Configuration files (accounts.yaml, replacement-sets.yaml)
- Docker deployment (with same volume mounts)
- Event processing (Ably and manual)
- Logging format
- Notifications
- Subprocess execution model
- Price caching mechanism

## Rollback Plan

If issues arise:
```bash
git checkout main
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

Rollback time: ~5 minutes

## Future Enhancements

- Add optional `broker` field to accounts.yaml (defaults to 'ibkr')
- Publish packages to private PyPI server
- Add Schwab connector package
- Add unit tests for calculator package
- Add integration tests with mocked brokers
```

---

## Risk Mitigation

### Risk 1: Import errors ⚠️ HIGH
**Mitigation:**
- ✅ Add try-except blocks with helpful error messages around all package imports
- ✅ Test imports in Docker before deployment
- ✅ Add package version logging on initialization
- Keep old code in Git for quick rollback
- Use explicit package dependencies in setup.py

**Validation:**
```bash
# Test imports work in container
docker-compose exec event-broker python -c "
from broker_connector_base import BrokerClient
from rebalance_calculator import TradeCalculator
from ibkr_connector import IBKRClient
print('All imports successful')
"
```

### Risk 2: Order ID type mismatch ⚠️ HIGH - FIXED
**Mitigation:**
- ✅ IBKR client converts int ↔ str transparently in ALL methods
- ✅ Trade.order_id changed to Optional[str] in base package
- ✅ Extensive testing of order placement/cancellation/status
- ✅ Validate order status tracking with string IDs
- Add validation tests for order ID conversion

**Validation:**
```python
# Test order_id string handling
order_result = await ibkr.place_order(...)
assert isinstance(order_result.order_id, str)  # Must be string
await ibkr.cancel_order(order_result.order_id)  # Should accept string
```

### Risk 3: Missing dependencies ⚠️ CRITICAL - FIXED
**Mitigation:**
- ✅ Dockerfile installs packages in correct dependency order (base → calculator → connector)
- ✅ Added verification step in Dockerfile to list installed packages
- ✅ Test in local Docker first before production
- Pin exact versions in setup.py (==, not >=)
- Create .dockerignore to ensure packages/ is NOT excluded

**Validation:**
```bash
# Verify all package dependencies are installed
docker-compose exec event-broker pip check
```

### Risk 4: Subprocess import issues ⚠️ MEDIUM - FIXED
**Mitigation:**
- ✅ Packages installed via pip are automatically in Python path
- ✅ sys.path.append('/app') documented as ONLY for app.* imports
- ✅ Add import error handling in subprocess execution
- Test subprocess execution thoroughly with logging
- Add debug mode to show Python path

**Validation:**
```bash
# Test subprocess imports work
docker-compose exec event-broker python -c "
import sys
print('Python path:', sys.path)
from ibkr_connector import IBKRClient
print('IBKRClient import successful in subprocess context')
"
```

### Risk 5: Performance regression ⚠️ LOW
**Mitigation:**
- ✅ Monitor execution times before/after with baseline tests
- Price caching remains unchanged
- Parallel execution unchanged
- Same algorithms, just reorganized
- No additional network calls or I/O

**Validation:**
```bash
# Compare execution times
time ./tools/rebalance.sh -account DUM959247  # Before refactor
time ./tools/rebalance.sh -account DUM959247  # After refactor
# Should be within 5% variance
```

### Risk 6: Config file access ⚠️ LOW
**Mitigation:**
- ✅ Verify docker-compose.yaml volume mounts preserved
- Test that replacement-sets.yaml is accessible from ibkr-connector
- Test that accounts.yaml is accessible from main app
- Volume mounts are at app level, not package level

**Validation:**
```bash
# Test config file access
docker-compose exec event-broker ls -la /app/*.yaml
docker-compose exec event-broker python -c "
import os
print('accounts.yaml exists:', os.path.exists('/app/accounts.yaml'))
print('replacement-sets.yaml exists:', os.path.exists('/app/replacement-sets.yaml'))
"
```

### Risk 7: .dockerignore excluding packages/ ⚠️ CRITICAL - NEW
**Mitigation:**
- ✅ Create .dockerignore file if missing
- ✅ Ensure packages/ directory is NOT excluded
- ✅ Verify COPY commands in Dockerfile succeed
- Test Docker build with verbose output

**Validation:**
```bash
# Verify .dockerignore doesn't exclude packages/
cat .dockerignore | grep -i package
# Should NOT see "packages/" in output

# Test that packages/ is copied into build
docker-compose build --progress=plain event-broker 2>&1 | grep "COPY packages"
```

---

## Success Metrics

### Pre-deployment
- [ ] All unit tests pass (if added)
- [ ] Manual testing successful (all 7 tests)
- [ ] Docker build completes without errors
- [ ] No import errors
- [ ] Package installation verified

### Post-deployment
- [ ] Zero errors in first hour
- [ ] Manual rebalance works
- [ ] Ably events processed
- [ ] Execution time ≤ baseline
- [ ] All accounts rebalance successfully
- [ ] Parallel execution works without conflicts

### Long-term
- [ ] 1 week of stable operation
- [ ] No increase in error rate
- [ ] Performance metrics stable
- [ ] Ready for Schwab connector addition

---

## Timeline Summary

| Phase | Duration | Tasks | CLAUDE.md Update |
|-------|----------|-------|------------------|
| **Phase 1**: Package scaffolding | 1 day | ⚠️ Create .dockerignore FIRST, create 3 packages, copy code, update imports | Add "Package Architecture (In Progress)" section |
| **Phase 2**: Update main app | 2 days | Update imports, factory, Dockerfile, remove old files | Add "Package Integration Status" section |
| **Phase 3**: Testing | 2 days | Baseline comparison, local, Docker, integration tests (7 tests) | Add "Testing Status" section |
| **Phase 4**: Deployment | 1 day | Deploy, monitor, validate | Mark as "✅ Production Ready" |
| **Phase 5**: Documentation | 1 day | Complete CLAUDE.md update, cleanup, final docs | Add comprehensive architecture documentation |
| **Total** | **7 days** | | Progressive updates throughout |

---

## Rollback Procedure

If critical issues arise:

```bash
# 1. Checkout previous version
git checkout main  # or specific commit hash

# 2. Rebuild
docker-compose down
docker-compose build --no-cache

# 3. Restart
docker-compose up -d

# 4. Verify
docker-compose logs -f event-broker
./tools/rebalance.sh -account DUM959247
```

**Rollback decision criteria:**
- Unable to connect to IBKR
- Orders failing systematically
- Data corruption
- Critical errors in logs
- Import errors preventing startup

**Rollback time estimate:** ~5 minutes

---

## Next Steps After Refactor

1. **Add Schwab connector** (following this architecture)
   - Implement BrokerClient interface
   - Implement BaseRebalancer abstract methods
   - Add to broker_factory.py

2. **Publish to PyPI** (optional, private server)
   - Package versioning strategy
   - Release workflow

3. **Add unit tests** for calculator package
   - Test trade calculation logic
   - Test cash constraint handling
   - Mock AccountSnapshot and ContractPrice

4. **Add integration tests** with mocked brokers
   - Mock BrokerClient implementations
   - Test rebalancing workflows

5. **Performance optimization** if needed
   - Profile execution times
   - Optimize price caching
   - Optimize parallel execution

---

## Questions & Answers

**Q: Will this break production?**
A: No. The refactor is transparent - all behavior remains identical. Same code, different organization.

**Q: Can we rollback quickly?**
A: Yes. Git checkout + rebuild takes ~5 minutes.

**Q: Do we need to update accounts.yaml?**
A: No. The broker field is optional and defaults to 'ibkr'. No changes required.

**Q: Will this affect performance?**
A: No. The same code runs with the same algorithms, just organized differently.

**Q: When can we add Schwab?**
A: After this refactor, Schwab connector can be added in ~3-5 days by implementing the BrokerClient interface.

**Q: What about the order_id type change?**
A: Transparent conversion: IBKR uses int internally but exposes string via the interface. Future brokers can use UUID strings natively.

**Q: Why move AllocationService and ReplacementService to ibkr-connector instead of keeping them in main app?**
A: **Design Decision - Keep in ibkr-connector** for these reasons:

1. **Tight Coupling**: These services are tightly coupled to the current IBKR implementation:
   - `AllocationService` fetches from a specific API endpoint structure
   - `ReplacementService` loads `replacement-sets.yaml` (IBKR-specific IRA restrictions)

2. **Future Flexibility**: When adding Schwab connector:
   - Schwab will likely have **different allocation sources** (different API format)
   - Schwab may have **different replacement needs** (different restrictions)
   - Each connector should have its own implementation

3. **Clean Separation**: Keeping them with IBKRRebalancer:
   - Maintains high cohesion (related code together)
   - Avoids premature abstraction
   - Makes IBKR connector fully self-contained

4. **Alternative Considered**: Moving to main app would:
   - Create unnecessary abstraction for single implementation
   - Mix broker-specific logic with orchestration logic
   - Make it harder to evolve per-broker implementations independently

**Recommendation**: Keep in ibkr-connector. When Schwab is added, create `schwab-connector/allocation_service.py` with Schwab-specific implementation.

---

## Appendix A: Pre-Flight Checklist

**⚠️ Complete these steps BEFORE starting the refactor:**

### Pre-Refactor Validation
- [ ] Create baseline tests (Task 3.0)
  - [ ] Run `./tools/rebalance.sh -account DUM959247 > baseline_preview.log 2>&1`
  - [ ] Run `./tools/rebalance.sh -account DUM959247 -exec rebalance > baseline_execute.log 2>&1`
  - [ ] Save `pip list > baseline_packages.txt`
- [ ] Verify current system is working
  - [ ] Paper account rebalance succeeds
  - [ ] No errors in logs
  - [ ] Ably connection active
- [ ] Create feature branch
  - [ ] `git checkout -b feat/package-refactor`
  - [ ] `git tag pre-refactor-$(date +%Y%m%d)`
- [ ] Backup critical files
  - [ ] `accounts.yaml`
  - [ ] `replacement-sets.yaml`
  - [ ] `docker-compose.yaml`

### Critical Files to Create
- [ ] Create `.dockerignore` (see Task 2.1)
- [ ] Verify packages/ directory exists: `mkdir -p packages`
- [ ] Verify Docker has access to project root

### Implementation Priority
1. **Phase 1** (Day 1): Create all package scaffolding
   - [ ] broker-connector-base (with __version__)
   - [ ] rebalance-calculator (with __version__)
   - [ ] ibkr-connector (with __version__)
   - [ ] ⚠️ Complete order_id string conversion in IBKRClient
2. **Phase 2** (Day 2-3): Update main application
   - [ ] ⚠️ Update Dockerfile (CRITICAL)
   - [ ] Update requirements.txt
   - [ ] Create broker_factory.py with error handling
   - [ ] Update trading_executor.py with import error handling
   - [ ] Update models.py (keep only app-specific)
3. **Phase 3** (Day 4-5): Testing
   - [ ] Local testing with editable installs
   - [ ] Docker build testing
   - [ ] All 7 integration tests
   - [ ] Compare with baseline tests
4. **Phase 4** (Day 6): Production deployment
5. **Phase 5** (Day 7): Documentation

---

## Appendix B: File Mapping

| Current Location | New Location | Action |
|------------------|--------------|--------|
| `event-broker/app/trading/trade_calculator.py` | `packages/rebalance-calculator/src/rebalance_calculator/calculator.py` | Copy + Update imports + Add __version__ |
| `event-broker/app/trading/ibkr_client.py` | `packages/ibkr-connector/src/ibkr_connector/client.py` | Copy + Inherit from `BrokerClient` + ⚠️ Complete order_id string conversion in ALL methods + Add version logging |
| `event-broker/app/trading/rebalancer.py` | `packages/ibkr-connector/src/ibkr_connector/rebalancer.py` | Copy + Inherit from `BaseRebalancer` + Update imports + Add error handling |
| `event-broker/app/trading/allocation_service.py` | `packages/ibkr-connector/src/ibkr_connector/allocation_service.py` | Move + Update imports to use `broker_connector_base` |
| `event-broker/app/trading/replacement_service.py` | `packages/ibkr-connector/src/ibkr_connector/replacement_service.py` | Move + Update imports to use `broker_connector_base` |
| `event-broker/app/models.py` (AccountSnapshot, ContractPrice, etc.) | `packages/broker-connector-base/src/broker_connector_base/models.py` | Extract market data models + Add __version__ |
| `event-broker/app/models.py` (Trade, AllocationItem, AccountConfig) | `packages/broker-connector-base/src/broker_connector_base/models.py` | ⚠️ **CRITICAL**: Move to base to avoid circular dependency + ⚠️ Change Trade.order_id to Optional[str] |
| `event-broker/app/models.py` (TradeCalculationResult) | `packages/rebalance-calculator/src/rebalance_calculator/models.py` | Extract calculator-specific model |
| `event-broker/app/models.py` (RebalanceResult, CalculateRebalanceResult) | `packages/broker-connector-base/src/broker_connector_base/models.py` | Move to base (broker-agnostic interface) |
| `event-broker/app/models.py` (CachedPrice) | `packages/ibkr-connector/src/ibkr_connector/models.py` | Extract IBKR-specific model |
| `event-broker/app/services/trading_executor.py` | Update in place | Update imports + use factory + ⚠️ Add import error handling + clarify sys.path usage |
| `event-broker/Dockerfile` | Update in place | ⚠️ **CRITICAL**: Add package COPY and pip install steps + Add verification step |
| N/A | `packages/broker-connector-base/` | Create new package (foundation models + __version__) |
| N/A | `packages/rebalance-calculator/` | Create new package (depends on base + __version__) |
| N/A | `packages/ibkr-connector/` | Create new package (depends on base + calculator + __version__) |
| N/A | `event-broker/app/trading/broker_factory.py` | Create new factory + Add error handling |
| N/A | `.dockerignore` | ⚠️ **CRITICAL**: Create with packages/ NOT excluded |

---

## Contact & Support

For issues during refactor:
- Check logs: `docker-compose logs -f event-broker`
- Review this document
- Git blame for code history
- Test in paper mode first (account DUM959247)
- Verify package installations: `docker-compose exec event-broker pip list`

---

## Appendix C: Critical Implementation Notes

### 1. Order ID String Conversion Checklist
**⚠️ ALL IBKRClient methods must handle string order_id:**

- [ ] `place_order()` - Return `OrderResult(order_id=str(...))`
- [ ] `cancel_order(order_id: str)` - Convert to int internally
- [ ] `get_order_status(order_id: str)` - Convert to int internally
- [ ] `Trade` model - `order_id: Optional[str] = None`
- [ ] Add ValueError handling for non-numeric strings
- [ ] Add logging for conversion errors
- [ ] Test with both numeric strings and invalid inputs

### 2. Package Installation Order
**⚠️ MUST install in this order (dependencies):**

```bash
# 1. Base (no dependencies on other local packages)
pip install /tmp/broker-connector-base

# 2. Calculator (depends on base)
pip install /tmp/rebalance-calculator

# 3. Connector (depends on base + calculator)
pip install /tmp/ibkr-connector
```

### 3. sys.path.append Usage Rules
**⚠️ ONLY use sys.path.append for app.* imports:**

```python
# ❌ WRONG - Packages don't need sys.path
sys.path.append('/app')
from broker_connector_base import BrokerClient  # Already in pip

# ✅ CORRECT - Only for app.* imports
sys.path.append('/app')
from app.trading.broker_factory import create_broker_client
```

### 4. Import Error Handling Pattern
**⚠️ Add to ALL files that import packages:**

```python
try:
    from broker_connector_base import BrokerClient
    from ibkr_connector import IBKRClient
except ImportError as e:
    raise ImportError(
        f"Failed to import required packages: {e}. "
        "Ensure packages are installed: pip install -e packages/broker-connector-base"
    )
```

### 5. Version Logging Pattern
**⚠️ Add to client initialization:**

```python
import broker_connector_base
self.logger.info(
    f"Initializing IBKRClient with broker-connector-base v{broker_connector_base.__version__}"
)
```

### 6. Dockerfile Build Verification
**⚠️ Add after package installation:**

```dockerfile
# Verify package installation (helpful for debugging)
RUN pip list | grep -E "(broker|rebalance|ibkr)" && \
    python -c "import broker_connector_base; print(f'Base: v{broker_connector_base.__version__}')"
```

---

## Appendix D: Key Corrections from Analysis

This refactor plan incorporates the following key corrections from the architectural analysis:

1. **Model Placement Corrections:**
   - `RebalanceResult` and `CalculateRebalanceResult` moved to broker-connector-base (not ibkr-connector)
   - These are broker-agnostic interface contracts

2. **Service Organization:**
   - `AllocationService` and `ReplacementService` moved to ibkr-connector package
   - They're part of IBKR rebalancing implementation, not generic

3. **Type Safety Improvements:**
   - `Trade.order_id` changed from `Optional[int]` to `Optional[str]`
   - Supports both IBKR integers and future UUID-based systems
   - IBKR connector handles transparent conversion

4. **Abstract Methods Added:**
   - `BaseRebalancer` now defines required abstract methods
   - `rebalance_account()` and `calculate_rebalance()` must be implemented
   - Provides clear interface contract for all brokers

5. **Dockerfile Optimization:**
   - Packages installed in correct dependency order
   - Config file volume mounts documented and preserved
   - System dependencies (gcc, tzdata) retained

6. **Testing Enhancements:**
   - Added Test 7: Parallel strategy execution
   - Validates client ID isolation
   - Tests concurrent multi-strategy scenarios

7. **Type Hints Support:**
   - Added `py.typed` marker files to all packages
   - Enables better IDE support and type checking
   - Follows PEP 561 standard

---

## Appendix E: Additional Implementation Guidelines

### 1. BrokerConnectionConfig Abstraction (Future Enhancement)

**Purpose**: Standardize broker connection configuration across different brokers.

```python
# packages/broker-connector-base/src/broker_connector_base/models.py

class BrokerConnectionConfig(BaseModel):
    """
    Broker-agnostic connection configuration.
    Each broker connector extends this with broker-specific fields.
    """
    broker_type: str  # 'ibkr', 'schwab', etc.
    trading_mode: Literal['paper', 'live']
    host: Optional[str] = None
    port: Optional[int] = None
    timeout: int = 30

# IBKR-specific extension
class IBKRConnectionConfig(BrokerConnectionConfig):
    """IBKR-specific connection configuration"""
    broker_type: str = 'ibkr'
    client_id: int
    gateway_host: str = '127.0.0.1'
    gateway_port: int = 4001  # Auto-detect based on trading_mode
```

**Usage in broker_factory.py:**
```python
def create_broker_connection_config(account_config: AccountConfig, client_id: int) -> BrokerConnectionConfig:
    """Create broker-specific connection config"""
    if account_config.broker == 'ibkr':
        port = 4002 if account_config.type == 'paper' else 4001
        return IBKRConnectionConfig(
            trading_mode=account_config.type,
            client_id=client_id,
            gateway_port=port
        )
    else:
        raise ValueError(f"Unsupported broker: {account_config.broker}")
```

---

### 2. Exception Mapping Guidelines

**Purpose**: Ensure consistent error handling across all broker connectors.

```python
# packages/broker-connector-base/src/broker_connector_base/exceptions.py

# Exception Hierarchy
class BrokerError(Exception):
    """Base class for all broker errors"""
    pass

class BrokerConnectionError(BrokerError):
    """Raised when broker connection fails"""
    pass

class BrokerAPIError(BrokerError):
    """Raised when broker API returns an error"""
    def __init__(self, message: str, api_code: Optional[str] = None):
        super().__init__(message)
        self.api_code = api_code

class OrderExecutionError(BrokerError):
    """Raised when order execution fails"""
    def __init__(self, message: str, order_id: Optional[str] = None):
        super().__init__(message)
        self.order_id = order_id

class AuthenticationError(BrokerConnectionError):
    """Raised when authentication fails"""
    pass

class RateLimitError(BrokerAPIError):
    """Raised when rate limit is exceeded"""
    pass
```

**IBKR Exception Mapping:**
```python
# packages/ibkr-connector/src/ibkr_connector/client.py

def _map_ibkr_exception(self, exc: Exception) -> BrokerError:
    """Map IBKR-specific exceptions to broker-agnostic exceptions"""
    error_msg = str(exc)

    # Connection errors
    if 'connection' in error_msg.lower() or 'timeout' in error_msg.lower():
        return BrokerConnectionError(f"IBKR connection failed: {error_msg}")

    # Authentication errors
    if 'login' in error_msg.lower() or 'authentication' in error_msg.lower():
        return AuthenticationError(f"IBKR authentication failed: {error_msg}")

    # Rate limit errors
    if 'rate limit' in error_msg.lower() or 'too many requests' in error_msg.lower():
        return RateLimitError(f"IBKR rate limit exceeded: {error_msg}")

    # Default to generic API error
    return BrokerAPIError(f"IBKR API error: {error_msg}")
```

---

### 3. Rollback Validation Test Procedure

**Purpose**: Ensure rollback works correctly before deployment.

```bash
#!/bin/bash
# rollback-validation.sh

echo "=== Rollback Validation Test ==="

# Step 1: Create backup of current state
echo "Step 1: Creating backup..."
git branch backup-before-refactor
git tag pre-refactor-$(date +%Y%m%d-%H%M%S)

# Step 2: Deploy refactored code
echo "Step 2: Deploying refactored code..."
git checkout feat/package-refactor
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Step 3: Wait for services to start
echo "Step 3: Waiting for services to stabilize..."
sleep 30

# Step 4: Test refactored code
echo "Step 4: Testing refactored code..."
./tools/rebalance.sh -account DUM959247
if [ $? -ne 0 ]; then
    echo "❌ Refactored code test FAILED"
    exit 1
fi
echo "✅ Refactored code test passed"

# Step 5: Simulate rollback
echo "Step 5: Simulating rollback..."
git checkout main
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Step 6: Wait for services to start
echo "Step 6: Waiting for services to stabilize..."
sleep 30

# Step 7: Test rolled-back code
echo "Step 7: Testing rolled-back code..."
./tools/rebalance.sh -account DUM959247
if [ $? -ne 0 ]; then
    echo "❌ Rollback test FAILED - cannot roll back safely!"
    exit 1
fi
echo "✅ Rollback test passed"

# Step 8: Return to refactored code
echo "Step 8: Returning to refactored code..."
git checkout feat/package-refactor
docker-compose down
docker-compose build --no-cache
docker-compose up -d

echo "=== Rollback Validation Complete ==="
echo "✅ Rollback procedure validated successfully"
echo "   - Refactored code works"
echo "   - Rollback to main works"
echo "   - Safe to proceed with deployment"
```

**Usage:**
```bash
chmod +x rollback-validation.sh
./rollback-validation.sh
```

---

### 4. Local Development Setup Instructions

**Purpose**: Comprehensive guide for developers to set up local environment.

```markdown
# Local Development Setup Guide

## Prerequisites

- Python 3.11+
- Docker and Docker Compose
- Git
- IBKR Gateway (if testing live connections)

## Step 1: Clone Repository

```bash
git clone <repository-url>
cd ibkr-portfolio-rebalancer
git checkout feat/package-refactor
```

## Step 2: Install Local Packages (Editable Mode)

```bash
# Install in dependency order
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector

# Verify installations
pip list | grep -E "(broker|rebalance|ibkr)"
```

Expected output:
```
broker-connector-base    1.0.0    /path/to/packages/broker-connector-base/src
rebalance-calculator     1.0.0    /path/to/packages/rebalance-calculator/src
ibkr-connector          1.0.0    /path/to/packages/ibkr-connector/src
```

## Step 3: Install Application Dependencies

```bash
cd event-broker
pip install -r requirements.txt
```

## Step 4: Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your credentials
vim .env
```

Required variables:
```bash
IB_USERNAME=your_username
IB_PASSWORD=your_password
TRADING_MODE=paper
ALLOCATIONS_API_KEY=your_api_key
REALTIME_API_KEY=your_ably_key
```

## Step 5: Run Application Locally

```bash
# From event-broker directory
python -m app.main
```

## Step 6: Run in Docker (Recommended)

```bash
# From project root
docker-compose up --build
```

## Troubleshooting

### Import Errors
```bash
# Ensure packages are installed in editable mode
pip list | grep broker-connector-base

# Re-install if needed
pip install -e packages/broker-connector-base --force-reinstall
```

### Type Checking Errors
```bash
# Ensure py.typed files exist
ls packages/broker-connector-base/src/broker_connector_base/py.typed
ls packages/rebalance-calculator/src/rebalance_calculator/py.typed
ls packages/ibkr-connector/src/ibkr_connector/py.typed
```

### Docker Build Fails
```bash
# Check .dockerignore doesn't exclude packages/
cat .dockerignore

# Verify COPY paths in Dockerfile
docker-compose build --progress=plain event-broker
```
```

---

### 5. Package Versioning Strategy

**Purpose**: Define version management for local packages.

```markdown
# Package Versioning Strategy

## Semantic Versioning

All packages follow [Semantic Versioning](https://semver.org/): MAJOR.MINOR.PATCH

- **MAJOR**: Incompatible API changes
- **MINOR**: Backward-compatible functionality additions
- **PATCH**: Backward-compatible bug fixes

## Initial Release

All packages start at **1.0.0** (this refactor).

## Version Dependencies

- `rebalance-calculator` depends on `broker-connector-base==1.0.0`
- `ibkr-connector` depends on both base and calculator at `==1.0.0`

## Updating Versions

### When to bump versions:

1. **MAJOR (2.0.0)**:
   - Change `BrokerClient` interface (add/remove/modify abstract methods)
   - Change `Trade` or `AccountConfig` model fields (breaking)
   - Change `RebalanceResult` structure

2. **MINOR (1.1.0)**:
   - Add new optional methods to `BrokerClient`
   - Add new optional fields to models (with defaults)
   - Add new exception types

3. **PATCH (1.0.1)**:
   - Fix bugs in `TradeCalculator`
   - Fix order_id conversion issues
   - Update documentation

### Updating Process:

```bash
# 1. Update version in setup.py
vim packages/broker-connector-base/setup.py
# Change: version="1.0.0" -> version="1.1.0"

# 2. Update dependent packages
vim packages/rebalance-calculator/setup.py
# Change: "broker-connector-base==1.0.0" -> "broker-connector-base>=1.1.0,<2.0.0"

# 3. Reinstall packages
pip install -e packages/broker-connector-base --force-reinstall
pip install -e packages/rebalance-calculator --force-reinstall

# 4. Update Dockerfile (for production)
# Versions are pinned in setup.py, so rebuild will pick up new versions

# 5. Tag release
git tag -a v1.1.0 -m "Release version 1.1.0"
git push origin v1.1.0
```

## Future: PyPI Publishing

When ready to publish to private PyPI:

```bash
# Build distributions
cd packages/broker-connector-base
python setup.py sdist bdist_wheel

# Upload to private PyPI
twine upload --repository-url https://pypi.your-company.com dist/*
```
```

---

### 6. Unit Tests for TradeCalculator

**Purpose**: Example unit tests for calculator package.

```python
# packages/rebalance-calculator/tests/test_trade_calculator.py

import pytest
from broker_connector_base import (
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    Trade,
    AllocationItem,
    AccountConfig,
)
from rebalance_calculator import TradeCalculator

@pytest.fixture
def account_config():
    return AccountConfig(
        account_id="TEST123",
        type="paper",
        strategy_name="test-strategy",
        cash_reserve_percent=1.0
    )

@pytest.fixture
def account_snapshot():
    return AccountSnapshot(
        account_id="TEST123",
        total_value=10000.0,
        cash_balance=1000.0,
        settled_cash=1000.0,
        positions=[
            AccountPosition(
                symbol="SPY",
                quantity=10.0,
                market_price=450.0,
                market_value=4500.0
            ),
            AccountPosition(
                symbol="AGG",
                quantity=20.0,
                market_price=110.0,
                market_value=2200.0
            )
        ]
    )

@pytest.fixture
def target_allocations():
    return [
        AllocationItem(symbol="SPY", allocation=0.6),  # 60%
        AllocationItem(symbol="AGG", allocation=0.4),  # 40%
    ]

@pytest.fixture
def market_prices():
    return [
        ContractPrice(symbol="SPY", bid=449.0, ask=451.0, last=450.0, close=449.5),
        ContractPrice(symbol="AGG", bid=109.5, ask=110.5, last=110.0, close=110.0),
    ]

def test_calculate_trades_basic(account_config, account_snapshot, target_allocations, market_prices):
    """Test basic trade calculation"""
    calculator = TradeCalculator()

    result = calculator.calculate_trades(
        account_snapshot=account_snapshot,
        target_allocations=target_allocations,
        market_prices=market_prices,
        account_config=account_config
    )

    assert isinstance(result.trades, list)
    assert all(isinstance(t, Trade) for t in result.trades)

def test_calculate_trades_respects_cash_reserve(account_config, account_snapshot, target_allocations, market_prices):
    """Test that cash reserve is respected"""
    calculator = TradeCalculator()

    result = calculator.calculate_trades(
        account_snapshot=account_snapshot,
        target_allocations=target_allocations,
        market_prices=market_prices,
        account_config=account_config
    )

    # Calculate total deployed capital
    total_deployed = sum(abs(t.quantity) * t.price for t in result.trades if t.quantity > 0)

    # Should leave 1% cash reserve
    expected_max_deployed = account_snapshot.total_value * 0.99
    assert total_deployed <= expected_max_deployed

def test_calculate_trades_handles_missing_positions(account_config, target_allocations, market_prices):
    """Test calculation when starting from all cash"""
    empty_snapshot = AccountSnapshot(
        account_id="TEST123",
        total_value=10000.0,
        cash_balance=10000.0,
        settled_cash=10000.0,
        positions=[]
    )

    calculator = TradeCalculator()
    result = calculator.calculate_trades(
        account_snapshot=empty_snapshot,
        target_allocations=target_allocations,
        market_prices=market_prices,
        account_config=account_config
    )

    # Should generate all buy orders
    assert all(t.quantity > 0 for t in result.trades)

def test_calculate_trades_handles_liquidation(account_config, account_snapshot, market_prices):
    """Test calculation when liquidating all positions"""
    empty_allocations = []

    calculator = TradeCalculator()
    result = calculator.calculate_trades(
        account_snapshot=account_snapshot,
        target_allocations=empty_allocations,
        market_prices=market_prices,
        account_config=account_config
    )

    # Should generate all sell orders
    assert all(t.quantity < 0 for t in result.trades)

# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Installing test dependencies:**
```bash
pip install pytest pytest-cov

# Run tests
cd packages/rebalance-calculator
pytest tests/ -v --cov=rebalance_calculator
```

---

**End of Refactor Plan**
