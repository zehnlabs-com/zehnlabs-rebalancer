# Rebalancing Logic

This is a standalone description of the rebalancing logic for the Zehnlabs
Rebalancer (system version 3.0.0). It covers the pure planning rules, the
account run, and the retry rules. The authoritative specification is the PRD
(`docs/refactor-prd.md`, §7, §8.1, §10.5); this document is a plain-language
summary of that behaviour with no history.

All quantities are whole shares for a whole-share broker (IBKR is one), or
multiples of `0.000001` for a fractional broker. Prices are `float`. Everywhere
a tolerance is needed an epsilon of `1e-9` is used. Share quantities are always
rounded **down** to the broker's granularity — never up.

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
- **Sell reference** (preview estimates): bid; else last; else close; else bar.
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
10. Compute the sell plan. Cross-check `V0` (our computed value) against IBKR's
    NetLiquidation; a mismatch over 2% fails a live run (preview warns).
11. Compute the cash reserve and log the plan. **Preview**: simulate the sells,
    run the buy plan, and return a preview. Stop here.
12. If there are sells, check the market is open, place all sells (market), and
    wait for them.
13. If any sell filled, poll the reported cash until it reflects the proceeds
    (up to `cash_update_timeout_seconds`). `cash_base` is the lower of the
    reported cash and `state0.cash + fills` — it can never overstate cash.
14. Compute `positions_after` as `state0` positions **minus the filled sell
    quantities** (never re-read from the broker).
15. Fetch fresh quotes for the buy leg.
16. Compute the buy plan (below) and log it.
17. If there are buys, check the market is open, place all buys (limit), and
    wait for them.
18. Return success with the executed orders, end estimates, and the run record.

The run never raises: every failure becomes a failed result describing what
already happened (which orders filled). On failure the run does **not** cancel
its own orders — the next run cancels them. The connection is always closed.

### Selling

For each position not in the targets, sell all quantity supported by the broker
(an IBKR fractional leftover is reported as a note to be sold manually). For a
target that is over its weight, sell the excess shares. A sell never exceeds the
held quantity.

### Buying

- `reserve` = `minimum_cash_reserve_percent` / 100 × `V0` (the start-of-run
  value), computed once. `budget` = `cash_base − reserve`.
- For each target in symbol order: `gap` = weight × `V1 − held_value`. If `gap`
  is at least one broker-sized unit, plan a buy of
  `round_quantity(gap / buy_reference)` shares at the limit price.
- If `budget ≤ 0`, no buys are placed (a warning is added).
- Total cost is the sum of `quantity × limit` per buy. If the raw cost exceeds
  `budget`, start with the full budget and fund raw buys by target weight
  descending, using reverse-alphabetical symbol priority for equal weights.
  Each final quantity is the smaller of its raw quantity and what the remaining
  budget affords at the limit price, rounded down with `round_quantity`.
- Emit funded orders in normal symbol order. Derive the warning and the run
  record's trimmed-symbol list from raw versus final quantities; fractional
  reductions show up to six decimals.
- Recompute the final limit-price cost and fail closed with an explicit runtime
  check if it exceeds `budget + eps`.
- The result is maximal under this target-weight priority: for every underfunded
  raw buy, the final remainder cannot buy one more broker-sized unit. It is not
  a global portfolio optimizer.

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
- Every final buy quantity is no greater than its raw desired quantity.
- Final limit-price cost is at most `budget + 1e-9`; buys never spend the
  configured reserve.
- No underfunded raw buy can take one more broker-sized unit from the final
  remainder.
- The buy plan contains no sells and the sell plan no buys.
- Identical inputs give an identical plan (deterministic symbol order).
