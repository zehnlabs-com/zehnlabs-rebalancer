# Architecture Redesign: Strategy-Level Parallel Processing

## Executive Summary

This document outlines a radical simplification of the IBKR Portfolio Rebalancer architecture. The core insight is that the current three-service architecture (event-broker → Redis queue → event-processor) adds unnecessary complexity without providing value. We propose consolidating everything into an enhanced event-broker that directly executes trades in parallel.

## Current Architecture Problems

### Performance Issue
- **Sequential Processing**: When a strategy event arrives for "etf-blend-102-25", the system creates individual events for each account
- **Account Locking**: Forces sequential processing even though event-processor supports 24 concurrent events
- **Result**: 7 accounts × 1 minute each = 7 minutes total processing time

### Architectural Complexity
- **Three separate services**: event-broker, event-processor, Redis
- **Queue indirection**: Events go through Redis queue unnecessarily
- **Complex deduplication**: Account-level deduplication in Redis
- **Shared IBKR client**: Account locking required to prevent conflicts
- **Heavy abstractions**: Command pattern, dependency injection, service containers

## Proposed Architecture: Single Service Solution

### Core Design Principles
1. **Single Responsibility**: Each component does ONE thing well
2. **Direct Execution**: No queues, no indirection
3. **Process Isolation**: Each strategy runs in subprocess with dedicated IBKR clients
4. **Simplicity**: Remove all unnecessary abstractions

### High-Level Architecture

```
Ably Event (strategy)
    ↓
Enhanced Event-Broker (subscribes once per strategy)
    ↓
Strategy Executor (deduplication + orchestration)
    ↓
Process Pool (isolation)
    ↓
Trading Executor (parallel account processing)
    ↓
IBKR Gateway
```

## Implementation Details

### 1. Enhanced Event-Broker Structure

```
event-broker/
├── app/
│   ├── services/
│   │   ├── ably_service.py       # Ably event subscription
│   │   ├── strategy_executor.py  # Strategy orchestration
│   │   └── trading_executor.py   # Pure trading logic
│   ├── trading/                   # Essential trading code
│   │   ├── ibkr_client.py       # IBKR client (from event-processor)
│   │   ├── allocation_service.py # Allocation API client
│   │   ├── rebalancer.py        # Core rebalancing logic
│   │   └── trade_calculator.py  # Trade calculation
│   ├── config.py
│   └── main.py
```

### 2. Strategy Executor (Main Orchestrator)

```python
# /event-broker/app/services/strategy_executor.py

class StrategyExecutor:
    """Orchestrates parallel execution of strategy trading"""

    def __init__(self):
        self.active_strategies = set()  # Simple deduplication
        self.executor = ProcessPoolExecutor(max_workers=32)

    async def execute_strategy(self, strategy_name: str, accounts: List[dict], event_data: dict):
        """Execute strategy for all accounts in parallel subprocess"""

        # Deduplication - prevent double execution
        if strategy_name in self.active_strategies:
            logger.info(f"Strategy {strategy_name} already running")
            return {'status': 'already_running'}

        self.active_strategies.add(strategy_name)

        try:
            # Execute in subprocess for complete isolation
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor,
                execute_strategy_batch,  # Pure function
                strategy_name,
                accounts,
                event_data,
                self._get_environment()
            )

            logger.info(f"Strategy {strategy_name} completed: {result}")
            return result

        finally:
            self.active_strategies.remove(strategy_name)

    def _get_environment(self):
        """Get environment variables for subprocess"""
        return {
            'TRADING_MODE': os.getenv('TRADING_MODE'),
            'IB_HOST': os.getenv('IB_HOST', 'ibkr-gateway'),
            'ALLOCATIONS_API_KEY': os.getenv('ALLOCATIONS_API_KEY'),
            'LOG_LEVEL': os.getenv('LOG_LEVEL', 'INFO')
        }
```

### 3. Trading Executor (Pure Functions)

