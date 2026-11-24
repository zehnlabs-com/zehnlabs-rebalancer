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
   - `BrokerConnection`: Async abstract base class — `connect`/`close`, `account_state()`, `quotes()`, `working_orders()`, `cancel_own_orders()`, `place()`, `order_states()`
   - `supports_fractional`: False for IBKR (whole-share broker)
   - Domain models (frozen dataclasses): `Quote`, `Position`, `AccountState`, `OrderRequest`, `OrderState`, `WorkingOrder`, plus the `Side`, `OrderStatus`, `PriceSource` enums
   - Exceptions: `BrokerError` and subclasses (`ConnectionFailedError`, `AccountDataError`, `PricingError`, `NoPriceError`, `CancelNotConfirmedError`, `OrderPlacementError`)

3. **rebalance-calculator** (v1.0.0) - Broker-agnostic calculation engine (pure functions; no I/O, no clock, no logging)
   - `validate_and_scale`: validate and scale raw API weights to sum to 1.0 (or empty list = sell everything)
   - `apply_replacements`: apply a built-in replacement set (REP)
   - `plan_sells` / `plan_buys`: two-phase broker-granularity rebalance plans (§7); sells market, buys limit, and constrained cash funds higher-weight buys first

4. **ibkr-connector** (v1.0.0) - IBKR-specific implementation
   - `IBKRConnection`: Implements `BrokerConnection` against the IBKR Gateway API
   - Own/foreign order handling, own-order cancel, whole-share-only placement
   - Quote chains (mid/last/close/bar) with market-data retries

### Main Application

Uses packages via:
- `accounts.py`: AccountConfig + AccountRegistry (active accounts filtered by mode + enabled)
- `allocations.py`: AllocationsClient (fetch raw allocations over HTTP, one aiohttp session per worker)
- `rebalance.py`: `rebalance_account(...)` — the single account-run operation (§8), never raises
- `results.py`: AccountRunResult / RunOutcome / FailureReason (the only pydantic result that crosses the process boundary)

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
   - **Ably Real-time**: Strategy allocation updates pushed to channels (one channel per strategy), source `signal`
   - **Manual Events**: JSON files dropped in `data/manual-rebalance/` directory, source `manual`
   - **Scheduled**: the market-open job, source `scheduled`

2. **Strategy grouping** (`ably_service.py` / `main.py`):
   - Loads `accounts.yaml` at startup; groups accounts by `strategy_name`
   - Subscribes once per strategy to Ably channels; routes incoming events to all active accounts in that strategy

3. **Parallel Execution** (`strategy_executor.py`):
   - One `ProcessPoolExecutor(max_workers, mp_context=fork)`; drops a trigger for a strategy already running
   - Parent detects live runs during a closed market before submitting (→ `MARKET_CLOSED`)
   - Post-processes each result: retry queue (§10.5), run record (§12), notification (§11) — each wrapped so one failure never stops the rest

4. **Worker** (`trading_executor.py`):
   - `execute_strategy_batch(...)` runs one strategy in a process-pool subprocess, opening one `AllocationsClient`
   - Runs `rebalance_account()` for every account with `asyncio.gather(..., return_exceptions=True)`
   - Each account gets a dedicated connection with a unique client_id (int of the digits of the account ID)

### Key Data Flow

```
Ably Event / manual file / scheduled job
  -> StrategyExecutor.execute(strategy, accounts, mode, source)
  -> ProcessPoolExecutor spawns subprocess
  -> trading_executor.execute_strategy_batch()
    -> For each account (gather):
      -> rebalance_account()            (event-broker/app/rebalance.py, §8)
        -> AllocationsClient.fetch()    (raw weights)
        -> validate_and_scale + apply_replacements
        -> BrokerConnection.account_state() / quotes()
        -> plan_sells() -> place sells (market), wait
        -> read updated cash -> plan_buys() -> place buys (limit), wait
      -> returns AccountRunResult (never raises)
    -> parent: retry queue (§10.5), run record (§12), notification (§11)
```

### Module Organization

