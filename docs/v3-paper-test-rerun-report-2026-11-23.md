# Ready 

Refactor v3 **can proceed to the separately approved production cutover**. All safety-critical
paper-account checks passed, every test order was reconciled, and the final broker audit found zero
working orders. The tested candidate is `refactor/v3` at `3f02623`, image
`sha256:6fb87a2918449c6bde7867fcb58d62a329e7a904b532b2ae1ffd814b2c778196`.

This is not authorization to change production. Production was already stopped by the user before
the rerun and remains stopped. The test project is also stopped.

## What passed in the rerun

- With the production Gateway offline, raw QQQ and VTI snapshots returned complete live data in
  0.057–0.104 seconds. The real v3 connector returned both symbols in 0.109 seconds, using midpoint
  marks, ask references for buys, and bid references for sells. All were far inside the configured
  15-second request bound.
- The normal v3 preview completed `PREVIEWED`, planned one sell and three buys, and placed no order.
  Its computed portfolio value differed from IBKR NetLiquidation by 0.0925%, inside the 2% gate.
  BTAL and the other allocation symbols had usable permitted live sources; no fallback was needed.
- The unmodified live paper path completed `SUCCEEDED`: one market sell and three limit buys were
  submitted in the required two phases, and all four finished `FILLED`.
- The funds probe showed that an affordable app-style limit order was accepted by IBKR. A deliberately
  oversized what-if order was not transmitted and was constrained by IBKR error 201.
- In the cash micro-cycle, the 98% post-sell cash threshold was visible after 1.002 seconds. One VTI
  share was sold and bought back, the position returned to 84 shares, and cash reconciled after
  commissions in another 1.003 seconds.
- An app-owned VTI order was deliberately left working across a disconnect. The next normal v3 live
  run recorded `Cancelled 1 earlier orders left by this app.`, completed successfully, and a fresh
  connection confirmed that no app-owned order remained.
- The rerun added exactly three unique run records and three matching notifications: preview,
  successful live rebalance, and successful reconnect run. Each worker log line carried the
  strategy, redacted account, and run ID without the old duplicate-line problem.
- The final audit found zero working paper orders. The retry file is `[]`, the temporary stop marker
  is absent, and both isolated test containers are stopped.
- The probe-only corrections were exercised inside the exact tested image with networking disabled:
  14 tests passed. No application code or candidate image changed during the rerun.

## Evidence retained from the first session

The exact v3 allocations client had already fetched three allocations for `etf-blend-302-25`.
The manual TWS order had already been observed as foreign, left untouched by the scheduled run,
attempted exactly once by the real 09:35 ET scheduler, removed from the retry file, and then cancelled
individually through client ID 0. That one manual order no longer existed, so those order-dependent
checks were not artificially recreated; their original passing evidence remains part of this
decision. All checks that the market-data conflict blocked in the first session were completed in
this rerun.


## Production cutover gate

Before any production v3 subscription starts, follow the cutover section of the agreed plan: back up
the v2 assets, pin this exact image, validate the migrated configuration, and run a one-off v3 preview
against every enabled live account. Review every planned market sell, limit buy, price source,
reserve, foreign order, and the 2% NetLiquidation comparison. Start the v3 event-broker only after
those previews are approved. Keep the v2 rollback assets until a normal production signal completes
and every resulting order is reconciled.


