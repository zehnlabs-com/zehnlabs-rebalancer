# Portfolio Rebalancer Refactor Plan

## Executive Summary

This document outlines the refactoring plan to split the monolithic `ibkr-portfolio-rebalancer` into a modular, broker-agnostic architecture using Python local packages. The refactor will extract core calculation logic and IBKR-specific code into separate packages while maintaining 100% backward compatibility with production deployments.

**Timeline:** 5-7 days
**Risk Level:** Low (with proper testing)
**Breaking Changes:** None (transparent refactor)

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
│           ├── allocation_service.py
│           └── replacement_service.py
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
│   │           ├── base_client.py
│   │           ├── base_rebalancer.py
│   │           ├── models.py
│   │           └── exceptions.py
│   ├── rebalance-calculator/                   ← NEW: Pure calculation
│   │   ├── setup.py
│   │   └── src/
│   │       └── rebalance_calculator/
│   │           ├── __init__.py
│   │           ├── calculator.py
│   │           └── models.py
│   └── ibkr-connector/                         ← NEW: IBKR implementation
│       ├── setup.py
│       └── src/
│           └── ibkr_connector/
│               ├── __init__.py
│               ├── client.py
│               ├── rebalancer.py
│               └── models.py
└── event-broker/
    ├── Dockerfile                              ← Updated: Install local packages
    ├── requirements.txt                        ← Updated: Add local package refs
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
            ├── allocation_service.py
            ├── replacement_service.py
            └── broker_factory.py               ← NEW: Broker client factory
```

---

## Phase 1: Create Package Scaffolding (Day 1)

### Task 1.1: Create broker-connector-base package

**Location:** `packages/broker-connector-base/`

**Files to create:**

```python
# packages/broker-connector-base/setup.py
from setuptools import setup, find_packages