**Packages** (in `packages/`):
- `app-config/`: Strict pydantic `AppConfig` models (extra forbidding) and `load_config`
- `broker-connector-base/`: Abstract `BrokerConnection` interface, errors, and the §6.1 domain models
- `rebalance-calculator/`: Pure rebalance math (targets, replacement sets, planner) — no I/O, no config lookups
- `ibkr-connector/`: `IBKRConnection`, the IBKR-specific `BrokerConnection` implementation

**Main Application** (in `event-broker/app/`):
- `main.py`: Entry point, startup order (§13), manual-event watcher, heartbeat
- `accounts.py`: `AccountConfig` + `AccountRegistry` (load_accounts, strict validation)
- `allocations.py`: `AllocationsClient` (async HTTP, one aiohttp session per worker)
- `market_clock.py`: `MarketClock` (XNYS calendar, next_scheduled_run)
- `rebalance.py`: `rebalance_account(...)` — the single account-run operation (§8)
- `results.py`: `AccountRunResult`, `RunOutcome`, `FailureReason`
- `settings.py`: `RuntimeSettings` read once from env (API key masked)
- `services/`:
  - `ably_service.py`: Real-time event subscription (task set added/discarded per signal)
  - `strategy_executor.py`: Process pool, running-strategy set, post-processing (retry/records/notifications)
  - `trading_executor.py`: Worker entry point `execute_strategy_batch(...)`
  - `scheduler_service.py`: market-open job + atomic schedule-file helpers
  - `notification_service.py`: Notifier ABC + NtfyNotifier + message rendering
  - `run_records.py`: write + prune `data/runs/*.jsonl` (30 days)

## Configuration Files

### config.yaml
Strict pydantic validation (extra=forbid at every level). Unknown or removed keys stop the service at startup with a message naming each key. All configuration values are passed into constructors; packages never call `get_config()` or read env vars.

**Structure**:
```yaml
ibkr:
  request_timeout_seconds: 15.0
  connection_timeout_seconds: 10
  connection_stabilization_delay_seconds: 0.5
  market_data_type: 1  # 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
  order_placement_delay_seconds: 1.0
  market_data_retry_delay_seconds: 2.0
  market_data_max_retries: 10
  ports:
    live_internal: 4003
    paper_internal: 4004
trading:
  minimum_cash_reserve_percent: 0.25   # 0-5. cash floor as % of total value
  buy_slippage_percent: 0.5            # 0-5. limit = buy ref * (1 + this/100)
  order_timeout_seconds: 300           # per leg
  order_status_check_interval_seconds: 2.0
  cancel_confirm_timeout_seconds: 30
  cash_update_timeout_seconds: 30
service:
  heartbeat_interval_seconds: 1.0
  manual_event_check_interval_seconds: 1.0
  error_recovery_delay_seconds: 5.0
  manual_event_file_path: "/app/data/manual-rebalance/rebalance.json"
scheduler:
  enabled: true
  market_open_time: "09:35"   # HH:MM, ET
  scheduled_file_path: "/app/data/market-open/scheduled.json"
executor:
  max_workers: 32
api:
  rebalance_auth_timeout_seconds: 10
  allocation_timeout_seconds: 30
```

**Important**: Changes to `config.yaml` require service restart: `docker-compose restart`

### accounts.yaml
Defines trading accounts and their strategy assignments, strictly validated. Each account supports:
- `account_id`: IBKR account ID (e.g., U123456, DUM959247)
- `type`: "paper" or "live" (must match TRADING_MODE env var)
- `enabled`: true/false to enable/disable account
- `strategy_name`: Strategy identifier (lowercase, hyphens, e.g., "etf-blend-200-35")
- `replacement_set` (optional): Name of a built-in replacement set (e.g., `ira`)
- `broker`: defaults to `ibkr`

The account's `client_id` is the int of the digits in the account ID (same rule was already used for IBKR connections). The cash floor is the global `trading.minimum_cash_reserve_percent`; there is no per-account cash reserve.

**Important**: Changes to `accounts.yaml` require service restart: `docker-compose restart`

### Replacement sets
Replacement sets (e.g. `ira`) are **built into the code** (`rebalance_calculator/replacement_sets.py`). There is no `replacement-sets.yaml` file. Reference a set from `accounts.yaml` with `replacement_set: ira`.

