# Rebalancing Engine — Refactor Assessment

**Status:** discussion document. No code has been changed.
**Scope:** the rebalancing algorithm and the surrounding service architecture.
**Inputs reviewed:** `docs/rebalancing-logic.md` (1,214 lines), `packages/rebalance-calculator`,
`packages/ibkr-connector`, `packages/broker-connector-base`, `packages/app-config`,
`event-broker/app/**`, `config.yaml`, `accounts.yaml`, `replacement-sets.yaml`, plus the installed
`ib_async` 2.0.1 in `.venv`, IBKR's TWS API documentation, and FINRA Rule 4210 as amended.

---

## 0. Verdict

You are thinking in the right direction. Almost every item on your list is a real simplification, not
just a preference — and in several cases I could reproduce the concrete defect the simplification
removes. Two items need adjusting before you act on them:

1. **Don't add `cancel_all_orders` at all.** The only cancellation in the codebase is the
   per-account loop at the start of each run (`rebalancer.py:62`), and nothing needs anything else —
   no code path cancels mid-run. The IBKR research does establish that `reqGlobalCancel` is
   gateway-wide and must not be used here, but the conclusion is "add nothing", not "add a safe
   variant".

2. **Delete the PDT service outright** — *corrected in review.* The rule is genuinely gone (FINRA
   deleted the day-trading provisions; effective June 4, 2026), and my first pass argued the service
   should survive as an execution ledger because retry needs "when did this account last run". That
   was wrong: a retry queue carries its own due time and attempt count, and observability belongs in
   logs and run records. Nothing reads those state files once the guard is gone. Delete it all.

The single biggest structural problem is not on your list: **planning, cash arithmetic, and execution
are interleaved across a 340-line calculator and a 457-line rebalancer, and the calculator's
`phase` parameter exists only so that one code path can compute buy sizes that are then thrown
away.** Removing `phase` deletes more complexity than any single item below, and it eliminates the
preview-vs-live divergence I reproduced (§5.2).

Recommended shape of the end state, in one sentence: **a pure planning function that turns (targets,
snapshot, quotes, spendable cash) into a list of orders and never scales anything; a thin executor
that submits, waits, records; and a broker interface that reports what it actually did.**

---

## 1. What the system does today

Accurate summary, because the assessment depends on it:

- `AblyEventSubscriber` loads `accounts.yaml`, filters by `type` (live/paper, from `TRADING_MODE`) and
  `enabled`, and groups accounts by `strategy_name`.
- An Ably event (or the market-open scheduler, or a manual file drop) calls
  `StrategyExecutor.execute_strategy(strategy_name, accounts, event_data)`.
- That runs `execute_strategy_batch` in a `ProcessPoolExecutor` worker, which runs all accounts of the
  strategy concurrently with `asyncio.gather`, each account getting its own `IBKRClient` whose
  `clientId` is the account's digits.
- Per account: fetch allocations from the strategy API → apply replacement set → snapshot →
  quotes → `TradeCalculator.calculate_trades(phase='all')` → cancel open orders → submit sells →
  wait → re-snapshot → `calculate_trades(phase='buy')` → affordability filter → submit buys → wait →
  final snapshot.
- Results bubble up as dicts, are turned into `AccountExecutionResult`s, and the parent sends one
  ntfy push per account (plus a second push if there are warnings).

That shape is sound. The two-phase protocol (sells first, re-snapshot, then buys) is the right
design and should survive the refactor unchanged — it is the one piece of complexity that earns its
keep. What follows is about the layers inside it.

---

## 2. Your items, assessed

| # | Item | Verdict |
|---|---|---|
| 1 | Remove commission calculation | **Agree.** Verified it idles 2.5% of a $10k account |
| 2 | Remove `cash_reserve_percent` | **Agree.** Verified it creates a denominator split that hides drift |
| 3 | Remove PDT classification and guard | **Agree on the rule, refine the deletion** — keep the ledger |
| 4 | `IsFractionalSupported` capability flag | **Agree.** IBKR=false is correct for the TWS API |
| 5 | Remove scaling and max-utilization; drop orders instead | **Agree.** Verified scaling overrides the strategy's own cash target |
| 6 | Replacement sets are a system artifact | **Agree**, and it fixes a compliance hole, not just a design smell |
| 7 | Replacement logic has a bug / a hole | **Confirmed — three of them**, reproduced |
| 8 | Add `cancel_all_orders` | **Don't add it.** The existing per-account loop covers every case; `reqGlobalCancel` must not be used |
| 9 | Use IBKR's cash field | **Agree** — `CashBalance` only, validated; no `AvailableFunds` |
| 10 | Requeue critical failures | **Agree**, but it is a feature with no host today — needs a typed failure reason and a close-time job |
| 11 | Combine notifications per round | **Agree.** Today an 11-account round can send 22 pushes |
| 12 | Enough logging to investigate | **Agree**, and log lines are not enough — you need a per-run record artifact |

### 2.1 Remove commission calculation — agree

`commission_rate: 0.01` is treated as a divisor on *all* spendable cash. On a $10,000 account with a
$100 minimum reserve and a single 100%-weight target, the planner buys 97 shares at $100.50 =
**$9,748.50 and leaves $251.50 idle** (verified, §8.2). That is 2.5% permanent cash drag from a
number that does not describe IBKR's commission schedule: IBKR US equity commissions are roughly
$0.0035/share with a $1 order minimum — cents on this trade, not 1% of the account.

The modelling is also the wrong *shape*. Commission is per order and per share, not ad valorem on the
whole spendable balance, so a percentage divisor can never represent it correctly; and because it
divides *before* scaling, it compounds with rounding in ways that are hard to reason about.

**Recommendation:** delete `commission_rate`, `commission_divisor`, and both usages. Keep a single
absolute `minimum_cash_reserve_usd` that covers fees, subscriptions, and commission — exactly your
proposal — and plan against `CashBalance − minimum_cash_reserve` and nothing else (§2.9).

**Risk to cover:** without the 1% gross-up, a buy that fills above the ask can leave the last order a
few dollars short and get it rejected, which surfaces as a spurious 300-second timeout. The reserve is
the right place to cover it: set it to at least 0.5% of your largest expected order (§7.1).

### 2.2 Remove `cash_reserve_percent` — agree

Two reasons, one of them a bug I can point at.

**It is already dead for your live accounts.** All nine live accounts and `DU789012` have
`cash_reserve_percent: 0.0`; only the paper account `DUM959247` uses `1.0`. So the field is one
config line away from being removable with zero behavior change.

**It creates a two-denominator comparison.** Sizing targets are computed from
`net_liquidation × (1 − reserve)`, but the allocation threshold compares the strategy *weight*
against `current_value / net_liquidation`. Those are different bases. Verified consequence: with a
1% reserve, a position sitting exactly at its 50% target of net liquidation is **not traded at all**
(the difference is 0.0 points), even though sizing says it is $500 over target. So the reserve is
silently unenforceable for any position within the 0.5-point threshold of its weight — and with a
larger reserve the suppression grows. `docs/rebalancing-logic.md` §2.7.3 describes this mismatch but
gets the direction wrong: it predicts that the reserve *causes* trades at target, whereas the code
*suppresses* the drift the reserve creates. Either way, the doc and the code disagree, which is the
clearest possible argument for deleting the mechanism rather than documenting it.

**Recommendation:** delete the field from `AccountConfig`, `accounts.yaml`, the calculator, and doc
§2.6.1. `available_value` becomes the computed total (§7.2). If an account should hold cash, the strategy
says so with weights summing to less than 1 — which only works once item 5 is done, because today
scale-up would spend that cash anyway. **These two removals are coupled; do them together.**

### 2.3 PDT — agree on the facts, refine the deletion

**The rule is gone.** FINRA filed SR-FINRA-2025-017 to replace the day-trading margin provisions of
Rule 4210 with intraday margin standards. The SEC granted accelerated approval on April 14, 2026
(Release 34-105226), and FINRA Regulatory Notice 26-10 sets an **effective date of June 4, 2026**
with a phase-in period ending **October 20, 2027**. The rule text is literal about it: paragraph
(a)(8)(B) now reads "Reserved. [Day Trading]", and the definitions of "day trading" and "pattern day
trader", the $25,000 minimum equity, and the 90-day cash-only penalty are struck. So the
classification you guard against no longer exists; the guard cannot protect you from anything.

**It is also dead code today.** `pdt_protection_enabled: false` on every account in `accounts.yaml`.
The 151-line service, `PDTProtectionConfig`, `PDTCheckResult`, doc §2.13, and the notification
special-casing for "PDT Protection"/"Auto-scheduled" strings are all inert.

**Where it is written.** `event-broker/app/services/trading_executor.py:158-160`, inside
`_execute_live_rebalance`, immediately after a successful rebalance and *not* gated on
`pdt_protection_enabled`:

```python
pdt_service = PDTProtectionService(logger=logger)
pdt_service.record_execution(account_config.account_id)     # always, even when the guard is off
```