setup(
    name="broker-connector-base",
    version="1.0.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "pydantic>=2.11.7",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/broker-connector-base/src/broker_connector_base/__init__.py
from .base_client import BrokerClient
from .base_rebalancer import BaseRebalancer
from .models import (
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    OrderResult,
    OpenOrder,
    OrderStatus,
)
from .exceptions import (
    BrokerConnectionError,
    BrokerAPIError,
    OrderExecutionError,
)

__all__ = [
    "BrokerClient",
    "BaseRebalancer",
    "AccountSnapshot",
    "AccountPosition",
    "ContractPrice",
    "OrderResult",
    "OpenOrder",
    "OrderStatus",
    "BrokerConnectionError",
    "BrokerAPIError",
    "OrderExecutionError",
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
            order_id: Order identifier
        """
        pass

    @abstractmethod
    async def get_order_status(self, order_id: str) -> str:
        """
        Get current status of an order.

        Args:
            order_id: Order identifier

        Returns:
            Normalized order status string
        """
        pass
```

```python
# packages/broker-connector-base/src/broker_connector_base/models.py
from typing import List, Optional
from pydantic import BaseModel

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
from abc import ABC
from typing import Optional
import logging
from .base_client import BrokerClient

class BaseRebalancer(ABC):
    """Base rebalancer class with common functionality"""

    def __init__(self, broker_client: BrokerClient, logger: Optional[logging.Logger] = None):
        self.broker = broker_client
        self.logger = logger or logging.getLogger(__name__)
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
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "pydantic>=2.11.7",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/rebalance-calculator/src/rebalance_calculator/__init__.py
from .calculator import TradeCalculator
from .models import (
    AllocationItem,
    Trade,
    TradeCalculationResult,
    AccountConfig,
)

__all__ = [
    "TradeCalculator",
    "AllocationItem",
    "Trade",
    "TradeCalculationResult",
    "AccountConfig",
]
```

**Copy from existing:**
- Copy `event-broker/app/trading/trade_calculator.py` → `packages/rebalance-calculator/src/rebalance_calculator/calculator.py`
- Extract relevant models from `event-broker/app/models.py` → `packages/rebalance-calculator/src/rebalance_calculator/models.py`

**Models to extract:**
- `AllocationItem`
- `Trade`
- `TradeCalculationResult`
- `AccountConfig`

**Update imports in calculator.py:**
```python
# OLD
from app.models import AccountSnapshot, AllocationItem, AccountConfig, Trade, ContractPrice, TradeCalculationResult

# NEW
from broker_connector_base import AccountSnapshot, ContractPrice
from .models import AllocationItem, AccountConfig, Trade, TradeCalculationResult
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
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "broker-connector-base>=1.0.0",
        "rebalance-calculator>=1.0.0",
        "ib-async>=2.0.1",
        "pydantic>=2.11.7",
    ],
    python_requires=">=3.11",
)
```

```python
# packages/ibkr-connector/src/ibkr_connector/__init__.py
from .client import IBKRClient
from .rebalancer import IBKRRebalancer
from .models import CachedPrice

__all__ = [
    "IBKRClient",
    "IBKRRebalancer",
    "CachedPrice",
]
```

**Copy from existing:**
- Copy `event-broker/app/trading/ibkr_client.py` → `packages/ibkr-connector/src/ibkr_connector/client.py`
- Copy `event-broker/app/trading/rebalancer.py` → `packages/ibkr-connector/src/ibkr_connector/rebalancer.py`

**Update imports:**

```python
# In client.py
# OLD
from app.models import AccountSnapshot, AccountPosition, OrderResult, OpenOrder, ContractPrice, CachedPrice

# NEW
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
    # ... existing implementation

    # Update order_id type from int to str
    async def place_order(...) -> OrderResult:
        # ...
        return OrderResult(
            order_id=str(trade.order.orderId),  # Convert to string
            symbol=symbol,
            quantity=quantity,
            status=trade.orderStatus.status
        )

    async def cancel_order(self, order_id: str):  # Changed from int
        order_id_int = int(order_id)  # Convert back to int for IBKR
        # ... rest of implementation

    async def get_order_status(self, order_id: str) -> str:  # Changed from int
        order_id_int = int(order_id)  # Convert back to int for IBKR
        # ... rest of implementation
```

```python
# In rebalancer.py
# OLD
from app.models import AccountConfig, AccountSnapshot, AllocationItem, Trade, RebalanceResult, CalculateRebalanceResult

# NEW
from broker_connector_base import BaseRebalancer, AccountSnapshot
from rebalance_calculator import TradeCalculator, AllocationItem, Trade, AccountConfig
from .models import RebalanceResult, CalculateRebalanceResult

class IBKRRebalancer(BaseRebalancer):
    # ... existing implementation
```

```python
# packages/ibkr-connector/src/ibkr_connector/models.py
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional
from broker_connector_base import ContractPrice

class CachedPrice(BaseModel):
    """Cached price data with timestamp for TTL validation"""
    price: ContractPrice
    cached_at: datetime

class RebalanceResult(BaseModel):
    """Result of rebalance operation"""
    orders: List  # Trade objects
    total_value: float
    cash_balance: Optional[float] = None
    success: bool
    error: Optional[str] = None
    warnings: List[str] = []

class CalculateRebalanceResult(BaseModel):
    """Result of rebalance calculation (preview)"""
    proposed_trades: List  # Trade objects
    current_value: float
    success: bool
    warnings: List[str] = []
```

---

## Phase 2: Update Main Application (Day 2-3)

### Task 2.1: Update Dockerfile to install local packages

```dockerfile
# event-broker/Dockerfile

FROM python:3.11-slim

WORKDIR /app

# Copy local packages first
COPY packages/broker-connector-base /tmp/broker-connector-base
COPY packages/rebalance-calculator /tmp/rebalance-calculator
COPY packages/ibkr-connector /tmp/ibkr-connector

# Install local packages
RUN pip install --no-cache-dir /tmp/broker-connector-base && \
    pip install --no-cache-dir /tmp/rebalance-calculator && \
    pip install --no-cache-dir /tmp/ibkr-connector && \
    rm -rf /tmp/broker-connector-base /tmp/rebalance-calculator /tmp/ibkr-connector

# Copy application code
COPY event-broker/app /app/app
COPY event-broker/requirements.txt /app/

# Install application dependencies
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "app.main"]
```

---

### Task 2.2: Update requirements.txt

```txt
# event-broker/requirements.txt

# Local packages are installed via Dockerfile
# Only external dependencies here

ably==2.0.12
PyYAML==6.0.2
pydantic==2.11.7
aiohttp==3.12.15
tzdata==2025.2
```

---

### Task 2.3: Create broker factory

```python
# event-broker/app/trading/broker_factory.py
"""Factory for creating broker clients"""

import os
import logging
from typing import Optional
from broker_connector_base import BrokerClient
from ibkr_connector import IBKRClient
from rebalance_calculator import AccountConfig

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
    # For now, only IBKR is supported
    # Future: Add 'broker' field to AccountConfig and switch based on that

    broker = getattr(account_config, 'broker', 'ibkr').lower()

    if broker == 'ibkr':
        return IBKRClient(client_id=client_id, logger=logger)
    else:
        raise ValueError(f"Unsupported broker: {broker}")
```

---

### Task 2.4: Update trading_executor.py

```python
# event-broker/app/services/trading_executor.py

# OLD imports
# from app.trading.ibkr_client import IBKRClient
# from app.trading.rebalancer import Rebalancer

# NEW imports
from app.trading.broker_factory import create_broker_client
from ibkr_connector import IBKRRebalancer

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
            sys.path.append('/app')

            # Use IBKR-specific rebalancer (future: make this dynamic too)
            rebalancer = IBKRRebalancer(broker_client, logger=logger)

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


# Update SubprocessManager
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
    AccountSnapshot,
    AccountPosition,
    ContractPrice,
    OrderResult,
    OpenOrder,
)
from rebalance_calculator import (
    AllocationItem,
    Trade,
    TradeCalculationResult,
    AccountConfig,
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
- `event-broker/app/trading/__init__.py` (if now empty)

Keep these files:
- `event-broker/app/trading/allocation_service.py` ✅
- `event-broker/app/trading/replacement_service.py` ✅
- `event-broker/app/trading/broker_factory.py` ✅ (new)

---

## Phase 3: Testing & Validation (Day 4-5)

### Task 3.1: Local development testing

```bash
# Install packages in development mode
cd packages/broker-connector-base
pip install -e .

cd ../rebalance-calculator
pip install -e .

cd ../ibkr-connector
pip install -e .

# Run the application
cd ../../event-broker
python -m app.main
```

---

### Task 3.2: Docker build testing

```bash
# Build the new Docker image
docker-compose build event-broker

# Check for build errors
docker-compose logs event-broker

# Verify packages are installed
docker-compose exec event-broker pip list | grep -E "(broker|rebalance|ibkr)"
```

Expected output:
```
broker-connector-base    1.0.0
rebalance-calculator     1.0.0
ibkr-connector          1.0.0
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

---

## Phase 4: Production Deployment (Day 6)

### Task 4.1: Pre-deployment checklist

- [ ] All tests passing
- [ ] Docker build successful
- [ ] No errors in logs
- [ ] Code review completed
- [ ] Git branch created and pushed
- [ ] Backup of current production state

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

# 6. Verify connection
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

### Task 5.1: Update CLAUDE.md

Add new architecture section:

```markdown
## Package Architecture

The system is now split into modular packages:

### Local Packages (in `packages/`)

1. **broker-connector-base**: Abstract interface for broker connectors
   - Defines `BrokerClient` base class
   - Common models: `AccountSnapshot`, `ContractPrice`, etc.
   - Exception types

2. **rebalance-calculator**: Pure calculation engine (broker-agnostic)
   - `TradeCalculator`: Calculates trades based on allocations
   - Two-phase execution (sells, then buys)
   - Cash constraint handling

3. **ibkr-connector**: IBKR-specific implementation
   - `IBKRClient`: Implements `BrokerClient` for IBKR
   - `IBKRRebalancer`: Rebalancing logic using IBKR client
   - IBKR-specific models

### Main Application

The event-broker uses these packages via:
- `broker_factory.py`: Creates broker clients
- `trading_executor.py`: Uses rebalancer from connector packages
```

---

### Task 5.2: Update README (if exists)

Add development setup instructions:

```markdown
## Development Setup

### Installing Local Packages

```bash
# Install packages in editable mode
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector
```

### Adding a New Broker Connector

1. Create new package in `packages/your-broker-connector/`
2. Implement `BrokerClient` interface from `broker-connector-base`
3. Update `broker_factory.py` to support new broker
4. Add broker type to `accounts.yaml` schema
```

---

### Task 5.3: Create migration notes

```markdown
# Migration Notes - Package Refactor

## What Changed

- Codebase split into 3 local packages
- IBKR-specific code isolated
- Calculation logic now broker-agnostic
- Order IDs changed from `int` to `str` (transparent conversion)

## What Stayed the Same

- All API behavior
- Configuration files (accounts.yaml, etc.)
- Docker deployment
- Event processing
- Logging format
- Notifications

## Rollback Plan

If issues arise:
```bash
git checkout main
docker-compose down
docker-compose build
docker-compose up -d
```

## Future Enhancements

- Add `broker` field to accounts.yaml (optional, defaults to 'ibkr')
- Publish packages to PyPI
- Add Schwab connector
```

---

## Risk Mitigation

### Risk 1: Import errors
**Mitigation:**
- Test imports in Docker before deployment
- Add comprehensive error logging
- Keep old code in Git for quick rollback

### Risk 2: Order ID type mismatch
**Mitigation:**
- IBKR client converts int ↔ str transparently
- Extensive testing of order placement/cancellation
- Validate order status tracking

### Risk 3: Missing dependencies
**Mitigation:**
- Dockerfile installs packages in correct order
- Verify package installation in build step
- Test in local Docker first

### Risk 4: Subprocess import issues
**Mitigation:**
- Ensure sys.path includes package locations
- Test subprocess execution thoroughly
- Add debug logging for imports

### Risk 5: Performance regression
**Mitigation:**
- Monitor execution times before/after
- Price caching remains unchanged
- Parallel execution unchanged

---

## Success Metrics

### Pre-deployment
- [ ] All unit tests pass (if added)
- [ ] Manual testing successful
- [ ] Docker build completes
- [ ] No import errors

### Post-deployment
- [ ] Zero errors in first hour
- [ ] Manual rebalance works
- [ ] Ably events processed
- [ ] Execution time ≤ baseline
- [ ] All accounts rebalance successfully

### Long-term
- [ ] 1 week of stable operation
- [ ] No increase in error rate
- [ ] Performance metrics stable
- [ ] Ready for Schwab connector addition

---

## Timeline Summary

| Phase | Duration | Tasks |
|-------|----------|-------|
| **Phase 1**: Package scaffolding | 1 day | Create 3 packages, copy code |
| **Phase 2**: Update main app | 2 days | Update imports, factory, Dockerfile |
| **Phase 3**: Testing | 2 days | Local, Docker, integration tests |
| **Phase 4**: Deployment | 1 day | Deploy, monitor, validate |
| **Phase 5**: Documentation | 1 day | Update docs, cleanup |
| **Total** | **7 days** | |

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

---

## Next Steps After Refactor

1. **Add Schwab connector** (following this architecture)
2. **Publish to PyPI** (optional)
3. **Add unit tests** for calculators
4. **Add integration tests** with mocked brokers
5. **Performance optimization** if needed

---

## Questions & Answers

**Q: Will this break production?**
A: No. The refactor is transparent - all behavior remains identical.

**Q: Can we rollback quickly?**
A: Yes. Git checkout + rebuild takes ~5 minutes.

**Q: Do we need to update accounts.yaml?**
A: No. The broker field is optional and defaults to 'ibkr'.

**Q: Will this affect performance?**
A: No. The same code runs, just organized differently.

**Q: When can we add Schwab?**
A: After this refactor, Schwab connector can be added in ~3-5 days.

---

## Appendix: File Mapping

| Current Location | New Location | Action |
|------------------|--------------|--------|
| `event-broker/app/trading/trade_calculator.py` | `packages/rebalance-calculator/src/rebalance_calculator/calculator.py` | Copy + Update imports |
| `event-broker/app/trading/ibkr_client.py` | `packages/ibkr-connector/src/ibkr_connector/client.py` | Copy + Make inherit from `BrokerClient` |
| `event-broker/app/trading/rebalancer.py` | `packages/ibkr-connector/src/ibkr_connector/rebalancer.py` | Copy + Update imports |
| `event-broker/app/models.py` | Split across packages | Extract to appropriate package models |
| `event-broker/app/services/trading_executor.py` | Update in place | Update imports + use factory |
| N/A | `packages/broker-connector-base/` | Create new |
| N/A | `event-broker/app/trading/broker_factory.py` | Create new |

---

## Contact & Support

For issues during refactor:
- Check logs: `docker-compose logs -f event-broker`
- Review this document
- Git blame for code history
- Test in paper mode first

---

**End of Refactor Plan**
