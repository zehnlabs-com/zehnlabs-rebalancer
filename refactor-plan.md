# IBKR Portfolio Rebalancer - Package Refactor Implementation Guide

**Version:** 2.0
**Status:** Ready for Implementation
**Last Updated:** 2025-11-18

## Overview

This document provides the complete implementation plan for refactoring the IBKR Portfolio Rebalancer into modular Python packages. All tasks are sequentially numbered for tracking completion.

### Goals
- Extract broker-agnostic calculation logic into reusable packages
- Create abstract broker interface for future multi-broker support
- Isolate IBKR-specific code into dedicated connector package
- Maintain 100% backward compatibility (zero breaking changes)

---

## Implementation Tasks

### Phase 1: Pre-Scaffolding Setup

#### 1.1 Create .dockerignore file
**Status:** [ ] Not Started
**Location:** Project root
**Priority:** CRITICAL - Must complete BEFORE creating packages

Create `.dockerignore` in project root:
```
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

# Documentation (except CLAUDE.md)
*.md
!CLAUDE.md

# IDE and editor files
.vscode/
.idea/
.DS_Store

# Node modules
node_modules/

# CRITICAL: packages/ must NOT be excluded (needed for Docker build)
```

**Validation:**
```bash
grep -i "^packages/" .dockerignore
# Should return nothing (exit code 1)
```

---

#### 1.2 Create package directory structure
**Status:** [ ] Not Started
**Priority:** CRITICAL

```bash
mkdir -p packages/broker-connector-base/src/broker_connector_base
mkdir -p packages/rebalance-calculator/src/rebalance_calculator
mkdir -p packages/ibkr-connector/src/ibkr_connector
```

**Validation:**
```bash
tree packages/ -L 3
```

---

#### 1.3 Create baseline test results
**Status:** [ ] Not Started
**Priority:** HIGH

```bash
./tools/rebalance.sh -account DUM959247 > baseline_preview.log 2>&1
./tools/rebalance.sh -account DUM959247 -exec rebalance > baseline_execute.log 2>&1
pip list > baseline_packages.txt
```

**Purpose:** Compare behavior after refactor to ensure identical operation

---

### Phase 2: Create broker-connector-base Package

#### 2.1 Create setup.py for broker-connector-base
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/setup.py`

```python
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

---

#### 2.2 Create __init__.py for broker-connector-base
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/__init__.py`

```python
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

__version__ = "1.0.0"

__all__ = [
    "BrokerClient",
    "BaseRebalancer",
    "Trade",
    "AllocationItem",
    "AccountConfig",
    "AccountSnapshot",
    "AccountPosition",
    "ContractPrice",
    "OrderResult",
    "OpenOrder",
    "OrderStatus",
    "RebalanceResult",
    "CalculateRebalanceResult",
    "BrokerConnectionError",
    "BrokerAPIError",
    "OrderExecutionError",
    "__version__",
]
```

---

#### 2.3 Create base_client.py
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/base_client.py`

```python
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
```

---

#### 2.4 Create base_rebalancer.py
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/base_rebalancer.py`

```python
from abc import ABC, abstractmethod
from typing import Optional
import logging
from .base_client import BrokerClient
from .models import RebalanceResult, CalculateRebalanceResult

class BaseRebalancer(ABC):
    """Base rebalancer class with common functionality"""

    def __init__(self, broker_client: BrokerClient, logger: Optional[logging.Logger] = None):
        self.broker = broker_client
        self.logger = logger or logging.getLogger(__name__)

    @abstractmethod
    async def rebalance_account(self, account_config) -> RebalanceResult:
        """Execute live rebalancing for account"""
        pass

    @abstractmethod
    async def calculate_rebalance(self, account_config) -> CalculateRebalanceResult:
        """Calculate rebalance without executing (preview mode)"""
        pass
```

---

#### 2.5 Create models.py for broker-connector-base
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/models.py`

```python
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
```

---

#### 2.6 Create exceptions.py for broker-connector-base
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/exceptions.py`

```python
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

---

#### 2.7 Create py.typed marker for broker-connector-base
**Status:** [ ] Not Started
**File:** `packages/broker-connector-base/src/broker_connector_base/py.typed`

Empty file (PEP 561 type checking support)

---

### Phase 3: Create rebalance-calculator Package

#### 3.1 Create setup.py for rebalance-calculator
**Status:** [ ] Not Started
**File:** `packages/rebalance-calculator/setup.py`

