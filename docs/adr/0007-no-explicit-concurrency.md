# One strategy at a time per process, with no locks added

Concurrency is limited to what already exists: one `ProcessPoolExecutor` using an
explicit `fork` start method (pinned because Python 3.14 changes the default, PRD §10.2
EXE-1), the asyncio event loop, and `asyncio.gather` over accounts inside one
worker. No new locks, semaphores, leases, file locks or threads. One worker handles
all accounts of one strategy concurrently on its event loop; duplicate triggers for a strategy that is
already running are dropped for Signals. Durable Work Items instead complete once
with `STRATEGY_BUSY` without reaching the broker. When
the pool itself breaks, `WORKER_CRASHED` results are built for every account and a
replacement pool is spawned. Adding per-account concurrency or a persistent
scheduler thread would need an ADR first.
