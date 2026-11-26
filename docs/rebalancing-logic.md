# Rebalancing Logic

This is a standalone description of the rebalancing logic for the Zehnlabs
Rebalancer (system version 3.0.0). It covers the pure planning rules, the
account run, and the retry rules. It describes current behavior; ADR 0010
records the whole-share optimizer decision. Historical refactor documents
describe the earlier 3.0.0 implementation and are not current planner rules.

IBKR is a whole-share broker. Its planner uses PuLP 4.3.1 with the in-process
HiGHS adapter and highspy 1.16.0 to choose the closest broker-reachable portfolio.
Existing fractional residues are immutable; orders change only whole shares.
A fractional-capable broker instead uses the unchanged six-decimal, round-down
planner and never invokes the solver. The whole-share model represents money as
integer $0.0001 ticks.

## 1. Targets and replacements

- Raw allocation weights come from the strategy's allocations API. Each symbol
  is normalised (`strip().upper()`). The run fails if a symbol or weight is
  missing/invalid, a symbol repeats, or (for a non-empty list) the weights total
  outside 99%–101%.
- The weights are scaled so they sum to exactly 100%. An **empty** list means
  "hold nothing": every position is sold and nothing is bought.
- Weight-0 entries are allowed and mean "sell that symbol if held".
- If an account has a replacement set (e.g. `ira`), the replacement algorithm
  runs **after** the 100% sum check, so the check never runs on the final,
  replaced weights.

## 2. Prices

- **Mark** (values holdings and the total): mid = (bid + ask) / 2 when both
  exist and ask ≥ bid; else last; else close; else bar close.
- **Buy reference** (sizes buys and the limit): ask; else last; else close;
  else bar close.
- **Sell reference** (initial cash feasibility and preview proceeds): bid; else
  last; else close; else bar.
- **Limit price** = buy reference × (1 + `buy_slippage_percent` / 100), rounded
  half-up to 2 decimals when ≥ 1.00, else to 4 decimals.
- If a needed price is missing, the run fails with a pricing error.

## 3. The account run

For each account, one run (`rebalance_account`) performs these steps:

1. Create a `run_id` and bind it to the logger.
2. Fetch raw allocations for the account's strategy.
3. Validate and scale the weights; apply replacements if configured.
4. Live only: if the market is closed, return `MARKET_CLOSED` (no connection is
   made). Preview runs ignore market hours.
5. Open the broker connection.
6. Live only: cancel the account's own working orders that were left by this
   app (with a confirmation timeout).
7. Read the working orders. Live: if any **foreign** working order exists, the
   run stops and lists them. Preview: they are shown as an info note.
8. Read the account state (cash, NetLiquidation, positions). Missing cash or
   NetLiquidation is an error — never treated as zero.
9. Fetch quotes and compute marks.
10. Compute `V0`, fix the dollar reserve from it, and optimize the initial
    portfolio. Only its sell orders become the sell plan. Cross-check `V0`
    (our computed value) against IBKR's
    NetLiquidation; a mismatch over 2% fails a live run (preview warns).
11. Log the sell plan. **Preview**: simulate those sells at their sell
    references, run the buy-only optimizer from the simulated snapshot, and
    return the final sell and buy plans. Stop here without placing orders.
12. If there are sells, check the market is open, place all sells (market), and
    wait for them.
13. If any sell filled, poll the reported cash until it reflects the proceeds
    (up to `cash_update_timeout_seconds`). `cash_base` is the lower of the
    reported cash and `state0.cash + fills` — it can never overstate cash.
14. Compute `positions_after` as `state0` positions **minus the filled sell
    quantities** (never re-read from the broker).
15. Fetch fresh quotes for the buy leg.
16. Run the buy-only optimizer from actual conservative cash, remaining
    positions, fresh quotes, and the original reserve; log the final buy plan.
17. If there are buys, check the market is open, place all buys (limit), and
    wait for them.
18. Return success with the executed orders, end estimates, and the run record.

The run never raises: every failure becomes a failed result describing what
already happened (which orders filled). A planning failure is
`OPTIMIZATION_FAILED`; a missing model price remains `PRICING_FAILED`. A live
buy optimization can fail after sells completed, in which case those sell
executions remain in the result and no buys are placed. On failure the run does
**not** cancel its own orders — the next run cancels them. The connection is
always closed.

### Selling