```python
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

---

#### 3.2 Create __init__.py for rebalance-calculator
**Status:** [ ] Not Started
**File:** `packages/rebalance-calculator/src/rebalance_calculator/__init__.py`

```python
from .calculator import TradeCalculator
from .models import TradeCalculationResult
from broker_connector_base import Trade, AllocationItem, AccountConfig

__version__ = "1.0.0"

__all__ = [
    "TradeCalculator",
    "TradeCalculationResult",
    "Trade",
    "AllocationItem",
    "AccountConfig",
    "__version__",
]
```

---

#### 3.3 Copy and update trade_calculator.py
**Status:** [ ] Not Started
**Source:** `event-broker/app/trading/trade_calculator.py`
**Destination:** `packages/rebalance-calculator/src/rebalance_calculator/calculator.py`

**Actions:**
1. Copy file
2. Update imports:
```python
# OLD
from app.models import AccountSnapshot, AllocationItem, AccountConfig, Trade, ContractPrice, TradeCalculationResult

# NEW
from broker_connector_base import AccountSnapshot, ContractPrice, Trade, AllocationItem, AccountConfig
from .models import TradeCalculationResult
```

---

#### 3.4 Create models.py for rebalance-calculator
**Status:** [ ] Not Started
**File:** `packages/rebalance-calculator/src/rebalance_calculator/models.py`

```python
from typing import List
from pydantic import BaseModel, Field
from broker_connector_base import Trade

class TradeCalculationResult(BaseModel):
    """Result of trade calculation with warnings"""
    trades: List[Trade]
    warnings: List[str] = Field(default_factory=list)
```

---

#### 3.5 Create py.typed marker for rebalance-calculator
**Status:** [ ] Not Started
**File:** `packages/rebalance-calculator/src/rebalance_calculator/py.typed`

Empty file

---

### Phase 4: Create ibkr-connector Package

#### 4.1 Create setup.py for ibkr-connector
**Status:** [ ] Not Started
**File:** `packages/ibkr-connector/setup.py`

```python
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

---

#### 4.2 Create __init__.py for ibkr-connector
**Status:** [ ] Not Started
**File:** `packages/ibkr-connector/src/ibkr_connector/__init__.py`

```python
from .client import IBKRClient
from .rebalancer import IBKRRebalancer
from .models import CachedPrice

__version__ = "1.0.0"

__all__ = [
    "IBKRClient",
    "IBKRRebalancer",
    "CachedPrice",
    "__version__",
]
```

---

#### 4.3 Copy and update ibkr_client.py
**Status:** [ ] Not Started
**Source:** `event-broker/app/trading/ibkr_client.py`
**Destination:** `packages/ibkr-connector/src/ibkr_connector/client.py`

**Critical Changes:**
1. Make IBKRClient inherit from BrokerClient
2. Convert all order_id handling to string (int internally)
3. Update imports:

```python
# NEW imports
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

class IBKRClient(BrokerClient):
    def __init__(self, client_id: int, logger: Optional[logging.Logger] = None):
        # ... existing code ...
        self.logger.info(
            f"Initializing IBKRClient with broker-connector-base v{broker_connector_base.__version__}"
        )
```

4. Update order_id methods:
```python
async def place_order(...) -> OrderResult:
    # Return with string order_id
    return OrderResult(
        order_id=str(trade.order.orderId),  # Convert int to string
        symbol=symbol,
        quantity=quantity,
        status=trade.orderStatus.status
    )

async def cancel_order(self, order_id: str):
    """Convert string to int for IBKR API"""
    try:
        order_id_int = int(order_id)
    except ValueError:
        raise ValueError(f"Order ID must be numeric for IBKR, got: {order_id}")
    # ... use order_id_int with IBKR API

async def get_order_status(self, order_id: str) -> str:
    """Convert string to int for IBKR API"""
    try:
        order_id_int = int(order_id)
    except ValueError:
        return 'ERROR'
    # ... use order_id_int with IBKR API
```

---

#### 4.4 Copy and update rebalancer.py
**Status:** [ ] Not Started
**Source:** `event-broker/app/trading/rebalancer.py`
**Destination:** `packages/ibkr-connector/src/ibkr_connector/rebalancer.py`

**Actions:**
1. Copy file
2. Make IBKRRebalancer inherit from BaseRebalancer
3. Update imports:

```python
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
        "Ensure packages are installed."
    )

class IBKRRebalancer(BaseRebalancer):
    # ... existing implementation
```

---