```python
# /event-broker/app/trading/trading_executor.py

def execute_strategy_batch(strategy_name: str, accounts: List[dict], event_data: dict, env: dict):
    """
    Pure function that executes trades for all accounts in a strategy.
    Runs in subprocess for complete isolation.
    No shared state, no side effects beyond trading.
    """

    # Set up environment
    for key, value in env.items():
        if value:
            os.environ[key] = value

    # Run async execution
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(
        process_strategy_accounts(strategy_name, accounts, event_data)
    )
    loop.close()

    return result

async def process_strategy_accounts(strategy_name: str, accounts: List[dict], event_data: dict):
    """Process all accounts for a strategy in parallel"""

    tasks = []
    for i, account in enumerate(accounts):
        # Deterministic client ID assignment
        client_id = 1000 + (i * 100)  # 1000, 1100, 1200, ...

        task = process_single_account(account, client_id, event_data)
        tasks.append(task)

    # Execute all accounts in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Format results
    return {
        'strategy': strategy_name,
        'timestamp': datetime.now().isoformat(),
        'event': event_data.get('exec'),
        'accounts_processed': len(accounts),
        'results': [
            {
                'account_id': acc['account_id'],
                'success': not isinstance(res, Exception),
                'error': str(res) if isinstance(res, Exception) else None,
                'details': res if not isinstance(res, Exception) else None
            }
            for acc, res in zip(accounts, results)
        ]
    }

async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process a single account with dedicated IBKR client"""

    from app.trading.ibkr_client import IBKRClient
    from app.trading.rebalancer import Rebalancer

    # Create dedicated IBKR client for this account
    ibkr = IBKRClient(client_id=client_id)

    try:
        # Connect to IBKR
        await ibkr.connect()

        # Create rebalancer with dedicated client
        rebalancer = Rebalancer(ibkr)

        # Execute command
        if event_data.get('exec') == 'rebalance':
            result = await rebalancer.rebalance_account(account)
            return {
                'action': 'rebalance',
                'trades_executed': len(result.get('orders', [])),
                'total_value': result.get('total_value')
            }

        elif event_data.get('exec') == 'print-rebalance':
            result = await rebalancer.calculate_rebalance(account)
            return {
                'action': 'print-rebalance',
                'proposed_trades': len(result.get('proposed_orders', [])),
                'current_value': result.get('current_value')
            }

        else:
            raise ValueError(f"Unknown command: {event_data.get('exec')}")

    finally:
        # Always disconnect
        if ibkr.is_connected():
            await ibkr.disconnect()
```

### 4. Simplified Ably Service

```python
# /event-broker/app/services/ably_service.py

class AblyEventSubscriber:
    """Subscribes to Ably channels and routes to strategy executor"""

    def __init__(self):
        self.strategy_executor = StrategyExecutor()
        self.strategies = {}  # strategy_name -> List[accounts]

    async def start(self):
        """Start Ably subscription"""

        # Load and group accounts by strategy
        self._load_accounts()

        # Subscribe once per strategy
        await self._subscribe_to_strategies()

    def _load_accounts(self):
        """Load accounts.yaml and group by strategy"""

        with open('/app/accounts.yaml', 'r') as f:
            config = yaml.safe_load(f)

        trading_mode = os.getenv('TRADING_MODE', 'paper')

        for account in config.get('accounts', []):
            if account.get('type') == trading_mode and account.get('enabled'):
                strategy = account['strategy_name']
                if strategy not in self.strategies:
                    self.strategies[strategy] = []
                self.strategies[strategy].append(account)

        logger.info(f"Loaded {len(self.strategies)} strategies with "
                   f"{sum(len(accs) for accs in self.strategies.values())} accounts")

    async def _subscribe_to_strategies(self):
        """Subscribe to Ably channel for each strategy"""

        self.ably = AblyRealtime(os.getenv('REALTIME_API_KEY'))

        for strategy_name, accounts in self.strategies.items():
            channel = self.ably.channels.get(strategy_name)

            # Create handler for this strategy
            async def create_handler(strat_name, strat_accounts):
                async def handler(message):
                    await self._handle_strategy_event(strat_name, strat_accounts, message)
                return handler

            handler = await create_handler(strategy_name, accounts)
            await channel.subscribe(handler)

            logger.info(f"Subscribed to strategy {strategy_name} with {len(accounts)} accounts")

    async def _handle_strategy_event(self, strategy_name: str, accounts: List[dict], message):
        """Handle incoming strategy event"""

        try:
            # Parse message
            event_data = message.data if isinstance(message.data, dict) else json.loads(message.data)

            # Execute strategy
            result = await self.strategy_executor.execute_strategy(
                strategy_name,
                accounts,
                event_data
            )

            # Log results
            successful = sum(1 for r in result.get('results', []) if r['success'])
            logger.info(f"Strategy {strategy_name}: {successful}/{len(accounts)} accounts successful")

        except Exception as e:
            logger.error(f"Failed to handle event for {strategy_name}: {e}")
```

