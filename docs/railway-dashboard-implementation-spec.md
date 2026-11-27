# Railway and dashboard implementation specification

## 1. Required result

The repository must produce the same two-container rebalancer stack for both Railway and self-hosted Docker Compose installations:

1. `rebalancer` runs the existing signal subscriber, scheduler, rebalance executor, and a FastAPI dashboard in one application container. One parent Python process owns FastAPI, orchestration, and SQLite; the existing process-pool children remain internal execution workers.
2. `ibkr-gateway` remains the Interactive Brokers Gateway container.

The `rebalancer` process must use one embedded SQLite database for Account configuration, pending work, Run history, and dashboard security state. The Gateway must retain its separate `ib_settings` volume. PostgreSQL, a separate dashboard container, a separate worker container, and a new management CLI are not part of this design.

The dashboard is the management interface on both Railway and a VPS. It is always started. Railway exposes it through Railway's HTTPS domain. Docker Compose publishes it only on the host loopback interface at `127.0.0.1:18778`; the subscriber decides how to reach that loopback-only service securely.

All existing portfolio calculation, order construction, broker safety checks, signal processing, retry policy, and notification behavior must remain unchanged unless this specification explicitly replaces a file-backed interface.

## 2. Hard constraints

- Keep one parent `rebalancer` process, one Uvicorn worker, and one Railway replica. Preserve the existing internal process pool.
- Keep the scheduler and rebalance execution inside the existing application. Do not create new deployable services for them.
- Do not add PostgreSQL, Redis, a message broker, an external identity provider, or a management CLI.
- Do not add locks, leases, leader election, or other concurrency-control infrastructure. The supported topology has one application process and one SQLite writer.
- Preserve the current explicit `fork` process-pool start method. Changing the start method or adding pool-warming/concurrency machinery requires separate user authorization.
- Do not make the dashboard optional.
- Do not expose the dashboard on a non-loopback host interface in the supplied Docker Compose file.
- Do not move IBKR credentials into SQLite. Environment variables remain the source of deployment secrets.
- Do not use the repository's `accounts.yaml` or any credentials already present on a developer machine for automated tests or the paper pilot.
- Do not change the stock IBKR Gateway image to a Zehnlabs-owned bootstrap or supervisor image.
- Support Railway Hobby or higher. Do not present Railway Free as supported because the stack requires two volumes and Free permits one.

## 3. Application structure

Use `rebalancer/` as the application source directory and `rebalancer` as the Docker Compose service and user-facing container name.

The application code must have these focused modules:

- The composition root creates configuration, the SQLite store, Account management, the existing event subscriber/executor/scheduler, and the FastAPI application.
- The dashboard package contains HTTP routes, authentication, templates, and static assets. It must not contain trading calculations or broker calls.
- An Account management module validates and saves complete Account configurations, discovers available Accounts and Strategies, and refreshes signal subscriptions.
- A work-management module accepts dashboard requests and supplies due work to the existing scheduler and executor.
- A SQLite state module owns schema migration and every SQL statement. Dashboard routes, the scheduler, the signal subscriber, and worker subprocesses must not access database tables directly.
- Existing broker connectors and the rebalance calculator remain independent of FastAPI and SQLite.

Use explicit typed models for Account configuration, Work Items, stored Runs, dashboard sessions, and discovery results. Do not pass untyped dictionaries between the dashboard, application modules, and state module. Serialization is permitted only at the SQLite and HTTP edges.

Use this file ownership:

- `rebalancer/app/main.py`: import-safe composition root and `python -m app.main` Uvicorn entry point;
- `rebalancer/app/application.py`: the typed in-process interface used by dashboard routes for status, discovery, Account replacement, work submission/cancellation, and history queries;
- `rebalancer/app/state/models.py`: durable-state models and enums;
- `rebalancer/app/state/sqlite.py`: schema migrations and every SQL statement;
- `rebalancer/app/services/account_manager.py`: complete-set Account validation, persistence choreography, and Ably reconciliation;
- `rebalancer/app/services/work_manager.py`: Work Item creation, idempotency, dispatch preparation, result persistence, and retry decisions;
- `rebalancer/app/web/app.py`: FastAPI construction and lifespan;
- `rebalancer/app/web/auth.py`: password, session, CSRF, and throttling behavior;
- `rebalancer/app/web/routes.py`: thin HTML route adapters that call `RebalancerApplication`;
- `rebalancer/app/web/templates/` and `rebalancer/app/web/static/`: local server-rendered UI;
- `packages/ibkr-connector/src/ibkr_connector/discovery.py`: managed-Account discovery.

Do not put SQL, allocation validation, subscription changes, scheduling decisions, or executor calls in route functions.

## 4. Process lifecycle

`python -m app.main` must start Uvicorn on `0.0.0.0:$PORT` with exactly one worker. `PORT` is an integer deployment setting whose default is `18778`; the Compose deployment sets it to `18778`, while Railway may inject it. FastAPI's lifespan must own the application lifecycle:

1. Construct the immutable application configuration and environment-backed runtime settings.
2. Open `/app/state/rebalancer.db`, apply schema migrations, enable foreign keys, and run `PRAGMA quick_check`.
3. Invalidate old dashboard sessions left by a previous process.
4. Mark Work Items and Runs left in `running` state as `interrupted`; never replay them automatically.
5. Load Account configuration for the current `TRADING_MODE` from SQLite.
6. Start the event subscriber only when at least one enabled Account has a valid Strategy assignment. An empty installation must still serve the onboarding UI.
7. Start the scheduler and arrange execution of the earliest pending Work Item.
8. Serve the dashboard.
9. On shutdown, stop accepting new dashboard mutations, stop the scheduler and signal subscriber, request process-pool shutdown, finish parent-side persistence, and stop further state access. Railway may force-stop work that exceeds its configured drain period; the next startup handles unfinished records as described above.

Uvicorn must remain the only signal handler. The application must expose explicit non-blocking `start()` and idempotent `stop()` lifecycle methods so FastAPI can own startup and shutdown.

The dashboard must remain usable when IBKR Gateway is unavailable, IBKR authentication is incomplete, the Zehnlabs key is missing or invalid, or no Accounts are configured. Those conditions must appear as actionable readiness errors instead of terminating the web process. A missing or unsafe dashboard password, invalid `TRADING_MODE` or `PORT`, and an unavailable, unmigratable, or corrupt SQLite database are startup failures. Runtime settings must therefore represent missing operational credentials without using them until the related operation is requested.