#### 4.5 Copy and update allocation_service.py
**Status:** [ ] Not Started
**Source:** `event-broker/app/trading/allocation_service.py`
**Destination:** `packages/ibkr-connector/src/ibkr_connector/allocation_service.py`

**Actions:**
1. Copy file
2. Update imports:
```python
# OLD
from app.models import AllocationItem, AccountConfig

# NEW
from broker_connector_base import AllocationItem, AccountConfig
```

---

#### 4.6 Copy and update replacement_service.py
**Status:** [ ] Not Started
**Source:** `event-broker/app/trading/replacement_service.py`
**Destination:** `packages/ibkr-connector/src/ibkr_connector/replacement_service.py`

**Actions:**
1. Copy file
2. Update imports:
```python
# OLD
from app.models import AllocationItem

# NEW
from broker_connector_base import AllocationItem
```

---

#### 4.7 Create models.py for ibkr-connector
**Status:** [ ] Not Started
**File:** `packages/ibkr-connector/src/ibkr_connector/models.py`

```python
from pydantic import BaseModel
from datetime import datetime
from broker_connector_base import ContractPrice

class CachedPrice(BaseModel):
    """Cached price data with timestamp for TTL validation"""
    price: ContractPrice
    cached_at: datetime
```

---

#### 4.8 Create py.typed marker for ibkr-connector
**Status:** [ ] Not Started
**File:** `packages/ibkr-connector/src/ibkr_connector/py.typed`

Empty file

---

### Phase 5: Update Main Application

#### 5.1 Update Dockerfile
**Status:** [ ] Not Started
**File:** `event-broker/Dockerfile`
**Priority:** CRITICAL

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# CRITICAL: Copy local packages first
COPY packages/broker-connector-base /tmp/broker-connector-base
COPY packages/rebalance-calculator /tmp/rebalance-calculator
COPY packages/ibkr-connector /tmp/ibkr-connector

# CRITICAL: Install packages in dependency order
RUN pip install --no-cache-dir /tmp/broker-connector-base && \
    pip install --no-cache-dir /tmp/rebalance-calculator && \
    pip install --no-cache-dir /tmp/ibkr-connector && \
    rm -rf /tmp/broker-connector-base /tmp/rebalance-calculator /tmp/ibkr-connector

# Verify package installation
RUN pip list | grep -E "(broker|rebalance|ibkr)"

# Copy application code
COPY event-broker/app /app/app
COPY event-broker/requirements.txt /app/

# Install remaining dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set environment
ENV PYTHONPATH=/app
ENV TZ=America/New_York

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import asyncio; from app.main import app; print('healthy' if asyncio.run(app.get_status()).get('running', False) else 'unhealthy')" || exit 1

CMD ["python", "-m", "app.main"]
```

---

#### 5.2 Update requirements.txt
**Status:** [ ] Not Started
**File:** `event-broker/requirements.txt`

```txt
# Local packages (broker-connector-base, rebalance-calculator, ibkr-connector)
# are installed via Dockerfile. Only list event-broker-specific dependencies here.

ably==2.0.12
tzdata==2025.2

# NOTE: pydantic, aiohttp, PyYAML, ib-async are installed via ibkr-connector
# Do NOT list them here to avoid version conflicts
```

---

#### 5.3 Create broker_factory.py
**Status:** [ ] Not Started
**File:** `event-broker/app/trading/broker_factory.py`

```python
"""Factory for creating broker clients"""

import logging
from typing import Optional

try:
    from broker_connector_base import BrokerClient, AccountConfig
    from ibkr_connector import IBKRClient
except ImportError as e:
    raise ImportError(
        f"Failed to import broker packages: {e}. "
        "Ensure packages are installed."
    )

def create_broker_client(
    account_config: AccountConfig,
    client_id: int,
    logger: Optional[logging.Logger] = None
) -> BrokerClient:
    """
    Factory to create appropriate broker client.

    Args:
        account_config: Account configuration
        client_id: Unique client ID for this connection
        logger: Optional logger instance

    Returns:
        BrokerClient instance
    """
    broker = account_config.broker.lower()

    if logger:
        logger.debug(f"Creating {broker} broker client with client_id={client_id}")

    if broker == 'ibkr':
        return IBKRClient(client_id=client_id, logger=logger)
    else:
        raise ValueError(f"Unsupported broker: {broker}")
