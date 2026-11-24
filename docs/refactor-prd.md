# Rebalancer Refactor: Product Requirements Document (PRD)

**Status:** approved for implementation · **Owner:** Faisal Haroon · **Written:** 2026-11-19
**Target release:** system version `3.0.0` (breaking configuration changes)
**Repository:** `zehnlabs-rebalancer` (this repository) · **Branch:** `feat/refactor-2026`

---

## 0. Read this first (rules for every implementing agent)

1. **This document is the single source of truth.** `docs/refactor.md`.
2. **Do exactly what is specified. Do not add features, settings, abstractions, retries, caches,
   locks, or "improvements" that are not written here.** If you think something is missing, write it down in your work-package report (§17.3) and continue with what is written.
3. **If something here is ambiguous, contradicts the code, or cannot be done as written: stop that
   item, record the problem in your report, and do not guess.** A wrong guess in this system can
   sell or buy real money. Leaving an item unfinished and reported is always acceptable; a guessed
   implementation is not.
4. **Never connect to a real IB Gateway (live or paper) and never send a real ntfy notification.**
   All tests use fakes. Real-broker checks are done later by Faisal (§16.5).
5. **No explicit concurrency code** (locks, semaphores, leases, file locks, threads) beyond what this
   PRD names. The only concurrency allowed is what already exists: one `ProcessPoolExecutor`, the
   asyncio event loop, and `asyncio.gather` over accounts inside a worker.
6. **Requirement IDs** (e.g. `PLAN-7`) must be referenced in commit messages and in your report so
   the implementation can be traced and validated.
7. Keep the code small, typed and readable. Prefer the standard library and the libraries already
   installed (pydantic, PyYAML, aiohttp, ib_async 2.1.0, APScheduler 3.x, exchange_calendars).
   Do not add new runtime dependencies. `pytest` is the only new development dependency.
8. Work on a git branch named `refactor/v3`. One commit (or more) per work package. Do not push and
   do not open pull requests unless Faisal asks.
9. Do not change: the Python version in the Dockerfile (3.12); the module-level
   `app = EventBrokerApp()` in `event-broker/app/main.py` (a test depends on it); the client-ID rule
   (digits of the account ID); the format of `data/market-open/scheduled.json` (a JSON array of
   account-ID strings, written by `tools/schedule.sh`); `tools/rebalance.sh`; `tools/schedule.sh`;
   the version numbers of the four local packages (all stay `1.0.0`).
10. Line endings: the repository uses `.gitattributes` (`*.sh` LF, others `auto`). Do not convert
    files wholesale.

---

## 1. Goal

Each rebalance run should bring an account as close as possible to its strategy's target weights,
using the broker's share granularity, without ever spending more cash than the account has. Failed runs are retried
once, at the next market open, with fresh data. The user gets exactly one notification per account
per run. A second broker or a second notification channel can be added later without redesign.
Here, "as close as possible" means a plan is maximal under the target-weight funding priority in
§7.6. It does not mean solving a global portfolio-optimization problem.

The current engine is replaced by a smaller one. **The behaviour changes on purpose**: no
0.5-point "close enough" band, no scaling of buys, quantities rounded down to broker granularity,
and a percentage cash floor. §3 lists every change.

## 2. Glossary

| Term | Meaning in this document |
|---|---|
| **Account** | One IBKR account listed in `accounts.yaml`. |
| **Strategy** | A Zehnlabs strategy (e.g. `etf-blend-200-35`). Its target weights come from the allocations API. One strategy can drive many accounts. |
| **Signal** | A message on the strategy's Ably channel. It triggers a run for every enabled account of that strategy. |
| **Run** | One attempt to rebalance one account. It has a `run_id`. |
| **Batch** | All the runs for one strategy that are started together (one worker process). |
| **Source** | What started the run: `signal` (Ably), `manual` (`tools/rebalance.sh`), `scheduled` (the market-open job). |
| **Mode** | `live` (`exec: rebalance`, places orders) or `preview` (`exec: print-rebalance`, places nothing). |
| **Weight** | A fraction of the account. `0.25` means 25%. Weights from the API are fractions. |
| **Targets** | The final weights for this account after validation, scaling, and replacement (§7.1–7.2). They always sum to exactly 1.0, unless the list is empty. |
| **Mark** | The price used to value a holding (§7.3). |
| **Buy reference** | The price used to size a buy (§7.3). |
| **Limit price** | The highest price a buy order may pay: buy reference × 1.005, rounded (§7.3). |
| **Total value (V)** | `cash + Σ(holding quantity × mark)`, computed by us. It is not IBKR's NetLiquidation. |
| **Cash floor / reserve** | `minimum_cash_reserve_percent` of V at the start of the run. Buys never take cash below it. |
| **Budget** | Cash available for buys: `cash_base − reserve` (§7.5). |
| **Own order** | A working order on this account placed by this app, i.e. with this account's client ID. |
| **Foreign order** | A working order on this account placed by anything else (manual in TWS or the mobile app, another program). |
| **Working order** | An order that is not yet filled, cancelled or rejected. |
| **Whole-share broker** | A broker whose `supports_fractional` is `False`. IBKR is one. |
| **Fractional broker** | A broker whose `supports_fractional` is `True`; quantities use `0.000001` granularity. |

## 3. Scope

### 3.1 Behaviour changes (what users will notice)

| ID | Change |
|---|---|
| SCOPE-1 | The 0.5-point allocation band (`allocation_threshold_percent`) is **removed**. Every run aligns every symbol to its target as closely as broker granularity allows. Deposits are invested on the next run. |
| SCOPE-2 | The 1% commission divisor (`commission_rate`) is removed. |
| SCOPE-3 | Buy scaling, the adjustment pass, `max_account_utilization`, the "skip if cash is low and all symbols are held" shortcut, and the separate affordability filter are removed. One rule replaces them: when cash is short, fund raw buys by target weight descending, with reverse-alphabetical symbol priority for equal weights (§7.6). |
| SCOPE-4 | Share quantities are always **rounded down** to broker granularity: whole shares for IBKR and `0.000001` for a fractional broker. An IBKR target smaller than one share is not bought. The README promise "at least one share of every target" is removed. |
| SCOPE-5 | `cash_reserve_percent` (per account) and `minimum_cash_reserve_usd` (global, $100) are removed. They are replaced by one global setting, `trading.minimum_cash_reserve_percent` (default `0.25`). |
| SCOPE-6 | PDT protection is removed completely (setting, service, state files, notification wording). |
| SCOPE-7 | Replacement sets move into code. `replacement-sets.yaml` and its Docker mount are deleted. The replacement algorithm is fixed (§7.2). |
| SCOPE-8 | Target weights from the API must total within 1% of 100%. They are then scaled to exactly 100%. Otherwise the run fails and the user is notified. An empty list means "sell everything". |
| SCOPE-9 | A live run triggered while the market is closed places no orders and is queued for the next market open. A live run that fails is queued for the next market open. A failed market-open run is not retried. |
| SCOPE-10 | The market-open job moves from 09:30 to **09:35 ET**. |
| SCOPE-11 | At the start of every live run, the app cancels this account's own working orders and waits for confirmation. If the account has foreign working orders, the run stops and the user is told. |
| SCOPE-12 | `config.yaml` and `accounts.yaml` are validated strictly at startup. Unknown or removed keys stop the service, with a message naming each key. |
| SCOPE-13 | One notification per account per run, with warnings folded in (today: a second "warnings" message). |
| SCOPE-14 | 30 days of per-run investigation records in `data/runs/`. |
| SCOPE-15 | Account data that is missing (cash, NetLiquidation, positions feed) fails the run. It is never treated as zero. |

### 3.2 Non-goals (do NOT do these)

- Fractional-share ordering for IBKR. `supports_fractional` exists, but IBKR returns `False`.
- Batching allocation fetches per strategy. One fetch per account is intended.
- Handling Ably messages missed during a disconnect.
- Changing the ntfy server, message channel, or adding channels.
- Any change to the IB Gateway container, `tools/*.sh`, or `.env` variables.
- Moving `app = EventBrokerApp()` out of module import.
- A "pending signal" queue for a strategy that is already running. Such signals are dropped (§10.6).
- Cancelling orders when a run fails or times out ("cancel on the way out"). Not wanted.
- Any use of `reqGlobalCancel`. Forbidden: it cancels every order of every account on the login.

---

## 4. Target architecture

### 4.1 Packages and modules

Keep the four local packages and the `event-broker` app. Their responsibilities change as follows.

```
packages/app-config/src/app_config/
    models.py        AppConfig and sections (strict, §5.1)
    loader.py        load_config(path) -> AppConfig; get_config() kept for the parent process only
packages/broker-connector-base/src/broker_connector_base/
    models.py        domain models (§6.1): Quote, Position, AccountState, OrderRequest,
                     OrderState, WorkingOrder, enums
    connection.py    BrokerConnection (ABC, §6.2)
    errors.py        BrokerError and its subclasses (§6.3)
packages/rebalance-calculator/src/rebalance_calculator/
    targets.py       validate_and_scale(raw) and apply_replacements(...) (§7.1–7.2)
    replacement_sets.py   REPLACEMENT_SETS constant + definition validation (§7.2)
    planner.py       plan_sells(...), plan_buys(...) (§7.4–7.6), pure functions
    models.py        RawAllocation, Targets, SellPlan, BuyPlan, PlannedOrder, Note
packages/ibkr-connector/src/ibkr_connector/
    connection.py    IBKRConnection(BrokerConnection) (§9)
event-broker/app/
    main.py          EventBrokerApp (startup order §13.1), manual-event watcher, heartbeat
    accounts.py      AccountConfig model, load_accounts(...) -> AccountRegistry (§5.2)
    settings.py      RuntimeSettings, read once from environment variables (§5.3)
    allocations.py   AllocationsClient: fetch raw allocations over HTTP (§8.2)
    market_clock.py  MarketClock (§10.1)
    rebalance.py     rebalance_account(...) -> AccountRunResult. The one operation (§8)
    results.py       AccountRunResult, RunOutcome, FailureReason (§8.4)
    services/
        ably_service.py        subscribe and route signals (logic largely unchanged)
        strategy_executor.py   StrategyExecutor: pool, running-strategy set, post-run handling (§10)
        trading_executor.py    worker entry point execute_strategy_batch(...) (§10.3)
        scheduler_service.py   market-open job + schedule-file helpers (§10.4)
        notification_service.py  Notifier ABC + NtfyNotifier + message rendering (§11)
        run_records.py         write and prune data/runs/*.jsonl (§12)
```