## 5. Configuration and secrets

### 5.1 Application defaults

Delete `config.yaml` and the YAML configuration loader. Preserve the current validated values as defaults on immutable, typed Python configuration models. Construct `AppConfig` directly in the composition root and inject it into consumers. Remove `get_config()` and all implicit global configuration reads. This is an intentional breaking removal of the repository's currently documented advanced file customization; do not replace it with dashboard-editable trading settings in this effort.

Remove configuration fields used only by the deleted file interfaces, including the manual-event file path, manual-event polling interval, schedule-file path, and run-record directory. Keep operational values that the running application still uses, including broker timeouts, trading limits, market-open time, executor size, and allocation timeouts.

These application defaults are not editable in the dashboard.

### 5.2 Environment variables

Keep all current environment variables that remain relevant to the two containers. Add one required variable:

- `DASHBOARD_ADMIN_PASSWORD`: password for the fixed dashboard username `admin`.

Support Railway's `PORT` variable and default it to `18778`. It changes only the container listener; the VPS host-side address remains `127.0.0.1:18778`.

The password must be at least 16 characters and must not equal `admin`, `password`, `changeme`, or the documented example value. Missing, blank, short, or known-default values must stop startup with a message that names the variable but never prints its value.

Railway must generate this variable with its `secret(32)` template function when the template is deployed. It must remain revealable to the Railway project owner because it is the first-login credential; do not configure it as a value that Railway cannot show again. A VPS subscriber sets it in `.env`. Password rotation consists of changing the environment variable and restarting the `rebalancer`; all previous dashboard sessions then become invalid.

Keep these responsibilities separate:

- `IB_USERNAME` and `IB_PASSWORD` are passed only to `ibkr-gateway`.
- `ZEHNLABS_API_KEY` is passed only to `rebalancer`.
- `TRADING_MODE`, allocation URL, notification settings, logging settings, and broker connection settings remain environment-backed runtime settings.
- Account-to-Strategy assignments are not environment variables; they live in SQLite.

Changing a Railway variable creates a staged change; the subscriber must click Deploy before affected services restart with the new value. Therefore switching `TRADING_MODE` means editing the Railway variable and deploying the staged change. Accounts stored for the other mode remain in SQLite but are never active in the current mode.

## 6. Durable state

### 6.1 Database ownership and location

Use Python's standard `sqlite3` module. Do not add an ORM or a database server. The production database path is `/app/state/rebalancer.db`. Tests must inject a temporary path instead of writing there.

Only the parent `rebalancer` process may open the database. Process-pool workers continue to receive immutable Account/configuration inputs and return typed `AccountRunResult` values. They must not receive the database path or write durable state.

Use parameterized SQL, UTC RFC 3339 timestamps ending in `Z`, explicit transactions for multi-row writes, `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = DELETE`, `PRAGMA synchronous = FULL`, and `PRAGMA user_version` for ordered, forward-only schema migrations. Use UUIDv4 for `work_id`, `submission_id`, and `batch_id`. Preserve the existing `new_run_id()` format, but generate each `run_id` in the parent and pass it to the worker. The current schema is version `2`. Version `1` databases migrate forward by replacing `work_items` atomically, preserving every row and using each legacy `work_id` as its `submission_id`; the migration also creates `onboarding_initializations` and advances `user_version` only after the complete transaction succeeds. A database whose `user_version` is newer than the application supports must fail startup. Startup must fail rather than silently replace or recreate a database that fails `PRAGMA quick_check` or a migration.

Open a short-lived SQLite connection inside each state-module operation, apply the required connection PRAGMAs, commit or roll back, and close the connection before returning. State methods are synchronous, contain no `await`, and finish before any worker submission. Do not add `aiosqlite`, a thread pool, or a long-lived connection that a forked worker could inherit.

### 6.2 Schema

The current schema must contain the following tables. Column and status names are part of the storage contract.

#### `accounts`