```

---

#### 5.4 Update trading_executor.py
**Status:** [ ] Not Started
**File:** `event-broker/app/services/trading_executor.py`

**Actions:**
1. Remove old imports:
```python
# REMOVE these
from app.trading.ibkr_client import IBKRClient
from app.trading.rebalancer import Rebalancer
```

2. Add new imports:
```python
from broker_connector_base import AccountConfig
from ibkr_connector import IBKRRebalancer
```

3. Update `process_single_account` function to use factory:
```python
async def process_single_account(account: dict, client_id: int, event_data: dict):
    account_config = AccountConfig(**account)
    # ... PDT protection logic ...

    manager = SubprocessManager()
    async with manager.managed_broker_client(account_config, client_id, logger) as broker_client:
        # Create rebalancer based on broker type
        if account_config.broker.lower() == 'ibkr':
            from ibkr_connector import IBKRRebalancer
            rebalancer = IBKRRebalancer(broker_client, logger=logger)
        else:
            raise ValueError(f"Unsupported broker: {account_config.broker}")

        # ... rest of existing logic
```

4. Update `SubprocessManager.managed_broker_client`:
```python
@asynccontextmanager
async def managed_broker_client(self, account_config: AccountConfig, client_id: int, logger):
    broker_client = None
    try:
        sys.path.append('/app')  # Only for app.* imports
        from app.trading.broker_factory import create_broker_client

        broker_client = create_broker_client(
            account_config=account_config,
            client_id=client_id,
            logger=logger
        )

        # ... rest of existing logic
```

---

#### 5.5 Update models.py
**Status:** [ ] Not Started
**File:** `event-broker/app/models.py`

**Actions:**
1. Remove models now in packages
2. Import from packages:

```python
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

# Keep only app-specific models:
class EventData(BaseModel):
    # ... existing code

class AccountExecutionResult(BaseModel):
    # ... existing code

class StrategyExecutionResult(BaseModel):
    # ... existing code

class PDTCheckResult(BaseModel):
    # ... existing code

class PDTExecutionInfo(BaseModel):
    # ... existing code
```

---

#### 5.6 Delete old files
**Status:** [ ] Not Started
**Priority:** Complete AFTER testing passes

Delete these files:
- `event-broker/app/trading/ibkr_client.py`
- `event-broker/app/trading/rebalancer.py`
- `event-broker/app/trading/trade_calculator.py`
- `event-broker/app/trading/allocation_service.py`
- `event-broker/app/trading/replacement_service.py`

Keep:
- `event-broker/app/trading/broker_factory.py` (new file)

---

### Phase 6: Testing

#### 6.1 Local development testing
**Status:** [ ] Not Started

```bash
# Install packages in editable mode
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector

# Verify installations
pip list | grep -E "(broker|rebalance|ibkr)"
```

---

#### 6.2 Docker build testing
**Status:** [ ] Not Started
**Priority:** CRITICAL

```bash
# Build with verbose output
docker-compose build --progress=plain event-broker

# Verify packages installed
docker-compose exec event-broker pip list | grep -E "(broker|rebalance|ibkr)"

# Verify package versions accessible
docker-compose exec event-broker python -c "
import broker_connector_base
import rebalance_calculator
import ibkr_connector
print(f'Base: v{broker_connector_base.__version__}')
print(f'Calculator: v{rebalance_calculator.__version__}')
print(f'IBKR: v{ibkr_connector.__version__}')
"
```

---

#### 6.3 Test 1: Paper account preview
**Status:** [ ] Not Started

```bash
./tools/rebalance.sh -account DUM959247
```

**Expected:**
- ✅ Connection successful
- ✅ Account snapshot retrieved
- ✅ Proposed trades calculated
- ✅ Preview displayed
- ✅ No errors

---

#### 6.4 Test 2: Paper account execution
**Status:** [ ] Not Started

```bash
./tools/rebalance.sh -account DUM959247 -exec rebalance
```

**Expected:**
- ✅ Trades executed
- ✅ Order status tracked
- ✅ Final snapshot updated
- ✅ No errors

---

#### 6.5 Test 3: PDT Protection
**Status:** [ ] Not Started

```bash
# Run rebalance twice
./tools/rebalance.sh -account DUM959247 -exec rebalance
./tools/rebalance.sh -account DUM959247 -exec rebalance
```

**Expected:**
- ✅ First rebalance succeeds
- ✅ Second rebalance blocked with PDT message

---

#### 6.6 Test 4: Baseline comparison
**Status:** [ ] Not Started

```bash
# Compare with baseline
./tools/rebalance.sh -account DUM959247 > refactored_preview.log 2>&1
diff baseline_preview.log refactored_preview.log
```

**Expected:**
- ✅ No significant differences (identical behavior)

---

#### 6.7 Test 5: Ably event processing
**Status:** [ ] Not Started

Wait for real-time event from Ably

**Expected:**
- ✅ Event received and parsed
- ✅ Subprocess spawned
- ✅ Accounts rebalanced
- ✅ Notifications sent

---

#### 6.8 Test 6: Error handling
**Status:** [ ] Not Started

```bash
./tools/rebalance.sh -account INVALID123
```

**Expected:**
- ✅ Error caught gracefully
- ✅ Helpful error message
- ✅ No crash

---

#### 6.9 Test 7: Parallel execution
**Status:** [ ] Not Started

```bash
./tools/rebalance.sh -account DUM959247 &
./tools/rebalance.sh -account DU789012 &
wait
```

**Expected:**
- ✅ Both execute in parallel
- ✅ Distinct client IDs
- ✅ No connection conflicts

---

### Phase 7: Production Deployment

#### 7.1 Pre-deployment checklist
**Status:** [ ] Not Started

- [ ] All tests passing (6.1-6.9)
- [ ] Docker build successful
- [ ] No errors in logs
- [ ] Git branch pushed
- [ ] Backup created

---

### Phase 8: Documentation

#### 8.1 Update CLAUDE.md - Architecture section
**Status:** [ ] Not Started
**File:** `CLAUDE.md`

Add new section after "Overview":

```markdown
## Package Architecture