### docker-compose.yaml
- Defines both ibkr-gateway and event-broker services
- Environment variables configure trading mode, API keys, notification channels
- Volume mounts for config.yaml, accounts.yaml, and data/ (no replacement-sets.yaml)
- Log rotation configured (max 100MB, 10 files)
- Version marker (`x-system-version`) is `3.0.0`
- Supports running multiple isolated instances on the same host — see [Multiple Instances](#multiple-instances) section

## Environment Variables

Set in `.env` file or docker-compose environment:

**Required**:
- `IB_USERNAME`: Interactive Brokers username
- `IB_PASSWORD`: Interactive Brokers password
- `ZEHNLABS_API_KEY`: Unified Zehnlabs key from Telegram `/api`, used for allocations and startup `/rebalance-auth`. Copy/store it safely; `/api regen` replaces a lost key and `/api revoke` invalidates it. `/me` reports account/subscription details only.

The subscriber fetches the shared read-only Ably key once at startup and passes it directly to the existing SDK. SDK reconnects retain that in-memory key. Subscribers configure no separate Ably key. See `docs/security-migration.md` for installation, container recreation, startup errors, and manual Ably rotation.

**Important**:
- `TRADING_MODE`: "paper" or "live" (controls which accounts are active and which IBKR port is used)
- `ALLOCATIONS_BASE_URL`: Allocation API base ending in `/strategies`; startup authentication uses the sibling `/rebalance-auth` route. Default: `https://workers.fintech.zehnlabs.com/api/v1/strategies`.

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
   - Compute the sell plan from target weights and marks (`plan_sells`)
   - Place all sells as **market** orders (symbol order) and wait for them

2. **Buy Phase**:
   - Read the updated cash (lower of reported cash vs. `state0.cash + fills`)
   - Recompute the buy plan with fresh quotes (`plan_buys`), sized at the ask + slippage buffer
   - Place buys as **limit** orders and wait for them

### Order Execution
- **Order Types**: MARKET orders for sells, LIMIT orders for buys; all orders are **DAY**
- **Buy limit**: ask × (1 + buy_slippage_percent/100), rounded half-up
- **Order placement**: sell all, wait, then buy all; within a leg, orders are placed in symbol order
- **At the start of a live run** the app cancels its own working orders, and warns (does not trade) if foreign working orders are present
- **Prices**: mark = mid, else last, else close, else bar; buys use ask, else last, else close, else bar
- **Order status** is read from the stored order handles only, never by re-scanning `ib.trades()` (order numbers repeat across client IDs)

### Client ID Strategy
Each IBKR connection requires a unique `client_id` to prevent collisions. It is the **int of the digits** of the account ID (e.g. `U123456` → `123456`), from `AccountConfig.client_id`. It is consistent across restarts and has no hash collisions.

### Cash Reserve & Retries
- A cash floor of `trading.minimum_cash_reserve_percent` (default 0.25%) of the account's total value is never spent. When cash is short, raw buys are funded from highest target weight to lowest; equal weights use reverse-alphabetical funding priority, while orders are still emitted in normal symbol order.
- A live run that fails, or that starts while the market is closed, is queued and retried **once** at the next market open (09:35 ET). A scheduled run that fails is not retried. There is no PDT protection.

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
   - Edit `config.yaml` or `accounts.yaml`
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

The `data/` directory is mounted from the local folder (`./data`), so the market-open queue and manual rebalance events are already isolated per folder.

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
- **Client ID**: One per account, derived from the digits of the account ID. Two active accounts producing the same client ID fail validation.
- **Strategy Still Running**: The system prevents duplicate strategy execution; a trigger for an already-running strategy is dropped. Wait for completion before triggering again.
- **Account Configuration**: Changes to accounts.yaml require restart - docker-compose restart won't reload the config without a container restart.
- **Missing account data fails the run**: a missing cash or NetLiquidation figure is an error, never treated as zero.

## Agent skills

### Issue tracker

Issues live as markdown files under `.scratch/<feature-slug>/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, each string equal to its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.

## Local context

If `AGENTS.local.md` exists alongside this file, read it and follow it. It holds machine-local
context (private paths, personal tooling, local knowledge-base notes) that is deliberately not
committed to this repository.
