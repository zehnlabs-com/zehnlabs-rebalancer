# Zehnlabs Rebalancer

The domain of the Zehnlabs Portfolio Rebalancer: turning a strategy's target
weightings for one account into a set of whole-share buy and sell orders, without
ever spending more cash than the account holds. The authoritative behaviour
specification is `docs/refactor-prd.md` (v3.0.0).

## Language

**Account**:
One IBKR account listed in `accounts.yaml`. A rebalance is always performed for a single account.
_Avoid_: portfolio, client, user

**Strategy**:
A Zehnlabs strategy (e.g. `etf-blend-200-35`) whose target weights come from the allocations API. One strategy can drive many accounts.
_Avoid_: model, portfolio template

**Allocation**:
One raw symbol–weight pair fetched from the allocations API for a strategy, before any normalisation or replacement.
_Avoid_: target, pick

**Target**:
The final weight for one symbol for an account after validation, scaling and replacement. Targets always sum to exactly 1.0 unless the list is empty.
_Avoid_: the raw API weight is an allocation, not a target

**Signal**:
A message on a strategy's Ably channel that triggers a run for every enabled account of that strategy.
_Avoid_: event, trigger message

**Source**:
What started a run: `signal` (Ably), `manual` (`tools/rebalance.sh`) or `scheduled` (the market-open job).
_Avoid_: event type, reason

**Run**:
One attempt to rebalance one account. Identified by a `run_id`. Runs never raise; every failure becomes a recorded outcome.
_Avoid_: job, trade (a run may place zero, one, or many trades)

**Batch**:
All the runs for one strategy started together in one worker process.
_Avoid_: job, task group

**Mode**:
`live` (places orders) or `preview` (places nothing).
_Avoid_: exec, dry-run

**Weight**:
A fraction of the account's strategy capital, excluding the fixed cash reserve (`0.25` means 25%). Weights from the API are fractions.
_Avoid_: percent (the glossary uses the fraction everywhere)

**Mark**:
The price used to value a holding. Derived per symbol from a fixed fallback chain (mid → last → close → bar close).
_Avoid_: price, fair value

**Buy reference**:
The price used to size a buy.
_Avoid_: trigger price, fill price

**Limit price**:
The highest price a buy order may pay: buy reference × (1 + slippage).
_Avoid_: max price

**Total value (V)**:
`cash + Σ(holding quantity × mark)`, computed by us. It is not IBKR's NetLiquidation.
_Avoid_: net worth, NetLiq

**Cash floor / reserve**:
`minimum_cash_reserve_percent` of total value at the start of a run (`V0`). It is a fixed dollar cash allocation outside the strategy, reused unchanged for sell and buy planning.
_Avoid_: minimum cash, safety buffer

**Target base**:
Account value available to the strategy: `max(0, V0 − reserve)` for sells and `max(0, V1 − reserve)` for buys.
_Avoid_: budget (budget is cash-only)

**Budget**:
Cash available for buys: cash base − reserve.
_Avoid_: spendable cash

**Own order**:
A working order on this account placed by this app (with this account's client ID).
_Avoid_: our order

**Foreign order**:
A working order on this account placed by anything else (manual TWS/mobile, another program).
_Avoid_: outside order

**Working order**:
An order that is not yet filled, cancelled or rejected.
_Avoid_: open order (reserved for the IBKR API term)

**Whole-share broker**:
A broker whose `supports_fractional` is false. IBKR is one; a portfolio-wide optimizer selects broker-reachable whole-share quantities while preserving any existing fractional residue.
_Avoid_: integer broker

**Replacement set**:
A fixed set of symbol-replacement rules (e.g. `ira`) compiled into the code, applied after targets are validated to keep an account's exposure within what it may hold.
_Avoid_: the removed config section formerly called `replacement`

**Run record**:
The per-run investigation data written to `data/runs/`, kept for 30 days.
_Avoid_: log entry, audit trail