- `account_id TEXT PRIMARY KEY`
- `account_type TEXT NOT NULL CHECK (account_type IN ('paper', 'live'))`
- `broker TEXT NOT NULL CHECK (broker = 'ibkr')`
- `enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))`
- `strategy_name TEXT NOT NULL`
- `replacement_set TEXT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

#### `work_items`

- `work_id TEXT PRIMARY KEY`
- `submission_id TEXT NOT NULL`
- `account_id TEXT NOT NULL`
- `action TEXT NOT NULL CHECK (action IN ('preview', 'rebalance'))`
- `source TEXT NOT NULL CHECK (source IN ('dashboard', 'initial', 'scheduled', 'retry'))`
- `status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'cancelled', 'interrupted'))`
- `execute_at TEXT NOT NULL`
- `request_token_hash TEXT NOT NULL UNIQUE`
- `run_id TEXT NULL REFERENCES runs(run_id) ON DELETE SET NULL`
- `message TEXT NULL`
- `created_at TEXT NOT NULL`
- `started_at TEXT NULL`
- `finished_at TEXT NULL`

Create an index on `(status, execute_at)`, an index on `account_id`, an index on `submission_id`, and a partial unique index on `(account_id, action)` where `status IN ('pending', 'running')`. There may be at most one outstanding Preview and at most one outstanding rebalance for an Account. Account deletion must cancel rather than erase pending Work Items, so `work_items.account_id` deliberately has no foreign key.

`submission_id` identifies the dashboard form submission that created the Work Item. All Work Items from one bulk submission share it; single-Account and retry submissions each receive a new value. It is distinct from a Run `batch_id`. Idempotent replay returns the originally stored Work Items and their original submission identity.

The only legal Work Item transitions are `pending -> running`, `pending -> cancelled`, `running -> completed`, and startup recovery `running -> interrupted`. Terminal Work Items are immutable. Execution failures belong to the linked terminal Run; they do not create a second failure vocabulary on Work Items.

#### `runs`

- `run_id TEXT PRIMARY KEY`
- `batch_id TEXT NOT NULL`
- `account_id TEXT NOT NULL`
- `strategy_name TEXT NOT NULL`
- `replacement_set TEXT NULL`
- `trading_mode TEXT NOT NULL CHECK (trading_mode IN ('paper', 'live'))`
- `run_mode TEXT NOT NULL CHECK (run_mode IN ('preview', 'live'))`
- `source TEXT NOT NULL CHECK (source IN ('signal', 'dashboard', 'initial', 'scheduled', 'retry'))`
- `status TEXT NOT NULL CHECK (status IN ('running', 'terminal', 'interrupted'))`
- `outcome TEXT NULL`
- `failure_reason TEXT NULL`
- `error_message TEXT NULL`
- `requested_at TEXT NOT NULL`
- `started_at TEXT NULL`
- `finished_at TEXT NULL`
- `result_json TEXT NULL`

Create indexes on `(requested_at DESC)`, `(account_id, requested_at DESC)`, `status`, and `outcome`. Before any Preview or rebalance enters the process pool, the parent creates one `running` row per Account using parent-generated `run_id` and `batch_id` values. `requested_at` comes from the parent; the worker's typed result supplies the actual `started_at` and `finished_at`. A returned `AccountRunResult` changes the row to `terminal` and supplies the outcome, timestamps, and JSON payload. Startup changes leftover `running` rows to `interrupted` and leaves unavailable terminal fields null. Run rows deliberately have no foreign key to `accounts`, so deleting an Account does not delete its history.

Runs are normally inserted as `running`; the only permitted direct terminal insert is a pre-execution `STRATEGY_BUSY` result that made no broker call. The only legal updates are `running -> terminal` and startup recovery `running -> interrupted`. Terminal and interrupted Runs are immutable.

Replace `AccountRunResult.record: dict` with an explicit, validated `RunRecord` model covering every field currently placed in that dictionary. `result_json` must be the lossless serialization of the complete typed result, not an independently assembled web payload.

`RunRecord` must contain these typed fields: `run_id`, `account_id`, `strategy_name`, `mode`, `source`, `started_at`, `finished_at`, `outcome`, `failure_reason`, `error_message`, `raw_weights`, `scaled_weights`, `targets`, `replacement_set`, `state0`, `prices`, `V0`, `reserve`, `planned_sells`, `cash_base`, `V1`, `planned_buys`, `trimmed`, `executed`, and `notes`. Add nested typed models for the recorded Account state, positions, and per-symbol price evidence. Reuse `PlannedOrderOut`, `ExecutedOrder`, and `NoteOut` rather than defining duplicate order/note shapes.

Replace the current source literal with a `RunSource` enum containing `signal`, `dashboard`, `initial`, `scheduled`, and `retry`. Replace the current mode literal with a `RunMode` enum containing the existing `preview` and `live` values. Add a separate `WorkAction` enum containing `preview` and `rebalance`. Use these enums in Work Items, Runs, `AccountRunResult`, notifications, filters, and persistence; do not translate back to the removed `manual` source.

#### `web_sessions`

- `session_hash TEXT PRIMARY KEY`
- `csrf_hash TEXT NOT NULL`
- `authenticated INTEGER NOT NULL CHECK (authenticated IN (0, 1))`
- `created_at TEXT NOT NULL`
- `last_seen_at TEXT NOT NULL`
- `absolute_expires_at TEXT NOT NULL`

#### `login_failures`

- `client_key TEXT PRIMARY KEY`
- `failure_count INTEGER NOT NULL`
- `window_started_at TEXT NOT NULL`
- `blocked_until TEXT NULL`

#### `onboarding_initializations`

- `trading_mode TEXT NOT NULL CHECK (trading_mode IN ('paper', 'live'))`
- `configuration_fingerprint TEXT NOT NULL`
- `submission_id TEXT NOT NULL UNIQUE`
- `created_at TEXT NOT NULL`
- `created_items_json TEXT NOT NULL`
- primary key on `(trading_mode, configuration_fingerprint)`

An initialization row is a durable idempotency receipt for the exact enabled Account configuration used by an onboarding finish submission. The fingerprint is a SHA-256 digest of a canonical versioned representation containing the trading mode and every enabled current-mode Account, sorted by Account ID, including Account ID, broker, Strategy, Replacement Set, `created_at`, and `updated_at`. Disabled Accounts and credentials are excluded. `created_items_json` losslessly stores the originally created typed Work Items so the aggregate submission receipt remains available after normal Work history pruning. Initialization rows are not operational history and are never removed by 30-day pruning.

### 6.3 State-module interface

The SQLite state module must expose typed operations for:

- initialization, migration, and integrity checking;
- listing all Accounts or Accounts for one trading mode;
- atomically replacing the complete Account set for one trading mode while leaving the other mode unchanged;
- creating, reading, listing, cancelling, transitioning, and pruning terminal Work Items;
- returning the earliest pending execution time and all due pending items;
- starting Runs before worker submission, finishing Runs from typed results, marking unfinished Runs interrupted, querying Runs, and pruning terminal Runs older than 30 days;
- creating, rotating, reading, expiring, deleting, and pruning dashboard sessions;
- recording, checking, clearing, and pruning login-failure throttles.

No generic repository framework is required. Keep these operations in one cohesive state module with typed inputs and outputs.

### 6.4 Atomic Account updates

The browser submits the complete Account configuration for the current trading mode. The server validates the entire submission before starting a database transaction. The state module then replaces that mode's Account rows and marks pending Work Items for removed, newly disabled, or Strategy/Replacement-Set-changed Accounts `cancelled` in the same transaction. A validation or write failure leaves the previous configuration and pending work untouched. This is an ordinary atomic save; do not introduce configuration revisions, draft rows, or partial-edit tables.

The UI submits one configuration save at a time. This specification does not authorize locks, version fields, leases, or a concurrent-edit protocol; do not claim support for independent simultaneous Account saves. Repeated submission of the same form is idempotent. Any broader concurrent-edit behavior requires separate user authorization.

After a successful save, refresh the in-memory Account registry and reconcile Ably subscriptions:

- subscribe to newly referenced Strategies;
- unsubscribe from Strategies no enabled Account references;
- route future signals through the refreshed registry;
- allow an already submitted Run to finish with the immutable Account input it received.

If this is the first enabled Strategy, start the subscriber and then reconcile its channel set. Initial Zehnlabs authentication, Ably connection, and subscribe/unsubscribe failures must set a degraded status and enter a single background reconciliation loop that retries every five seconds until the subscriber's actual channel set equals the current committed registry or shutdown begins. The loop always reads the latest committed registry before each attempt; it never retries a stale candidate Account form. A valid database save remains committed while this external recovery runs. Do not claim that the current SDK reconnect behavior repairs an initial authentication or subscription failure by itself.

## 7. Account and Strategy discovery

### 7.1 IBKR Accounts

Add a read-only IBKR Account discovery operation to `ibkr-connector`. It must:

1. connect to the configured Gateway host and port with `readonly=True`, no Account filter, no startup position/order fetch, and the reserved client ID `2147483647`;
2. read `managedAccounts()`;
3. normalize, validate, de-duplicate, and sort the returned Account IDs;
4. disconnect in `finally`;
5. return typed discovery results and typed connection/authentication errors without exposing credentials.

Reserve client ID `2147483647` for discovery. Account validation must reject an Account whose digit-derived client ID equals that value.

Discovery reads Gateway state only. It must not place, cancel, bind, or inspect orders and must not save Accounts automatically.

### 7.2 Zehnlabs Strategies

Add a typed Strategy-catalog operation to the existing allocation client:

- Request `GET {ALLOCATIONS_BASE_URL}/names/all`.
- Accept only a `200` JSON response with `status: "success"` and a `strategies` list whose items contain non-empty `name` and `long_name` strings.
- Return the list sorted by `long_name`, then `name`.
- Use the existing allocation timeout and error-redaction rules.

The catalog is for display. Before saving Account assignments, fetch allocations for every distinct selected Strategy through the existing authenticated `GET {ALLOCATIONS_BASE_URL}/{strategy}/allocations` operation. The save succeeds only if the configured Zehnlabs API key can fetch and validate every selected Strategy. Report failures by Strategy name without exposing the key or an upstream response body.

Validation means more than accepting the HTTP envelope: pass each fetched allocation list through the existing `validate_and_scale()` function and apply each selected built-in Replacement Set. Any malformed weight, invalid total, invalid target, or Replacement Set failure prevents the complete Account save.

### 7.3 Replacement Sets

Read Replacement Sets from the existing built-in registry in `rebalance-calculator`. Display the set name and its existing descriptive metadata if available. The server, not the submitted form, decides which names are valid.

## 8. Work management, scheduling, and history

### 8.1 Work submission

All dashboard Preview, Rebalance Now, initial rebalance, and Schedule for Next Market Open actions create durable Work Items before execution. The confirmation form contains a server-generated, single-use request token. Store only its SHA-256 hash. Repeating the same POST must return the original Work Item instead of executing twice.

For a bulk onboarding submission, derive one stable token per Account from the form token and Account ID so a repeated POST returns the same complete set of Work Items.

If an Account already has a `pending` or `running` Work Item with the same action, a different request token must not create another. Return `409` with a link to the existing Work Item. A Preview and a rebalance may both exist for one Account, but the dispatcher must run them sequentially as defined below.

The scheduler remains the single owner of pending-work dispatch:

- A Preview has `execute_at` equal to the current time and may run outside market hours.
- Rebalance Now and initial rebalance use the current time and source `dashboard` or `initial` when the market is open.
- When the market is closed, Rebalance Now and initial rebalance use source `scheduled` and the next configured market-open execution time. The confirmation page and resulting receipt must say that the action will be scheduled rather than executed immediately.
- Schedule for Next Market Open always uses source `scheduled` and that next execution time.
- Existing retry decisions create a `retry` Work Item for the next market open instead of editing `scheduled.json`.

Preserve the current retry-once policy exactly: a failed or market-closed Signal, immediate dashboard rebalance, or immediate initial rebalance may create one pending `retry` rebalance for that Account; a `scheduled` or `retry` execution never creates another retry. If a pending rebalance already exists, it satisfies the retry requirement and no second rebalance is created; add a note to the failed Run identifying the existing Work Item. A successful Signal, immediate dashboard rebalance, or immediate initial rebalance cancels any still-pending retry. Preview work never blocks creation of the one pending rebalance. Adding the same Account/action/execution time is idempotent.

The scheduler must always arrange one date job for the earliest pending Work Item. After an insert, cancellation, or execution, it recalculates that job. When the job fires, it loads due pending items, selects at most one item per Account, and leaves the other item pending for the next dispatch. Selection order is `execute_at`, then action priority (`rebalance` before `preview`), then `created_at`, then `work_id`. Group the selected items by action and Strategy, sort groups by the same earliest-item order, and await each group to completion before starting the next group. This prevents two scheduler-created batches for one Strategy from racing each other and guarantees that an Account/client ID appears at most once in a dispatch cycle.

At startup, a pending Preview whose `execute_at` has passed is due immediately. A pending rebalance whose time passed while the process was down is due immediately only if XNYS is currently open and the configured execution time for that session has passed; otherwise update its `execute_at` to `MarketClock.next_scheduled_run()` and show the new time in the dashboard. This missed-time rule applies only to `pending` items and never replays `interrupted` work.

An item for a deleted, disabled, wrong-mode, or invalid Account becomes `cancelled` with a safe explanation. The existing Strategy-running gate must accept or reject the batch synchronously before the first `await`. For an accepted Work Item batch, create the typed Run rows, associate their IDs with the Work Items, and mark both records `running` in one transaction before process-pool submission. If that transaction fails, release the Strategy gate and do not submit the worker. On return, store each `AccountRunResult` and mark the Work Item `completed`; the linked Run outcome states whether trading succeeded, Previewed, failed, or found the market closed. Coordinator and worker exceptions must become terminal `AccountRunResult` failures through the existing failure path. If terminal persistence fails after broker work may have occurred, stop accepting new trading work, expose a critical storage error, and never infer success or automatically retry. Never replay a Work Item or Run found in `running` state after restart; startup marks it `interrupted` for user review.

Extend `FailureReason` with `STRATEGY_BUSY`. Preserve the existing behavior of dropping an Ably Signal when its Strategy is already running, before creating a Run row. A rejected dashboard or scheduled Work Item must instead be completed with a terminal failed Run whose reason is `STRATEGY_BUSY`; create and finish that synthetic Run in one state transaction and make no broker call.

Only `pending` Work Items may be cancelled. Cancelling future work does not claim to cancel an order already submitted to IBKR.

### 8.2 Executor changes

Refactor `StrategyExecutor.execute(...)` so its existing Strategy-running gate returns a typed accepted/busy decision, accepted execution receives parent-generated `run_id` and `batch_id` values, and the call returns the typed result for every requested Account. Preserve its process-pool grouping and existing Signal duplicate suppression. Persist returned results through the SQLite state module instead of `RunRecords` JSONL files. Replace schedule-file retry updates with the Work Item operation described above.

Ably signal execution may continue to call the executor directly; it does not need a Work Item. The parent must still create its Run rows before process-pool submission and finish them afterward. Preserve the current behavior that drops a signal for a Strategy already running.

Update notification rendering for every new source and failure reason. Use the user-facing labels `Signal`, `Dashboard`, `Initial setup`, `Scheduled`, and `Next-open retry` for the corresponding `RunSource` values. Render `STRATEGY_BUSY` as “The Strategy was already running; this request did not reach the broker.” Dashboard/scheduled busy results follow the existing failed-Run notification path; dropped duplicate Signals retain the existing log-only behavior.

### 8.3 Retention

Prune terminal Run history and completed/cancelled Work Items older than 30 days after recording a Run and once during startup. Never prune pending, running, or interrupted Work Items or interrupted Runs automatically. The dashboard may filter history by Account, outcome, source, and date.

## 9. Dashboard behavior

Use FastAPI with server-rendered Jinja templates, local CSS, and minimal local JavaScript. Do not add a separate frontend build, Node runtime, SPA framework, CDN asset, or public JSON management API.

Every page must be keyboard usable, use explicit form labels, expose validation errors next to the relevant field and in a summary, and never use color as the only indication of paper/live mode or success/failure. Display `PAPER` or `LIVE` persistently in the page header.

### 9.1 Routes

Implement these HTML routes:

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/healthz` | Unauthenticated liveness response; no secrets or Account data. |
| `GET` | `/login` | Login form and pre-authentication CSRF session. |
| `POST` | `/login` | Authenticate the fixed `admin` user and rotate the session ID. |
| `POST` | `/logout` | Revoke the current session. |
| `GET` | `/` | Status page: mode, onboarding state, Gateway, Zehnlabs, Ably, pending work, and recent Runs. |
| `GET` | `/onboarding` | Current wizard stage and readiness errors. |
| `POST` | `/onboarding/discover` | Discover Accounts, Strategies, and Replacement Sets without saving. |
| `POST` | `/onboarding/save` | Validate and atomically save the complete current-mode Account configuration. |
| `POST` | `/onboarding/preview` | Create Preview Work Items for all enabled Accounts. |
| `POST` | `/onboarding/finish` | Create initial rebalance Work Items only after every enabled Account has a successful Preview newer than its `updated_at`. |
| `GET` | `/accounts` | List and edit current-mode Accounts and assignments. |
| `POST` | `/accounts/discover` | Refresh IBKR discovery for the edit page. |
| `POST` | `/accounts/save` | Validate and atomically save all current-mode Account rows. |
| `POST` | `/accounts/import` | Validate and import the current `accounts.yaml` format for migration. |
| `GET` | `/accounts/export` | Download the current configuration in the existing `accounts.yaml` format. |
| `GET` | `/accounts/{account_id}/preview` | Show Preview confirmation. |
| `POST` | `/accounts/{account_id}/preview` | Create one Preview Work Item. |
| `GET` | `/accounts/{account_id}/rebalance` | Show Rebalance Now confirmation and whether it will run now or at next market open. |
| `POST` | `/accounts/{account_id}/rebalance` | Create one Rebalance Work Item. |
| `GET` | `/accounts/{account_id}/schedule` | Show next-market-open scheduling confirmation. |
| `POST` | `/accounts/{account_id}/schedule` | Create one scheduled Rebalance Work Item. |
| `GET` | `/work` | List pending and recent Work Items. |
| `GET` | `/work/{work_id}` | Show status, timing, safe error, and linked Run. |
| `POST` | `/work/{work_id}/cancel` | Cancel a pending Work Item. |
| `GET` | `/runs` | Filterable Run history. |
| `GET` | `/runs/{run_id}` | Run outcome, planned/executed orders, notes, and safe errors. |