### 5. Simplified Rebalancer (No Locking)

```python
# /event-broker/app/trading/rebalancer.py

class Rebalancer:
    """Simplified rebalancer without account locking"""

    def __init__(self, ibkr_client: IBKRClient):
        self.ibkr = ibkr_client  # Dedicated client per account
        self.allocation_service = AllocationService()

    async def rebalance_account(self, account: dict):
        """Execute rebalancing for account"""

        account_id = account['account_id']

        # Get target allocations
        allocations = await self.allocation_service.get_allocations(account)

        # Get current positions
        snapshot = await self.ibkr.get_account_snapshot(account_id)

        # Calculate required trades
        trades = self._calculate_trades(snapshot, allocations, account)

        # Execute sell orders first
        sell_orders = [t for t in trades if t['quantity'] < 0]
        for trade in sell_orders:
            await self.ibkr.place_order(
                account_id,
                trade['symbol'],
                trade['quantity']
            )

        # Wait for sells to complete
        await self._wait_for_orders_complete(sell_orders)

        # Execute buy orders
        buy_orders = [t for t in trades if t['quantity'] > 0]
        for trade in buy_orders:
            await self.ibkr.place_order(
                account_id,
                trade['symbol'],
                trade['quantity']
            )

        # Wait for buys to complete
        await self._wait_for_orders_complete(buy_orders)

        return {
            'orders': trades,
            'total_value': snapshot['total_value']
        }
```

## Key Simplifications

### What We Remove

1. **Event-Processor Service** - Entire service deleted
2. **Redis Queue** - No queueing needed
3. **Account Locking** - Each account has dedicated IBKR client
4. **Service Container/DI** - Direct instantiation
5. **Command Pattern** - Just function calls
6. **Complex Deduplication** - Simple set-based tracking

### What We Keep

1. **IBKR Client** - Essential for trading
2. **Allocation Service** - Gets strategy allocations
3. **Trade Calculation** - Core business logic
4. **accounts.yaml** - Single source of truth

## Implementation Plan

### Phase 1: Code Migration (Day 1-2)
1. Copy essential trading code from event-processor to event-broker
2. Remove unnecessary abstractions and dependencies
3. Create simplified versions without locking

### Phase 2: Core Implementation (Day 3-4)
1. Implement StrategyExecutor class
2. Implement trading_executor functions
3. Update AblyEventSubscriber for strategy-level subscription

### Phase 3: Testing (Day 5-6)
1. Test with paper trading accounts
2. Verify parallel execution
3. Test failure scenarios
4. Load testing with multiple strategies

### Phase 4: Deployment (Day 7)
1. Update Docker configuration
2. Remove event-processor and Redis services
3. Deploy to staging
4. Monitor and validate
5. Deploy to production

## Docker Configuration

```yaml
services:
  event-broker:
    build: ./event-broker
    container_name: event-broker
    environment:
      - TRADING_MODE=${TRADING_MODE:-paper}
      - IB_HOST=ibkr-gateway
      - REALTIME_API_KEY=${REBALANCE_EVENT_SUBSCRIPTION_API_KEY}
      - ALLOCATIONS_API_KEY=${ALLOCATIONS_API_KEY}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - ./accounts.yaml:/app/accounts.yaml:ro
      - ./replacement-sets.yaml:/app/replacement-sets.yaml:ro
      - ./event-broker/logs:/app/logs
    depends_on:
      - ibkr-gateway
    restart: unless-stopped

  ibkr-gateway:
    # Unchanged from current configuration

  management-service:
    # Keep if needed for monitoring
    # Can be simplified to remove Redis dependency
```

## Performance Benefits

### Current System
- 7 accounts on same strategy: **7 minutes** (sequential)
- Complex service coordination
- Redis queue overhead
- Multiple Docker containers

### New System
- 7 accounts on same strategy: **1 minute** (parallel)
- Single service
- Direct execution
- Minimal containers

## Risk Mitigation

1. **Process Crashes**: Each strategy runs in subprocess, isolated from others
2. **Client ID Conflicts**: Deterministic assignment (1000 + index*100)
3. **Resource Limits**: ProcessPoolExecutor with max_workers=32
4. **Rollback Plan**: Keep existing code, deploy side-by-side initially

## Monitoring and Observability

### Account-Level Logging Architecture

