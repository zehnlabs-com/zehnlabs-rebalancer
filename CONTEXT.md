# Zehnlabs Rebalancer

The Rebalancer turns a Strategy's target weights for one Account into whole-share IBKR orders without spending more cash than the Account holds. `docs/railway-dashboard-implementation-spec.md` is the current source of truth.

## Language

**Account**: One SQLite-configured IBKR Account. A Run is always for one Account. Avoid “portfolio”, “client”, or “user”.

**Strategy**: A Zehnlabs Strategy whose allocations can drive many Accounts. Avoid “model” or “portfolio template”.

**Allocation**: A raw symbol/weight pair from the API before normalization or replacement. Avoid “target”.

**Target**: A final Account symbol weight after validation, scaling, and optional Replacement Set.

**Signal**: An Ably message on a Strategy channel that requests a Run for every enabled Account assigned to that Strategy.

**Source**: What started a Run: `signal`, `dashboard`, `initial`, `scheduled`, or `retry`.

**Run**: One attempt for one Account, identified by `run_id`. A Run never raises across the process boundary; failures are typed outcomes.

**Batch**: Runs for one Strategy submitted together to one worker process.

**Mode**: `live` places orders; `preview` places and cancels nothing.

**Work Item**: A durable dashboard/initial/scheduled/retry request. Its action is `preview` or `rebalance`. Signals do not create Work Items.

**Run record**: The typed investigation detail stored losslessly with the Run in SQLite for 30 days.

**Weight**: A fraction of Strategy capital outside the fixed reserve.

**Mark**: The holding valuation price from mid → last → close → bar close.

**Buy reference**: The price used to size a buy. **Limit price** is buy reference plus configured slippage.

**Total value (V)**: cash plus marked holdings, calculated by this application rather than IBKR NetLiquidation.

**Cash floor / reserve**: A fixed dollar amount calculated once from start-of-Run value and kept outside Strategy capital.

**Target base**: Account value available to Strategy targets after the reserve. **Budget** is cash available to buys after the reserve.

**Own order**: A working order placed by this application/client ID. **Foreign order**: a working order placed by anything else. **Working order**: an order not terminal.

**Whole-share broker**: A broker whose Account changes use whole shares; IBKR is one. Existing fractional residue remains immutable.

**Replacement Set**: Compiled symbol-replacement rules applied after target validation.