### 4.2 Dependency rules

- `broker-connector-base` depends on nothing local.
- `rebalance-calculator` depends only on `broker-connector-base`. It does **not** import `app_config`
  and does no I/O and no logging side effects. Its functions return notes; they do not log.
- `ibkr-connector` depends on `broker-connector-base` and `app-config`. **Remove** its
  `rebalance-calculator` dependency from `setup.py`.
- `event-broker/app` may import all four packages.
- No package calls `get_config()` or reads environment variables. Configuration objects are passed
  in through constructors and function parameters. `get_config()` stays only for the parent process
  in `event-broker/app`.

### 4.3 Files to delete

`replacement-sets.yaml`; `event-broker/app/services/pdt_protection_service.py`;
`packages/broker-connector-base/src/broker_connector_base/base_rebalancer.py`;
`packages/broker-connector-base/src/broker_connector_base/base_client.py` (replaced by `connection.py`);
`packages/ibkr-connector/src/ibkr_connector/{rebalancer.py,client.py,models.py,allocation_service.py,replacement_service.py}`
(replaced as in §4.1); `packages/rebalance-calculator/src/rebalance_calculator/calculator.py`;
`event-broker/app/trading/broker_factory.py` (the broker choice moves to one function, §8.3).
Remove the `PDTCheckResult` model. Remove every `SubprocessManager` and `CachedPrice` reference.

---

## 5. Configuration

### 5.1 `config.yaml` (strict)

**CFG-1.** Every pydantic model in `app_config/models.py` sets
`model_config = ConfigDict(extra="forbid")`. Unknown keys at any level are errors.

**CFG-2.** The complete new schema follows. Defaults are the values shown. Ranges are validated.
Replace the repository's `config.yaml` with exactly these keys and short comments.

```yaml
ibkr:
  request_timeout_seconds: 15.0            # 1–120 (above IBKR's 11 s snapshot window)
  connection_timeout_seconds: 10           # 1–120
  connection_stabilization_delay_seconds: 0.5   # 0–10
  market_data_type: 1                      # 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
  order_placement_delay_seconds: 1.0       # 0–10, pause after each placed order
  market_data_retry_delay_seconds: 2.0     # 0.1–30
  market_data_max_retries: 10              # 0–30
  ports:
    live_internal: 4003
    paper_internal: 4004
trading:
  minimum_cash_reserve_percent: 0.25       # 0–5. Cash floor as % of the account's total value
  buy_slippage_percent: 0.5                # 0–5. Limit price = buy reference × (1 + this/100)
  order_timeout_seconds: 300               # 10–3600, per leg
  order_status_check_interval_seconds: 2.0 # 0.1–30
  cancel_confirm_timeout_seconds: 30       # 1–300
  cash_update_timeout_seconds: 30          # 0–300
service:
  heartbeat_interval_seconds: 1.0
  manual_event_check_interval_seconds: 1.0
  error_recovery_delay_seconds: 5.0
  manual_event_file_path: "/app/data/manual-rebalance/rebalance.json"
scheduler:
  enabled: true
  market_open_time: "09:35"                # HH:MM, ET
  scheduled_file_path: "/app/data/market-open/scheduled.json"
executor:
  max_workers: 32
api:
  rebalance_auth_timeout_seconds: 10
  allocation_timeout_seconds: 30
```

**CFG-3. Removed keys.** If any of these is present, startup fails with the specific message shown
(one line per offending key, all offending keys listed together):

| Key | Message |
|---|---|
| `ibkr.price_cache_ttl_seconds` | `config.yaml: 'ibkr.price_cache_ttl_seconds' is no longer supported (the price cache was removed). Delete this line.` |
| `ibkr.synthetic_ask_offset_usd` | `... (prices now fall back to last/close/bar). Delete this line.` |
| `trading.minimum_cash_reserve_usd` | `... replaced by 'trading.minimum_cash_reserve_percent'. Delete this line.` |
| `trading.commission_rate` | `... (commissions are covered by 'trading.minimum_cash_reserve_percent'). Delete this line.` |
| `trading.max_account_utilization` | `... (removed with buy scaling). Delete this line.` |
| `trading.allocation_threshold_percent` | `... (every run now aligns all symbols to target). Delete this line.` |
| `trading.order_tif` | `... (orders are always DAY). Delete this line.` |
| `trading.post_completion_delay_seconds` | `... replaced by 'trading.cash_update_timeout_seconds'. Delete this line.` |
| `replacement` (whole section) | `config.yaml: section 'replacement' is no longer supported (replacement sets are built in). Delete this section.` |
| `pdt_protection` (whole section) | `config.yaml: section 'pdt_protection' is no longer supported (PDT protection was removed). Delete this section.` |

In the table, `...` stands for `config.yaml: '<key>' is no longer supported`.

Any other unknown key produces: `config.yaml: unknown setting '<dotted.key>'. Check the spelling or delete it.`
Implementation hint: check removed and unknown keys on the raw YAML dict before building the
model, so the messages are exact. Then build the model, and let pydantic range errors surface as
`config.yaml: <pydantic message>`.

**CFG-4.** `load_config` logs the validated model once (one `logger.info` with
`model_dump()`), replacing the 14 hand-written lines. There are no secrets in `AppConfig`.

**CFG-5.** Startup with a config error exits with a non-zero code (existing behaviour in
`main.py`), after logging every message at ERROR level.

### 5.2 `accounts.yaml` (strict)

**ACC-1.** Model (in `event-broker/app/accounts.py`):

```python
class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: str          # pattern ^[A-Z0-9]+$, must contain at least one digit
    type: Literal["paper", "live"]
    enabled: bool = True
    strategy_name: str       # pattern ^[a-z0-9]+(-[a-z0-9]+)*$
    replacement_set: str | None = None   # must be a key of REPLACEMENT_SETS
    broker: Literal["ibkr"] = "ibkr"

    @property
    def client_id(self) -> int: ...   # int of the digits in account_id (existing rule)

class AccountsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounts: list[AccountConfig]
```

**ACC-2.** `load_accounts(path: Path, trading_mode: str) -> AccountRegistry` validates the **whole
file**, including disabled accounts and accounts of the other trading mode. It collects all
problems and fails once, listing them all. Message formats:

- Removed keys:
  `accounts.yaml: account #<n> (<account_id>): 'cash_reserve_percent' is no longer supported. Delete it; the cash floor is now 'trading.minimum_cash_reserve_percent' in config.yaml.`
  `accounts.yaml: account #<n> (<account_id>): 'pdt_protection_enabled' is no longer supported (PDT protection was removed). Delete it.`
- Unknown key: `accounts.yaml: account #<n> (<account_id>): unknown setting '<key>'.`
- Unknown replacement set: `accounts.yaml: account #<n> (<account_id>): replacement_set '<x>' does not exist. Valid: <comma-separated names>.`
- Duplicate account: `accounts.yaml: account '<account_id>' is listed more than once (#<n>, #<m>).`
- Duplicate client ID among accounts of the **active** trading mode:
  `accounts.yaml: accounts '<a>' and '<b>' produce the same IBKR client ID <id>; they cannot both be active.`
- Client ID out of range (> 2,147,483,647): `accounts.yaml: account '<a>': client ID <id> is too large.`
- Any other validation error: `accounts.yaml: account #<n> (<account_id or "?">): <pydantic message>`.

`#<n>` is the 1-based position in the file.

**ACC-3.** `AccountRegistry` is a small concrete class holding only the **active** accounts
(enabled and `type == trading_mode`). Methods: `get(account_id) -> AccountConfig | None`,
`for_strategy(name) -> list[AccountConfig]`, `strategies() -> list[str]` (sorted).

**ACC-4.** Update `accounts.yaml.example` so it has no removed keys. Faisal edits his own
`accounts.yaml`. Do not edit it, and do not commit it.

### 5.3 Environment variables

**ENV-1.** `RuntimeSettings` (pydantic, frozen) is built once in the parent from: `TRADING_MODE`
(`paper`|`live`, default `paper`), `IB_HOST` (default `ibkr-gateway`), `IB_PORT` (optional int),
`ALLOCATIONS_BASE_URL` (default `DEFAULT_ALLOCATIONS_BASE_URL`), `ZEHNLABS_API_KEY` (required,
non-blank), `USER_NOTIFICATIONS_ENABLED` (`true`/`false`, default `false`, case-insensitive) and
`USER_NOTIFICATIONS_CHANNEL` (string, default empty). It is passed to workers as an argument
(it is picklable). Remove `StrategyExecutor._get_environment()` and the `os.environ` copying in
`execute_strategy_batch`.

**ENV-2.** Never log `ZEHNLABS_API_KEY` or the Ably key. Give `RuntimeSettings` a `__repr__`
that masks the API key.

---

## 6. Domain model and broker interface (`broker-connector-base`)

### 6.1 Models

Use `@dataclass(frozen=True, slots=True)` for these plain value types. They never cross a process
boundary; only `AccountRunResult` (§8.4, pydantic) does. Prices are `float`. Quantities
are `float`. For a whole-share broker, every quantity is a whole number.