```python
# /event-broker/app/trading/trading_executor.py

import logging
import sys
from contextlib import contextmanager

@contextmanager
def account_logger_context(account_id: str, strategy_name: str):
    """Create a logger context with account-specific formatting"""

    # Create account-specific logger
    logger = logging.getLogger(f"account.{account_id}")
    logger.setLevel(logging.INFO)

    # Add handler with account ID in every log line
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        f'[%(asctime)s] [ACCOUNT:{account_id}] [STRATEGY:{strategy_name}] '
        f'[%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    try:
        yield logger
    finally:
        # Clean up handler to prevent duplication
        logger.removeHandler(handler)

async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process a single account with dedicated logging context"""

    account_id = account['account_id']
    strategy_name = account['strategy_name']

    # All logs within this context automatically include account ID
    with account_logger_context(account_id, strategy_name) as logger:
        logger.info(f"Starting rebalance with client_id={client_id}")

        from app.trading.ibkr_client import IBKRClient
        from app.trading.rebalancer import Rebalancer

        ibkr = IBKRClient(client_id=client_id, logger=logger)

        try:
            # Connect with account-specific logging
            logger.info(f"Connecting to IBKR Gateway at {os.getenv('IB_HOST')}:4003")
            await ibkr.connect()
            logger.info("Successfully connected to IBKR")

            # Create rebalancer with account logger
            rebalancer = Rebalancer(ibkr, logger=logger)

            # Execute command
            if event_data.get('exec') == 'rebalance':
                logger.info("Executing LIVE rebalance")
                result = await rebalancer.rebalance_account(account)
                logger.info(f"Rebalance completed: {len(result.get('orders', []))} trades executed")
                return {
                    'action': 'rebalance',
                    'trades_executed': len(result.get('orders', [])),
                    'total_value': result.get('total_value')
                }

        except Exception as e:
            # Account-specific error logging with full context
            logger.error(f"Rebalance failed: {type(e).__name__}: {str(e)}")
            logger.exception("Full traceback:")
            raise

        finally:
            # Ensure cleanup with logging
            if ibkr.is_connected():
                logger.info("Disconnecting from IBKR")
                await ibkr.disconnect()
                logger.info("Successfully disconnected")
```

### Structured Log Output

```
# Example log output (easily searchable by account ID)
[2024-01-15 10:00:00] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Starting rebalance with client_id=1000
[2024-01-15 10:00:01] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Connecting to IBKR Gateway at ibkr-gateway:4003
[2024-01-15 10:00:02] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Successfully connected to IBKR
[2024-01-15 10:00:02] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Executing LIVE rebalance
[2024-01-15 10:00:03] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Fetching allocations from API
[2024-01-15 10:00:04] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Retrieved 10 target allocations
[2024-01-15 10:00:05] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Getting account snapshot
[2024-01-15 10:00:06] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [ERROR] Rebalance failed: ConnectionError: Lost connection to IBKR
[2024-01-15 10:00:06] [ACCOUNT:U21240665] [STRATEGY:etf-blend-102-25] [INFO] Disconnecting from IBKR

# Searching logs:
grep "ACCOUNT:U21240665" logs/event-broker.log  # Get all logs for specific account
grep "ERROR.*ACCOUNT:U21240665" logs/event-broker.log  # Get errors for specific account
```

### Error Aggregation and Reporting

```python
# /event-broker/app/services/strategy_executor.py

class StrategyExecutor:
    """Enhanced with error aggregation and reporting"""

    async def execute_strategy(self, strategy_name: str, accounts: List[dict], event_data: dict):
        """Execute strategy with comprehensive error tracking"""

        if strategy_name in self.active_strategies:
            logger.info(f"Strategy {strategy_name} already running")
            return {'status': 'already_running'}

        self.active_strategies.add(strategy_name)

        try:
            # Track execution start time
            start_time = datetime.now()

            # Execute in subprocess
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor,
                execute_strategy_batch,
                strategy_name,
                accounts,
                event_data,
                self._get_environment()
            )

            # Log summary with account-level details
            execution_time = (datetime.now() - start_time).total_seconds()
            successful_accounts = [r for r in result['results'] if r['success']]
            failed_accounts = [r for r in result['results'] if not r['success']]

            logger.info(
                f"Strategy {strategy_name} completed in {execution_time:.1f}s: "
                f"{len(successful_accounts)}/{len(accounts)} successful"
            )

            # Log failures with account context
            for failure in failed_accounts:
                logger.error(
                    f"Strategy {strategy_name} failed for account {failure['account_id']}: "
                    f"{failure['error']}"
                )

            # Store detailed results for monitoring
            await self._store_execution_results(strategy_name, result)

            return result

        except Exception as e:
            logger.error(f"Strategy {strategy_name} execution failed completely: {e}")
            # Return structured error response
            return {
                'status': 'failed',
                'strategy': strategy_name,
                'error': str(e),
                'accounts_affected': len(accounts)
            }

        finally:
            self.active_strategies.remove(strategy_name)

    async def _store_execution_results(self, strategy_name: str, result: dict):
        """Store execution results for monitoring and debugging"""

        # Write to structured log file for analysis
        with open(f'logs/executions/{strategy_name}_{datetime.now():%Y%m%d_%H%M%S}.json', 'w') as f:
            json.dump(result, f, indent=2)

        # Log to monitoring system (could be DataDog, CloudWatch, etc.)
        for account_result in result.get('results', []):
            if not account_result['success']:
                # Create alert for failed accounts
                logger.warning(
                    f"ALERT: Account {account_result['account_id']} failed rebalancing",
                    extra={
                        'alert_type': 'rebalance_failure',
                        'account_id': account_result['account_id'],
                        'strategy': strategy_name,
                        'error': account_result.get('error')
                    }
                )
```