That writes `data/last-executions/{account_id}.json` = `{last_executed, next_execution}` — the eleven
files currently sitting under `data/last-executions/`.

**Correction to my first pass.** I argued this file should survive as an execution ledger because the
retry feature needs "when did this account last run". That was wrong. A retry is a queue entry
carrying its own `due_at` and `attempt_count`; it does not need the previous round's history. And
observability belongs in logs and the run record (§2.12), not in a state file whose only reader was
the PDT check. After the guard is deleted, nothing reads these files.

**Recommendation:** delete the service, its config block, `PDTCheckResult`, the
`pdt_protection_enabled` field, the `last-executions` files, and doc §2.13.

The one thing worth considering separately: if you want a clean refusal when a run is already in
flight for an account, that is an in-flight marker, not a history record — and it is concurrency code,
so it should be a deliberate decision rather than a leftover from a deleted rule. Today there is no
protection at all: a second session with the same `clientId` is refused by IBKR with error 326 (§5.9),
which `ib_async` turns into a stall until the connect timeout.

One residual consideration: the new intraday margin standards are not "nothing". They require
same-day intraday margin deficits to be satisfied promptly in margin accounts. That is a weaker and
different constraint than PDT, but it is still a reason to default to one rebalance per account per
session rather than to remove rate limiting entirely. Since your accounts are IRAs (cash accounts)
and the individual accounts already have the flag off, this is a policy default, not a code path.

### 2.4 `IsFractionalSupported` — agree, and IBKR=false is right for this API

IBKR's own API documentation states that the APIs "do not support fractional or cash quantity
trading with exception to Cryptocurrencies and Forex trading", and fractional *equity* orders placed
through the TWS API fail with error 10242 ("Fractional-sized order cannot be modified via API").
Fractional equities are supported in the TWS/app UI and via the Client Portal API's `cashQty`, not
through the TWS API your connector uses. `ib_async` exposes `order.cashQty`, but that is the crypto/FX
path, not a fractional-equity path. So **defaulting the capability to `false` and setting it `false`
for IBKR is correct** — and it matches what the code already assumes (whole-share `int` quantities,
`int(-current_shares)` truncation).

Design notes that matter more than the flag itself:

- **Put it on the interface as a property, driven by config**, not hardcoded in the connector. IBKR is
  actively extending fractional support (CPAPI is the official path today), so this becomes a config
  flip rather than a code change.
- **Never round a sell up.** "Drop the fractional portion" means `floor` in both directions. A sell of
  `12.7` becomes `12`; a buy of `0.6` shares becomes no order. Rounding a sell up would ask the broker
  to sell shares you do not hold.
- **The sell residual is permanent, and the notification should say so once per account, not once per
  run.** A `0.7`-share remainder can never be closed through this API. Today the code warns only when
  the whole position is below one share; a `12.7` holding silently leaves `0.7` behind with no
  warning at all (doc §2.7.1 acknowledges this). Recommend a per-account "needs manual action" list
  that persists, so it is reviewed rather than re-alerted.