```python
class Side(StrEnum): BUY = "BUY"; SELL = "SELL"

class OrderStatus(StrEnum):
    WORKING = "WORKING"; FILLED = "FILLED"; CANCELLED = "CANCELLED"; REJECTED = "REJECTED"

class PriceSource(StrEnum):
    MID = "mid"; ASK = "ask"; BID = "bid"; LAST = "last"; CLOSE = "close"; BAR = "bar"

@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float | None; ask: float | None; last: float | None
    close: float | None; bar_close: float | None
    # Each field is a finite number > 0, or None. The connector converts NaN, 0, negatives to None.
    def mark(self) -> tuple[float, PriceSource]: ...          # §7.3
    def buy_reference(self) -> tuple[float, PriceSource]: ... # §7.3
    def sell_reference(self) -> tuple[float, PriceSource]: ...# §7.3
    # Each raises NoPriceError(symbol) if the chain has no value.

@dataclass(frozen=True)
class Position:
    symbol: str; quantity: float          # quantity > 0 (see IB-6)

@dataclass(frozen=True)
class AccountState:
    account_id: str
    cash: float                           # USD cash, may be negative (margin debt)
    net_liquidation: float                # IBKR's NetLiquidation, USD
    positions: tuple[Position, ...]       # one entry per symbol

@dataclass(frozen=True)
class OrderRequest:
    symbol: str; side: Side; quantity: float
    limit_price: float | None             # None = market order. Buys: always a limit. Sells: always market.
    run_id: str                           # written to the order's reference field

@dataclass(frozen=True)
class OrderState:
    handle: str                           # opaque id returned by place()
    symbol: str; side: Side; quantity: float
    status: OrderStatus
    filled_quantity: float
    average_fill_price: float | None
    message: str | None                   # broker's last error/warning text, if any

@dataclass(frozen=True)
class WorkingOrder:
    symbol: str; side: Side; quantity: float
    own: bool                             # True if placed by this app (this account's client ID)
```

### 6.2 `BrokerConnection` (abstract base class)

One connection object serves **one account**. The account ID is given to the constructor.

```python
class BrokerConnection(ABC):
    @property
    @abstractmethod
    def supports_fractional(self) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...          # raises ConnectionFailedError
    @abstractmethod
    async def close(self) -> None: ...            # never raises; idempotent
    @abstractmethod
    async def account_state(self) -> AccountState: ...         # raises AccountDataError
    @abstractmethod
    async def quotes(self, symbols: Collection[str]) -> dict[str, Quote]: ...  # raises PricingError
    @abstractmethod
    async def working_orders(self) -> list[WorkingOrder]: ...  # own + foreign, this account only
    @abstractmethod
    async def cancel_own_orders(self, timeout_seconds: float) -> int: ...
        # cancels every own working order, waits until each is done; returns the count;
        # raises CancelNotConfirmedError if any is still working at the timeout
    @abstractmethod
    async def place(self, order: OrderRequest) -> str: ...     # returns a handle; raises OrderPlacementError
    @abstractmethod
    async def order_states(self, handles: Sequence[str]) -> list[OrderState]: ...
```

Also provide `async with` support in the base class: `__aenter__` calls `connect()` and returns
`self`; `__aexit__` calls `close()`.

### 6.3 Errors

`BrokerError(Exception)` and these subclasses: `ConnectionFailedError`, `AccountDataError`,
`PricingError`, `NoPriceError(PricingError)`, `CancelNotConfirmedError`, `OrderPlacementError`.
Messages are user-readable and never contain credentials.

---

## 7. Rebalance algorithm (`rebalance-calculator`, pure)

All functions in this section are pure: no I/O, no clock, no logging, no config lookups. They
return notes (`Note(level: "info"|"warning", text: str)`), which the caller logs and puts in the
notification. Use `EPS = 1e-9` everywhere a tolerance is needed.

Helper used everywhere:

```python
def round_quantity(x: float, supports_fractional: bool) -> float:
    if x <= 0: return 0.0
    if not supports_fractional:
        return float(math.floor(x + EPS))
    return math.floor(x * 1_000_000 + EPS) / 1_000_000   # 6 decimals, rounded down
```

### 7.1 Validating and scaling strategy weights

Input: a list of `RawAllocation(symbol: str | None, weight: float | None)` from the API.

**TGT-1.** Normalise each symbol with `strip().upper()`. Then fail with
`InvalidTargetsError` (failure reason `TARGETS_INVALID`) if any of these holds:
- a symbol is missing or empty, or does not match `^[A-Z0-9.\-]{1,12}$`;
- a weight is missing, not a finite number, or negative;
- a symbol appears more than once (after normalisation);
- the list is non-empty and `abs(sum(weights) − 1.0) > 0.01`.

The error message names the problem, e.g. `Strategy weights total 97.20%, outside 99%–101%.` or
`Strategy lists SPY twice.`

**TGT-2.** An **empty list is valid** and means "hold nothing": the targets are empty, so every
position is sold (§7.4) and nothing is bought. Add an info note: `Strategy has no allocations: selling all positions.`

**TGT-3.** Otherwise divide every weight by the sum, so the weights sum to 1.0. If the original sum
differed from 1.0 by more than `EPS`, the caller logs it (INFO), e.g.
`weights scaled from 99.70% to 100%`. It is **not** a user notification.

**TGT-4.** Weight-0 entries are allowed. They are removed from the targets, so that symbol is sold
if held.

**TGT-5. Order of steps (critical).** `validate_and_scale` (TGT-1..4) runs on the raw API weights,
**before** replacements. Never run the 1% check after replacements: replacements change the total
on purpose.

### 7.2 Replacement sets

**REP-1.** Replacement sets live in code (`replacement_sets.py`):

```python
@dataclass(frozen=True)
class ReplacementRule:
    source: str; target: str; scale: float

REPLACEMENT_SETS: dict[str, tuple[ReplacementRule, ...]] = {
    "ira": (
        ReplacementRule("DBC",  "PDBC", 1.0),
        ReplacementRule("UUP",  "USDU", 1.0),
        ReplacementRule("VIXY", "VXX",  1.0),
        ReplacementRule("VIXM", "VXZ",  1.0),
        ReplacementRule("UVXY", "VXX",  1.5),
        ReplacementRule("SVXY", "USMV", 1.0),
    ),
}
```

**REP-2. Definition validation** runs at import time. It raises `ValueError` (so the service cannot
start) if, in any set: a symbol is empty or not upper case; `scale < 1.0` or not finite;
`source == target`; a source appears twice; or a target is also a source in the same set (no
chains). A unit test asserts the shipped sets pass, and one test per rule asserts that a bad
definition is rejected.

**REP-3. Algorithm** `apply_replacements(weights: dict[str, float], rules) -> (dict[str, float], list[Note])`.
Input weights already sum to 1.0 (§7.1).

```
protected = {}   # weight created by replacement rules, by target symbol
ordinary  = {}   # weight of symbols with no rule (including direct holdings of a target symbol)
for symbol, w in weights:
    if symbol has a rule r:  protected[r.target] += w * r.scale
    else:                    ordinary[symbol]    += w
P = sum(protected); O = sum(ordinary)
if P >= 1:                              # replacements alone fill the account
    result = {s: w / P for s, w in protected}          # ordinary symbols get 0
    note warning: "Replacement exposure is {P:.1%}; holding {targets} only, at 100%."
elif P + O > 1 + EPS:                   # shrink the ordinary symbols to make room
    f = (1 - P) / O
    result = protected + {s: w * f for s, w in ordinary}   # add when a symbol is in both
    note info: "Replacements need {P+O−1:.1%} extra; other symbols reduced by {(1−f):.1%}."
else:
    result = protected + ordinary
drop symbols whose result weight is 0
assert abs(sum(result) - 1) < 1e-9
```

Why protect the replacement weight: the replaced instruments are the volatile ones (that is why
their scale is above 1). Shrinking them would move the account furthest from the strategy.

**REP-4.** When no replacement set is configured, `apply_replacements` is not called.

**REP-5.** The run logs the raw API weights, the scaled weights and the final targets (§12.1).

### 7.3 Prices

**PRC-1. Mark** (valuing holdings and the total): `mid = (bid + ask) / 2` if both exist and
`ask >= bid`; else `last`; else `close`; else `bar_close`; else `NoPriceError`.

**PRC-2. Buy reference** (sizing buys and setting the limit): `ask`; else `last`; else `close`;
else `bar_close`; else `NoPriceError`. A buy never requires a bid.

**PRC-3. Sell reference** (preview estimates only): `bid`; else `last`; else `close`; else
`bar_close`.

**PRC-4. Limit price** = `buy_reference × (1 + buy_slippage_percent / 100)`, rounded half-up to
2 decimals if the reference is ≥ 1.00, otherwise to 4 decimals. Use `decimal.Decimal` with
`ROUND_HALF_UP` for this rounding.

**PRC-5.** Never invent a price with a fixed dollar offset (the old `bid + $1.00` rule is
deleted). Record the `PriceSource` used for every symbol in the run record. Any source other than
`mid` (for marks) or `ask` (for buys) adds an info note:
`Priced XYZ from last trade (no live quote).`

### 7.4 Sell plan

`plan_sells(positions, targets, marks, cash, supports_fractional) -> SellPlan`

```
V = cash + Σ(p.quantity × marks[p.symbol] for p in positions)
for p in positions, in symbol order:
    w = targets.get(p.symbol, 0)
    if w == 0:                                   # not a target: sell all of it
        qty = round_quantity(p.quantity, supports_fractional)
        leftover = p.quantity − qty
        if leftover > EPS: add warning note:
            "{symbol}: {leftover:.4f} shares can't be sold through the API (fractional). Sell them manually in TWS or the IBKR app."
    else:                                        # a target that is over its weight
        excess_value = p.quantity × mark − w × V
        qty = 0 if excess_value <= 0 else
              min(round_quantity(p.quantity, sf), round_quantity(excess_value / mark, sf))
    if qty > 0: emit PlannedOrder(symbol, SELL, qty, limit_price=None)
return SellPlan(total_value=V, orders=[...], notes=[...])
```