All routes except `/healthz`, `/login` GET, and `/login` POST require an authenticated session. Unknown Accounts, Work Items, and Runs return `404` without disclosing other identifiers. `GET` requests may create or refresh only the minimum web-session state needed for authentication and CSRF; they must not change Accounts, Work Items, Runs, schedules, or broker state.

### 9.2 Onboarding

An installation is onboarded for the current trading mode when at least one valid Account row exists for that mode and all enabled Accounts have valid assignments. The wizard must:

1. show whether required environment settings are present without displaying their values;
2. establish that Gateway is reachable and authenticated, retrieve managed IBKR Accounts, retrieve the Zehnlabs Strategy catalog, and load built-in Replacement Sets;
3. let the user choose which discovered Accounts to store, assign a Strategy and optional Replacement Set to every chosen Account, and enable the Accounts that should trade;
4. validate the complete submission and verify allocation access for every selected Strategy;
5. save all current-mode Accounts atomically;
6. Preview every enabled Account and show each result;
7. finish only after every enabled Account Preview succeeds, then create initial rebalance work for all enabled Accounts; partial activation is not allowed.

Discovery results are transient and are not Account rows. An unselected discovered Account is not stored. Every stored Account, including a disabled Account, has a valid Strategy assignment; disabling preserves that assignment for later re-enablement.