The fixed reserve is `minimum_cash_reserve_percent / 100 × V0`. Initial strategy
capital is `max(0, V0 − reserve)`, and each target dollar value is its weight
times that capital. The whole-share model considers all target and held symbols
together. It chooses final whole shares, buys, and sells so projected cash—cash
plus sells at sell references minus provisional buys at buy limits—stays at or
above the reserve.

The initial model may sell a target that looks acceptable in isolation when the
resulting cash permits a closer portfolio elsewhere. A held non-target is driven
to zero broker-tradable shares, leaving only an immutable fractional residue.
Only the model's sell orders are returned and submitted; its provisional buys
exist solely to make the global sell decision cash-aware.

### Buying

- Reuse the dollar `reserve` computed from `V0`; never recompute it from `V1`.
  Post-sell strategy capital is `max(0, V1 − reserve)`, where `V1` uses
  conservative `cash_base`, remaining holdings, and fresh marks.
- Sells are prohibited. Buy cost at limit prices must not exceed
  `max(0, cash_base − reserve)`. If cash is at or below reserve, return no buys
  with the existing warning and no trimmed symbols.
- For every symbol, measure drift with the mark—not the limit or sell reference—
  and choose the broker-reachable final quantity. Tight bounds stop at the first
  whole-share lattice point at or above the target after any immutable residue.
- Emit buy orders in symbol order. A symbol is `trimmed` only when its optimized
  final quantity is below the independently closest buy-only quantity it could
  reach without the global cash constraint. Warnings name every and only those
  final trimmed symbols.

### Whole-share objective and solver policy

For each symbol, `final_whole = held_whole + buy − sell`; buys, sells, and final
shares are nonnegative integers, sells cannot exceed held whole shares, and a
binary direction constraint prevents buying and selling the same symbol. Two
linear inequalities make each error variable the absolute difference between
marked final value and target dollars. Final-share and trade bounds come from
holdings and the first useful lattice point, not an arbitrary big-M.

The optimizer uses sequential lexicographic solves:

1. Minimize total absolute dollar drift across all modeled symbols.
2. With that optimum fixed, minimize each symbol's drift by target weight
   descending, then symbol ascending. Held non-targets have weight zero.
3. With all drift optima fixed, minimize each final share quantity in the same
   order. This chooses the lower quantity for an exact half-share tie.

Priority never permits greater total drift. Every stage must return PuLP
`Optimal`, a solution, and native HiGHS `kOptimal`. The time limit for each
stage is `trading.optimization_timeout_seconds` (default `10.0`, valid
`0.1`–`60.0`). Time, node, gap, numerical, memory, interrupt, and other limits
are failures even if HiGHS has an incumbent. Direct postconditions recheck
integrality, holdings, direction, cash, and safe numeric bounds before orders
are created.

### Fractional-capable brokers

When `supports_fractional=True`, `plan_sells` and `plan_buys` keep the existing
six-decimal round-down, affordability, warning, and deterministic priority
behavior. PuLP and HiGHS are not invoked for this branch.

## 4. Retry rules

| Source | Mode | Outcome | Queue change | Notification |
|---|---|---|---|---|
| signal / manual | live | succeeded | remove the ID if present | success |
| signal / manual | live | failed | add the ID | will retry |
| signal / manual | live | market closed | add the ID | queued (market closed) |
| scheduled | live | succeeded | removed by the job | success |
| scheduled | live | failed / market closed | never re-added | not retried |
| any | preview | previewed | none | preview |
| any | preview | failed | none | preview failed |

- A live run that fails for any reason — including a foreign working order or an
  invalid-targets error — is retried **once** at the next market open with fresh
  targets and fresh account data.
- A **scheduled** run that fails is not retried.
- If the scheduler is disabled, failed live runs are not queued.
- The next market-open time is `scheduler.market_open_time` (default 09:35 ET).

## 5. Safety invariants

- A sell never exceeds the held quantity; a position never goes negative.
- Every emitted share quantity is > 0 and respects broker granularity.
- A whole-share symbol is never both bought and sold in one model.
- Initial projected cash stays at or above the fixed reserve; post-sell buy
  cost does not exceed `max(0, cash_base − reserve)`.
- Whole-share price, cash, quantity-product, and objective bounds stay within
  the exact integer-money safety limit before solving.
- The buy plan contains no sells and the sell plan no buys.
- Identical inputs give an identical plan (deterministic symbol order).