The system uses modular Python packages:

### Local Packages (in `packages/`)

1. **broker-connector-base** - Abstract broker interface
   - `BrokerClient`: Base class with async methods
   - `BaseRebalancer`: Required abstract methods
   - Common models: `AccountSnapshot`, `ContractPrice`, `RebalanceResult`
   - Exceptions: `BrokerConnectionError`, `BrokerAPIError`

2. **rebalance-calculator** - Broker-agnostic calculation engine
   - `TradeCalculator`: Calculates trades from allocations
   - Two-phase execution (sells, then buys)
   - Cash constraint handling

3. **ibkr-connector** - IBKR-specific implementation
   - `IBKRClient`: Implements `BrokerClient` for IBKR
   - `IBKRRebalancer`: Rebalancing logic
   - `AllocationService`: Fetches strategy allocations
   - `ReplacementService`: Handles IRA ETF replacements

### Main Application

Uses packages via:
- `broker_factory.py`: Creates broker clients based on config
- `trading_executor.py`: Uses rebalancer from connector packages
```

---

#### 8.2 Update CLAUDE.md - Development workflow
**Status:** [ ] Not Started
**File:** `CLAUDE.md`

Update "Development Workflow" section:

```markdown
## Development Workflow

### Working with Local Packages

```bash
# Install packages in editable mode
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector
```

### Code Changes

1. **Package changes**: Edit code in `packages/`, changes immediately available (editable install)
2. **Main app changes**: Edit `event-broker/app/`, restart container
3. **Rebuild after package structure changes**: `docker-compose build event-broker`
```

---

#### 8.3 Update CLAUDE.md - Module organization
**Status:** [ ] Not Started
**File:** `CLAUDE.md`

Update "Module Organization" section to reflect new structure:

```markdown
### Module Organization

- `event-broker/app/main.py`: Entry point, event file watcher
- `event-broker/app/models.py`: App-specific models only
- `event-broker/app/services/`:
  - `ably_service.py`: Real-time event subscription
  - `strategy_executor.py`: Orchestrates parallel execution
  - `trading_executor.py`: Uses broker packages
  - `notification_service.py`: User notifications
  - `pdt_protection_service.py`: PDT protection
- `event-broker/app/trading/`:
  - `broker_factory.py`: Broker client factory
- `packages/broker-connector-base/`: Abstract interfaces
- `packages/rebalance-calculator/`: Calculation engine
- `packages/ibkr-connector/`: IBKR implementation
```

---

## Task Tracking

Use checkboxes above to track completion. Mark each task as:
- [ ] Not Started
- [X] Completed

## Critical Notes

1. **Order ID Handling**: All order_id fields are strings. IBKR connector converts int↔string internally.
2. **Package Installation Order**: MUST install base → calculator → connector
3. **sys.path.append**: ONLY use for app.* imports, NOT for packages (installed via pip)
4. **.dockerignore**: MUST NOT exclude packages/ directory
5. **Import Errors**: Add try-except with helpful messages around all package imports

---

**End of Implementation Guide**