If the market is closed, the final page must show the exact next execution time with the `America/New_York` timezone and create pending work for that time. It must not claim that a rebalance already occurred.

### 9.3 Day-to-day use

The status page is informational. A subscriber needs to log in only to change Account assignments or enablement, request a Preview or rebalance, schedule/cancel work, or inspect detailed history.

Account saves affect future signals and Work Items. Disabling, deleting, or changing the Strategy or Replacement Set of an Account cancels its pending Work Item in the same Account-save transaction, so work can never execute with an assignment different from the one the user confirmed. A Run already in progress keeps its immutable Account snapshot; a save does not stop that Run or cancel broker orders.

A live-trading confirmation must name the Account, Strategy, Replacement Set, action, and execution time and require an unchecked acknowledgement that the action may place orders. It must not ask the user to log in a second time. Paper confirmations must still identify the action and timing but do not require the live acknowledgement.

## 10. Dashboard security

### 10.1 Authentication and sessions

- The only username is the exact string `admin`. There is no registration, username change, email reset, anonymous setup, or second identity-provider login.
- Compare credentials with `secrets.compare_digest`, bound submitted username/password lengths, and return the same public error for an invalid username and invalid password.
- Never store or log the password.
- Use at least 32 random bytes for session IDs and CSRF tokens. Store only their SHA-256 hashes.
- The cookie name is `__Host-rebalancer-session`. It contains only the opaque session ID and has `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and no `Domain` attribute.
- Sessions expire after 30 minutes idle or 12 hours absolute, whichever occurs first.
- Rotate the session ID after login. Logout revokes it. Clear all sessions on process startup so a password change plus restart invalidates old sessions.
- Before creating or reading a session, delete expired session rows. On startup and after a login attempt, delete login-failure rows whose window and block both ended more than 24 hours earlier.
- Login and authenticated responses use `Cache-Control: no-store`.

### 10.2 Request protection

- Every state-changing request, including login and logout, is `POST` and requires a session-bound CSRF token plus a same-origin `Origin` or `Referer` check.
- `GET` routes must never change durable state or invoke broker operations.
- CORS is disabled. Use relative redirects; never construct a redirect from an unvalidated request host.
- Apply login throttling by the normalized client address. When Railway's provided `RAILWAY_ENVIRONMENT_ID` is present, use Railway's documented `X-Real-IP` header. If that header is missing or invalid, use one fixed `railway-unknown-client` throttle identity; never fall back to `X-Forwarded-For`. Outside Railway, ignore all forwarded-client headers and use the socket peer address. Hash the chosen identity before storage. Five failures within 15 minutes block that identity for 15 minutes and return `429` with `Retry-After`. A successful login clears that identity's failure row.
- Add a restrictive same-origin Content Security Policy, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a restrictive `Permissions-Policy` to every HTML response.
- Do not log request bodies, cookies, authorization headers, CSRF values, passwords, API keys, or upstream response bodies. Production error pages must not contain tracebacks, environment values, SQL, or filesystem paths.

### 10.3 Trading safety

- The server must verify every Account ID, Strategy, Replacement Set, mode, enabled state, Work Item, and action from server-side state; hidden form values are not trusted.
- Preview must never place or cancel an order. Preserve the existing preview execution path and test its broker call boundary.
- A disabled or wrong-mode Account cannot be previewed, rebalanced, or scheduled.
- Every receipt must distinguish Preview, immediate rebalance, and scheduled rebalance.
- Logout, Account disablement, or cancellation of pending work must not claim to cancel broker orders already sent.

## 11. Removal and compatibility work

Remove these runtime file interfaces:

- `accounts.yaml` as the live source of Account state;
- `config.yaml` and its bind mount;
- `data/manual-rebalance/rebalance.json` and the manual-event watcher;
- `data/market-open/scheduled.json` and schedule-file helpers;
- `data/runs/*.jsonl` and `RunRecords`;
- the `./data:/app/data` bind mount;
- shell tooling whose only purpose is to write the removed manual or schedule files.

Keep YAML parsing only for dashboard import/export compatibility. Import is available only when the database contains no Accounts. It accepts the existing strict `accounts.yaml` document shape, validates both trading modes as one complete set, and saves nothing until all selected Strategies pass allocation access checks. It then inserts that complete set in one transaction. Export includes all stored paper and live Accounts in the same shape and never includes deployment secrets, sessions, pending work, or Run history. Keep `accounts.yaml.example` only as the documented import/export format.

Migration intentionally does not import `config.yaml`, scheduled JSON, manual-request files, or JSONL Run history. The new release uses the exact code defaults even if an existing subscriber changed the currently documented YAML settings. Upgrade documentation must identify this as a breaking loss of customization, require the subscriber to compare their file with the shipped defaults, and obtain their explicit acceptance before activating the new release. Existing pending schedules must be reviewed and recreated in the dashboard. Old Run files remain in the pre-upgrade backup for their remaining retention period. A manual-request file is never replayed because its execution state is ambiguous.

Update `AGENTS.md`, `CONTEXT.md`, the README, example environment file, Docker instructions, Railway instructions, and architecture documentation so none of them tells users to edit `accounts.yaml`, `config.yaml`, or manual-event files during normal operation.

## 12. Container and deployment changes

### 12.1 Image

Add explicit runtime dependencies for FastAPI, Uvicorn, Jinja2, multipart form parsing, and the minimal privilege-drop utility used by the entrypoint. Keep all dashboard assets inside the image. Add a root entrypoint whose only privileged filesystem action is to create `/app/state` if needed and recursively set that directory's ownership to the existing `appuser`; it must then `exec` the application as `appuser`. Python, Uvicorn, the scheduler, the process pool, and every worker must run as that non-root UID. Set Railway's `RAILWAY_RUN_UID=0` only so this entrypoint can repair the root-mounted volume before it drops privileges. Do not run the application itself as root.

Replace the heartbeat-file health check with an HTTP check of `http://127.0.0.1:${PORT:-18778}/healthz` using Python's standard library and the same defaulted runtime setting used by Uvicorn. `/healthz` returns `200` only when the event loop is responsive and SQLite passes a lightweight query. It must still return `200` while onboarding is incomplete or an external service is unavailable, so those conditions do not cause a restart loop.

### 12.2 Docker Compose

The supplied Compose stack must:

- define exactly `rebalancer` and `ibkr-gateway` services;
- keep `ib_settings` mounted at the Gateway's current settings path;
- add a named `rebalancer_state` volume mounted at `/app/state`;
- remove the `config.yaml`, `accounts.yaml`, and `data` mounts;
- publish the dashboard as `127.0.0.1:18778:18778`;
- retain any Gateway API or VNC host mappings only on loopback by default;
- pass `DASHBOARD_ADMIN_PASSWORD` and existing application variables to `rebalancer`;
- pass IBKR credentials only to `ibkr-gateway`;
- use `rebalancer` consistently in service names, health dependencies, log commands, and examples.

Keep the current Gateway release pinned exactly as `gnzsnz/ib-gateway:10.51.1e@sha256:85975f493bcacc8341cb42e6946c460868d165e5f64d9924b1958c8f03e7bee4` in both Compose and the Railway template unless a separate, reviewed Gateway-upgrade change is approved.

`docker compose config` must show no wildcard host binding for the dashboard, Gateway API, or VNC ports.

### 12.3 Railway

Provide a Railway template and README Deploy to Railway button that create:

- one `rebalancer` service built from this repository;
- one pinned stock IBKR Gateway image service;
- one volume mounted at `/app/state` on `rebalancer`;
- one volume mounted at the Gateway's existing settings path;
- a private network connection from `rebalancer` to the Gateway;
- one public Railway HTTPS domain for port `18778` on `rebalancer` only;
- one replica for each service;
- the existing required user-supplied secrets and settings;
- one shared `TRADING_MODE` template input referenced by both `rebalancer` and `ibkr-gateway`, so a staged mode change redeploys both services with the same value;
- a generated `DASHBOARD_ADMIN_PASSWORD` using Railway's `secret(32)` template function;
- `RAILWAY_RUN_UID=0` on `rebalancer`, used only by the privilege-dropping entrypoint;
- `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0` and `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=900` on `rebalancer`;
- Railway's `Always` restart policy on both `rebalancer` and `ibkr-gateway`;
- `/healthz` as the rebalancer health-check path.

The template must set or reference `PORT=18778` so its public-domain target, healthcheck, and application listener agree. Documentation must explain in plain language that Railway sends `SIGTERM`, waits up to 15 minutes, and then force-stops a process that has not exited. This grace period covers the configured normal order timeouts but is not a promise that every broker operation can finish before a forced stop.

Railway templates do not guarantee that volume backup schedules are enabled. Add an unavoidable post-deploy checklist that directs the project owner to the Backups tab for each service, enables daily and weekly schedules for both volumes, takes a manual backup before every application or Gateway upgrade, and records a successful paper-mode restore drill. The README must describe Railway as providing backup tooling, not as making backups automatic without this step.

Do not publish Gateway API or VNC publicly. If Railway cannot express the complete multi-service template in a repository file, create the template in Railway and keep a repository document that lists its exact services, images, mounts, variables, references, ports, health checks, and generated-secret expression so it can be reproduced and reviewed.

## 13. Required automated tests

All existing calculation, connector, execution, notification, and safety tests must continue to pass. Replace tests of deleted file interfaces with the following coverage.

### 13.1 Configuration tests

- `AppConfig()` reproduces every retained current `config.yaml` value.
- Configuration is immutable and rejects invalid constructed values.
- No production module reads `config.yaml` or calls `get_config()`.
- Runtime settings redact the Zehnlabs key.
- Missing, blank, short, and known-default dashboard passwords fail startup without including the value in output.
- Invalid `TRADING_MODE` and `PORT` fail startup; missing Zehnlabs or unavailable Gateway credentials do not prevent `/login` and `/healthz` from starting.

### 13.2 SQLite tests

- A new temporary database migrates to the expected `user_version` and exact schema.
- Initialization is idempotent; reopening preserves Accounts, pending work, login throttles, and Runs.
- Foreign keys are enabled on every connection.
- Every connection uses `DELETE` journaling and `FULL` synchronous durability, closes before return, and no connection/store object crosses the process-pool seam.
- A database with a newer unsupported `user_version` fails startup without modification.
- A failed migration or failed `quick_check` stops startup without recreating the file.
- Replacing one mode's Account set is atomic and leaves the other mode unchanged.
- Invalid or injected values cannot escape parameterized queries or violate constraints.
- Account deletion, disablement, or Strategy/Replacement-Set change cancels pending work in the Account-save transaction but retains Runs.
- Work Item transitions reject illegal transitions and permit cancellation only from `pending`.
- Duplicate request tokens return the original Work Item and never create a second execution.
- A different request token for an Account with the same pending/running action returns `409` and the existing receipt; the database invariant permits at most one outstanding item per Account/action.
- Startup changes `running` to `interrupted` and does not dispatch it.
- Run serialization round-trips every typed `AccountRunResult` and `RunRecord` field losslessly.
- Run rows exist in `running` state before a worker is submitted; a state-write failure prevents submission.
- Startup changes unfinished Run rows to `interrupted` and never replays them.
- Terminal Runs and completed/cancelled Work Items older than 30 days are pruned while newer records and every pending, running, or interrupted record remain.

### 13.3 Discovery and Account-management tests

- IBKR discovery uses the reserved ID, read-only mode, no Account filter, and no startup fetch; it always disconnects.
- IBKR discovery de-duplicates/sorts valid managed Accounts and makes no place/cancel/bind/order calls.
- An Account deriving the reserved discovery client ID is rejected.
- The Strategy catalog accepts only the documented response shape, sorts deterministically, times out correctly, and redacts upstream errors.
- Saving verifies each distinct Strategy exactly once through the existing allocation operation.
- Any invalid Account, duplicate client ID, unknown Replacement Set, inaccessible Strategy, or malformed allocation prevents the entire save.
- A valid save refreshes the registry and subscribes/unsubscribes exactly the changed Strategy set.
- Repeating the same Account-save form is idempotent; no lock, revision, lease, or concurrent-edit protocol is introduced or claimed.
- A subscription failure leaves the committed Account state intact and reports degraded status.
- Initial authentication/subscription failure retries every five seconds from the latest committed registry and eventually makes the actual channel set equal that registry.
- Account YAML import accepts the current valid format and rejects the same invalid cases as the form; export round-trips without secrets.
- Discovery does not persist unselected Accounts; every stored enabled or disabled Account has a valid assignment.

### 13.4 Work and scheduler tests

- Preview is due immediately even while the market is closed.
- Rebalance Now and onboarding finish are immediate during market hours and scheduled for the exact next configured market open otherwise.
- Explicit scheduling and existing retry decisions create durable `retry`/`scheduled` Work Items instead of files.
- The scheduler always targets the earliest pending time and recalculates after insert, cancel, and completion.
- Startup executes overdue pending Previews, executes overdue pending rebalances only when the market is open and that session's configured execution time has passed, and otherwise moves them to the exact `MarketClock.next_scheduled_run()` time; interrupted work is never included.
- Due work is grouped by action and Strategy and produces one stored result per Account.
- Dispatch order and rebalance-before-Preview priority are deterministic; groups run sequentially and no batch contains the same Account/client ID twice.
- Retry creation reuses an existing pending rebalance, can coexist with one pending Preview, and never creates a second pending rebalance.
- Deleted, disabled, wrong-mode, and invalid Accounts are cancelled before executor invocation.
- A process exception produces a terminal failed Run and a completed Work Item and does not strand either in `running`.
- A Linux production-image test proves workers receive no SQLite store/connection or FastAPI/Ably object, never access SQLite, and process-pool shutdown releases inherited network descriptors and the listening port. If that probe fails, stop and request authorization for a process-start redesign instead of silently changing `fork`.
- Ably signal results are stored without creating dashboard Work Items.
- Existing duplicate-Strategy suppression and retry-once behavior remain unchanged.
- A Work Item submitted while its Strategy is running finishes once with `STRATEGY_BUSY`, makes no broker call, and is not stranded or silently dropped; an Ably Signal in the same condition retains the existing drop behavior.
- New Run sources and `STRATEGY_BUSY` render the exact labels/messages in section 8.2 in both the dashboard and notifications.
- No test creates `scheduled.json`, a manual-rebalance file, or JSONL Run records.

### 13.5 Dashboard tests

Use FastAPI's in-process test client with fake discovery, clock, subscriber, executor, and a temporary SQLite file.

- Every route has the authentication and method behavior in the route table.
- Crawling every `GET` route leaves Account, Work Item, Run, schedule, and broker state unchanged; session creation and idle-timestamp refresh are the only permitted writes.
- Wrong username and wrong password have the same status and public message.
- Login rotates the session ID; logout, idle expiry, absolute expiry, and restart invalidation reject the old cookie.
- Expired anonymous/authenticated sessions and stale login-failure rows are pruned according to section 10.1.
- The session cookie has the required name and attributes and contains no credential or Account data.
- Missing, invalid, cross-session, and cross-origin CSRF submissions make no state change.
- The sixth failed login from one client receives `429`; throttling survives a database reopen, expires after 15 minutes, and a successful login clears it.
- Railway-mode throttling uses only valid `X-Real-IP`; missing/invalid values share the fixed unknown identity, forged `X-Forwarded-For` values are ignored, and VPS mode uses only the socket peer.
- Authenticated HTML responses contain all required security and cache headers; CORS is not enabled.
- Validation errors are escaped, actionable, and contain no trace, secret, SQL, or filesystem path.
- Onboarding cannot advance past a failed readiness, assignment, allocation-access, or Preview check.
- One failed enabled-Account Preview blocks all initial activation; no subset is submitted.
- A valid onboarding flow stores Accounts, previews each enabled Account, and creates immediate or scheduled initial work according to the fake clock.
- A live submission without the acknowledgement is rejected without work or broker calls.
- Repeated form submission creates one Work Item and one executor call.
- Two independently opened forms for the same Account/action cannot create duplicate work; a Preview and rebalance may coexist but dispatch sequentially.
- Account disablement, schedule cancellation, and receipts describe their effects accurately.
- Preview produces zero broker place/cancel calls.

### 13.6 Container and deployment tests

- Build the production image and start it with an empty state volume and fake external dependencies; `/login` and `/healthz` respond.
- Restart it against the same volume and prove Account, Work Item, and Run persistence.
- Start the image with a root-owned state volume, prove the entrypoint makes it writable, inspect the running Python and worker UIDs to prove they are non-root, and prove there is no required Node runtime.
- `docker compose config` contains exactly the two required services and two named volumes, the loopback dashboard binding, and no removed file mounts.
- Compose and the Railway manifest use the exact approved Gateway tag and digest.
- Compose and Railway pass one identical `TRADING_MODE` value to both services; a Railway mode change is a staged deployment of both.
- The production health check succeeds before onboarding and does not expose configuration.
- The Railway template definition or reproducible manifest contains the exact service, volume, variable, `secret(32)`, `RAILWAY_RUN_UID`, port, replica, teardown, restart-policy, image, and health-check contract in section 12.3; the deployment checklist covers backup schedules for both services.
- Migration tests prove only Account YAML imports; documentation detects/explains changed `config.yaml` values and requires acceptance of code defaults, pending schedules require recreation, manual files never replay, and legacy Run files remain unchanged in the backup.

## 14. End-to-end acceptance scenarios

The implementation is complete only when all of these scenarios pass:

1. **Fresh VPS installation:** `docker compose up` starts two containers; the dashboard is reachable at `http://localhost:18778` on the host but not through a non-loopback host address; login with `admin` and the `.env` password works; onboarding completes without editing a file.
2. **Fresh Railway installation:** the Deploy to Railway flow creates the two services and two volumes, generates the admin password, exposes only the dashboard through HTTPS, and preserves state across a rebalancer redeploy.
3. **Paper onboarding:** with separately supplied paper credentials, discovery lists the managed paper Accounts and available Strategies; inaccessible Strategies cannot be saved; valid enabled Accounts Preview successfully and initial work runs now or at next market open as stated by the UI.
4. **Day-to-day operation:** a logged-in user can Preview, rebalance, schedule, cancel pending work, enable/disable Accounts, change assignments, and inspect Run details. Repeated submissions never place duplicate work.
5. **Signal operation:** allocation signals continue to route to every enabled Account for the Strategy, produce the same trading behavior as before, write Run history to SQLite, and preserve retry-once behavior.
6. **Restart safety:** completed state survives; pending work remains scheduled; work interrupted during process shutdown is visibly marked `interrupted` and is not replayed.
7. **Mode change:** changing `TRADING_MODE` and redeploying activates only stored Accounts for the new mode. If none exist, the dashboard stays available and returns to onboarding for that mode.
8. **Security:** unauthenticated, CSRF-invalid, wrong-origin, disabled-Account, wrong-mode, unacknowledged-live, and duplicate requests make no unauthorized state change or broker call; no secret appears in HTML, logs, errors, or SQLite exports.
9. **Legacy migration:** an existing valid `accounts.yaml` can be imported through the dashboard and exported in the same format. No runtime behavior depends on that file afterward.
10. **Regression:** the full automated suite passes and the separate paper pilot confirms the production image before the Railway button is presented as supported for live trading.

## 15. Deliverables and validation command

The implementation must include code, schema migrations, templates/assets, updated tests, updated Compose/image configuration, Railway template definition, README and environment documentation, and updated domain/architecture documentation in the same change.

Add `tools/validate.sh` as the single documented validation command. It must run formatting/linting if configured, the complete Python suite with `PYTHONPATH=rebalancer`, `docker compose config`, and the production-image smoke test. It must return nonzero on any failure and must be the command used for final implementation validation.
