# Production-image paper acceptance

## Result

The isolated Oracle VPS paper pilot passed on November 27, 2026. The application image, dashboard, SQLite state, process pool, Zehnlabs allocation access, IBKR paper Gateway, trading calculations, order execution, simulated Signal delivery, forced-process recovery, and backup restore were exercised without changing the production deployment.

Railway-specific acceptance remains pending. Do not publish the Deploy to Railway button or present Railway live trading as supported until the actual Railway project passes the final section below.

## VPS evidence

The pilot used separately supplied paper credentials, the Compose project `zehnlabs-rebalancer-test`, and its own `rebalancer_state` and `ib_settings` volumes. The production project retained the same container IDs, start times, and zero restart counts throughout testing.

- `./tools/validate.sh` passed with 300 tests, Compose validation, the non-root production image, and the Linux `fork`/descriptor probe. Thirty-two workers retained no listener descriptor.
- Fresh Compose started exactly `rebalancer` and `ibkr-gateway`. Dashboard, Gateway API, and VNC ports remained bound to host loopback.
- Gateway authentication, managed paper-Account discovery, Strategy discovery, allocation-access validation, Account save/enable/disable, scheduling, cancellation, Run history, and dashboard restart behavior passed.
- Four Previews placed and cancelled no orders.
- Multiple materially different live paper rebalances passed. Mixed Runs completed all sells before placing buys. The final broker state contained 48 filled paper orders, 282 reconciled execution rows, and no working orders.
- The whole-share optimizer defects exposed by the pilot were fixed and retested successfully, including the final follow-up Preview and rebalance.
- Repeated submissions remained idempotent, and work for one Account dispatched sequentially.
- SQLite retained Accounts, Work Items, Runs, and investigation evidence across restarts. A separate restored copy passed SQLite integrity, foreign-key, and typed-model checks with no pending work.

## Signal-path evidence

A production Ably publication was not available on demand, so acceptance used two complementary tests:

1. An automated integration test injects the real Signal JSON into the exact production Ably callback. It routes every enabled Account through the real `StrategyExecutor`, persists `source=signal` Runs in a temporary real SQLite database, and proves that failed live Signal Runs create the durable retry-once Work Items.
2. On the isolated VPS, an actual `ably.types.message.Message` containing `print-rebalance` was delivered to the same callback. The test used the committed Account registry, production Linux process pool, real Zehnlabs allocations, real paper Gateway, and persistent test SQLite volume. It produced one terminal `source=signal`, `mode=preview`, `PREVIEWED` Run and executed no orders.

This proves the complete application path after Ably delivers a message without publishing an artificial event to production channels. Ably transport authentication and subscription remained part of normal service startup; the controlled test deliberately bypassed only network publication and delivery.

## Forced-interruption evidence

The production image first completed a dashboard Preview to start its normal 32-worker process pool. All worker children were then suspended and a second Preview was submitted. SQLite showed both its Work Item and linked Run as `running` before shutdown.

Docker gave only three seconds for shutdown and then killed the isolated `rebalancer` with exit code 137; the container was not OOM-killed. Before restart, the same Work and Run remained `running` with no finish time, outcome, or result. On startup:

- the same Work Item became `interrupted` with `Interrupted by process restart; not replayed.`;
- the same linked Run became `interrupted` without a fabricated outcome or result;
- the dashboard displayed the interrupted state and linked Run;
- Work and Run counts remained unchanged after the scheduler had time to dispatch and after another restart;
- IBKR retained zero working orders and the same 282 execution rows.

The test used Preview work, so interruption itself could not place or cancel a broker order.

## Remaining Railway acceptance

Between the manually configured Railway reference project and a separate clean project deployed
only from the finished template URL, the following must still be proved before distribution:

1. Both projects create exactly the documented two services and two volumes without inherited
   secrets or registry credentials.
2. The clean template project receives a Railway-generated, revealable dashboard password and
   exposes only the dashboard through HTTPS. The reference project uses an owner-generated password.
3. The Gateway is reachable only over private networking, and a fresh `ib_settings` volume has working ownership and persistence.
4. A rebalancer redeploy preserves SQLite state, and a shared `TRADING_MODE` change redeploys both services consistently.
5. In the reference project, daily and weekly backup schedules are enabled for both volumes, manual
   backups work, and both volumes restore successfully inside the same paper-only Railway project
   and environment, which is Railway's supported restore boundary.
6. The tested multi-platform manifest and Railway-selected platform digest match the pinned
   Rebalancer source; the Gateway digest matches its approved source; image auto-updates are off for
   both services.
7. Health checks, restart policies, one replica, one Uvicorn worker, and the documented shutdown
   period match the template contract.

If either project fails its assigned item, keep the button withheld and correct the design or
implementation before retesting.
