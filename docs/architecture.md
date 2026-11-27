# Architecture

The supported topology has exactly two deployable services: `rebalancer` and the pinned stock `ibkr-gateway`. `rebalancer/` remains the Python source directory; it is not a third service.

`rebalancer` has one Uvicorn worker and one parent Python process. That process owns FastAPI, Account and Work management, Ably subscriptions, the scheduler, the existing `ProcessPoolExecutor`, notifications, and SQLite. The explicit Linux `fork` pool remains an internal execution boundary. Workers receive immutable typed Account/configuration/run inputs and return typed results; they never receive SQLite, FastAPI, or Ably objects.

SQLite at `/app/state/rebalancer.db` stores Account configuration, Work submissions and their Work Items, onboarding initialization receipts, Run history, dashboard sessions, and login throttles. Only the parent opens it. Short-lived connections use foreign keys, DELETE journaling, FULL synchronous durability, schema migrations, and integrity checks.

A Work submission is the durable identity of one dashboard form submission. Bulk Preview and initial-setup Work Items share that identity, while executor batches retain their separate Run `batch_id`. Initial onboarding completion also stores a durable receipt for the enabled Account configuration so replay cannot create a second initial batch after terminal Work history is pruned. Onboarding status itself remains derived from current Account rows.

Workers capture Account state, reserve, and price evidence before invoking the whole-share planner. These inputs survive a solver failure in the typed Run record.

The dashboard is always started. Missing Accounts or unavailable Gateway/Zehnlabs/Ably dependencies appear as onboarding or degraded readiness rather than terminating the web process. `/healthz` checks the responsive event loop and SQLite only.

Signals execute through the same typed application services used by dashboard work. A Signal that collides with its Strategy's active Run keeps the established log-and-drop behavior. Durable dashboard/scheduled work instead finishes once with `STRATEGY_BUSY` and no broker call.

There is no PostgreSQL, Redis, separate dashboard, separate worker service, management CLI, lock, lease, or leader election. One application process and one SQLite writer are the supported topology.
