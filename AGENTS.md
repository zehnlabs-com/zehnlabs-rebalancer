# AGENTS.md

This file provides guidance to AI when working with code in this repository.

## Overview

This is a **Zehnlabs Portfolio Rebalancer** system that automatically rebalances investment portfolios across multiple broker accounts based on strategy allocation events. The system consists of a dockerized event-broker service that subscribes to real-time strategy events via Ably and executes trades through broker APIs (currently supporting Interactive Brokers via the IBKR Gateway API).

## Package Architecture

The system uses modular Python packages for broker-agnostic design:

### Local Packages (in `packages/`)

1. **app-config** (v1.0.0) - Application configuration management
   - Centralized configuration with Pydantic validation
   - Type-safe config models with range validation
   - All hard-coded constants moved to `config.yaml`
   - See [Configuration](#configuration-files) section for details

2. **broker-connector-base** (v1.0.0) - Abstract broker interface
   - `BrokerClient`: Abstract base class with async methods (connect, disconnect, place_order, get_account_snapshot, etc.)
   - `BaseRebalancer`: Required abstract methods for rebalancing
   - Common models: `Trade`, `AccountSnapshot`, `AccountPosition`, `ContractPrice`, `RebalanceResult`, `CalculateRebalanceResult`
   - Exceptions: `BrokerConnectionError`, `BrokerAPIError`, `OrderExecutionError`

3. **rebalance-calculator** (v1.0.0) - Broker-agnostic calculation engine
   - `TradeCalculator`: Calculates trades from target allocations and current positions
   - Two-phase execution strategy (sells first, then buys with updated cash)
   - Cash constraint handling with scaling algorithm
   - Allocation threshold logic (configurable via `config.yaml`)

4. **ibkr-connector** (v1.0.0) - IBKR-specific implementation
   - `IBKRClient`: Implements `BrokerClient` for Interactive Brokers Gateway API
   - `IBKRRebalancer`: Implements `BaseRebalancer` with IBKR-specific rebalancing logic
   - `AllocationService`: Fetches strategy allocations from API
   - `ReplacementService`: Handles IRA ETF replacements with scaling
   - Price caching (configurable TTL) to reduce API calls

### Main Application

Uses packages via:
- `broker_factory.py`: Creates broker clients based on `AccountConfig.broker` field
- `trading_executor.py`: Uses `managed_broker_client()` context manager with broker factory
- `models.py`: Imports shared models from packages, keeps app-specific models only

### Design Benefits

- **Multi-broker support**: Easy to add Schwab, Fidelity, etc. by creating new connector packages
- **Testing**: Calculation logic testable without broker connection
- **Reusability**: Packages can be used in other projects
- **Separation of concerns**: Business logic independent of broker APIs

## Common Commands

### Docker Operations
```bash
# Start all services
docker-compose up -d

# View logs (real-time monitoring)
docker-compose logs -f
docker-compose logs -f event-broker
docker-compose logs -f ibkr-gateway

# Restart services after configuration changes
docker-compose restart

# Rebuild and restart event-broker after code changes
docker-compose stop event-broker
docker-compose build event-broker
docker-compose up -d event-broker

# Quick reload during development
./tools/reload.sh
```

### Manual Rebalancing
```bash
# Preview trades for an account (dry-run)
./tools/rebalance.sh -account U123456

# Execute actual trades for an account
./tools/rebalance.sh -account U123456 -exec rebalance
```

### Python Development (within event-broker)
The event-broker service is Python-based. To work with the code:
```bash
# Run the app locally (requires environment variables)
cd event-broker
python -m app.main

# The Dockerfile defines the production build process
# See event-broker/Dockerfile for dependencies and setup
```

## Architecture

### System Components

1. **ibkr-gateway** (Docker service)
   - Interactive Brokers Gateway container
   - Provides API access to IBKR trading platform
   - Handles authentication via TWS credentials
   - `IB_LIVE_API_PORT` (default 4001, 127.0.0.1) -> live trading internal port 4003
   - `IB_PAPER_API_PORT` (default 4002, 127.0.0.1) -> paper trading internal port 4004
   - `VNC_PORT` (default 5900, `VNC_BIND_ADDRESS`) -> VNC access for debugging

2. **event-broker** (Python service)
   - Main application service built from `event-broker/` directory
   - Subscribes to Ably real-time strategy events
   - Orchestrates parallel account rebalancing using ProcessPoolExecutor
   - Monitors manual rebalance events via file watching
   - Uses `ib-async` library for IBKR API communication

### Event Flow Architecture

The system operates on a **strategy-based event model**:

1. **Event Sources**:
   - **Ably Real-time**: Strategy allocation updates pushed to channels (one channel per strategy)
   - **Manual Events**: JSON files dropped in `data/manual-rebalance/` directory

2. **Strategy Grouping** (`ably_service.py`):
   - Loads `accounts.yaml` at startup
   - Groups accounts by `strategy_name`
   - Subscribes once per strategy to Ably channels
   - Routes incoming events to all accounts in that strategy

3. **Parallel Execution** (`strategy_executor.py`):
   - Uses ProcessPoolExecutor with 32 workers
   - Prevents duplicate execution per strategy (deduplication via `active_strategies` set)
   - Spawns isolated subprocess for each strategy execution
   - Sends per-account notifications on completion

4. **Trading Execution** (`trading_executor.py`):
   - Each subprocess handles one strategy with multiple accounts
   - Accounts processed sequentially within subprocess (IBKR API limitation)
   - Each account gets dedicated IBKR connection with unique client_id (derived from account_id hash)

### Key Data Flow

```
Ably Event (strategy channel)
  -> AblyEventSubscriber._handle_strategy_event()
  -> StrategyExecutor.execute_strategy()
  -> ProcessPoolExecutor spawns subprocess
  -> trading_executor.execute_strategy_batch()
    -> For each account:
      -> PDTProtectionService.is_execution_allowed() (if pdt_protection_enabled)
      -> IBKRClient.connect()
      -> Rebalancer.rebalance_account()
        -> AllocationService.get_allocations() (fetch from API)
        -> ReplacementService.apply_replacements() (if replacement_set configured)
        -> IBKRClient.get_account_snapshot()
        -> TradeCalculator.calculate_trades()
        -> Execute sells first, wait for completion
        -> Recalculate with updated cash
        -> Execute buys
      -> IBKRClient.disconnect()
      -> PDTProtectionService.record_execution() (after successful rebalance)
```

### Module Organization

**Packages** (in `packages/`):
- `app-config/`: Application configuration management with Pydantic validation
- `broker-connector-base/`: Abstract broker interfaces and shared models
- `rebalance-calculator/`: Broker-agnostic trade calculation logic
- `ibkr-connector/`: IBKR-specific client, rebalancer, allocation, and replacement services

**Main Application** (in `event-broker/app/`):
- `main.py`: Entry point, event file watcher, signal handling
- `models.py`: App-specific models only (imports shared models from packages)
- `services/`:
  - `ably_service.py`: Real-time event subscription
  - `strategy_executor.py`: Orchestrates parallel execution
  - `trading_executor.py`: Subprocess worker for account processing (uses broker factory)
  - `notification_service.py`: User notifications
  - `pdt_protection_service.py`: PDT (Pattern Day Trader) protection tracking
- `trading/`:
  - `broker_factory.py`: Creates broker clients based on account configuration

## Configuration Files

### config.yaml
**New in v2.1**: Centralized application configuration file that replaces all hard-coded constants.

All configuration values are validated at startup using Pydantic models with type and range checking. Invalid configuration will cause the service to fail immediately with clear error messages.

**Structure**:
```yaml
# IBKR Connection Settings
ibkr:
  request_timeout_seconds: 10.0
  connection_timeout_seconds: 10
  connection_stabilization_delay_seconds: 0.5
  price_cache_ttl_seconds: 30
  market_data_type: 1  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
  synthetic_ask_offset_usd: 1.00
  order_placement_delay_seconds: 1.0
  ports:
    live_internal: 4003
    paper_internal: 4004

# Trading Financial Parameters
trading:
  minimum_cash_reserve_usd: 100.0
  commission_rate: 0.01  # 1%
  max_account_utilization: 0.995  # 99.5%
  order_tif: DAY  # Time In Force: DAY, GTC, IOC, GTD
  buy_slippage_percent: 0.5
  allocation_threshold_percent: 0.5
  order_timeout_seconds: 300
  order_status_check_interval_seconds: 2.0
  post_completion_delay_seconds: 1.0

# ETF Replacement Settings
replacement:
  normalization_trigger_threshold: 0.0001
  normalization_failure_threshold: 0.01
  minimal_non_replaced_allocation_percent: 0.1

# PDT Protection
pdt_protection:
  next_execution_time: "09:30"  # Market open (24-hour format, ET timezone)

# Service Configuration
service:
  heartbeat_interval_seconds: 1.0
  manual_event_check_interval_seconds: 1.0
  error_recovery_delay_seconds: 5.0
  manual_event_file_path: "/app/data/manual-rebalance/rebalance.json"

# Executor Configuration
executor:
  max_workers: 32

# API Configuration
api:
  allocation_timeout_seconds: 30
```

**Testing Configuration**:
```bash
# Validate configuration file
python test-config.py
```

**Important**: Changes to `config.yaml` require service restart: `docker-compose restart`

### accounts.yaml
Defines trading accounts and their strategy assignments. Each account requires:
- `account_id`: IBKR account ID (e.g., U123456, DUM959247)
- `type`: "paper" or "live" (must match TRADING_MODE env var)
- `enabled`: true/false to enable/disable account
- `strategy_name`: Strategy identifier (lowercase, hyphens, e.g., "etf-blend-200-35")
- `cash_reserve_percent`: Buffer percentage (0-100, typically 1.0 for 1%)
- `replacement_set` (optional): Name of replacement set for IRA restrictions
- `pdt_protection_enabled` (optional): Prevents same account from rebalancing twice in same trading day (defaults to false)

**Important**: Changes to `accounts.yaml` require service restart: `docker-compose restart`

### replacement-sets.yaml
Maps restricted ETFs to other alternatives:
- Keyed by replacement set name (e.g., "ira")
- Each entry specifies `source`, `target`, and `scale` factor
- Applied when account has `replacement_set` configured
- Used for accounts that cannot trade certain ETFs

### docker-compose.yaml
- Defines both ibkr-gateway and event-broker services
- Environment variables configure trading mode, API keys, notification channels
- Volume mounts for **config.yaml**, accounts.yaml, replacement-sets.yaml, data/
- Log rotation configured (max 100MB, 10 files)
- Config path can be overridden with `CONFIG_PATH` environment variable (defaults to `/app/config.yaml`)
- Supports running multiple isolated instances on the same host — see [Multiple Instances](#multiple-instances) section

## Environment Variables

Set in `.env` file or docker-compose environment:

**Required**:
- `IB_USERNAME`: Interactive Brokers username
- `IB_PASSWORD`: Interactive Brokers password
- `REALTIME_API_KEY`: Ably API key for event subscription
- `ALLOCATIONS_API_KEY`: API key for fetching strategy allocations

**Important**:
- `TRADING_MODE`: "paper" or "live" (controls which accounts are active and which IBKR port is used)
- `ALLOCATIONS_BASE_URL`: API endpoint to fetch strategy allocations

**Optional**:
- `LOG_LEVEL`: INFO (default), DEBUG, WARNING, ERROR
- `USER_NOTIFICATIONS_ENABLED`: true/false for ntfy notifications
- `USER_NOTIFICATIONS_CHANNEL`: ntfy channel name
- `VNC_PASSWORD`: Password for VNC access to IBKR Gateway (default: "password")
- `AUTO_RESTART_TIME`: Daily restart time for IBKR Gateway (default: "10:00 PM")
- `IB_PORT`: Manual override for IBKR port (auto-detected from TRADING_MODE if not set)

**Multi-instance (required when running more than one instance on the same host)**:
- `COMPOSE_PROJECT_NAME`: Unique project namespace — prefixes all container, volume, and network names to prevent collisions (e.g., `zehnlabs-rebalancer-accountA`)
- `IB_LIVE_API_PORT`: Host port mapped to IBKR Gateway live API (default: 4001)
- `IB_PAPER_API_PORT`: Host port mapped to IBKR Gateway paper API (default: 4002)
- `VNC_PORT`: Host port mapped to VNC (default: 5900)
- `VNC_BIND_ADDRESS`: Network interface VNC binds to (default: 127.0.0.1)

## Trading Logic Details

### Two-Phase Execution
The rebalancer uses a two-phase approach to handle cash constraints:

1. **Sell Phase**:
   - Calculate all needed sells based on target allocations
   - Execute all sell orders
   - Wait for order completion

2. **Buy Phase**:
   - Refresh account snapshot to get updated cash balance
   - Recalculate buy orders with actual available cash
   - Execute buy orders in allocation-weighted order

### Order Execution
- **Order Types**: MARKET orders for sells, LIMIT orders for buys
- **Time In Force (TIF)**: All orders explicitly set TIF from config (default: DAY)
- **Buy orders**: Use ask price * (1 + buy_slippage_percent/100) as limit price
- **Sell orders**: Use bid price (MARKET orders)
- **Price caching**: 30-second TTL to reduce API calls during multi-account processing

### Client ID Strategy
Each IBKR connection requires a unique client_id to prevent collisions. The system uses:
```python
client_id = hash(account_id) % 10000 + 1000  # Range: 1000-10999
```
This ensures consistent client IDs per account across restarts while avoiding conflicts.

### PDT Protection
Prevents the same account from rebalancing multiple times within the same trading day.

- **Enable**: Set `pdt_protection_enabled: true` in accounts.yaml
- **Behavior**: Blocks rebalances until next trading day at 9:30 AM ET
- **Storage**: Tracks execution timestamps in `data/last-executions/{account_id}.json`
- **Override**: Delete the account's JSON file in `data/last-executions/` to allow immediate rebalance

## Manual Event System

The event-broker watches `data/manual-rebalance/rebalance.json` every second. When a file appears:
1. File is read and parsed as JSON
2. File is immediately deleted (prevents reprocessing)
3. Account is looked up by `account_id`
4. Strategy executor processes single account

**Event format**:
```json
{
  "account_id": "U123456",
  "exec": "print-rebalance",
  "source": "manual",
  "timestamp": "2025-01-15T10:00:00.000Z"
}
```

Use `./tools/rebalance.sh` to safely create these events.

## Development Workflow

### Working with Local Packages

For local development (outside Docker):
```bash
# Install packages in editable mode
pip install -e packages/broker-connector-base
pip install -e packages/rebalance-calculator
pip install -e packages/ibkr-connector

# Verify installations
pip list | grep -E "(broker|rebalance|ibkr)"
```

### Code Changes

1. **Package changes** (in `packages/`):
   - Edit code in `packages/broker-connector-base/`, `rebalance-calculator/`, or `ibkr-connector/`
   - In editable mode: Changes immediately available (no rebuild)
   - In Docker: Rebuild required: `docker-compose build event-broker`

2. **Main app changes** (in `event-broker/app/`):
   - Edit Python files in `event-broker/app/`
   - Rebuild: `docker-compose build event-broker`
   - Restart: `docker-compose up -d event-broker`
   - Or use shortcut: `./tools/reload.sh`

3. **Configuration Changes**:
   - Edit `accounts.yaml` or `replacement-sets.yaml`
   - Restart services: `docker-compose restart`

4. **Testing Manual Rebalancing**:
   - Always preview first: `./tools/rebalance.sh -account U123456`
   - Check logs: `docker-compose logs -f event-broker`
   - Execute if trades look correct: `./tools/rebalance.sh -account U123456 -exec rebalance`

5. **Debugging IBKR Connection Issues**:
   - Check VNC: Connect to `localhost:5900` with VNC client
   - Verify gateway logs: `docker-compose logs -f ibkr-gateway`
   - Check TRADING_MODE matches account type in accounts.yaml
   - Verify port detection in event-broker logs

## Multiple Instances

You can run multiple fully isolated instances of the entire stack (each with its own IBKR Gateway, event-broker, credentials, and accounts) on a single host. Each instance lives in its own folder on disk with its own `.env` and `accounts.yaml`.

### Isolation mechanism

`COMPOSE_PROJECT_NAME` namespaces all Docker resources for the instance:
- Container names: `<project>-ibkr-gateway-1`, `<project>-event-broker-1`
- Volume: `<project>_ib_data` (separate IBKR Gateway session per instance)
- Network: `<project>_ibkr-network` (instances cannot cross-talk)

The `data/` directory is mounted from the local folder (`./data`), so PDT protection state and manual rebalance events are already isolated per folder.

### Setup for a second instance

1. Copy the entire repo folder to a new location (or clone again)
2. Edit `.env` — set unique values for all four port vars and `COMPOSE_PROJECT_NAME`:

```env
COMPOSE_PROJECT_NAME=zehnlabs-rebalancer-accountB

IB_LIVE_API_PORT=4011
IB_PAPER_API_PORT=4012
VNC_BIND_ADDRESS=127.0.0.1
VNC_PORT=5901
```

3. Edit `accounts.yaml` with the accounts for this instance
4. Start normally: `docker-compose up -d`

### Port assignment convention

| Instance | Live API | Paper API | VNC  |
|----------|----------|-----------|------|
| A        | 4001     | 4002      | 5900 |
| B        | 4011     | 4012      | 5901 |
| C        | 4021     | 4022      | 5902 |

## Common Pitfalls

- **Port Mismatch**: Ensure TRADING_MODE env var matches account type. Live accounts need port 4001, paper accounts need port 4002.
- **Client ID Collisions**: The system auto-generates client IDs from account IDs. If you see connection errors, check for hash collisions.
- **Stale Price Cache**: Price cache has 30s TTL. For rapid retesting, wait 30s or restart service.
- **Strategy Still Running**: The system prevents duplicate strategy execution. Wait for completion before triggering again.
- **Account Configuration**: Changes to accounts.yaml require restart - docker-compose restart won't reload the config without a container restart.

## Project knowledge base

Verified architecture notes for this system live in the wiki at
`C:\Users\mharo\source\faisal-second-brain`. Before changing behaviour here, read `index.md`,
then the page for this repo: it records verified behaviour with evidence markers
(`^[code: file:line]` / `^[cfg: file]` / `^[doc: file]`), known documentation-vs-code drift, and
open questions. Repo docs are hints — code beats docs beats memory.