### Connection Cleanup and Resource Management

```python
# /event-broker/app/trading/trading_executor.py

import signal
import atexit
from contextlib import asynccontextmanager

class SubprocessManager:
    """Manages subprocess lifecycle and cleanup"""

    def __init__(self):
        self.active_connections = {}

        # Register cleanup handlers
        atexit.register(self._cleanup_all)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    @asynccontextmanager
    async def managed_ibkr_client(self, account_id: str, client_id: int, logger):
        """Context manager for IBKR client with guaranteed cleanup"""

        ibkr = None
        try:
            from app.trading.ibkr_client import IBKRClient

            logger.info(f"Creating IBKR client with ID {client_id}")
            ibkr = IBKRClient(client_id=client_id, logger=logger)

            # Track active connection
            self.active_connections[account_id] = ibkr

            # Connect
            logger.info("Establishing IBKR connection")
            connected = await ibkr.connect()

            if not connected:
                raise ConnectionError("Failed to connect to IBKR Gateway")

            logger.info("IBKR connection established successfully")
            yield ibkr

        except Exception as e:
            logger.error(f"IBKR client error: {e}")
            raise

        finally:
            # Guaranteed cleanup
            if ibkr:
                try:
                    if ibkr.is_connected():
                        logger.info("Cleaning up IBKR connection")
                        await ibkr.disconnect()
                        logger.info("IBKR connection closed successfully")
                except Exception as e:
                    logger.error(f"Error during cleanup: {e}")
                finally:
                    # Remove from tracking
                    self.active_connections.pop(account_id, None)

    def _cleanup_all(self):
        """Emergency cleanup of all connections"""
        for account_id, ibkr in self.active_connections.items():
            try:
                if ibkr.is_connected():
                    # Synchronous disconnect for emergency cleanup
                    ibkr.ib.disconnect()
                    print(f"Emergency cleanup: Disconnected {account_id}")
            except Exception as e:
                print(f"Emergency cleanup failed for {account_id}: {e}")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print(f"Received signal {signum}, initiating cleanup...")
        self._cleanup_all()
        sys.exit(0)

# Updated process_single_account with managed connection
async def process_single_account(account: dict, client_id: int, event_data: dict):
    """Process single account with managed resources"""

    account_id = account['account_id']
    strategy_name = account['strategy_name']

    with account_logger_context(account_id, strategy_name) as logger:
        manager = SubprocessManager()

        async with manager.managed_ibkr_client(account_id, client_id, logger) as ibkr:
            # All IBKR operations here - connection is guaranteed to be cleaned up
            from app.trading.rebalancer import Rebalancer

            rebalancer = Rebalancer(ibkr, logger=logger)

            if event_data.get('exec') == 'rebalance':
                logger.info("Executing LIVE rebalance")
                result = await rebalancer.rebalance_account(account)
                logger.info(f"Rebalance completed: {len(result.get('orders', []))} trades executed")
                return {
                    'success': True,
                    'action': 'rebalance',
                    'trades_executed': len(result.get('orders', [])),
                    'total_value': result.get('total_value')
                }
```

## Conclusion

This redesign radically simplifies the architecture.

The key insight is that the current three-service architecture adds complexity without value. Direct execution with process isolation gives us all the benefits with none of the overhead.