- **Buys below one share will be common and should not look like an error.** A 0.5% target on a $20k
  account is $100; a $400 ETF is 0 shares. Aggregate these into one line ("4 targets under one share,
  $310 unallocated") rather than four warnings.
- **Keep `quantity` as `int` in the order model.** If you make it a float "for fractional later", you
  will eventually send 0.9999 shares to a broker that truncates differently than you do. Model the
  rounding policy explicitly in the planner and keep the wire type integral.

### 2.5 Remove scaling and max-utilization; drop orders instead — agree

This is the most consequential item, and the verification supports it.

**Correction: my first example was invalid for your inputs.** I demonstrated scale-up with weights
summing to 0.90 — a strategy holding 10% cash — which this system does not produce, because the
allocation set is normalised to 1.0. With weights summing to 1.0 there is no surplus cash for the
engine to deploy, and `sum(deficits) = cash`, so the factor comes out near 1.0 by construction. The
measured value on a fully-invested $100k account was **0.9906**: a 397-share buy trimmed to 393
(§8.10). That is what the scaling factor, the adjustment pass and the max-utilization cap actually
accomplish in the normal case — a 1% trim.

So the case for deleting them is **not** that they wreck the portfolio. It is that ~200 lines, three
config knobs and a documented list of edge cases (negative factor, one-share floor, residual overage,
surplus deployment) exist to perform a small trim, and the trim is still needed after they are gone
(§7.1). Delete the machinery, keep the correction, state it in one rule.

**Where scale-up does fire: accounts with no replacement set.** `ReplacementService` — and therefore
normalisation — only runs when `account.replacement_set` is set (`rebalancer.py:116`). Two accounts in
`accounts.yaml`, `U21243161` and `U24591836`, have no replacement set, so their weights are used
exactly as the allocations API returns them. If the API ever returns weights summing to 0.98 for one
of those, the engine holds 2% of the account as surplus cash and *does* scale buys up by ~1.02, ending
fully invested at weights ~2% above the strategy — my original illustration, confined to the
unnormalised path. That is an argument for moving normalisation out of the replacement service and
onto the allocation input itself, where it applies to every account (§5.14), not an argument for
keeping scale-up.

**Preview and live disagree.** With six targets, $300 cash, and $50 shares, the calculator returns six
one-share buys costing $301.50 against $198.02 available (verified, §8.4). The live path then drops
them in `_filter_buy_orders_by_cash`; the preview path (`calculate_rebalance`) never calls that
filter. So preview shows orders that will not be sent — which doc §2.14 explicitly forbids ("Preview
MUST use the same calculation path").

**Four mechanisms do one job.** A scaling factor, an adjustment pass, a per-order affordability
filter, and a max-utilization cap all constrain the same thing. Each has its own edge case
documented in §2.9 and §2.17 of the spec. One rule replaces all four.

**`max_account_utilization` is redundant *and* wrong.** Once scaling is gone, planned buys can only
approach the account's value when cash does — i.e. an all-cash account, where
spending it is the correct behavior. So the cap never binds on a legitimate plan, and when it does
bind it caps correct deployment. Delete it.

**Recommended rule, stated as the single invariant:**

> The sum of buy cost must not exceed spendable cash. If it does, reduce the lowest-weight orders
> share by share until it fits, dropping an order only once it is down to one share. Sells are never
> touched.

After the pricing fix in §7.1 the residual is share rounding plus the NLV gap — usually a share or
two — so "share by share from the bottom" leaves cents idle, and at a genuine cash shortage it empties
the smallest orders completely, which is the behaviour you asked for. The pricing fix is the part that
removes the systematic 1%; the loop only mops up integers.

Two details to pin down in implementation:

- **Order.** Sort buys by target weight descending and work from the end (lowest weight first). This
  is subtly different from today's greedy first-fit, which can fund one large buy and then skip two
  small ones that together would have fit.
- **One order that alone exceeds cash** (a single share of a very expensive instrument) is dropped.
  Warn; do not half-buy it, and do not silently retry.

### 2.6 Replacement sets as a system artifact — agree, and it closes a compliance hole

Current loading: hardcoded path `/app/replacement-sets.yaml`, parsed with raw dict keys, no schema, no
validation, exceptions logged and swallowed, and — the important one — **an unknown or missing set
name is a silent pass-through**:

```python
if not replacement_set_name or replacement_set_name not in self.replacement_sets:
    return allocations          # silently: no replacements
```

So if the file is missing, renamed, or a set name is misspelled, every IRA account in `accounts.yaml`
(`replacement_set: ira`) rebalances into the restricted instruments with a successful result and no
error. That is a compliance failure that looks like a healthy run. It is not a theoretical risk: the
whole point of the `ira` set is that those accounts are prohibited from holding VIXY/UVXY/DBC/UUP.

And a bad value is destructive, not just wrong. With a rule `UVXY → VXX, scale 9.0` (a plausible
typo) and 20% UVXY, 80% SPY, the output is **VXX 95.7% / SPY 4.3%** — and the run reports success,
with one log line at warning level (verified, §8.5).

**Recommendation:** define the sets in code as a typed registry in one module — e.g.
`REPLACEMENT_SETS: dict[str, tuple[ReplacementRule, ...]]` — validated at import: sources unique,
target ≠ source, `1.0 ≤ scale ≤ 3.0`, no rule whose excess exceeds the non-replaced total. Make an
unknown set name a **startup failure** (or at minimum a per-account hard error), never a pass-through.
`accounts.yaml` keeps `replacement_set: ira` and nothing else.

Trade-off to accept consciously: changing a set becomes a code review and a deploy rather than a file
edit. That is the point — it is an auditable change to what instruments an account may hold. At your
scale (one ops person, a handful of sets) a PR plus `./tools/reload.sh` is fine.

### 2.7 Replacement-set logic — confirmed bugs

Your instinct is right. Three holes, all reproduced against the actual `ReplacementService`:

**(a) Weight that already exists in the target symbol escapes the compression.** The service records
`replaced_symbols` as a set of *target* symbols, then treats every allocation whose symbol is in that
set as "replaced, keep as-is". If the strategy also allocates the target directly, that weight is
never compressed.

```
rule: UVXY -> VXX x2.0
in:   UVXY .20, VXX .10, SPY .70
out:  VXX .50, SPY .50                       <- service
      VXX .475, SPY .525                     <- consistent answer
```

The error grows with the scale factor and with the size of the direct weight. The root cause is that
"which symbols were replaced" is tracked as a set of symbol names instead of as a scalar total.

**(b) `scale < 1` is silently inverted by normalization.** Absorption only runs when
`total_excess > 0`; when a rule reduces weight, the total falls below 1 and the unconditional
normalization divides everything back up — including the replaced symbol — so a "hold half the
exposure" rule increases exposure:

```
rule: SVXY -> USMV x0.5
in:   SVXY .20, SPY .80
out:  USMV .1111, SPY .8889                  <- service (total forced to 1.0)
      USMV .10,   SPY .80, 10% cash          <- what a 0.5 scale means
```

No current rule uses a scale below 1.0, so this is latent — but it is a trap for the next person who
adds one, and the doc's §2.5.1 definition of `scale` invites it.

**(c) The "floor" path concentrates the portfolio and then hides it.** When substitutions demand more
weight than the non-replaced remainder provides, the service keeps
`minimal_non_replaced_allocation_percent × non_replaced_total` and accepts an over-allocation. Because
normalization then forces the total back to 1.0, the concentration looks correct downstream:

```
rule: UVXY -> VXX x9.0 ; in: UVXY .20, SPY .80
out:  VXX .9574, SPY .0426, total 1.0        <- sums to 1, so nothing notices
```

**Simplification that removes all three.** Consolidate first, then work with two scalars, and stop
special-casing symbols:

```
1. substitute:  for each allocation, if source in rules -> (target, w * scale)
2. consolidate: sum weights per symbol
3. totals:      replaced_total     = sum of weights that came from a rule
                non_replaced_total = total - replaced_total
4. if total > 1 and non_replaced_total > 0:
       factor = (non_replaced_total - (total - 1)) / non_replaced_total
       if factor < 0: FAIL (invalid set)      # never clamp into a 95%-one-symbol portfolio
       scale every non-replaced weight by factor
5. if |total - 1| > 1e-9:  FAIL with the numbers  # no silent normalization
```

This deletes `replaced_symbols`, the consolidation special case, `normalization_trigger_threshold`,
`normalization_failure_threshold`, `minimal_non_replaced_allocation_percent`, and roughly 60 lines.
It also turns (c) from "produce a concentrated portfolio" into "refuse to run and say why" — the right
failure mode for a data error in a compliance artifact.

Add: **log the resolved weights before and after substitution at INFO on every run that uses a
replacement set.** This is the only place the engine rewrites the strategy's intent, and today it is
invisible in production logs.

One decision this exposes (see §7): should `scale < 1` be supported at all? I recommend restricting
validation to `scale ≥ 1.0` and expressing any exposure *reduction* in the strategy's weights. That
removes the branch entirely and makes the arithmetic single-directional.

### 2.8 Cancellation — add nothing

**Where cancellation actually happens.** The only cancel in the system is `rebalancer.py:62` →
`_cancel_pending_orders(account_id)`, at the start of each account's run: it lists that account's open
orders and cancels each by id. `grep -rn cancel event-broker packages/*/src` returns exactly this one
call site. There is no mid-run cancel anywhere, and no path needs one — a run is per account, so it
only ever touches its own orders.

**So the answer to "where is that situation required" is: nowhere.** Don't add `cancel_all_orders` to
`IBrokerConnection`, and don't add a gateway-wide cancel to the round. The existing loop is already the
correct and safe implementation:

- It cannot touch another account's orders, so parallel accounts are safe.
- It clears orders left by an earlier container instance, because the `clientId` is derived from the
  account number and IBKR's cancel authority is the client id, not the session.
- It works for a single manual run, which has no "round" to hang a global cancel off.

**The IBKR research, for the record.** `reqGlobalCancel` is gateway-wide and unsuitable here
regardless: it "cancel[s] all open orders, regardless of how they were originally placed" (IBKR
*Cancelling Orders*), "including those placed by other clients or TWS/IB Gateway" (`ib_async` 2.0.1,
`ib.py:857`). Running it once at the top of a round would be safe only when nothing is in flight, which
means gating it against the manual-run path — a new failure mode, in exchange for doing what the
per-account loop already does.

**One consequence for the retry design.** A retry at the next round is safe because the run cancels the
account's own orders before it plans. If that cancel fails, the retry must not proceed — refuse and
alert, rather than risk competing with a live order. That is a precondition on the retry, not a reason
to add an interface method.

### 2.9 Use IBKR's cash field instead of our own math — half already done

`get_account_snapshot` already reads `CashBalance` and `SettledCash` (USD) off `accountValues`. So
"read the broker's cash" is not a change — the complex logic you are reacting to is the *arithmetic
applied to* that number: reserve, commission divisor, scaling. Items 1, 2, and 5 delete it.

What is worth changing is *which* numbers you read and what you do when they are missing:

- **`CashBalance` is validated as the right field for your case.** It is the cash ledger, and it
  updates on trade date, so sale proceeds are spendable immediately — which is exactly why the
  accounts are margin-enabled without using margin (in a cash account you would wait for T+1).
  `SettledCash` lags and must not gate buying. `AvailableFunds` is equity-with-loan-value minus
  initial margin, which in a non-leveraged margin account is *larger* than cash, so taking the
  smaller of the two would always return cash and add nothing; it would only bind near a margin call
  or with working orders reserving buying power, and the round cancels orders first.
  **Conclusion: `CashBalance - minimum_cash_reserve` is sufficient. Drop the `AvailableFunds`
  suggestion I made earlier — it was defensive advice for a case you do not run.** The one property
  that must hold is that cash never goes negative, because that would be borrowing; the cash check is
  the load-bearing invariant, and that is the argument for §7.1.
- **Do not use `TotalCashBalance`** — it aggregates segments and currencies in the base currency. For
  a USD account, the USD `CashBalance` the code reads today is the right field.
- **Model the balances as one typed object** (`AccountState { cash, settled_cash, positions }`) rather
  than four floats on a pydantic model. `settled_cash` is read today and never used; keep it only if
  the run record reports it (useful when diagnosing why a buy was rejected).

The zero-total failure mode that lived in the same function is covered in §5.5 — and it disappears
from sizing entirely once the total is computed rather than read (§7.2).

### 2.10 Requeue critical failures — agree, but this is a feature, not a cleanup

The intent is right, and the design constraint you named (retry at close for a morning failure, retry
at open for a close failure) is sound. Four things stand between you and it today:

1. **There is no host for it.** `_process_scheduled_accounts` finishes with an unconditional
   `_clear_scheduled_file()`, so failed accounts are forgotten the moment the round ends. And there is
   only one scheduled job — market open. "Retry at market close" has nothing to run on. You need a
   second cron entry (a close-time job) or the retry is only ever "tomorrow at open".
2. **Failures are stringly typed.** The child raises `Exception("Orders failed: ...")` or
   `Exception("Order execution timeout after N seconds")` and the parent only sees `error: str`.
   Deciding "retryable or not" from that string is exactly the kind of parsing that breaks silently.
   The result model needs a typed reason: `SELL_LEG_INCOMPLETE | BUY_LEG_INCOMPLETE | PARTIAL_FILL |
   TIMEOUT | NO_QUOTES | CONFIG_INVALID`, plus `retryable: bool`.
3. **The retry queue must not be the file the scheduler clears wholesale.** `add_account_to_schedule`
   (used by the PDT path, and the obvious thing to reuse) writes to `scheduled.json`, which
   `_process_scheduled_accounts` then overwrites with `[]`. A retry queued during a round can be
   erased by the same round. Use a separate `retry-queue.json` keyed by account with
   `{next_attempt_at, attempts, reason}`, written by an idempotent function.
4. **A retry after a timeout is only safe if the previous run's orders are gone.** Your two-phase run
   starts by cancelling open orders, which is what makes a retry a clean fresh run — the spec relies
   on this (§2.12). Make it explicit: if the account's orders cannot be confirmed gone, do not retry;
   alert instead. Otherwise a retry can double-fill.

Also add a **maximum attempt count** (2 is enough: the original plus one retry per side). A
permanently broken symbol — one that fails contract qualification, or has no quote at any hour —
would otherwise generate an infinite open/close retry loop that looks like activity.

Finally: "the user should be notified if retry queued" is better served by the round summary (item 11)
than by a separate push. A queue event is not urgent; the round report can say "2 accounts retrying at
the close".

### 2.11 Combine notifications at end of round — agree

Today: one success-or-failure push per account, plus a second push for warnings per account
(`StrategyExecutor._send_account_notifications`). An 11-account round is 11–22 pushes; a
warning-producing account sends a "Success" push and a "Warnings" push that say nearly the same thing.
And warnings that cannot be resolved (fractional residue; a target permanently below one share) repeat
identically on every run, so the noise is permanent.

**Recommendation:** build a `RoundReport` and send one message per round per channel:

- header: round time, accounts attempted / succeeded / failed / retried
- per account: one line — masked id, strategy, trades, notional, end cash, and an outcome word
- a "needs attention" section: failed accounts with reason, retries queued and when, standing manual
  items (fractional residue), and unaffordable targets with the shortfall
- totals: sells, buys, dropped buys, cash before/after

Then keep one exception: **escalate immediately, outside the round summary, for loss-of-control
events** — sells completed but buys failed, partial fills, or a timeout where orders may still be
live. A portfolio that is half-liquidated should not wait for the round to finish before anyone is
told. This distinction (routine summary vs. urgent event) is worth modelling explicitly, because
otherwise either everything is one message (too slow for the urgent case) or nothing is (too noisy).

Note the ownership change this implies: notifications are currently assembled in the parent per
*strategy*, while a "round" spans strategies that ran in parallel. With multiple strategies, you want
to aggregate after the `asyncio.gather`, once, not per strategy. That also means the parent needs the
per-account details it currently throws away.

### 2.12 Enough logging to investigate — agree, with a stronger form

The logs are verbose but they are not *investigable*. Concretely, today, if an account ends up in the
wrong shape, you cannot answer "what did it see, what did it decide, and why" from the output, because:
stdout from parallel subprocesses is interleaved; there is no run or attempt identifier; the plan is
logged as human-readable text, not as data; the quotes actually used and their ages are not recorded;
and nothing records *why* an order was suppressed (threshold vs. phase vs. cash vs. below one share).

**Recommendation: a run record, not more log lines.** One JSON document per account per attempt, at
`data/runs/{account_id}/{started_at}.json`:

```
{ run_id, account_id, strategy, attempt, started_at, finished_at,
  inputs:   { cash, minimum_cash_reserve, computed_total,
              targets_before_replacement, targets_after_replacement,
              quotes: [{symbol, bid, ask, age_s, synthetic: bool}] },
  decisions:[{symbol, target_weight, current_weight, target_value, current_value,
              decision: BUY|SELL|SUPPRESSED|DROPPED|BELOW_ONE_SHARE, reason, shortfall}],
  orders:   [{order_id, symbol, side, requested_qty, order_type, limit, submitted_at,
              statuses: [...]}],
  snapshot_final: {...},
  outcome:  { status: COMPLETED|DRIFT|FAILED, failure_kind, retryable, warnings } }
```

That artifact answers the only questions that matter six months later: what did the engine see, what
did it choose, what did the broker do, and what was the difference between the last two. It is also
trivially testable (assert on the record, not on log text) and it is what a regulator or an unhappy
subscriber would ask for.

Keep structured logging alongside it (account, attempt, order id on every line) but treat the record
as the unit of truth.

---

## 3. The interfaces

Your instinct is right: DI plus narrow interfaces is the correct direction here, and the reason is not
testability-by-dogma — it is that the *planner* cannot currently be exercised without an IB connection,
a config file, an HTTP endpoint, and a real clock. That is why there is exactly one test file and it
covers startup security.

My verdict on the six, in order of value:

| Interface | Verdict |
|---|---|
| `IBrokerConnection` | **Yes** — already exists as `BrokerClient`; it is the highest-value seam and needs reshaping, not adding |
| `IQuoteCache` | **Yes** — removes a real smell (cache policy leaked into the broker interface) |
| `IAccountList` | **Yes**, as a repository with a typed model |
| `INotification` | **Yes**, but split transport from composition |
| `IConfig` | **No ABC.** Inject the typed `AppConfig` object; delete `get_config()` from constructors |
| `ILog` | **No ABC.** Python's `logging.Logger` is already the interface; the missing thing is the run record |

A note on naming, once: `I`-prefixed interfaces are a C# convention. In Python the idiomatic and
equivalent tool is a `typing.Protocol` plus constructor injection — same seam, less ceremony, and
it lets `IBKRClient` satisfy the protocol without inheriting from anything. Use whichever you prefer;
just be aware that `ABC` + `I` prefix is doing nothing for you that the language does not already do.

### 3.1 `IBrokerConnection` — reshape, don't just extend

Today's `BrokerClient` mixes concerns and leaks policy. Suggested contract:

```python
class BrokerConnection(Protocol):
    supports_fractional: bool          # per broker/account, config-driven

    async def connect(self) -> None                    # raise on failure, not bool
    async def close(self) -> None
    async def account(self, account_id: str) -> AccountState
    async def quotes(self, symbols: Sequence[str]) -> Mapping[str, Quote]
    async def place(self, request: OrderRequest) -> OrderAck
    async def order(self, order_id: str) -> OrderState        # incl. filled_qty, avg_price
    async def open_orders(self, account_id: str) -> Sequence[OpenOrder]
    async def cancel(self, order_id: str) -> None      # per-order only; no gateway-wide cancel
```

Specific changes and why:

- **Delete `use_cached_prices` / `use_cache`.** Today the caller has to know that the broker has a
  cache, and a `bool` is not a freshness policy. Move caching in front of the interface
  (`IQuoteCache`) and let the caller state `max_age`. This also removes the documented trap where
  "cash is fresh but quotes may be 30s old" (§2.10.4) — with an explicit `max_age` the caller decides,
  and the run record records what was actually used.
- **`connect() -> bool` becomes `connect() -> None` raising.** A bool that means "don't raise for
  routine failure" is a trap: one forgotten `if not connected` and you proceed with no session.
- **`order()` must return a normalized status, not a broker string.** `get_order_status` returns the
  raw TWS string and the wait loop interprets it with two hand-written lists (§5.3). Map it in the
  connector, as doc §2.2.4 already requires. Fills are deliberately *not* part of this contract: they
  are readable from the broker when troubleshooting (§5.4), so the per-poll call stays cheap.
- **Normalize statuses in the connector**, as doc §2.2.4 already specifies. Today
  `_wait_for_orders_complete` has its own `TERMINAL_STATES`/`FAILED_STATES` lists and a hole where an
  empty status counts as success (§5.3).
- **Drop `is_connected`** unless you need it for health checks; if you keep it, it should not be the
  thing the executor gates on (the executor already handles `connect()` failure).

### 3.2 `IQuoteCache` — yes

```python
class QuoteSource(Protocol):
    def get(self, symbols: Sequence[str], max_age: timedelta) -> Mapping[str, Quote]
```

Implementation: a decorator around `BrokerConnection.quotes` holding `symbol → (quote, fetched_at)`.
Inject the clock. Benefits that are immediate, not theoretical: the planner can be tested with canned
quotes; synthetic-price substitution becomes a property of the source (and is flagged on the `Quote`
itself as `synthetic: bool`, so it reaches the run record and the warnings without a separate
channel); and "re-acquire quotes before the buy leg" becomes a one-line policy change rather than a
code change.

### 3.3 `IAccountList` — yes

Today `accounts.yaml` is parsed inside `AblyEventSubscriber._load_accounts`, filtered by
`type`/`enabled`, and re-exposed as `strategies: dict[str, list[dict]]`; the scheduler reaches the same
data through a closure (`accounts_lookup`) that walks that dict; and the manual-event path does the
same. Three readers, one loader, no validation beyond presence.

Extract an `AccountRepository` with `all()`, `get(account_id)`, `for_strategy(name)`, returning typed
`AccountConfig` objects, and validate on load: unique account ids, known strategy name, known
replacement set, `minimum_cash_reserve ≥ 0`. Fail at startup, not at 09:30.

An interface (Protocol) rather than a concrete class is justified here because you already plan to
move account/subscriber data behind an API — at which point the repository becomes remote and the
callers should not notice.

### 3.4 `INotification` — yes, but split it

Two responsibilities are tangled today: composing a message (string formatting, strategy-slug
beautification, account masking) and delivering it (ntfy HTTP). Split:

- `RoundReport` — a data object (what happened).
- `Notifier.send(report)` — composes and delivers; one implementation per channel (`NtfyNotifier`,
  later email/SMS).

That makes "one message per round" (§2.11) a property of the report, not a change to the transport,
and lets you test composition without HTTP.

### 3.5 `IConfig` — skip the ABC

`get_config()` is called in 13 places, including inside constructors (`TradeCalculator.__init__`,
`IBKRClient.__init__`, `IBKRRebalancer.__init__`, both services). That is the actual problem: a module
singleton that (a) hides a dependency, (b) makes construction fail if no config file was loaded, and
(c) makes the calculator untestable without touching disk.

Fix: pass `AppConfig` into constructors. That is dependency injection with no interface, no container,
and no new dependency. There will never be a second `IConfig` implementation — a typed pydantic model
is already the contract.

(Related: the Dockerfile comments "gcc for dependency-injector" and mentions a DI library that is not
in `requirements.txt` and not used anywhere. If you were considering `dependency-injector`, don't:
constructor injection covers everything here, and a DI framework in a seven-service codebase is a
second configuration system.)

### 3.6 `ILog` — skip the ABC, add the artifact

`logging.Logger` is already an interface with interchangeable handlers. Wrapping it in `ILog` adds a
layer and buys nothing. What is missing is not an abstraction over `logger.info` — it is a record of
decisions (§2.12). Spend the effort there.

### 3.7 The interface you did not list, and need most

```python
class TradePlanner(Protocol):
    def plan(self, account: AccountParams, targets: Sequence[TargetWeight],
             state: AccountState, quotes: Mapping[str, Quote]) -> Plan
```

**Pure.** No I/O, no globals, no clock, no logging side effects. Given the same four inputs it returns
the same plan, including ordering. That one constraint is what makes the entire algorithm reviewable,
testable, and safe to change — and it is what lets `phase` disappear:

- a live run calls `plan()` before the sells (use only the sells), then again after (use only the buys);
- preview calls `plan()` once, simulates the sell fills at bid, adds the proceeds to cash, and plans
  again — which is a *better* preview than today's, because it no longer shows pre-sale sizing.

Executor, separately: submit, wait, escalate, record. No arithmetic.

---

## 4. The simplified algorithm

The whole engine, after the changes above, is about this much logic:

```
run(account):
    record = start_run(account)

    targets = resolve_targets(strategy(account), replacement_set(account))   # validated at startup
    state   = broker.account(account)
    quotes  = quote_source.get(state.symbols | targets.symbols, max_age=...)

    sells = plan_sells(state, targets, quotes)            # non-target positions, floor to whole shares
    record(plan)
    broker.cancel_open_orders(account)                    # this account's orders only
    submit_and_wait(sells)                                # abort -> retryable failure

    state2  = broker.account(account)                     # fresh cash only
    spendable = min(state2.cash - reserve, state2.available_funds)
    buys    = plan_buys(state2, targets, quotes, spendable)
    buys    = drop_lowest_weight_until_affordable(buys, spendable)
    record(plan)
    submit_and_wait(buys)

    finish(record, broker.account(account))


plan_buys(state, targets, quotes, spendable):
    for t in targets:
        current = held(t.symbol) * mid(quotes[t.symbol])
        total   = state.cash + sum(q_i * mid_i)              # computed, never read from the broker
        target  = total * t.weight
        if |t.weight - current / total| < threshold:  continue     # same denominator
        qty = whole_shares((target - current) / ref_price(t.symbol, direction))
        if qty == 0:  record(BELOW_ONE_SHARE or AT_TARGET); continue
        emit Order(t.symbol, qty, LIMIT ask*(1+slip) | MARKET bid)
```

What disappeared: `phase`, the commission divisor, the cash-reserve percentage,
`max_account_utilization`, the scaling factor, the adjustment pass, the affordability filter as a
separate stage, the fixed-vs-scaleable split, the `replaced_symbols` set, three replacement config
knobs, the PDT guard, the duplicated available-cash formula in two files, and the "provisional buys
that are never submitted" code path. That is roughly 500 of the 1,400 lines in the packages plus a
large part of a 1,200-line spec.

What stayed, deliberately: two-phase execution; sells as market orders; buys as limit at ask +
slippage; mid-price for measuring current value and bid/ask for pricing orders; the allocation
threshold (with a corrected denominator); quote retry and synthetic-price fallback (with the synthetic
flag surfaced); per-account connections keyed by account number.

---

## 5. Other bugs and design problems (separate from your list)

These are not simplifications. They are defects I found while reading, in rough order of severity.

**5.1 An unqualifiable position blocks the account, and that is intended.** *(Reclassified after
review — a design decision, not a defect.)* `_qualify_contracts` raises if any requested symbol fails
to qualify, and `get_account_snapshot` requests a price for every position, so one holding that is not
a plain US stock — a mutual fund, an option, a delisted ticker, a symbol needing a different exchange
— fails the whole run for that account, every round, with
`Contract qualification failed for symbols: [...]`. The position cannot be liquidated through this API
either, so no retry will clear it and the account needs a human. That is the desired outcome: refuse
to trade an account you cannot price, and tell the operator.

The only requirement is that it *notifies* rather than only logging. Today it does — the exception
propagates to `AccountExecutionResult(success=False, error=...)` and the failure notification carries
the message. Keep that. Do not "improve" it into a partial run that silently ignores the holding: a
portfolio sized without one of its positions is worse than no rebalance.

**5.2 Preview and live produce different plans.** Verified in §8.4. `calculate_rebalance` never
applies the affordability filter, so a cash-constrained preview lists buys that the live run drops.
Doc §2.14 forbids exactly this. Fixed for free by the pure-planner split (§3.7).

**5.3 An order with an empty status counts as filled.** In `_wait_for_orders_complete`:
`if status and status.upper() not in TERMINAL_STATES` — an empty or `None` status matches neither
branch, so `all_complete` stays true and the batch is treated as successful. `REJECTED` and `EXPIRED`
are also absent from `FAILED_STATES` (IBKR currently maps rejections to `Inactive`, which is why this
has not bitten yet, but the contract in doc §2.2.4 is not implemented). Fix: normalize to an enum in
the connector and fail closed on unknown.

**5.4 Fills are never read — accepted gap, no action.** *(Closed after review.)* `Trade.quantity` is
the requested quantity and is never updated from `orderStatus.filled`, so `trades_executed` counts
orders rather than shares, and a partial fill followed by a cancel is reported as a failure with no
record of what moved. The truth stays at the broker: orders and executions are retained, and the run's
final snapshot already shows the net effect. Reading fills on every run would add API calls, latency
and failure modes to serve troubleshooting that can be done after the fact. Leave it. One thing to
remember when reading notifications: "12 trades executed" means twelve orders placed, not twelve
fills.

**5.5 A zero snapshot can report success.** *(Researched after review; my first hypothesis was wrong.)* I
suggested a race where the account-value feed has not arrived and every field reads 0. That cannot
happen: `ib_async`'s `connectAsync` issues `reqAccountUpdatesAsync` and **awaits it** before returning,
so the values are populated by the time `connect()` succeeds.

What can still produce a zero account is identity, not timing. `accountValues(account=...)` and
`positions()` both filter on the exact string, so a typo or a different id form in `accounts.yaml`
returns *nothing* — no positions, no cash, no value. With no cash and no positions there is nothing to
trade, the plan is empty, and the run reports `success: true`. That is indistinguishable from a
genuinely empty new account, which should legitimately do nothing.

Two cheap measures:

- `if account_id not in self.ib.managedAccounts(): raise ValueError(...)` before anything is read. The
  gateway publishes the accounts it serves, so a mismatch is caught immediately. **This is the only
  guard needed once the sizing base stops coming from the broker (§7.2)** — with a computed total,
  zero cash and zero positions is a real state, not a suspicious one.
- Log the position count and the computed total at INFO on every run, so "0 positions, $0" is visible
  instead of passing silently.

**5.6 Two implementations of "available cash".** `TradeCalculator._apply_cash_constraint_scaling` and
`IBKRRebalancer._calculate_available_cash` compute the same formula in two places, with a comment
saying they must match. They will diverge. One implementation, in the planner.

**5.7 The dollar threshold is a percentage-point threshold.** `allocation_threshold_percent` is
compared in points of net liquidation, so on a $1M account a 0.4% drift is $4,000 left untraded by
design. That is the intended churn control, but it is the knob most likely to be mis-set per account,
and it is currently global. Consider making it per account when accounts vary in size.

**5.8 `success` is overloaded.** A run that sold everything and bought nothing for lack of cash returns
`success: true`. That is defensible for the engine, but not for the operator or the subscriber. Model
an outcome enum: `COMPLETED | COMPLETED_WITH_DRIFT | FAILED`, plus the warnings. Notifications and
dashboards both need the distinction.

**5.9 No mutual exclusion per account — and the failure mode is the opposite of what I said.**
*(Corrected after review; the earlier claim was wrong.)* Doc §2.17 says the caller must enforce it;
nothing does, so a manual event and a scheduled round can overlap for the same account. I wrote that
the second connection "kicks the first off the gateway". It does not. IBKR answers a duplicate client
id with **error 326, "Unable to connect as the client id is already in use"**, and **closes the new
socket**; the existing session keeps running and keeps its orders. `ib_async` then waits on its
startup handshake, so the second run does not fail fast — it stalls until its connect timeout and
reports a connection failure. Two concurrent runs therefore present as a slow connection problem, not
as a duplicate run.

With PDT gone there is no regulatory reason to prevent same-day overlap; the remaining reason is that
the second run is guaranteed to fail confusingly. If that matters, refuse it at the start of the run
with a per-account in-flight check — and note that this is concurrency code, so it should be an
explicit decision rather than a leftover.

**5.10 Notification text is built from string matching on error messages.**
`is_pdt_error = "PDT Protection" in error` — the parent decides presentation by substring-searching a
child's exception text. This disappears with the typed failure reasons, but note it as the pattern to
avoid when you add retry.

**5.11 One allocations request per account, not per strategy.** `AllocationService.get_allocations` is
called per account, so eleven accounts on the same strategy make eleven identical HTTP calls at the
same moment. Harmless today; will matter when the API is rate-limited or the subscriber count grows.
A per-strategy memo within a round fixes it.

**5.12 Gateway client connection limit.** Every account holds a connection for the duration of its run,
and `max_workers: 32` allows many processes. IBKR documents a ceiling of **32 simultaneous API clients
per TWS/Gateway instance** ("Up to 32 clients can be connected to a single instance of the
TWS/Gateway", TWS API `EClient` reference) — a hard cap, not a tuning knob. You are at 11 accounts, so
this is not urgent, but the subscriber model in the investor plan multiplies accounts and the failure
mode (connections refused across accounts at 09:30) will look like an IBKR problem. Worth a written
limit before the account count grows.

**5.13 Stale duplicate packages in the repo.** `packages/*/build/lib/` holds a second copy of every
module (identical today). Add `build/` to `.gitignore` and delete it; only the installed copies run.

**5.14 Normalisation only happens on the replacement path.** `apply_replacements_with_scaling` is the
only code that normalises weights to 1.0, and it only runs for accounts with a `replacement_set`. So
"the strategy allocations always sum to 1" is currently enforced for the nine accounts with
`replacement_set: ira` and not enforced at all for `U21243161` and `U24591836`.

**Decision (review): always normalise, for every account.** Move it onto the allocation input — one
place, before replacement, with a warning when the deviation exceeds a threshold — and let the
replacement service assume the weights already sum to 1. This matters beyond tidiness: with weights
summing to 1 the cash arithmetic closes by construction (§7.1). Any account left unnormalised silently
reintroduces the surplus-cash scale-up path.


A second pass over the code, after the decisions above, is in §9: what to delete, what to shrink, and
the gaps that will still bite.

---

## 6. Suggested order of work

Each step is independently shippable and independently verifiable. Do not combine 1 and 4 in one
release: step 1 changes *what the engine wants*, step 4 changes *how it handles what the broker did*,
and you want to be able to attribute a change in behavior to one of them.

| Step | Work | Why this order | Risk |
|---|---|---|---|
| 0 | **Characterization tests** on today's calculator: the doc's worked example, the replacement cases in §8.5, a cash-constrained case, a fractional case | Freezes current behavior before you change it. The doc example already reproduces exactly (17/72/22 provisional; 45/196/60 after sells), so it is a ready golden test | None |
| 1 | **Pure planner**: extract `plan()`, inject config and quotes, delete `phase`, commission, cash reserve, scaling, utilization cap | Largest change, highest payoff, fully covered by step 0 | Behavior change by design: no more scale-up, so accounts with weights < 1 will hold the cash the strategy asked for |
| 2 | **Replacement registry** in code, validated at startup; consolidate-then-scale; unknown set name fails | Depends on the planner existing but not on execution | Requires a deploy to change a set |
| 3 | **Broker interface**: capability flag, typed statuses, computed total, cached-price parameters removed | Changes the contract the planner and executor both use | Low; the connector is the only implementation |
| 4 | **Execution**: run records, typed failure reasons, retry queue + close-time job | Touches money movement; needs step 3's typed outcomes | Medium: retry can double-execute if cancel is unconfirmed — gate on a confirmed empty book |
| 5 | **Notifications**: `RoundReport`, one message per round, urgent escalation path | Needs step 4's typed outcomes to be useful | None |
| 6 | **Delete** PDT service, `last-executions` files, config, models, doc §2.13; delete `working-docs/` duplicates and `build/lib` copies | Cleanup last, so nothing depends on something already gone | None |

Before step 1, one gate: the spec must be rewritten to match the new behavior. Right now
`docs/rebalancing-logic.md` is a faithful description of the current engine — including commission
scaling, the reserve, and the frequency guard — so if the code changes and the doc does not, the doc
becomes a liability, and it is currently the only artifact detailed enough to reimplement the system.
Recommend rewriting §2.5, §2.6, §2.9, §2.13, §2.18 and the invariants in §2.16 as part of step 1, not
after.

---

## 7. Decisions this raises

Listed with my recommendation. Per your preference I am not asking you to settle all of them now — §7.1
is the one that gates the rest.

**7.1 The cash constraint can be satisfied by construction — here is the formula.** *(You are right; my
"the trim is unavoidable" conflated two different things.)*

**Term 1, self-inflicted.** The code turns a target *value* into shares by dividing by `ask x 1.005` —
the limit price — and then checks the resulting cost against cash. The limit is a ceiling, not the price
you expect to pay, so the plan reserves 0.5% more cash than it will spend, on every order, every run.
Measured: $99.75 on a 399-share order.

**Term 2, real but not a cash problem.** Buying costs the ask while target values are measured at the
mid. That difference is half the spread. It means the position lands a hair below its mid-value target;
it does not mean you are short of cash.

Size at the price you expect to pay and Term 1 disappears:

```
shares = floor(deficit / ask)          # not floor(deficit / (ask * 1.005))
cost   = shares * ask                  # what you will actually spend
```

Because `sum(deficit) = cash` when weights sum to 1 and the base equals cash + marks (that is an
identity, not an approximation), `sum(cost) <= cash` then holds by construction. Verified on a $20,000
order: **399 shares x $50.05 = $19,969.95 against $20,000**, leaving $30.05 — no scaling, no trimming,
no dropped order. The same order sized at the limit overshoots and needs a one-share correction
(§8.11).

**What remains is integer residue, not scaling.** Two terms survive:

- **Whole-share rounding:** up to one share per symbol, either direction. Ten symbols at $100 is up to
  $1,000 in the worst case, usually a few dollars.
- **The reserve:** the buy leg is capped at `cash − reserve`, so the plan deliberately leaves the
  reserve unspent and the last order gives up a share or two.

Both are absorbed by one loop: `while sum(cost) > cash: reduce the lowest-weight order by one share`.
In normal operation that moves one or two shares. It is not "scaling orders" — no global factor, no
order resized proportionally, no order dropped — and if it ever needs to remove more than a handful of
shares, that is a signal the inputs are wrong (stale quote, wrong base), which is worth surfacing
rather than absorbing.

**When an order should be dropped instead:** only when cash is genuinely short — an account that is
mostly cash with eight targets and can fund five. Then dropping the smallest is correct, because eight
half-positions are worse than five full ones. Rule: reduce by shares first, drop an order only once it
is down to one share, and log both.

**The one behaviour change to accept.** Sizing at the ask means the limit can exceed what you budgeted:
if the market moves up between sizing and the fill, an order can fill at up to `ask x 1.005` — 0.5% more
than planned — and the last order could be rejected for insufficient funds. Two ways to cover it, pick
one: (a) keep `minimum_cash_reserve` large enough to cover 0.5% of the largest order ($100 covers a
$20,000 buy, which is the current default), or (b) let the final share-loop check the last order
against the limit price rather than the ask. (a) is simpler and is what an absolute reserve is for.

**7.2 Sizing base: compute it — do not read `NetLiquidation`.** *(Revised after your ask to zoom out.
This reverses my previous "keep it" recommendation; the evidence is below, not the doctrine.)*

The goal of a rebalance is one sentence: for every symbol, `value_i / total = w_i`. That needs a total.
The only question is which one, and it follows from what the weights are *for* — they are fractions of
what can actually be invested. `NetLiquidation` is a reporting number: it includes accrued dividends,
fees and the broker's own marks, none of which can be traded until they become cash. A target built on
it is therefore always slightly larger than the account can fund, and the shortfall is a permanent gap
rather than a rounding error.

Compute the total from what you hold:

```
V = cash + sum(q_i * mid_i)
```

That is not a workaround; it is the definition of the amount available to allocate. It needs no extra
broker field: cash and positions are already in the snapshot, marks are already in the quotes.

**Measured** (§8.12) — same account, same quotes, same weights, run repeatedly through the current
calculator with only the base changed:

| base | first rebalance | cash left | then |
|---|---|---|---|
| `NLV` = marks + 300 | 25 shares | **$297.30** | stops, 0.30 pt worst drift |
| `V` = cash + marks | 28 shares | **$197.20** | stops, 0.40 pt worst drift |

Both converge and both stop at the threshold, so be clear about the size of this: the financial
difference is ~$100-300 of idle cash on a $99k account (~0.3%), *inside the drift you already accept*.
**This is an architectural simplification, not a bug fix.** What it buys:

- **`sum(deficits) = cash` becomes an identity**, so the plan fits by construction (§7.1) and the cash
  machinery only mops up whole-share rounding. With NLV the identity is broken by the gap —
  `sum(deficits) = cash + gap` — so every run carries a structural shortfall.
- **One denominator** for sizing, for the threshold and for the cash check. The "target is a fraction
  of available value but current weight is a fraction of net liquidation" split (§2.2) disappears by
  construction rather than by documentation.
- **No `NetLiquidation` read, no `<= 0` guard, and no rule** that the broker's total is authoritative
  and must not be recomputed from positions — spec §2.2.2 and §2.6.4 exist only to manage the
  disagreement between the two numbers.
- **No silent-zero sizing**: a zero total now means zero cash and no positions, which is a real state.
  The account-identity guard (§5.5) remains, and is the only guard left.

**What you give up.** `V` is your valuation, so a stale or synthetic quote moves the total — but it
moves the sizing by the same amount, so the plan stays internally consistent, whereas NLV plus a bad
quote makes the numerator and the denominator disagree. A position you cannot price blocks the total
and therefore the run, which is the behaviour you want (§5.1). And the percentages on an IBKR statement
will read ~0.2-0.3% below the strategy weights, because the statement divides by NLV and the engine
divides by V. Cosmetic, and it shrinks as accrued cash lands and gets invested.

**Reserve handling.** Keep the reserve out of `V`. `minimum_cash_reserve` is a floor on *cash while
buying*, not part of the target base: if it were subtracted from `V`, a fully-invested account with
zero cash would sell $100 of a position every run just to top the reserve up. Capping the buy leg
instead means a balanced, fully-invested account does nothing, and a cash-rich account spends down to
the reserve.

**7.3 Is `scale < 1` ever legitimate?** If not, restrict validation to `1.0 <= scale <= 3.0` and delete
the branch that normalization currently inverts. **Recommend no** — express exposure reduction in
weights.

**7.4 Retry budget and close-time job.** Maximum attempts (recommend 2) and whether a market-close job
is worth adding now or only when a close-time event exists. **Recommend 2 attempts, and add the close
job** — otherwise "morning failure retries at close" is not implementable.

**7.5 One run per account per session?** Since PDT is gone there is no regulatory reason. The
remaining reason is idempotency: a manual run and a scheduled round can currently overlap for the same
account, and the second connection is refused by IBKR (error 326) in a way that presents as a connect
timeout rather than "already running" (§5.9). **Recommend yes** — but as an in-flight guard (concurrency), not a
history file, and only if you want a clean refusal instead of the current confusing failure.

---

## 8. Evidence appendix

All probes were run against the repository's own packages (imported from `packages/*/src`, config
loaded from the repo's `config.yaml`), not against a reimplementation.

**8.1 The spec's worked example matches the code exactly.** Inputs per doc §2.15 (NLV 100,000; cash
10,000; reserve 1%; min cash 100; commission 1%; threshold 0.5pt; slippage 0.5%). Phase `all`:

```
E -250 MARKET @ 20.00      D -30 MARKET @ 400.00
A  +17 LIMIT  @ 100.60     B +72 LIMIT @ 50.35     C +22 LIMIT @ 201.20
```

After sells (cash 27,000), phase `buy`: `A +45`, `B +196`, `C +60`. Both identical to the table in
§2.15.3 and §2.15.4. So the implementation is faithful to the spec — the problem is the spec.

**8.2 Commission divisor idles 2.5% of a small account.** NLV 10,000, cash 10,000, one target at 100%,
$100/share, reserve 100, commission 1%: planner buys 97 shares → **$9,748.50 spent, $251.50 idle**.

**8.3 Scale-up, corrected.** My first run used weights summing to 0.90 — an input this system does
not produce — and reported 44.4/33.0/21.7 against a 40/30/20 target. That result is real for that
input and irrelevant to yours: with weights summing to 1.0 there is no surplus to deploy, and the
measured factor on the account in §8.10 was 0.9906.

**8.4 Preview returns an unaffordable plan.** Six targets at 1/6 weight, $300 cash, $50 shares,
reserve 100: planner returns six one-share orders totalling **$301.50** against **$198.02** available.
The live path's affordability filter would drop three or four of them; the preview path does not.

**8.5 Replacement service, three defects** (real `ReplacementService`, rules injected to isolate from
the YAML):

```
(a) collision    UVXY .20 -> VXX x2.0, with VXX .10, SPY .70
                 out: VXX .50 / SPY .50          consistent: VXX .475 / SPY .525
(b) scale < 1    SVXY .20 -> USMV x0.5, SPY .80
                 out: USMV .1111 / SPY .8889      intended: USMV .10 / SPY .80 + 10% cash
(c) floor path   UVXY .20 -> VXX x9.0, SPY .80
                 out: VXX .9574 / SPY .0426 (sum 1.0 — looks correct downstream)

control (correct) VIXY .05 x1.0 + UVXY .05 x1.5 -> VXX, SPY .90
                 out: VXX .1250 / SPY .8750      correct
```

**8.6 `reqGlobalCancel` is gateway-wide.** IBKR TWS API, *Cancelling Orders*: `reqGlobalCancel`
"will cancel all open orders, regardless of how they were originally placed". The installed
`ib_async` 2.0.1 (`ib.py:857`) docstring: "Cancel all active trades including those placed by other
clients or TWS/IB Gateway."

**8.7 FINRA Rule 4210.** SR-FINRA-2025-017; SEC Release 34-105226 (April 14, 2026); FINRA Regulatory
Notice 26-10, effective **June 4, 2026**, phase-in ends **October 20, 2027**. Attachment A shows
paragraph (a)(8)(B) as "Reserved. [Day Trading]" with the "day trading", "pattern day trader", $25,000
minimum equity, and 90-day cash-only provisions struck.

**8.8 IBKR fractional equities via TWS API.** IBKR Campus, *Order Types*: "Interactive Brokers APIs do
not support fractional or cash quantity trading with exception to Cryptocurrencies and Forex trading."
Error 10242 ("Fractional-sized order cannot be modified via API") is the observed failure for
fractional equity orders on this path. `ib_async.Order.cashQty` exists (`order.py:139`) but is the
crypto/FX cash-quantity path, not fractional equities.

**8.9 Cash tags.** IBKR account values: `CashBalance` (the account's cash), `SettledCash` (settled
only), `AvailableFunds` ("what you have available for trading", = equity with loan value − initial
margin), `BuyingPower` (a multiple of available funds), `TotalCashBalance` (aggregate across segments
and currencies). The connector reads `CashBalance`/`SettledCash` and filters to USD; `AvailableFunds`
is not read.

**8.10 Sizing base and the residual shortfall.** Fully-invested $100k account after its sell leg:
AAA 400 sh at a $100.00 mid, BBB 200 sh at a $200.00 mid, cash $19,780.80, target weights 40/40/20.

```
own base   = 19,780.80 + 40,000 + 40,000 = 99,780.80
broker NLV = 100,000.00                     gap 219.20  (0.22% of the account)

base = NLV        CCC target 20,000.00 -> 398 sh = 20,019.40   over cash by 238.60
base = own marks  CCC target 19,956.16 -> 397 sh = 19,969.10   over cash by 188.30

fitted by the current code (factor 0.9906): 393 sh = 19,767.90
   -> 1.16% under a 20,000 target, or 0.94% under a 19,956 target

absorption at base = own marks:
   trim shares 397 -> 393 : cash left 12.90      (0.01% of the account)
   drop the order         : cash left 19,780.80  (19.82% of the account)
```

Where the 188.30 comes from: the buy limit is `50.05 x 1.005 = 50.3002`, so sizing $1.00 of target at
the limit costs $1.006 — a 0.60% over-demand on every buy; the sells paid the bid, 0.10% below mid, on
$29,170.80 of sales (-$29.17); the rest is share rounding.

**8.11 Sizing at the ask vs at the limit.** Same order, weights summing to 1, base = cash + marks so
the identity holds: cash $20,000, target $20,000, ask $50.05, mid $50.00, limit $50.30.

```
size at the limit (current code)
  q = round(20,000 / 50.30) = 398
  cost at the limit        = 398 x 50.30 = 20,019.40   over cash by   +19.40  -> needs a correction
  expected fill at the ask = 398 x 50.05 = 19,919.90   actually $80 under cash

size at the ask (proposed)
  q = floor(20,000 / 50.05) = 399
  cost                     = 399 x 50.05 = 19,969.95   under cash by   30.05  -> nothing to do
  mid-value shortfall      =      50.00  (0.25%, half the spread - not a cash problem)

per dollar bought, the limit demands 50.30/50.05 - 1 = +0.4995% more cash than the ask
whole-share rounding      < 1 share (< $50.05 here)

if the broker NetLiquidation were kept as the base, the gap would add on top (this is what §7.2 removes):
  deficit 20,220 vs cash 20,000 -> over by 220 (1.10%), and no pricing change can fix that

**8.12 Does the base change whether repeated rebalances converge?** The repository's calculator run in a
loop against one synthetic account — cash $3,000; AAA 370 sh at a $100.00 mid; BBB 295 sh at a $200.00
mid; weights 40/60; a constant $300 of accrued value that only the broker can see — re-snapshotting and
re-planning after each fill until it stops trading:

```
base = NLV (marks + 300)
  round 1: 25 shares bought -> AAA 393, BBB 297, cash 297.30
  round 2: nothing          -> stopped
  final:   AAA 39.698% (target 40.0, -0.302 pt), BBB 60.002%, cash 297.30
           investable cash left behind = the 300 gap, permanently

base = cash + marks
  round 1: 28 shares bought -> AAA 398, BBB 295, cash 197.20
  round 2: nothing          -> stopped
  final:   AAA 40.203% (+0.203 pt), BBB 59.598% (-0.402 pt), cash 197.20
           leftover = the 100 reserve plus share rounding
```

Two things to read from this. First, **the gap does not disappear into rounding — it becomes idle cash**:
with NLV the account cannot fund the last $300 of its own target, so it holds $297.30 in cash forever.
That is the number an operator eventually asks about. Second, the per-symbol drift is one-share
granularity at these prices (one AAA share is 0.10 pt, one BBB share is 0.20 pt), so the 0.302 vs 0.402
difference is quantisation noise, not a quality difference. The financial effect of the whole change is
~0.3% of the account — inside the 0.5-point threshold — so the case for it is structure, not return.

---

## 9. Second pass: what else is in the code

Line references are to the current source. Decisions taken in review are marked.

### 9.1 Decisions

| Finding | Decision |
|---|---|
| Typos in `accounts.yaml` silently ignored | **Fix** (§9.4) |
| Strategy fetched per account | **Keep** — intended behaviour, not a defect |
| `SubprocessManager` | **Remove** (§9.6) |
| Fabricated ask price for thin ETFs | **Change the rule** (§9.2) — the requirement to keep is "never skip the buy" |
| Dead classes, fields and log helpers | **Remove** (§9.5) |

### 9.2 The reference-price rule (replaces the three-stage fallback)

The problem is real: some strategies hold thinly traded ETFs whose books are empty at times, and a
rebalance must still buy them. But the current fallback invents the wrong thing. It manufactures an
*ask* as `bid + $1.00`, and that number goes straight into the buy quantity: a $1 offset is a **1%**
sizing error on a $100 ETF and **0.1%** on a $1,000 one. Inconsistent by construction, and invisible in
the output.

Two different needs are being served by one number, and they should be separated:

**Valuing a position** needs a plausible mark, and the best available is an *observed* price, in
decreasing order of quality: the mid from a live two-sided quote → the last trade → the previous close
→ the close of the most recent one-minute bar. That last one is worth adding: it is a real trade with a
timestamp, available for anything that has ever traded, and it costs one request only for the symbols
that failed. It replaces an invented number with an observed one.

**Buying** needs a ceiling, not a market price. A buy does not require a bid at all — only a price you
are willing to pay. So: ask if present → last trade → previous close → one-minute bar, and the limit is
that reference x 1.005. Proportional in every branch, never a fixed dollar offset, never `bid + 1`.

**Selling** does not need either: sells are market orders, and the bid is only used for notional
reporting and for valuing what remains.

The rule, in one table:

| use | 1st | 2nd | 3rd | 4th |
|---|---|---|---|---|
| mark a position (for the total) | mid of a two-sided quote | last trade | previous close | latest 1-min bar close |
| buy sizing and limit | ask | last trade | previous close | latest 1-min bar close |
| sell notional (reporting only) | bid | last trade | previous close | latest 1-min bar close |

Consequences, stated plainly:

- **The round still never stops for a thin symbol.** Anything that has traded gets a price and gets
  bought. Only a symbol with no bid, no ask, no last, no close and no bar is skipped — at which point
  there is nothing to price and nothing to buy.
- **A held position that cannot be priced still aborts the run**, because the account cannot be valued
  and every target depends on that value (§5.1).
- **A fallback price now moves every target, not just one order.** With the total computed from marks
  (§7.2), an estimated mark shifts the total and therefore every target in the plan. Under
  `NetLiquidation` it did not — the broker's total insulated the rest of the plan from a bad local
  mark. So the quality of marks matters *more* after §7.2, not less: prefer observed prices, and record
  which symbol used which field and how old it was. The round summary should say so in one line
  ("2 symbols priced from last trade, 4m old"), because that is the difference between a plan built on
  observations and one built on estimates.

Net: ~120 lines of three-stage retry-and-fabricate become a ~30-line lookup with a fallback chain, no
fixed dollar offsets, and every substitution recorded.

### 9.3 The container healthcheck always passes

`Dockerfile`:

```
HEALTHCHECK ... CMD python -c "... print('healthy' if asyncio.run(app.get_status()).get('running', False) else 'unhealthy')" || exit 1
```

`print` exits 0 either way, so Docker sees success and the container is reported healthy no matter what
the string says. The string is not informative either: in a fresh process `running` is `self.ably is
not None`, which is always `False`, so it would print "unhealthy" even when the service is fine. The net
effect is a healthcheck that cannot fail and cannot inform.

Fix: have the main loop touch a heartbeat file on each iteration (`_run_forever` already sleeps on
`heartbeat_interval_seconds`), and let the healthcheck exit non-zero when the file is older than, say,
five intervals. Five lines, and it measures the thing you actually care about — that the process is
alive and looping.

### 9.4 `accounts.yaml` validation (decided: fix)

Three parts, ~15 lines:

1. `AccountConfig` gets `model_config = ConfigDict(extra="forbid")`, so a misspelled key is an error
   instead of a silent ignore. (No model in the repo sets this today.)
2. The whole file is parsed into `list[AccountConfig]` **at startup, in the parent**, before Ably
   subscribes — so a bad file fails the container immediately with a precise message instead of failing
   one account at 09:30 inside a worker.
3. That startup pass is also the natural place to check that every `strategy_name` exists and every
   `replacement_set` is a known set (§2.6), since both are cheap and both currently fail late.

### 9.5 Remove (decided)

| Item | Where | What replaces it |
|---|---|---|
| `OrderStatus` | `models.py:72-81` | The status enum introduced in §3.1, which will actually be used |
| `BaseRebalancer` | `base_rebalancer.py` | Nothing — one implementation, and after the planner split the rebalancer is a thin orchestrator |
| `Trade.current_shares / target_value / current_value` | `models.py:5-14` | The run record's decision entries (§2.12) |
| `_log_account_snapshot`, `_log_target_allocations`, `_log_planned_orders` | `rebalancer.py:369-457` | The run record, plus **one** compact plan line per run (keep a short human-readable summary — tailing logs is how problems get noticed) |
| Preview re-implementation | `rebalancer.py:268-310` | `plan()` + return (§3.7) |
| `packages/*/build/lib/` | repo | — |
| `SubprocessManager` | `trading_executor.py:234-311` | A 10-line `@asynccontextmanager connect_broker(...)`. Precise on what it does today: the `atexit` callbacks are no-ops (each context manager has already disconnected and emptied its dict by exit), and the signal handler does disconnect one client and `sys.exit(0)` — no better than the default disposition, and it reaches into `broker_client.ib`, breaking the abstraction |

### 9.6 Smaller items, with the specific fix

**Config logging (`loader.py:54-70`).** Fourteen hand-written lines mirror `AppConfig` and drift the
moment a field is renamed — the log then describes a config that no longer exists. Replace with one
`logger.info` of the validated model. There are no secrets in `AppConfig`.

**Contract qualification (`client.py:252-285`, `client.py:415`).** Qualifies one contract per `await`,
then re-qualifies the same contract for every order placed. On ten symbols and ten orders that is ~20
extra round trips per account per run. Batch the set once (`qualifyContractsAsync(*contracts)` accepts
several), cache `symbol -> contract` on the client for the run, and let `place_order` use the cache.

**`app = EventBrokerApp()` at import (`main.py:222`).** Importing the module builds the executor and the
subscriber, which is why the healthcheck does real work to answer a liveness question (§9.3). Construct
inside `main()`, or lazily on first access.

**Per-account logger setup (`trading_executor.py:203-230`).** Clears handlers and rebuilds a formatter
per account, with the removal in a `finally` that is easy to get wrong. A `logging.LoggerAdapter`
carrying `account_id` and `strategy_name` does the same in three lines, and the format string lives in
one place.

**A new `aiohttp` session per allocation fetch (`allocation_service.py:33`).** The fetch cadence is
intended (§9.1) but the session need not be: create one per client and reuse it. Note this is a
different issue from fetching per account, which stays.

### 9.7 Size of the change

```
rebalance_calculator/calculator.py   340  ->  ~110
ibkr_connector/rebalancer.py         457  ->  ~140
ibkr_connector/replacement_service.py 184 ->   ~50
ibkr_connector/client.py             514  ->  ~400   (reference-price chain, batched qualification)
app/services/trading_executor.py     310  ->  ~200
app/services/pdt_protection_service.py 151 ->    0
```

About 1,950 lines of logic become roughly 900, before the run-record writer adds some back. The point
is not the line count; it is that afterwards every number in a rebalance traces from the inputs to the
order without a detour through a config knob or a scaling factor.