In the pseudocode, `sf` means the `supports_fractional` argument.

**SELL-1.** A sell quantity never exceeds the whole shares held (or the held quantity for a
fractional broker). There is no rounding up anywhere.
**SELL-2.** Sells are market orders, DAY, regular hours only.
**SELL-3.** The sell plan is computed once per run, before any order is placed.

### 7.5 Cash available for buys

**CASH-1.** `reserve = minimum_cash_reserve_percent / 100 × V0`, where `V0` is the sell plan's
`total_value` at the start of the run. The reserve is computed once per run.

**CASH-2.** `cash_base` (the cash the buy plan may count on):
- if no sells were placed: `cash_base = state0.cash` (the cash read at the start);
- otherwise: `cash_base = min(reported_cash_after_wait, state0.cash + Σ(filled_qty × avg_fill_price))`
  over the filled sells. `reported_cash_after_wait` comes from §8.1 step 13.

Taking the lower of the two numbers can never overstate cash. If IBKR's cash figure lags, the buy
leg is a little smaller this run.

**CASH-3.** `budget = cash_base − reserve`. If `budget <= 0`, no buys are placed. Add a warning note:
`Cash ${cash_base:,.2f} is at or below the minimum reserve ${reserve:,.2f}; no buys placed.`

### 7.6 Buy plan

`plan_buys(positions_after, targets, quotes, cash_base, reserve, slippage_pct, supports_fractional) -> BuyPlan`

```
V1 = cash_base + Σ(p.quantity × mark(p.symbol) for p in positions_after)
for symbol, w in targets (symbol order):
    held_value = held_qty(symbol) × mark(symbol)
    gap = w × V1 − held_value
    if gap <= 0: continue
    ref = buy_reference(symbol); limit = limit_price(ref)
    qty = round_quantity(gap / ref, sf)
    if qty == 0:
        if held_qty(symbol) == 0: add info note
            minimum = "one share" if not sf else "the broker minimum of 0.000001 share"
            "{symbol}: target ${w×V1:,.2f} would buy less than {minimum} at ${ref:,.2f}; not bought."
        continue
    append raw buy (symbol, target weight, qty, limit)
budget = cash_base − reserve
if budget <= 0: drop all buys (CASH-3 note) and return
raw_cost = Σ(raw.qty × raw.limit)
if raw_cost > budget + EPS:
    remaining = budget
    # highest target weight first; equal weights use reverse-alphabetical symbols
    for raw in sorted(raw_buys, key=(target weight, symbol), reverse=True):
        affordable = round_quantity(remaining / raw.limit, sf)
        final.qty = min(raw.qty, affordable)
        if final.qty × raw.limit > remaining + EPS:
            reduce final.qty by one broker-sized unit
        remaining −= final.qty × raw.limit
else:
    final quantities = raw quantities
trimmed = raw symbols whose final quantity is less than their raw quantity
emit positive final orders in normal symbol order
final_cost = Σ(final.qty × final.limit)
if final_cost > budget + EPS: raise RuntimeError        # fail closed in optimized builds too
```

**BUY-1.** Recompute the final limit-price cost and check at runtime that
`Σ(final_qty × limit) <= budget + EPS`. This is the safety invariant: even if every buy fills at
its limit price, cash stays at or above the reserve. An `assert` alone is not sufficient.
**BUY-2.** Higher target weights receive budget first. For equal weights, reverse-alphabetical
funding priority preserves the existing rule that alphabetically earlier symbols absorb a
shortfall first. A higher-priority unaffordable share does not block affordable lower-priority
shares. This priority defines "as close as possible"; it is not a global portfolio optimizer.
**BUY-3.** Derive warnings and the run record's `trimmed` symbols from the final quantities. If
anything was trimmed, add a warning note listing each genuinely trimmed symbol:
`Not enough cash for all buys: TLT reduced by 3 shares; XLU skipped (2.0% target).`
Fractional reductions show up to six decimals and must never be truncated through `int`.
**BUY-4.** Every final quantity is between zero and its raw desired quantity and respects broker
granularity (whole shares for IBKR, `0.000001` for a fractional broker). For every underfunded raw
buy, the final remaining budget cannot fund one more broker-sized unit at that buy's limit price.
**BUY-5.** The buy plan never contains sells. Positions over target at this point are left alone.
**BUY-6.** Buys are emitted in normal symbol order and are limit orders at `limit`, DAY, regular
hours only. Funding priority never changes placement order.

### 7.7 Properties the planner must satisfy (tested in §16.2)

P1 no sell exceeds the held quantity; P2 no position goes negative; P3 every emitted quantity is
positive and respects broker granularity; P4 `Σ(final_qty × limit) <= budget + EPS` whenever the
budget is positive; P5 the buy plan has no sells, and the sell plan no buys; P6 targets sum to 1
within 1e-9 (or are empty); P7 identical inputs give an identical plan (deterministic order: sells
by symbol, buys by symbol); P8 `0 <= final_qty <= raw_qty`; P9 for every underfunded raw buy, the
final remainder cannot fund one more broker-sized unit at its limit price.

---

## 8. The account run (`event-broker/app/rebalance.py`)

### 8.1 Steps

`async def rebalance_account(account, mode, source, deps) -> AccountRunResult` is **the one
operation** called for signals, manual runs and scheduled runs. `deps` holds the `AppConfig`, the
`RuntimeSettings`, an `AllocationsClient`, a connection factory
(`Callable[[AccountConfig], BrokerConnection]`), a `MarketClock`, and a logger adapter.

It never raises. Every exception becomes a failed result. Steps, in order:

| # | Step | On failure (reason, §8.4) |
|---|---|---|
| 1 | Create `run_id` = `<YYYYmmdd-HHMMSS ET>-<account_id>-<4 hex chars>`. Bind it to the logger. | — |
| 2 | Fetch raw allocations for `account.strategy_name` (§8.2). | `ALLOCATIONS_UNAVAILABLE` |
| 3 | `validate_and_scale` (§7.1), then `apply_replacements` if the account has a replacement set (§7.2). | `TARGETS_INVALID` |
| 4 | Live mode only: if `MarketClock.is_open(now)` is false, return outcome `MARKET_CLOSED` with `error_message = "The market was closed."` (no connection made). | — |
| 5 | Open the broker connection (`async with`). | `CONNECTION_FAILED` |
| 6 | Live only: `cancel_own_orders(cancel_confirm_timeout_seconds)`. Note the count if > 0: `Cancelled 2 earlier orders left by this app.` | `CANCEL_NOT_CONFIRMED` |
| 7 | `working_orders()`. Live: if any foreign order exists, fail, listing each (`BUY 50 QQQ`). Preview: list them as an info note. | `FOREIGN_ORDERS` |
| 8 | `state0 = account_state()`. | `ACCOUNT_DATA_UNAVAILABLE` |
| 9 | `quotes0 = quotes(target symbols ∪ held symbols)`; compute marks. | `PRICING_FAILED` |
| 10 | `plan_sells(...)` → `V0`. Cross-check: if `state0.net_liquidation > 0` and `abs(V0 − NLV) / NLV > 0.02`, live: fail; preview: warning note. If `NLV <= 0` and `V0 > 0`, the same. | `ACCOUNT_DATA_MISMATCH` |
| 11 | Compute the reserve (§7.5). Log the plan line (§12.1). **Preview:** simulate the sells at the sell reference, compute cash, run `plan_buys` with `quotes0`, and return outcome `PREVIEWED` with both plans. Stop here. | — |
| 12 | If there are sells: check `is_open(now)` (else fail, `MARKET_CLOSED_DURING_RUN`); place each sell; wait for all (§8.5). | `ORDER_PLACEMENT_FAILED`, `ORDERS_FAILED`, `ORDERS_TIMED_OUT` |
| 13 | If any sell filled: poll `account_state().cash` every 1 s for up to `cash_update_timeout_seconds`. Stop early when `reported_cash >= state0.cash + 0.98 × gross_proceeds`. Compute `cash_base` (CASH-2). If the timeout is reached, log a WARNING (not a failure). | `ACCOUNT_DATA_UNAVAILABLE` (only if `account_state()` raises) |
| 14 | `positions_after` = `state0` positions minus filled sell quantities (computed by us; do not wait for IBKR's positions feed). Drop zero quantities. | — |
| 15 | `quotes1 = quotes(target symbols ∪ symbols in positions_after)`. Fresh prices for the buy leg. | `PRICING_FAILED` |
| 16 | `plan_buys(...)`. Log the buy plan line. | — |
| 17 | If there are buys: check `is_open(now)` (else fail, `MARKET_CLOSED_DURING_RUN`); place each buy; wait for all (§8.5). | as step 12 |
| 18 | Return `SUCCEEDED` with the executed orders, notes, and end estimates: `cash_end = cash_base − Σ(buy fill qty × avg price)` and `value_end = V1` (the buy plan's total). | — |

**RUN-1.** Every failure result records **what already happened**: the fills of every order this
run placed (symbol, side, filled quantity, average price), from the latest `order_states`. If
nothing was placed, it says so.
**RUN-2.** On failure the run does **not** cancel its own orders. The next run cancels them (step 6).
**RUN-3.** `close()` is always called (the `async with` guarantees it).
**RUN-4.** The connection factory is the single place where `account.broker` is mapped to a
connection class (today only `"ibkr"` → `IBKRConnection`).

### 8.2 Allocations client

**ALC-1.** `AllocationsClient(base_url, api_key, timeout_seconds)` is used as an async context
manager owning **one** `aiohttp.ClientSession`, which the worker shares across its accounts.
`fetch(strategy) -> list[RawAllocation]` calls `GET {base_url}/{strategy}/allocations` with header
`x-api-key`.
**ALC-2.** Keep today's response parsing: the body must be a JSON object with
`status == "success"` and `data.allocations` a list; each item uses `ticker`, else `symbol`, and
`allocation`. A missing `allocation` becomes `None` (rejected by TGT-1); it no longer defaults to 0.
**ALC-3.** On a non-200 status, the error message contains the status code only, **never the
response body** (it may echo credentials). Timeouts and transport errors use
`Allocations API unavailable for <strategy> (timeout)` / `(network error)`.

### 8.3 Order placement details

**ORD-1.** Place sells first (all), wait for them, then buys (all), as in §8.1. Within a leg,
place orders in symbol order.
**ORD-2.** If `place()` raises during a leg, stop placing, read `order_states` once for what was
placed, and fail with `ORDER_PLACEMENT_FAILED`.

### 8.4 Result types (`results.py`)

```python
class RunOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"; FAILED = "FAILED"; MARKET_CLOSED = "MARKET_CLOSED"; PREVIEWED = "PREVIEWED"

class FailureReason(StrEnum):       # each member's value equals its name
    ALLOCATIONS_UNAVAILABLE; TARGETS_INVALID; CONNECTION_FAILED; CANCEL_NOT_CONFIRMED;
    FOREIGN_ORDERS; ACCOUNT_DATA_UNAVAILABLE; ACCOUNT_DATA_MISMATCH; PRICING_FAILED;
    MARKET_CLOSED_DURING_RUN; ORDER_PLACEMENT_FAILED; ORDERS_FAILED; ORDERS_TIMED_OUT;
    WORKER_CRASHED; INTERNAL_ERROR

class ExecutedOrder(BaseModel): symbol; side; quantity; filled_quantity; average_fill_price | None; status
class PlannedOrderOut(BaseModel): symbol; side; quantity; limit_price | None   # mirror of PlannedOrder
class NoteOut(BaseModel): level: Literal["info", "warning"]; text: str         # mirror of Note

class AccountRunResult(BaseModel):         # pydantic: it crosses the process boundary
    run_id: str; account_id: str; strategy_name: str
    mode: Literal["live", "preview"]; source: Literal["signal", "manual", "scheduled"]
    outcome: RunOutcome
    failure_reason: FailureReason | None
    error_message: str | None               # user-readable, no secrets, no stack traces
    executed: list[ExecutedOrder]           # everything placed this run (empty for preview)
    planned_sells: list[PlannedOrderOut]; planned_buys: list[PlannedOrderOut]   # for preview + record
    notes: list[NoteOut]
    value_end: float | None; cash_end: float | None
    record: dict                            # the run record (§12.2)
```

Unexpected exceptions map to `INTERNAL_ERROR`. The message is `Internal error: <ExceptionType>: <str(e)>`
truncated to 300 characters; the full traceback goes to the log only.

### 8.5 Waiting for orders

**WAIT-1.** Poll `order_states(handles)` every `order_status_check_interval_seconds` until no order
is `WORKING`, or until `order_timeout_seconds` has passed since the last order of the leg was placed.
**WAIT-2.** When none is working: if any is not `FILLED` → fail `ORDERS_FAILED`, listing each
(`SELL 40 XLE: Rejected — <message>`). An empty or unknown broker status counts as `WORKING`,
**never** as filled.
**WAIT-3.** At the timeout → fail `ORDERS_TIMED_OUT`, listing each still-working order with its filled
quantity. Do not cancel them (RUN-2).

---

## 9. IBKR connection (`ibkr_connector/connection.py`)

**IB-1. Connect.** `IBKRConnection(account_id, client_id, ibkr_config: IBKRConfig, settings: RuntimeSettings, logger)`.
Port: `settings.ib_port` if set, otherwise `live_internal` / `paper_internal` by trading mode. Call:

```python
await ib.connectAsync(host, port, clientId=client_id, timeout=connection_timeout_seconds,
                      account=account_id, raiseSyncErrors=True,
                      fetchFields=StartupFetch.POSITIONS | StartupFetch.ORDERS_OPEN)
await bounded(ib.reqAccountUpdatesMultiAsync(account_id), request_timeout_seconds)
```

`ORDERS_OPEN` is **required**. Without it the own-order cancel step sees nothing and lets a
duplicate order through. A unit test asserts these exact flags. Completed orders, executions and
startup account updates must **not** be fetched. Request only this account through
`reqAccountUpdatesMultiAsync`; the legacy `reqAccountUpdates` call permits only one account across
Gateway clients and makes concurrent account runs fail with warning 2101. Keep the multi-account
subscription open for the connection lifetime so post-sell cash updates continue to arrive.
After connecting: sleep `connection_stabilization_delay_seconds`; fail if disconnected; fail if
`account_id` is not in `ib.managedAccounts()` (message:
`Account <id> is not available on this IB Gateway login.`); call
`reqMarketDataType(market_data_type)`. Every failure raises `ConnectionFailedError`. `connect()`
returns nothing (no booleans).

**IB-2. Cash.** From `ib.accountValues(account_id)` with currency `USD`: use tag
`$LEDGER-CashBalance` if present, otherwise `CashBalance`. If neither is present →
`AccountDataError("USD cash balance not reported by IBKR")`.

**IB-3. NetLiquidation.** Tag `NetLiquidation`, currency `USD`. Missing →
`AccountDataError("NetLiquidation not reported by IBKR")`. Never default any value to 0.

**IB-4. Values parse.** A value that does not parse as a finite float → `AccountDataError`.

**IB-5. Positions.** `ib.positions(account_id)`; skip quantity 0. One entry per symbol.

**IB-6. Unsupported positions.** If any position has `contract.secType != "STK"`, or a negative
quantity, raise `AccountDataError("Unsupported position: <secType> <symbol> <qty>. Close it manually or remove it from this account.")`.

**IB-7. Contracts.** Qualify `Stock(symbol, "SMART", "USD")` for all needed symbols in **one**
`qualifyContractsAsync(*contracts)` call. Cache `symbol → Contract` on the connection. Any symbol
that does not qualify → `PricingError`. `place()` uses the cache.

**IB-8. Quotes.** `reqTickersAsync(*contracts)`. Convert NaN, ≤ 0 and None to `None`. For symbols
without both bid and ask, retry (only those symbols) up to `market_data_max_retries` times, sleeping
`market_data_retry_delay_seconds` between tries. Then, for symbols with no bid, ask, last or close,
request the latest 1-minute bar: `reqHistoricalDataAsync(contract, endDateTime="", durationStr="1800 S",
barSizeSetting="1 min", whatToShow="TRADES", useRTH=False)` and use the last bar's close. If a symbol
still has no price at all → `NoPriceError`.

**IB-9. Place.** `MarketOrder` for sells, `LimitOrder` for buys. Set `tif="DAY"`,
`account=account_id`, `orderRef=f"zl-rebalancer:{run_id}"`, `outsideRth=False`. The quantity must be a
whole number (`quantity.is_integer()`); otherwise raise `OrderPlacementError` (IBKR does not accept
fractional API orders). Pass `int(quantity)`. Keep the returned `Trade` object in a dict keyed by the
handle (`str(trade.order.orderId)`), then sleep `order_placement_delay_seconds`.

**IB-10. Order states.** Read status **only from the stored `Trade` objects** (never by scanning
`ib.trades()` for a matching order number: numbers repeat across client IDs). Map:
`Filled` → FILLED; `Cancelled`, `ApiCancelled` → CANCELLED; `Inactive` → REJECTED; anything else,
including empty → WORKING. `filled_quantity = trade.orderStatus.filled`,
`average_fill_price = trade.orderStatus.avgFillPrice or None`, `message` = the last
`trade.log` entry's `message`, if not empty.

**IB-11. Own orders and cancel.** Own working orders = `ib.openTrades()` where
`trade.order.account == account_id` and `trade.order.clientId == client_id` and not
`trade.isDone()`. `cancel_own_orders`: call `ib.cancelOrder(trade.order)` for each, then poll every
0.5 s until all are `isDone()` or the timeout passes. Any still working → `CancelNotConfirmedError`
listing them.

**IB-12. Foreign orders.** `working_orders()` calls `await ib.reqAllOpenOrdersAsync()` and returns
every trade with `order.account == account_id` and not done. `own = (order.clientId == client_id)`.
Note for Faisal's paper check (§16.5): it must be confirmed that manual TWS/mobile orders appear here.

**IB-13.** `supports_fractional` returns `False`.

**IB-14. Close.** `ib.disconnect()` if connected; swallow and log errors.

**IB-15.** Never call `reqGlobalCancel`, legacy `reqAccountUpdates`, `reqCompletedOrders`, or
`reqExecutions`. Use targeted `reqAccountUpdatesMultiAsync(account_id)` only as specified by IB-1.

---

## 10. Triggers, market hours, scheduling, retry

### 10.1 MarketClock (`market_clock.py`)

**CLK-1.** Built on `exchange_calendars.get_calendar("XNYS")` and an injectable `now()` (default:
`datetime.now(ZoneInfo("America/New_York"))`).
- `is_open(now) -> bool`: `now` is in a session and `session_open <= now < session_close`, using the
  calendar's real times (13:00 ET closes on half days are included).
- `next_scheduled_run(now) -> datetime`: the first session date whose `market_open_time` (config,
  ET) is strictly after `now`, at that time.
- `is_trading_day(date) -> bool`.

**CLK-2.** Preview runs ignore market hours.

### 10.2 StrategyExecutor (parent process)

**EXE-1.** One `ProcessPoolExecutor(max_workers, mp_context=multiprocessing.get_context("fork"))`.
Explicit `fork`: see the refactor validation, R12. Python 3.14 changes the default.

**EXE-2.** `execute(strategy, accounts, mode, source) -> list[AccountRunResult] | None`:
1. If `strategy` is in `running`: log WARNING
   `Dropped <source> trigger for strategy <s>: a run is already in progress.` Return `None`.
   No notification for `signal` and `manual`. For `scheduled`, see SCH-6.
2. Add to `running`. Submit `execute_strategy_batch(strategy, accounts, mode, source, app_config, settings)`
   to the pool and await it.
3. In a `finally`, remove `strategy` from `running`, **before** any post-processing.
4. If the pool raises `BrokenProcessPool`: build a `FAILED` result with reason `WORKER_CRASHED` for
   each account (message: `The worker process stopped unexpectedly. Orders may have been placed; check the account.`),
   then replace the pool: if `self._pool is the_broken_pool`, assign a new pool (single event-loop
   thread; no lock needed). Any other exception from submitting or awaiting the batch (for example a
   pickling error) produces `INTERNAL_ERROR` results for every account, and the pool is kept.
5. Post-process each result: apply §10.5 (scheduled-file changes), write the run record (§12.2),
   send the notification (§11). Each post-processing step is wrapped so that one failure is logged and
   does not stop the others.

**EXE-3.** Live runs while the market is closed are detected **in the parent before submitting**:
if `mode == live` and not `is_open(now)`, build `MARKET_CLOSED` results for the accounts without starting
a worker, then post-process them. The in-worker check (step 4) is a second guard.

### 10.3 Worker (`trading_executor.py`)

**WRK-1.** `execute_strategy_batch(...)` creates a new event loop, opens one `AllocationsClient`, runs
`rebalance_account` for every account with `asyncio.gather(..., return_exceptions=True)`, converts
any exception into an `INTERNAL_ERROR` result, closes the loop, and returns a `list[dict]`
(`model_dump()`). It must never raise: wrap the whole body in `try/except BaseException` and return
`INTERNAL_ERROR` results for every account. Remove `SubprocessManager` (its signal handlers and
`sys.exit` go too).

**WRK-2.** Logging: a `logging.LoggerAdapter` with `extra = {run_id, account_id, strategy}`. Use one
module-level stdout handler with format
`[%(asctime)s] [%(levelname)s] [%(strategy)s %(account_id)s %(run_id)s] %(message)s`. Do not create
or remove handlers per account.

### 10.4 Market-open job (`scheduler_service.py`)

**SCH-1.** APScheduler 3.x `AsyncIOScheduler`, `CronTrigger(day_of_week="mon-fri", hour, minute)` from
`scheduler.market_open_time` (default 09:35, ET; the container `TZ` is New York). Pass
`misfire_grace_time=300, coalesce=True, max_instances=1`. Pin `APScheduler>=3.10,<4` in
`event-broker/requirements.txt`.

**SCH-2.** When it fires: if `not is_trading_day(today)`, log and return (entries stay for the next
trading day).

**SCH-3.** Read `scheduled.json`. If it is missing → treat as empty. If it is not a JSON array of
strings → log ERROR and leave the file untouched (do not clear a file we do not understand). Remove
duplicate IDs (keep first occurrence).

**SCH-4.** For IDs not in the `AccountRegistry` (unknown, disabled, other mode): log WARNING and
treat them as processed (they are removed in SCH-7).

**SCH-5.** Group the rest by strategy and call `StrategyExecutor.execute(strategy, accounts, live, "scheduled")`
for all strategies concurrently (`asyncio.gather`).

**SCH-6.** If `execute` returns `None` (strategy already running), send each of those accounts a
`SKIPPED_FINAL` notification (§11.3) and treat them as processed.

**SCH-7.** At the end, **remove only the processed IDs** from the file: re-read the file, filter out
the processed IDs, and write atomically (`<file>.tmp`, then `os.replace`). Do not write `[]` blindly.
An ID added while the job was running (for example by a manual run that failed at 09:40) survives.

**SCH-8.** Accepted edge, do not code around it: if an account is in the morning list **and** a manual
live run of the same account fails while the job is running, the job's removal also removes the new
entry. The failure notification will have promised a retry that does not happen.

### 10.5 Retry rules (applied by the parent in EXE-2 step 5)

| Source | Mode | Outcome | Schedule-file change | Notification (§11.3) |
|---|---|---|---|---|
| signal / manual | live | `SUCCEEDED` | remove the account's ID if present | `SUCCESS` |
| signal / manual | live | `FAILED` | add the ID (if not present) | `FAILED_WILL_RETRY` |
| signal / manual | live | `MARKET_CLOSED` | add the ID (if not present) | `QUEUED_MARKET_CLOSED` |
| scheduled | live | `SUCCEEDED` | (removed by SCH-7) | `SUCCESS` |
| scheduled | live | `FAILED` or `MARKET_CLOSED` | (removed by SCH-7), never re-added | `FAILED_FINAL` |
| any | preview | `PREVIEWED` | none | `PREVIEW` |
| any | preview | `FAILED` | none | `PREVIEW_FAILED` |

**RTY-1.** If `scheduler.enabled` is false, failures and market-closed runs are not queued, and the notification is
`FAILED_FINAL` with the extra line `Automatic retry is disabled (scheduler.enabled: false).`
**RTY-2.** Adding and removing use the same atomic read-filter/append-write helper as SCH-7.
Only the parent process writes the file (workers never touch it). `tools/schedule.sh` may also write
it. A simultaneous write is an accepted risk, so add no locking.
**RTY-3.** A failure for any reason is retried once, including `FOREIGN_ORDERS` and
`TARGETS_INVALID`. The next morning uses fresh targets, fresh account data and a fresh plan.
**RTY-4.** Delete `add_account_to_schedule`'s PDT use. Its replacement is the RTY-2 helper.

### 10.6 Signals and manual events

**SIG-1.** Ably: keep the handler logic. Keep a reference to every task created in the message
callback in a set, and discard it when done (`task.add_done_callback(set.discard)`).
**SIG-2.** `exec` values: `rebalance` → live, `print-rebalance` → preview; anything else → log ERROR
and drop. Ably events use source `signal`.
**SIG-3.** Manual events (`data/manual-rebalance/rebalance.json`): unchanged file handling. Source
`manual`, for a single account looked up in the `AccountRegistry`. Unknown account → log ERROR, no run.

---

## 11. Notifications

### 11.1 Interface

**NTF-1.** `class Notifier(ABC): async def send(self, result: AccountRunResult, context: NotificationContext) -> None`.
`NotificationContext` carries `kind` (§11.3) and `next_run` (datetime or None).
`NtfyNotifier` implements it. Rendering is a pure function `render(result, context) -> Message(title, body, tags)`,
so it is testable without HTTP.
**NTF-2.** Exactly **one** message per account per run. The old separate "warnings" message is removed.
**NTF-3.** HTTP POST to `https://ntfy.sh/<channel>` with headers `Title`, `Priority: default` and `Tags`.
Total timeout 10 s per request (`aiohttp.ClientTimeout(total=10)`). Failures are logged and never raised.
Disabled or no channel → log at INFO and skip (unchanged).
**NTF-4.** The body is at most 3,500 bytes in UTF-8. If it is longer, keep the header lines and the first notes,
then end with `…and <k> more notes (see logs)`.
**NTF-5.** Keep `_mask_account_id` and `_format_strategy_name` and use them in every message. Never
send a full account ID or run ID to ntfy, including inside an error or note.

### 11.2 Common header lines (every message)

```
Account: U***456
Strategy: ETF Blend 200-35
Time: 2026-11-20 15:52:10 ET
```

### 11.3 Kinds

| Kind | Title | Body after the header |
|---|---|---|
| `SUCCESS` | `✅ Rebalance complete` | `Orders: <s> sells, <b> buys` · `Portfolio value ≈ $<value_end>` · `Cash ≈ $<cash_end>` · notes (if any) |
| `FAILED_WILL_RETRY` | `⚠️ Rebalance failed: will retry` | `Error: <error_message>` · what happened (RUN-1) · `Retry: <Day YYYY-MM-DD> <HH:MM> ET (next market open).` · notes |
| `QUEUED_MARKET_CLOSED` | `🕒 Market closed: rebalance queued` | `No orders were placed.` · `Will run: <Day YYYY-MM-DD> <HH:MM> ET.` |
| `FAILED_FINAL` | `❌ Rebalance failed: not retried` | `Error: …` · what happened · `This account will not be retried automatically. Fix the issue, then run: ./tools/rebalance.sh -account <account-id> -exec rebalance` · notes |
| `SKIPPED_FINAL` | `❌ Scheduled rebalance skipped` | `The strategy was already running at <time>, so this scheduled run was skipped and will not be retried. Run it manually if needed.` |
| `PREVIEW` | `🔎 Rebalance preview` | `Planned: <s> sells, <b> buys` · one line per order, e.g. `SELL 55 XLE (market)`, `BUY 62 TLT ≤ $92.11` (max 20 lines; then `…and k more`) · `Portfolio value ≈ $V0` · notes |
| `PREVIEW_FAILED` | `❌ Rebalance preview failed` | `Error: …` |

"What happened" lines: `Nothing was traded.` when `executed` is empty. Otherwise
`Already done: SOLD 40 XLE @ $84.97, SOLD 12 GLD @ $249.90; BUY orders did not complete.`
List every executed order with filled quantity > 0. Partial fills show `SOLD 30 of 40 XLE`.
Dollar amounts use thousands separators and 2 decimals. `<HH:MM>` is `scheduler.market_open_time`; the date comes from `MarketClock.next_scheduled_run`.

---

## 12. Investigation data

### 12.1 Logs

**LOG-1.** Each run logs, at INFO: the raw API weights, the scaled weights, the final targets;
`state0` (cash, NLV, each position); the price used for each symbol with its `PriceSource`; one
compact **plan line** per leg:
`PLAN sells: XLE -100 | V0 $104,031.45 reserve $260.08` and
`PLAN buys: GLD +22 ≤$251.35, SPY +8 ≤$603.10, TLT +4 ≤$90.50 | cash_base $10,995.00 budget $10,734.92 cost≤ $10,716.50`;
each order placed and its final state; and the outcome.
**LOG-2.** Remove the old multi-line snapshot and plan banners (`_log_account_snapshot`,
`_log_target_allocations`, `_log_planned_orders`).

### 12.2 Run records (30 days)

**REC-1.** The parent appends one JSON line per result to `/app/data/runs/<YYYY-MM-DD>.jsonl`
(date in ET). Only the parent writes these files.
**REC-2.** Record fields: `run_id, account_id, strategy_name, mode, source, started_at, finished_at,
outcome, failure_reason, error_message, raw_weights, scaled_weights, targets, replacement_set,
state0 {cash, net_liquidation, positions}, prices {symbol: {bid, ask, last, close, bar_close, used_mark, mark_source, buy_reference, buy_source}},
V0, reserve, planned_sells, cash_base, V1, planned_buys, trimmed, executed, notes`. `trimmed`
contains only symbols whose final buy quantity is less than its raw desired quantity. Values that
do not apply are `null`. Build the record inside the worker (it has the data) and return it in
`AccountRunResult.record`.
**REC-3.** At startup and after each write, delete `*.jsonl` files in that directory whose date is
more than 30 days before today. Never delete other files.
**REC-4.** Create the directory if missing. A write failure is logged at ERROR and never affects the
run's outcome or its notification.

---

## 13. Startup, health, shutdown

**BOOT-1. Startup order** in `EventBrokerApp.start()`:
1. config is already loaded at import (unchanged);
2. build `RuntimeSettings` (fails if `ZEHNLABS_API_KEY` is missing or blank, with the existing message);
3. `load_accounts` (§5.2);
4. **strategy check:** for each strategy in the registry, fetch allocations once. Any failure stops
   startup with `Strategy '<s>' is not available from the allocations API (<reason>). Check the name and your subscription.`
   Do not validate weights here;
5. fetch the Ably key (unchanged security behaviour) and subscribe one channel per strategy;
6. start the scheduler; prune run records;
7. run the main loop and the manual-event watcher.
A failure in steps 2–5 stops the service with a non-zero exit code.

**BOOT-2. Heartbeat.** `_run_forever` touches `/tmp/heartbeat` (its mtime) every loop iteration.
Dockerfile:
`HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/heartbeat')<30 else 1)"`.
**BOOT-3.** Version strings `x-system-version` in `docker-compose.yaml` and `get_status()["version"]`
become `3.0.0`.
**BOOT-4.** `docker-compose.yaml`: remove the `replacement-sets.yaml` volume line. Nothing else changes.
**BOOT-5.** Shutdown: unchanged, except `StrategyExecutor.cleanup()` shuts the pool down with
`wait=True, cancel_futures=True` (as today).

---

## 14. Removals checklist

All of the following must be gone (searching the repository for each name finds only the changelog
or the README's upgrade notes): `commission_rate`, `commission_divisor`, `max_account_utilization`,
`allocation_threshold_percent`, `minimum_cash_reserve_usd`, `cash_reserve_percent`,
`pdt_protection` / `PDTProtectionService` / `PDTCheckResult` / `last-executions`, `price_cache_ttl`,
`CachedPrice`, `use_cached_prices`, `use_cache`, `synthetic_ask_offset`, `post_completion_delay`,
`order_tif`, `ReplacementConfig`, `minimal_non_replaced_allocation_percent`,
`normalization_trigger_threshold`, `normalization_failure_threshold`, `replacement-sets.yaml`,
`BaseRebalancer`, `IBKRRebalancer`, `TradeCalculator`, `SubprocessManager`, `phase='buy'`,
`OrderStatus` (the old string-constant class; the new enum replaces it), the `Trade` model,
`BrokerClient`, `create_broker_client`, `send_warnings`, `get_open_orders`, `get_order_status`.

---

## 15. Documentation

**DOC-1. README.md**: remove "Cash Usage Maximization" (scaling) and "Minimum Purchase Quantity"
(one share). Add a "How a rebalance works" section (§7 in plain words, 10–15 lines). Remove
`cash_reserve_percent`, `pdt_protection_enabled` and `replacement-sets.yaml`. Document
`trading.minimum_cash_reserve_percent` (default 0.25%; under 0.25% may not cover commissions on
large purchases of low-priced shares). Add retries (§10.5), the 09:35 job, the foreign-order rule,
the built-in `ira` replacement set (with its table), and these upgrade notes: config keys removed
(§5.1), account keys removed, and "the first run after upgrading may trade more than usual, because
small drifts are now corrected". Add: "Designed for margin and IRA accounts. In a cash account, sale
proceeds may not be spendable until settlement, so buys can be rejected; the account is then retried
at the next open."
**DOC-2. AGENTS.md**: update the architecture, data flow, config and trading-logic sections to this
design. Remove PDT, the price cache and the hash-based client-ID text (the real rule is the digits
of the account ID).
**DOC-3. docs/rebalancing-logic.md**: replace its content with the new specification: §7, §8.1 and
§10.5 of this PRD, rewritten as a standalone description of the system (no history).
**DOC-4.** Add `CHANGELOG.md` with a `3.0.0` entry listing §3.1.

---

## 16. Testing

### 16.1 Environment

- Python 3.12 (as in the Dockerfile). Create a virtual environment and install:
  `pip install -e packages/app-config -e packages/broker-connector-base -e packages/rebalance-calculator -e packages/ibkr-connector`,
  then `pip install -r event-broker/requirements.txt pytest httpx`. ib_async must be **2.1.0** (the
  pinned version; an old `.venv` in the repo may hold 2.0.1. Do not rely on it).
- Run with `PYTHONPATH=event-broker python -m pytest -q tests`.
- Add `requirements-dev.txt` with `pytest>=8,<9` and `httpx`.
- Async tests use `unittest.IsolatedAsyncioTestCase` (stdlib) or `asyncio.run`. No new plugins.
- `tests/fakes.py`: `FakeBrokerConnection(BrokerConnection)` (scripted state, quotes, working
  orders, fill behaviour: fill at a given price, reject, never fill, partial), `FakeAllocations`,
  `FakeClock`, `RecordingNotifier`.

### 16.2 Required tests (minimum; names are suggestions)

**Planner (`tests/test_planner.py`)**
- Every example in Appendix A gives exactly the stated numbers.
- Property test over at least 5,000 random whole-share and fractional-broker accounts with a fixed seed (1–12 targets, prices
  $2–$1,200, spreads 0.01%–1%, random holdings including fractional quantities and non-target
  symbols, cash $0–$200k): P1–P9 (§7.7). Buys are assumed to fill at their limit price.
- Deterministic examples cover a cheap buy restored after an expensive-share overshoot, partial
  restoration, final-only warning text, equal-weight tie priority, an unaffordable expensive share
  followed by affordable cheaper shares, exact/±$0.01/EPS budget boundaries, zero/negative budget,
  empty/no-shortfall plans, and sub-$1 fractional buys at six-decimal granularity.
- Weight-0 target held: sold exactly `floor(held)`, never `held + 1`.
- Missing price for a held symbol → `NoPriceError`; buy with no bid but an ask → bought.
- `round_quantity` edge cases: `99.9999999999 → 100` (within EPS of 100, so treated as floating-point noise),
  `99.999999 → 99`, `100.0 → 100`, `0.9999 → 0`. (Corrected 2026-11-20; §7's formula was always authoritative.)

**Targets and replacements (`tests/test_targets.py`)**: TGT-1..5 each; REP-2 each rule; Appendix A
replacement examples; replacement applied after the 1% check (a strategy of 70% UVXY / 30% SPY
passes validation and resolves to VXX 100%).

**Config and accounts (`tests/test_config.py`, `tests/test_accounts.py`)**: every CFG-3 message; an
unknown nested key; the shipped `config.yaml` loads; `accounts.yaml.example` loads; every ACC-2
message; a disabled account with a removed key still fails; registry filtering.

**IBKR connection (`tests/test_ibkr_connection.py`)**, using a mocked `ib_async.IB`: connect
arguments (exact `fetchFields`, `account`, `raiseSyncErrors=True`); targeted multi-account update;
two different accounts connect concurrently and retain valid cash/NLV snapshots; unmanaged account fails; cash
from `$LEDGER-CashBalance`, from `CashBalance`, prefixed one preferred when both are present,
neither → error; NLV missing → error; non-STK or negative position → error; status mapping
including empty → WORKING; own vs foreign classification; fractional quantity refused by `place`;
quote fallback chain including the bar request.

**Account run (`tests/test_rebalance.py`)** with fakes: full happy path (cancel → snapshot → sells →
cash wait → fresh quotes → buys); preview places nothing and does not cancel; market closed → no
connection; foreign order → `FOREIGN_ORDERS` and no orders placed; cancel not confirmed → fail before
the snapshot; sell rejected → no buys, and "what happened" lists the fills; buy timeout →
`ORDERS_TIMED_OUT` and no cancel call; NLV mismatch > 2% → fail; cash lag → `cash_base` uses the
lower number; an unexpected exception → `INTERNAL_ERROR`, never raised; preview and live records
and notifications list only symbols genuinely trimmed in the final buy plan.

**Executor, retry, scheduler (`tests/test_executor.py`, `tests/test_scheduler.py`)**: every row of
the §10.5 table (file changes and notification kind); dropped trigger while running → no
notification for signal/manual, `SKIPPED_FINAL` for scheduled; running mark released before
notifications are sent (assert inside a notifier fake); `BrokenProcessPool` → `WORKER_CRASHED`
results and a new pool; SCH-3 bad file left untouched; SCH-7 keeps IDs added during the job;
duplicates in the file run once; misfire settings passed to `add_job`; the pool uses the fork
context; scheduler disabled → `FAILED_FINAL` with the RTY-1 line.

**Notifications (`tests/test_notifications.py`)**: each kind's title and key lines; body ≤ 3,500
bytes with 200 notes; masking; one message per result; timeout configured.

**Run records (`tests/test_run_records.py`)**: one line per result; pruning keeps 30 days and
non-matching files.

### 16.3 Existing tests

`tests/test_security_startup.py` must keep testing these behaviours, adapted to the new module
layout where patch targets moved: the Ably key is fetched once at startup and its value never
appears in logs or errors; HTTP failures and invalid payloads stop startup without exposing the
response body; the auth URL derivation and invalid-base rejection; a missing `ZEHNLABS_API_KEY`
fails before anything else; startup failure starts neither the scheduler nor the manual watcher;
`main()` waits for signal shutdown; manual events route to the executor; scheduled events use the
loaded accounts. Do not delete any of these assertions. Update only the setup they need.
Example of a needed setup change: the test fixture `ACCOUNT` contains `cash_reserve_percent`, which the
strict `accounts.yaml` rules now reject. Remove that key from the fixture; do not loosen the rule.

### 16.4 Definition of done (whole refactor)

All tests pass. §14 removals are verified with a search. `docker compose build event-broker` succeeds
(if Docker is available to the agent; otherwise say so in the report). Starting the container with
the old `config.yaml` or the old `accounts.yaml.example` fails with the §5 messages. The README,
AGENTS.md, rebalancing-logic.md and CHANGELOG.md are updated.

### 16.5 Before going live (Faisal, not the agents)

During market hours, on paper, after telling Faisal (a paper login may disconnect production):
(1) how IBKR checks funds for a limit buy; (2) how quickly cash updates after a fill; (3) that orders
left working by this app's client ID in an earlier connection are visible and cancellable after
reconnecting; (4) that manual TWS/mobile orders appear in `reqAllOpenOrders` for the foreign-order
rule; (5) a preview of every live account, reviewed before the first live signal.

---

## 17. Work packages

Do them in this order. WP3 and WP4 may run in parallel after WP2. Each work package must leave
`python -m pytest -q tests` green (tests for later packages may be marked
`@unittest.skip("WPn")` until then; remove the skip in that package).

| WP | Scope | Requirements | Depends on |
|---|---|---|---|
| WP1 | Test environment, `tests/fakes.py` skeleton, `requirements-dev.txt` | §16.1 | — |
| WP2 | `broker-connector-base` models, ABC, errors | §6 | WP1 |
| WP3 | `rebalance-calculator`: targets, replacement sets, planner | §7, Appendix A | WP2 |
| WP4 | `ibkr-connector`: `IBKRConnection` | §9 | WP2 |
| WP5 | `app-config` strict config; `accounts.py`; `settings.py` | §5 | WP1 |
| WP6 | `allocations.py`, `market_clock.py`, `results.py`, `rebalance.py` | §8, §10.1 | WP3, WP4, WP5 |
| WP7 | Executor, worker, scheduler, retry, notifications, run records, Ably task set, main/startup/heartbeat, **and the §16.3 adaptation of `tests/test_security_startup.py`** (WP7 rewrites the modules it tests, so WP7 adapts it to stay green) | §10.2–10.6, §11, §12, §13, §16.3 | WP6 |
| WP8 | Deletions, Docker/compose, docs, changelog, removal search, final check | §4.3, §13.3–13.4, §14, §15, §16.4 | WP7 |

### 17.1 Per-package acceptance

A package is done when: its requirements are implemented; its tests from §16.2 exist and pass;
no requirement of another package was started early without reason; and the report (§17.3) is
written.

### 17.2 Things that are easy to get wrong (read before coding)

1. Running the 1% weight check **after** replacements (TGT-5). Every IRA account would fail.
2. Leaving `ORDERS_OPEN` out of `fetchFields` (IB-1). The cancel step then cancels nothing.
3. Treating an empty order status as filled (WAIT-2).
4. Looking orders up by order number in `ib.trades()` (IB-10).
5. Defaulting a missing cash or NetLiquidation value to 0 (IB-2/3). That sells the whole account.
6. Budgeting buys at the ask instead of the limit price (BUY-1).
7. Releasing the running mark **after** notifications (EXE-2 step 3).
8. Clearing `scheduled.json` with `[]` instead of removing processed IDs (SCH-7).
9. Retrying a failed **scheduled** run (§10.5). It must be final.
10. Re-reading positions from IBKR after sells instead of subtracting fills (§8.1 step 14).
11. Using `round()` instead of round-down for quantities (`round_quantity`).
12. Calling `get_config()` or `os.getenv` inside packages or workers (§4.2, ENV-1).
13. Cancelling orders on failure (RUN-2) or using `reqGlobalCancel` (§3.2).

### 17.3 Report (one per work package)

Append to `docs/refactor-implementation-log.md`: work package, commits, requirement IDs done, tests
added, anything not done and why, every deviation from this PRD with its reason, and open questions.
Do not mark a requirement done when it is not.

---

## Appendix A. Worked examples (exact expected results)

All computed with the rules in §7 (`EPS = 1e-9`, slippage 0.5%, reserve 0.25%, whole shares).

### A.1 Targets and replacements (set `ira`)

| # | API weights | Expected targets |
|---|---|---|
| A1 | UVXY 0.70, SPY 0.30 | VXX 1.0 (SPY removed), plus a warning note |
| A2 | UVXY 0.50, SPY 0.50 | VXX 0.75, SPY 0.25 |
| A3 | UVXY 0.20, VXX 0.10, SPY 0.70 | VXX 0.3875, SPY 0.6125 (direct VXX is ordinary: protected 0.30, ordinary 0.80, factor 0.875) |
| A4 | VIXY 0.10, UVXY 0.20, SPY 0.70 | VXX 0.40, SPY 0.60 |
| A5 | SPY 0.60, TLT 0.395 (no set; total 99.5%) | SPY 0.603015075…, TLT 0.396984924… |
| A6 | DBC 0.25, GLD 0.75 | PDBC 0.25, GLD 0.75 |
| A7 | SPY 0.60, TLT 0.38 (total 98%) | `TARGETS_INVALID` |
| A8 | SPY 0.5, spy 0.5 | `TARGETS_INVALID` (duplicate after normalisation) |
| A9 | SPY 1.05, TLT −0.05 | `TARGETS_INVALID` (negative) |
| A10 | empty list | targets empty; every held position sold |

Compare floats with a tolerance of 1e-9.

### A.2 Two-leg run

Quotes (bid / ask): SPY 599.90 / 600.10 · TLT 89.95 / 90.05 · GLD 249.90 / 250.10 · XLE 84.95 / 85.05.
Holdings: SPY 70, TLT 400, GLD 60, XLE 100.37. Cash $2,500.00.
Targets: SPY 0.45, TLT 0.35, GLD 0.20 (XLE not a target).

- Marks: SPY 600.00, TLT 90.00, GLD 250.00, XLE 85.00. **V0 = 104,031.45**.
- Sell plan: **SELL 100 XLE**. Warning note: XLE 0.3700 shares can't be sold (fractional).
  SPY, TLT and GLD are under target, so no sells.
- Reserve = 0.25% × 104,031.45 = **260.08** (260.078625).
- XLE fills at 84.95: proceeds 8,495.00. With reported cash ≥ 10,995.00, **cash_base = 10,995.00**.
- positions_after: SPY 70, TLT 400, GLD 60, XLE 0.37. Fresh quotes are unchanged.
  **V1 = 104,026.45**.
- Untrimmed buys (sized at the ask): GLD 23, SPY 8, TLT 4. Limits: GLD 251.35, SPY 603.10, TLT 90.50.
  Cost at limit 10,967.85 > budget **10,734.92** (10,734.921375).
- Fund in target-weight priority: SPY 8 and TLT 4 are fully funded first; the remaining budget
  funds 22 of the 23 raw GLD shares.
- **Final buys: GLD 22 ≤ $251.35, SPY 8 ≤ $603.10, TLT 4 ≤ $90.50.** Cost at limit 10,716.50 ≤ budget.
  Note: `Not enough cash for all buys: GLD reduced by 1 share.`

### A.3 Cash shortfall from the limit buffer

All cash, $10,000.00. Targets AAA 0.5, BBB 0.3, CCC 0.2. Quotes AAA 9.99 / 10.01, BBB 19.99 / 20.01,
CCC 24.99 / 25.01. Reserve 25.00. Budget 9,975.00.
Untrimmed: AAA 499, BBB 149, CCC 79. Limits 10.06, 20.11, 25.14. Cost 10,002.39.
Fund AAA and BBB fully first; the remaining budget funds 77 of the 79 raw CCC shares.
**Final: AAA 499, BBB 149, CCC 77**, cost 9,952.11.

### A.4 Cash below the reserve (`plan_buys` called directly)

Inputs: positions_after SPY 10 (quote 599.90 / 600.10, mark 600.00); TLT not held (quote 90.00 / 90.05,
mark 90.025); cash_base −50.00 (margin debt); reserve 14.875 (0.25% of a V0 of 5,950.00);
targets SPY 0.5, TLT 0.5.
Budget = −50.00 − 14.875 = −64.875 ≤ 0 → **no buys**, and the CASH-3 warning note is present.

---

## Appendix B. Where the decisions came from

`docs/refactor-validation.md` §0 (decisions), §0A (second pass) and its §0A.0 / §0A.6 answers.
Prototype scripts (not product code) used to check the rules: `decided_planner.py`,
`pct_floor.py`, `replacement_fit.py` and `prd_reference.py` in Faisal's validation workspace.
