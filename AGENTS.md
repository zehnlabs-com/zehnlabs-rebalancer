# AGENTS.md

## Repository purpose

The Zehnlabs Portfolio Rebalancer follows Strategy allocation Signals in subscriber-owned IBKR Accounts. The supported deployment has exactly two containers:

- `rebalancer`: one Uvicorn worker/parent process owning FastAPI, orchestration, SQLite, scheduling, Ably, notifications, and the existing internal process pool;
- `ibkr-gateway`: the digest-pinned stock Gateway image.

The current behavior source of truth is `docs/railway-dashboard-implementation-spec.md`. Trading details are in `docs/rebalancing-logic.md` and `docs/adr/`.

## Non-negotiable architecture

- Keep application source under `rebalancer/` and use `rebalancer` consistently as the service and user-facing name.
- The parent is the only SQLite owner. Workers receive immutable typed inputs and return typed `AccountRunResult` values.
- Preserve one Uvicorn worker, one Railway replica, the current process pool, and explicit Linux `fork`.
- Do not add locks, leases, leader election, threads, pool warming, or another concurrency mechanism without explicit user authorization.
- Do not add PostgreSQL, Redis, a message broker, external identity provider, separate dashboard/worker/scheduler service, or management CLI.
- Keep broker connectors and the rebalance calculator independent of FastAPI and SQLite.
- Keep Gateway credentials in its environment only. Never store them in SQLite.
- Never use the working `accounts.yaml`, `.env`, or developer credentials in tests or pilots.

## Application modules

- `rebalancer/app/main.py`: import-safe composition root and Uvicorn entry point.
- `application.py`: typed dashboard-facing application façade.
- `state/models.py`, `state/sqlite.py`: durable models, migrations, and every SQL statement.
- `services/account_manager.py`: complete Account validation/persistence/subscription reconciliation.
- `services/work_manager.py`: idempotent work creation, deterministic dispatch, Run persistence, retries.
- `services/strategy_executor.py`, `trading_executor.py`: parent pool boundary and worker entry point.
- `web/app.py`, `web/auth.py`, `web/routes.py`, templates/static: server-rendered dashboard.
- `packages/ibkr-connector/.../discovery.py`: read-only managed-Account discovery.

## Durable state

Production state is `/app/state/rebalancer.db`. Use standard-library `sqlite3`, parameterized SQL, UTC RFC3339 `Z` timestamps, short-lived connections, foreign keys, DELETE journal mode, FULL synchronous durability, and forward-only `user_version` migrations. Do not add an ORM or `aiosqlite`.

Accounts, Work Items, Runs, sessions, and login throttles live in SQLite. `accounts.yaml` exists only as dashboard import/export compatibility. `config.yaml`, manual-request files, scheduled JSON, JSONL Runs, and their runtime tools are removed.

## Trading invariants

- Preview places and cancels no orders.
- Live Runs cancel only own orders and stop on foreign working orders.
- Missing Account data and missing prices fail the Run; never substitute zero or an invented price.
- Preserve the fixed reserve outside Strategy capital and portfolio-wide whole-share optimization.
- Place all sell market orders, wait, then place DAY limit buys in symbol order.
- Read status only from stored order handles.
- Preserve retry-once semantics and the Strategy-running Signal drop behavior.
- Parent-generated Run IDs must be persisted before worker submission.

## Configuration

`AppConfig()` contains immutable validated defaults. There is no YAML loader or global `get_config()`. Environment-backed `RuntimeSettings` carries deployment choices and secrets. Missing external operational credentials must leave the dashboard usable; an unsafe dashboard password, invalid `TRADING_MODE`/`PORT`, or unusable SQLite is startup-fatal.

## Commands

```bash
docker compose up -d --build
docker compose logs -f rebalancer
./tools/reload.sh
./tools/validate.sh
```

The supplied Compose file binds dashboard, Gateway API, and VNC ports to loopback and defines only `rebalancer` and `ibkr-gateway` with `rebalancer_state` and `ib_settings` volumes.

## Testing

Run `./tools/validate.sh`. It must execute the complete Python suite with `PYTHONPATH=rebalancer`, `docker compose config`, and the production-image smoke test. Use temporary SQLite paths, synthetic Account IDs, fake external dependencies, and separate paper credentials for manual pilots.

The Linux production-image probe is authoritative for the `fork`/descriptor boundary. If it fails, stop and request authorization before proposing a process-start redesign.

## Documentation

Use the vocabulary in `CONTEXT.md`. Before behavior changes, read applicable ADRs and the machine-local knowledge base described in `AGENTS.local.md`. Update README, architecture, deployment, and upgrade guidance in the same change. Historical refactor reports may retain historical file names when clearly labeled as such.

## Git and local state

Do not commit without explicit user authorization. Preserve unrelated worktree changes and untracked user files. Use `apply_patch` for edits. If `AGENTS.local.md` exists, read and follow it.
