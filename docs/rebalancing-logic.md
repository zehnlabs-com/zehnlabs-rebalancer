# Rebalancing logic

This document summarizes the current execution contract. The Railway/dashboard implementation specification and ADRs are authoritative.

## One Account Run

1. The parent creates `run_id` and `batch_id` and persists a `running` Run before worker submission.
2. The worker fetches allocations, validates/scales targets, and applies the Account's built-in Replacement Set.
3. A live Run checks the XNYS market before opening a broker connection.
4. A live Run cancels only this application's working orders; any foreign working order stops it. Preview cancels nothing.
5. Read Account cash, NetLiquidation, positions, and quotes. Missing values or prices fail closed.
6. Calculate `V0`, the fixed reserve, and a portfolio-wide sell plan. A live value mismatch above 2% stops before orders.
7. Preview simulates sell proceeds, plans buys, records both plans, and places no orders.
8. Live execution submits market sells in symbol order and waits for terminal states.
9. Read conservative post-sell cash, use remaining positions and fresh quotes, and plan buys with the same reserve.
10. Submit DAY limit buys in symbol order and wait for terminal states.
11. Return one typed `AccountRunResult`; the parent stores its complete typed record losslessly and finishes any linked Work Item.

## Durable work

Dashboard Preview is due immediately, including outside market hours. Rebalance Now and initial work are immediate only while the market is open; otherwise they are scheduled for the exact next configured XNYS time. “Schedule for Next Market Open” is always scheduled.

The scheduler owns dispatch. It selects at most one pending item per Account in order of `execute_at`, rebalance before Preview, `created_at`, and `work_id`; groups by action and Strategy; and awaits groups sequentially. A pending/running Preview and rebalance may coexist for one Account, but duplicate Account/action work may not.

Overdue Preview work runs immediately after restart. Overdue rebalance work runs only after that session's configured time while the market is open; otherwise its time moves to the next scheduled session. Interrupted work is never replayed.

## Retry policy

An eligible failed or market-closed live Signal, immediate Dashboard Run, or immediate Initial setup Run may create one `retry` rebalance Work Item for next market open. A pending rebalance already satisfies that requirement. `scheduled` and `retry` Runs never create another retry. Preview never retries and never blocks the one pending rebalance.

A successful eligible Run cancels any still-pending retry. Ably Signals do not create Work Items. A Signal colliding with its Strategy's active Batch is logged and dropped; durable Work instead completes once with `STRATEGY_BUSY` and no broker call.

## Sources

Run source labels are Signal (`signal`), Dashboard (`dashboard`), Initial setup (`initial`), Scheduled (`scheduled`), and Next-open retry (`retry`). Work sources omit Signal and use Dashboard, Initial, Scheduled, or Retry.

## Restart and retention

Startup marks leftover `running` Runs and Work Items `interrupted` and never replays them. Terminal Runs and completed/cancelled Work Items older than 30 days are pruned. Pending, running, and interrupted records are retained.
