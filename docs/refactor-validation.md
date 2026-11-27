# Refactor plan — validation

> Historical validation of the file-backed v3 release; it is not current dashboard/SQLite release guidance.

**Date:** 2026-11-19 · **Status:** review only. No repository code was changed.
**Reviewed:** `docs/refactor.md` (the plan), `docs/rebalancing-logic.md` (the spec of today's engine),
`docs/research/ib-gateway-upgrade.md`, every source file under `packages/*/src` and `event-broker/app`,
`config.yaml`, `accounts.yaml` (account numbers not reproduced here), `replacement-sets.yaml`,
`docker-compose.yaml`, the Dockerfile, `README.md`, `AGENTS.md`, the tests, and the library the code
actually runs on (`ib_async` 2.1.0, the pinned version). External sources: IBKR TWS API documentation
and release notes, IBKR's PDT guidance, FINRA Regulatory Notice 26-10, and ib_async / TWS API
community threads (labelled as such where used).
**Ran:** 20 offline probes that import the repository's real calculator, replacement service,
order-wait loop and IBKR client, with crafted account states. All 20 pass, i.e. every behaviour
described below as "verified" was reproduced against the real code. Location and how to re-run: §9.

Words used below: **NLV** = net liquidation value, the account total IBKR reports. **Threshold band** =
`allocation_threshold_percent` (0.5 points): a symbol within 0.5 percentage points of its target is
not traded. **Limit price** = the most a buy order may pay; today `ask × 1.005`. **Leg** = the sell
half or the buy half of a run.

---

## 0. Decisions after Faisal's review (2026-11-19)

Sections 1–9 below are the original review; §0A is the second pass before coding. This section records what was decided and what is still
open, and it overrides any recommendation below that it contradicts.

### Decided

| Topic | Decision |
|---|---|
| Items 1–3 | Remove the commission divisor, `cash_reserve_percent`, and PDT protection (service, config, state files, notification special cases). |
| Item 4 | `supports_fractional` is a property every broker implementation must provide; IBKR returns `False`. Not a config setting. Leftover fractional shares the API cannot sell are reported to the user in the account notification. No persistent list. |
| Item 5 | Remove scaling, the adjustment pass, the utilization cap, the early skip and the separate affordability filter. On a cash shortfall, trim the lowest-weight buys first, share by share, and drop an order at zero; the smallest targets absorbing the shortfall is **intended**. Update the README. |
| Item 6 | Replacement sets live in code. Remove the compose mount for `replacement-sets.yaml` and update the README. Community users are told manually. |
| Item 7 | Fix all three replacement bugs. Fail only for invalid set definitions, caught by startup validation. A legitimately heavy replacement (e.g. >66.7% UVXY at ×1.5) is **capped at 100%** rather than failing the account (inferred from "fail only for definition errors"; confirm). Scales below 1.0 are rejected at validation. |
| Item 9 / O4 | Cash is the USD per-currency cash balance. IBKR names it `$LEDGER-CashBalance` or `CashBalance` depending on a Gateway API setting (production currently has the prefix off); read whichever is present, prefer the prefixed one if both arrive, and fail the run if neither does. Target Gateway: 10.51.1e. After the sell leg, wait (bounded) until cash reflects the fills. Accounts are IRA or margin; margin is used only to avoid settlement delays, so **cash must never go negative**. |
| Items 10 + N2 | One "rebalance account" operation is called by a signal, a manual run, and the next-morning retry. A run that fails, or that is triggered while the market is closed, places no orders (or cancels its own), notifies the user that it will be retried at the next market open, and is retried then with **fresh targets and a fresh plan**. If the market-open retry also fails, the user is told the account will not be retried and must be fixed manually. |
| Item 11 | Keep one notification per account per run (plus the retry notices above). Improve the code, not the behaviour. |
| Item 12 | Keep enough investigation data for **30 days**. No preference on mechanism. |
| §3 | Broker and notifier stay interfaces so a second broker (e.g. Alpaca) or a second channel can be added without an overhaul. Quote cache: delete. Accounts loader, planner: concrete. |
| Item 8 / O1 | At the start of each account's run, cancel only that account's working orders and wait (bounded) for IBKR to confirm; fail the run if any remain. No gateway-wide cancel. **No cancel on the way out** after a timeout or failure: DAY orders left working may still fill until 16:00, so the failure notice can precede late fills; the next-morning retry plans from the actual account state, so it cannot double-trade. |
| N4 / O2 | Budget buys against each order's **limit price** (sizing may use the ask). |
| Threshold band / N6 / O3 | **Remove `allocation_threshold_percent` and the 0.5-point rule entirely.** Every run aligns each symbol to its target to within whole-share rounding, so deposits are invested on the next run. Accepted cost (simulated, $100k, ten ETFs, ±3% drift plus a two-weight signal): about 7 orders per run instead of 2, ~5 of them under $1,000, a few dollars of extra commission, and small realised gains in taxable accounts. |
| O5 / D3 | One rule for every account (IRA and individual): if target weights total within 1% of 100%, scale them to exactly 100% and log it; otherwise refuse the run and notify the user. An empty list is not subject to this rule (it means sell everything). Weights below 100% no longer mean "hold cash". |
| O6 / D6 | Drop the fractional portion: buy and sell quantities round **down** to whole shares (settled with item 4). A target smaller than one share gets no buy; the README's "at least one share of every target" promise is removed. |
| O7 / D10 | An `accounts.yaml` that still contains a removed setting (`cash_reserve_percent`, `pdt_protection_enabled`) or any other unknown key **refuses to start**, with a message naming each key and account to fix. No transition release. |
| N1 | Fix: fail the run when account data is missing. |
| N3 | A signal for a strategy that is already running is logged and dropped. No user notification. |
| N7 | An HTTP 200 response with an empty allocation list means "sell everything, buy nothing" — intended. Negative weights and duplicate symbols are rejected and the user notified. |
| N8, N9, N11 | Fix. |
| N10 | Catch every error inside the worker so an ordinary exception can never escape it, **and** recover the pool if a worker dies anyway (see note below). |
| §6 | S1–S7 accepted, subject to research before implementation. |

**Implementation notes that follow from the decisions** (no further decision needed):

- *N10.* A `try/except` stops ordinary errors, but it cannot stop a worker from being killed outright
  (out of memory, a native-library crash). Today's `SubprocessManager` also calls `sys.exit` from a
  signal handler inside the worker; removing it (settled) removes one cause. So: catch-all in the
  worker, plus "if the pool reports it is broken, replace it" in the parent.
- *Item 10 locking.* The retry list would be written only by the parent process, from the results the
  workers already return, and read by the scheduler in the same process. With one writer there is no
  locking to add, so the standing rule about explicit concurrency code is not triggered. If the design
  changes so workers write the file, it is.
- *Item 12.* Docker's log rotation is by size (100 MB × 10), not by age, so it does not guarantee 30
  days. Also, several worker processes cannot safely share one rotating log file. The mechanism will be
  chosen and verified during implementation.
- *N3 edge.* A market-open retry that collides with a manual run of the same strategy would be dropped
  by the N3 rule, and that account would get neither its retry nor the "will not be retried" notice.
  The retry path should therefore report a dropped retry, even though a dropped signal is not reported.
- *N7.* A target with weight 0 is sold exactly as a liquidation (the shares held), not via today's
  rounding, which asks for one share more than held.

### Still open

None. All second-pass questions are answered (§0A.0).

#### Deposit walkthrough (resolved by removing the threshold band) — today's real code

Script: `n6_walkthrough.py` in the probe folder. It runs the real calculator for the sell plan, fills
sells at the bid, re-plans buys against the new cash exactly as the live run does (`phase='buy'`), applies
the live path's own affordability filter, and fills buys at the ask. Config is the repository's
`config.yaml` (0.5-point band, $100 minimum cash, 1% commission divisor).

Starting account: $100,000 spread across 10 ETFs at 10% each (SPY, QQQ, IWM, EFA, EEM, TLT, IEF, GLD,
VNQ, XLE at $46–$600), whole shares, $0 cash. Then a $4,000 deposit lands: cash $3,993, NLV $104,000.

| Case | What triggers the run | What happens, step by step | Cash afterwards |
|---|---|---|---|
| 1 | Manual run, or a signal with the same weights | Each ETF is now 9.50–9.81% against a 10% target, gaps 0.19–0.50 pt. Every gap below 0.5 is skipped; QQQ sits exactly at 0.50 and buys 1 share. No sells. | **$3,473 (3.3%) idle** |
| 2 | Same, but the deposit is $6,000 | Gaps rise to 0.38–0.68 pt; nine of ten ETFs cross 0.5 and are bought. | $668 (0.6%) |
| 3 | A signal that moves TLT 10→15% and XLE 10→5% | Sell 55 XLE. Buy pass: only TLT is outside the band. Today's scale-up pours the deposit plus the XLE proceeds into TLT: 95 shares instead of the 62 it needs. | $282, but **TLT ends at 17.8% vs a 15% target** |
| 3 after the refactor | Same signal, scale-up removed | TLT buys the 62 shares it needs and ends at 14.97%. | **$3,252 (3.1%) idle** |

What this shows: with ten equal targets, a deposit smaller than roughly 5% of the account is not
invested by a manual run or by a same-weights signal (case 1). Today it is invested only when a signal
happens to move some weights, and then it all lands in the symbols that moved (case 3). Once scale-up is
removed, as decided, it stays in cash until a later run pushes the gaps past 0.5 points.

Resolution: the threshold band is removed (see Decided), so case 1 buys every under-weight ETF, with
the trim loop keeping buys within cash.

---

## 0A. Second pass before coding (2026-11-19)

**Result.** None of the decisions in §0 needs to be reversed. The plan holds up. But the second
pass found **15 places where coding the decisions as currently written would cause a bug or a
surprise partway through**. Most of these are about the order of steps or a missing detail, and the fix
for each one follows directly from a decision you already made, so they are recorded below as
implementation rules. **Two need a decision from you** (§0A.4). The replacement-cap question left open in
§0 is now settled by analysis (§0A.1, R2).

How this pass was done: I re-read every runtime file (event-broker services, client, rebalancer,
calculator, config models, compose, Dockerfile, the scheduler and tools), wrote a prototype of the
*decided* planner (`decided_planner.py`), and ran it on 20,000 random accounts. I also checked library and
vendor behaviour (TWS API cancellation rules, APScheduler defaults, ntfy limits, IBKR cash-account
settlement, Python start methods). Labels: **verified** = reproduced by running something; **from code** =
read directly; **from docs** = vendor or library documentation.

### 0A.0 Faisal's answers to the second pass (2026-11-19)

| Item | Decision |
|---|---|
| R1, R2, R3, R6 (summary #1, #2, #3, #5) | Agreed. |
| R14 (summary #7, your own `accounts.yaml`) | Settled; Faisal updates his file and tells the community. |
| Q1 | Yes: `config.yaml` refuses to start on removed or unknown keys, same message style as `accounts.yaml`. |
| Q2 | Market-open job moves to 09:35 ET. |
| R5 (summary #4) | **No separate retry file.** Afternoon failures are added to the existing `scheduled.json`. Everything the market-open job runs is final: a failure there is notified as "will not be retried" and nothing is re-queued. Two small rules remain: the job removes only the accounts it ran (instead of emptying the file, so an entry added while it runs survives), and a later successful run of an account removes it from the list. No locking; `tools/schedule.sh` writing at the same instant is an accepted, negligible risk. |
| R7 (summary #6) | Correction: the API *can* cancel every order regardless of origin, via `reqGlobalCancel`, but only for the whole login (all accounts, manual orders included); there is no per-account form. Per-order `cancelOrder` works only for orders placed by the same client ID (client 0 can also bind manual TWS orders). Because signals for different strategies arrive seconds apart and run in parallel, a global cancel at the start of one run would cancel other accounts' orders mid-run. Decision stands: per-account cancel of this app's own orders; open orders must be loaded at connect. |
| §0A.2 cash floor (summary #8) | **Agreed:** one global `trading.minimum_cash_reserve_percent`, default 0.25%, measured against the account total at the start of the run; replaces `minimum_cash_reserve_usd` and the per-order commission estimate. Accepted range 0–5%; README warns that values under 0.25% may not cover commissions on large purchases of cheap shares (analysis in 0A.6). |
| Working orders the app did not place | **Stop the run and tell the user** if the account has any working order not placed by this app (manual TWS/mobile orders or another tool). It follows the normal failure path: afternoon → retried at the next open; at the open → "will not be retried, fix manually". The message names the orders (symbol, side, quantity) so the user knows what to cancel. Implementation note: seeing other clients' and manual orders needs `reqAllOpenOrders`; confirm in paper that manual orders appear (added to the paper checks). |

### 0A.1 Would break mid-implementation (fix is a rule, no decision needed)

**R1 — Check the 1% weight rule *before* applying replacements. From code + decision O5.**
The replacement step changes the total on purpose: UVXY at ×1.5 turns a 100% strategy into 135%. If the
"within 1% of 100%" check runs after replacement, **every IRA account fails** whenever the strategy holds
UVXY. Order: fetch → validate (duplicates, negatives, total within 1%) → scale to exactly 100% → apply the
replacement set → fit to 100% (R2) → plan.

**R2 — How a replacement that overflows 100% is fitted (closes the open item in §0). Verified by
calculation.** You left the method to me. Example: UVXY 70% + SPY 30%, with UVXY → VXX ×1.5 in IRAs,
asks for VXX 105% + SPY 30%. Three ways to fit that into 100%:

| Method | Result | How far from the strategy (tracking error, typical case) |
|---|---|---|
| Keep the replaced exposure, cut the other symbols first | VXX 100 / SPY 0 | **3–7%** |
| Shrink everything by the same factor | VXX 77.8 / SPY 22.2 | 10–27% |
| Cut every symbol by the same amount | VXX 87.5 / SPY 12.5 | 5–18% |

The first method is closest under every volatility and correlation assumption I tried
(`replacement_fit.py`: VXX volatility 40–100%, SPY 18%, correlation −0.8 to 0). The reason: the replaced
symbol is the volatile one (that is why it has a multiplier above 1), so missing part of it costs far more
than missing part of SPY. The rule: after replacement, if the total is above 100%, reduce the
**non-replaced** symbols proportionally until the total is 100%; if the replaced symbols alone are above
100%, scale them to 100% and give the rest 0. Log it and mention it in the account's notification. At
UVXY 50% the result is VXX 75 / SPY 25. Two IRA rules point at VXX (UVXY ×1.5 and VIXY ×1.0), so add
weights that point at the same symbol together first.

**R3 — A slow notification can silently drop the next signal. From code.** The strategy stays marked
"running" until all its notifications are sent (`strategy_executor.py:66` sends the notifications inside the
`try`; the mark is removed in `finally` at `:81-82`). The ntfy requests are sent one after another
and have no timeout of their own (aiohttp's default limit is 5 minutes per request). With the N3 decision
("a signal for a running strategy is dropped silently"), a slow ntfy.sh could make the app drop a real
signal without telling anyone. Rule: remove the "running" mark before sending notifications, and give
each ntfy request a short timeout (e.g. 10 s).

**R4 — "One notification per account" is two today, and the merged one has a size limit. From code
+ ntfy source.** A successful run with warnings sends a success message *and* a separate warnings
message (`strategy_executor.py:115-132`). Merging them is the fix, but ntfy truncates any message
over 4,096 bytes. Once the leftover-fractional-share notices, trimmed buys, replacement-fit notices and
the "will retry" text are all in one message, a ten-symbol account can get close to that. Rule: keep each
line short, put the outcome and action first, and cap the warning list ("…and 4 more, see logs").

**R5 — The retry list must not share today's market-open file. From code.** Today the market-open job
reads `scheduled.json`, runs everything (minutes), then **writes an empty list**, whatever happened in
between (`scheduler_service.py:256`). The host tool `tools/schedule.sh` also writes that file. If retries go
in the same file, an account that fails while the 9:30 job is running is queued and then wiped by the
clear, and you would have two writers (the container and the host script), which is concurrency you have
not approved. Rules:
- Keep a separate retry file that only the parent process writes, so no locking is needed.
- Each entry records the account, the reason, and whether it is already a retry. A failed retry sends
  "will not be retried" and is not added again.
- Delete entries one at a time as they are processed, not by clearing the whole file.
- At 9:30, combine the two lists and drop duplicate accounts before running. Otherwise an account that is
  in both lists runs twice, and the second run is dropped by the N3 rule.
- **A later successful run of the account removes its pending retry.** Example: the signal fails at
  15:52, you fix the problem and run it by hand at 15:56, and it succeeds. Without this rule the account
  would still be re-run at 9:30 the next morning for no reason.

**R6 — The 9:30 job is skipped if it starts more than 1 second late. From docs.** APScheduler 3.x
skips a scheduled run that starts more than one second after its time (`misfire_grace_time` defaults to 1
s), and logs it at INFO level. Today that only loses manually scheduled accounts. With retries it would
lose every retry for that day, and nobody would be told. Rule: set a grace period of several minutes and
`coalesce=True` on the market-open job. Also pin `APScheduler<4`: 4.x removes `AsyncIOScheduler`. It is
still pre-release (latest stable is 3.11.3), so it is not urgent, but `>=3.10` lets it in.

**R7 — "Cancel this account's working orders" only sees this app's own orders, and the narrow connect
can make it see nothing. From docs + library code.** TWS API: *"cancelOrder can only be used to cancel an
order that was placed originally by a client with the same client ID."* `ib_async` loads open orders
at connect with `reqOpenOrders`, which returns only this client ID's orders (`ib.py:2058`). Because the
client ID is the account number's digits, orders this app placed earlier for the same account (e.g. a
buy left working by a run that timed out earlier the same day, or any GTC order) *are* visible and
cancellable. DAY orders expire at the close, so the case that matters is a same-day re-run. Two consequences:
- Orders **you** place by hand in TWS or the mobile app are not seen and are left alone. That fits the
  "only this account, only what we placed" intent. Write it in the README.
- The narrow connect (S2) passes a list of what to load at connect (`fetchFields`). If that list leaves out
  open orders, the cancel step finds nothing, "confirms" that nothing is working, and proceeds. That is
  the fail-open behaviour this refactor is meant to remove. Rule: open orders must be in the list, and a
  test must check it.

**R8 — What cash to use after the sells. From code + docs.** IBKR updates account values "when a
position changes or every 3 minutes", so the cash figure can lag behind the fills. Rule: after the sells
fill, wait (bounded, e.g. 30 s) for the reported cash to rise, then budget against
**the lower of** the reported cash and *(cash before the sells + net sale proceeds from the fills)*. The
lower figure is never more than the true cash, so this can never overspend. If the report is late, the
cost is a slightly smaller buy leg, which the next run makes up.

**R9 — The buy leg needs fresh prices. From code + decision S1.** Today the buy leg reuses the
prices fetched before the sells, through the price cache. The cache is being deleted, and buys are
limit orders at ask × 1.005. A quote that is a few minutes old can put the limit below the market, so the
buy never fills, times out, and the run fails into a retry. Rule: fetch prices again, once, for the buy
leg.

**R10 — The second planning pass must only buy. From code.** The planner is called again after the
sells with the new cash. If it can also produce sells (because prices moved), a run can keep selling.
Rule: the post-sell pass keeps the same targets, uses the fresh cash and prices, and produces buys only.

**R11 — The market-hours check must use the real closing time, and previews are exempt. From code.**
Use the exchange calendar's actual close for the day (13:00 ET on half days, not a fixed 16:00).
Previews (`print-rebalance`) run any time, place nothing, and are never queued for retry.

**R12 — Do not change Python's process start method (or the Python version) in this refactor. From
code + Python docs.** Workers get their configuration only because Linux copies the parent process
("fork"), and `get_config()` reads a module-level global (`app_config/loader.py:13`). Python 3.14 changes
the Linux default to "forkserver". Under forkserver or spawn, the workers would have no config and fail
on every run. They would also re-import `app.main`, which builds a whole `EventBrokerApp` at import time
(a settled decision keeps that). The Dockerfile uses Python 3.12, so nothing breaks today. My offline
probes used 3.14, which is not a problem for them because they do not use the pool. Rule: create the pool
with an explicit `fork` context so that a future base-image bump cannot change the behaviour silently.

**R13 — The "failed, will retry" message must say what already happened. From code.** A run whose
sells filled but whose buys failed or timed out has changed the account. Today the failure message only
shows the error. Rule: the failure notification lists the fills that did execute ("Sold 40 XLE, 12 GLD;
buys did not complete; retrying at next open").

**R14 — Strict `accounts.yaml` checks: three details. From code + your file.**
- Every entry in your own production `accounts.yaml` contains both removed keys
  (`cash_reserve_percent`, `pdt_protection_enabled`). **Your install will refuse to start until the file is
  edited.** Put that edit in the deployment steps.
- The check covers disabled accounts and accounts for the other trading mode as well. Otherwise an old key
  hidden in a disabled entry would start failing the day that entry is switched on.
- Add a check that **each account ID appears only once**. Today one account listed under two strategies
  would be traded by both, with the same client ID, at the same time.

**R15 — Compare the account total with what was summed. From code.** The new planner adds up the
account itself (cash + positions × price). If the positions feed arrives incomplete, the sum is too
low and the plan is wrong, although never by more than the cash available. IBKR's own figure (NLV) catches
this. Rule: log both every run, and fail the run (which then retries) if they differ by more than 2%. NLV
includes accruals such as interest and dividends, so a tight tolerance would cause false failures.

### 0A.2 Follows from "cash must never go negative" (no decision needed)

**Commissions are not in the budget any more.** The 1% commission divisor is gone (item 1), and the
$100 minimum reserve was going to cover commissions. That holds for a typical rebalance, but not for a
large deployment into low-priced ETFs. Under IBKR Fixed pricing ($0.005/share, $1 minimum), putting $1M
into $25 ETFs is 40,000 shares, or about $200. In practice the 0.5% limit buffer mostly goes unused,
because buys usually fill at the ask, so this almost never shows up. But "never negative" would then be
true only by luck. Rule: the broker supplies a conservative commission estimate per order (IBKR: the
larger of $1 and $0.005 × shares), and the budget subtracts it.

### 0A.3 Behaviour to expect (verified with the decided-rules prototype; no action unless you object)

Prototype: `decided_planner.py`, `decided_planner_test.py`, `spread_sensitivity.py` and
`sizing_variants.py` (20,000 random accounts, 1–12 targets, prices $2–$1,200, with and without
fractional holdings and existing positions).

- **Safety invariants held in every case:** no sell larger than the whole shares held, no short
  positions, and cash at or above the $100 minimum even if every buy fills at its limit (commissions
  excluded, see 0A.2).
- **About 0.45% of the account stays in cash after a run** (median; 95th percentile about 1.4%). This is the
  cost of budgeting at the limit price, which you approved: buys are budgeted at ask × 1.005 but fill at
  about the ask. The next run invests it. A second buy pass would bring it down to about 0.05%, but it
  adds ~1.7 orders and another wait to every run, so I do not recommend it.
- **Running again right after a successful run usually trades a little.** With liquid ETFs, an
  immediate identical re-run placed small trades in 17% of accounts (median sell $168, 0.015% of the
  account, at most 0.3%). With some thin ETFs in the mix, it was 50%. Why: each run pays the bid-ask spread,
  which lowers the account value a little, so a cheap holding can end up one share over its target. Re-runs
  happen with retries, manual runs, and a second signal on the same day. This is the price of the
  "always align" rule you chose. The only remedy is a small no-trade margin, which is the band you
  removed, so I recommend accepting it.
- **On a large cash deployment, the smallest-weight target absorbs the price buffer.** Because buys are
  sized at the ask and budgeted at the limit, the ~0.5% gap between them is trimmed from the lowest-weight
  buy first. In the all-cash simulation, the worst-hit symbol ended a median 3.3% below its own target, against
  1.9% if buys were sized at the limit instead (cash left idle: 0.6% against 1.0%). This is minor and
  consistent with your trim rule, so I suggest keeping it as decided.

### 0A.4 Two decisions needed

| # | Question | Context | Recommendation |
|---|---|---|---|
| Q1 | Should `config.yaml` get the same "refuse to start on removed or unknown keys" rule as `accounts.yaml`? | Today the config models accept and **silently ignore** unknown keys (no `extra="forbid"` anywhere). After the refactor, a community `config.yaml` that still sets `allocation_threshold_percent: 2.0` or `commission_rate` would start normally and behave differently from what its owner believes. `config.yaml` is tracked in git, so users who never edited it get the new file on `git pull`. Only users who customised it are affected. | Yes, same rule, same clear message, for consistency with your `accounts.yaml` decision. |
| Q2 | What time should the market-open job (scheduled accounts and retries) run? | It runs at exactly 09:30:00 today (`config.yaml: market_open_time`). In the first minutes after the open, spreads are at their widest and some thin ETFs have not printed yet. Sells are market orders, and buys are limits priced off those first quotes. A retry is by definition an account that already had a problem, so it should run in the calmest conditions available. | 09:35. Still early, and past the opening auction. |

### 0A.6 Follow-up explanations and the cash floor

**R5 in plain terms (the retry list).** Today `data/market-open/scheduled.json` is *your* list: you add
accounts with `tools/schedule.sh`, and at the open the app runs them and then empties the file. If the app
also wrote its retries into that file, three things go wrong:

1. *A retry is wiped before it runs.* You schedule account B. At 09:35 it runs and fails (say a price
   cannot be fetched). The app writes "retry B tomorrow" into the file and notifies "will retry". A few
   seconds later the job finishes and empties the file. B is never retried, though the user was told it would be.
2. *Two writers.* `schedule.sh` on the host and the app in the container could write the file at the same
   moment; one change is lost. Preventing that needs locking, which you have not authorised.
3. *Stale retries.* Monday's 15:52 signal fails for account A ("will retry tomorrow"). You restart the
   gateway and run A by hand at 15:57; it succeeds. Without a rule, A is still re-run Tuesday morning with
   a second notification. Harmless but confusing.

Proposal: leave your list and `schedule.sh` exactly as they are. The app keeps its own retry file that only
it writes. At the open it runs both lists together, each account once. A successful run of an account
removes its pending retry.

**R7 in plain terms (cancelling old orders).** IBKR only lets a program cancel orders that the same
program connection placed. Each account's connection uses a fixed number derived from the account number,
so it can see and cancel orders this app placed earlier for that account, but not orders you placed by hand
in TWS or the mobile app (those are left alone, which is what we want). Example of why the cancel step
matters: the 15:51 signal places "buy 50 QQQ" at a limit; it has not filled after 5 minutes, the run times
out and stops, and the order is still working. You run the account by hand at 15:57. That run must cancel
the QQQ order first; otherwise both the old order and the new one can fill and the account buys about
100 QQQ. The pitfall: one planned speed-up (S2) tells the connection to load only what is needed. If "open
orders" is accidentally left off that list, the app sees no working orders, "confirms" nothing needs
cancelling, and the double buy happens. Rule: always load open orders, with a test that checks it.

**Cash floor as a percentage (Faisal's proposal for #8).** Agreed, and it is better than a dollar floor
plus a commission estimate: one setting, no broker-specific fee formula, and it scales with the account.
Simulated with the decided-rules prototype (`pct_floor.py`, 17,100 accounts over $5,000, every buy filling at
its limit price, IBKR Fixed commissions of max($1, $0.005/share) per order):

| Floor | Runs where cash would go below $0 after commissions | Typical cash left idle after a run |
|---|---|---|
| 0% | 8,734 (51%) | 0.44% |
| 0.10% | 27 | 0.54% |
| **0.25%** | **0** | **0.69%** |
| 0.50% | 0 | 0.94% |

Commissions for a whole run were at most 0.28% of the account (99th percentile 0.09%). The worst cases are
small accounts placing many $1-minimum orders. 0.25% held in every case, partly because buys usually
fill below their limit.

Notes:
- This is the removed `cash_reserve_percent` back in a working form. It failed before only because the
  scale-up step spent it and the threshold check used a different base, and both of those are being removed.
- Recommended shape: one setting in `config.yaml` (`trading.minimum_cash_reserve_percent: 0.25`), replacing
  `minimum_cash_reserve_usd`. It is measured against the account total at the start of the run, and the
  broker commission estimate in §0A.2 is dropped. Not per account (no current need).
- Values below 0.25% are allowed but the README should say they may not cover commissions on large deployments
  into cheap shares. Values outside 0–5% are rejected at startup.

### 0A.5 Work-order and testing notes

- **Do not write "golden" tests of today's calculator output.** Band removal, round-down, and the
  removal of scaling deliberately change most outputs. Characterise only what is kept (the fail-closed
  snapshot, order status handling, the replacement definitions), and test the new planner with the
  invariant tests already written for the prototype.
- **First signal after deployment will trade more than usual.** It catches up on all drift that sat
  inside the old 0.5-point band and invests idle deposit cash. Tell users to expect it.
- **Before the first live signal, run a preview (`print-rebalance`) for every live account during market
  hours** and read the plans. This places no orders and is the cheapest end-to-end check of the new
  planner against real holdings.
- **Paper checks to run during market hours** (tell you first, because the paper login may disconnect
  production): (1) how IBKR checks funds for a limit buy (C2); (2) how long cash takes to update after a fill
  (R8); (3) that orders left working by this app's client ID in an earlier session are visible and
  cancellable after reconnecting (R7); (4) whether completed orders come back with their original order
  numbers (N8). (5) that a manual TWS/mobile order on the account is reported by
  `reqAllOpenOrders`, so the stop-on-foreign-orders rule can see it.
- **Known gap, not in scope:** if the app's live-message connection (Ably) drops for more than about
  two minutes, signals published in that time are never received. A retry only covers runs that started.
  Worth a separate item later.
- **Community note:** in an IBKR *cash* account (as opposed to margin, or IRA margin), sale proceeds
  cannot be spent until they settle, so the buy leg may be rejected after the sells. The next-morning retry
  then usually succeeds, because the proceeds have settled by then (T+1). Say in the README that the
  sell-then-buy flow assumes margin-type accounts.

---

## 1. Result

**The plan is pointed in the right direction and most of it should be done.** Ten of the twelve
items are valid as stated or with small adjustments, and the single biggest idea in it — one pure
planning function instead of a calculator called twice with a `phase` switch — is right and removes a
real preview-vs-live mismatch (reproduced).

**But it is not safe to implement as written.** Five claims the plan relies on are wrong, and two of
them would make the new engine worse than today's in specific, reproducible cases:

1. **"After the change, buys fit in the cash by construction."** False. Whenever a held position sits
   slightly *over* its target but inside the threshold band (so it is not sold), the buys ask for more
   than the cash available. Probe: the planner wants $1,600 of buys against $200 of spendable cash.
   The "trim the smallest orders" loop is therefore the real cash guard, not a rounding mop-up (§4, C1).
2. **"Size buys at the ask instead of the limit price."** Doing that *and* checking cash at the ask
   removes today's protection against submitting buys whose limit-price total exceeds cash. In an IRA
   the broker would most likely reject the last buy after the sells have already executed; in a
   margin account it would borrow a little. Size at the ask if you like, but check the budget at the
   limit price (§4, C2).
3. **"A zero account snapshot cannot happen."** False for this deployment, and the consequence today
   is severe: if the account-value feed is missing when the run starts, **the current engine plans to
   sell every position** (and slightly more than is held). Reproduced end-to-end through the real
   client and calculator. This is a live bug, independent of the refactor — fix it first (§5, N1).
4. **"Nothing stops two runs for the same account overlapping."** False — the opposite happens. The
   app refuses a second run for a strategy that is already running and **silently drops it**. A
   manual run for one account can swallow the real strategy signal for every account on that strategy
   (§5, N3).
5. **"The PDT rule is gone."** FINRA's change is real, but IBKR's own pages say accounts *may still be
   subject to the old PDT rules until October 2027*. Deleting the PDT guard is still the right call,
   for different reasons (§2, item 3).

Three other things need a decision before implementation, because the plan contradicts itself or the
product: always normalising weights to 100% conflicts with "a strategy holds cash by allocating less
than 100%"; making a "too-heavy" replacement fail the account would freeze IRA accounts exactly when
a strategy moves into a large volatility hedge; and strict validation of `accounts.yaml` plus deleting
two fields will **refuse to start every existing community install**, because every
`accounts.yaml` (and the example file) contains those fields.

Finally, the plan proposes several abstractions with no present second user (a quote-cache interface,
an account-list interface, a planner interface, a config switch for fractional shares, a persistent
"manual action" list, a run-record file store). Under your design rules these should be dropped or
made concrete (§3).

I also found **eleven defects the plan does not mention** (§5). The most important after N1:
live runs outside market hours queue market sells for the next morning and never place the buys
(N2); deposits smaller than a few percent of the account are never invested (N6, reproduced); the
"cancel old orders" step runs after the plan is made, never confirms, and swallows errors (N5).

---

## 2. Your twelve items

| # | Item | Verdict |
|---|---|---|
| 1 | Remove commission calculation | **Valid** — only safe together with the limit-price budget check (C2) |
| 2 | Remove `cash_reserve_percent` | **Valid, stronger than the plan says** — it does not work today at all |
| 3 | Remove PDT classification and guard | **Valid, but the plan's reason is wrong** |
| 4 | `IsFractionalSupported` capability | **Valid as a constant; not as a config switch** |
| 5 | Remove scaling and max-utilization; drop orders instead | **Valid, with two corrections** (cash fit, limit-price check) |
| 6 | Replacement sets as a system artifact | **Valid goal; community trade-off must be decided** |
| 7 | Replacement logic bugs | **All three confirmed; one proposed fix needs changing** |
| 8 | Add `cancel_all_orders` | **Valid in a per-account, confirmed form** — the plan's "add nothing" is wrong |
| 9 | Use IBKR's cash field | **Valid; the plan missed the `$LEDGER-` renaming** |
| 10 | Requeue critical failures | **Valid feature; depends on 8 and on N2; needs your sign-off (timing/concurrency code)** |
| 11 | One notification per round | **Valid goal; there is no "round" for signal events** |
| 12 | Enough logging to investigate | **Valid; a smaller form is enough** |

### Item 1 — Remove the commission divisor

**What it is.** `commission_rate: 0.01` divides all spendable cash by 1.01 before sizing
(`calculator.py:242`, duplicated at `rebalancer.py:196`).

**Verdict: valid.** Reproduced the plan's example: a $10,000 all-cash account buying one $100 target
gets 97 shares and leaves $251.50 idle. A flat 1% does not describe IBKR Pro commissions (per share
with a per-order minimum — a few dollars for a whole rebalance), and the $100 minimum reserve already
covers them. Small correction: the plan's "$0.0035/share, $1 minimum" mixes IBKR's two schedules
(Tiered starts at $0.0035 with a $0.35 minimum; Fixed is $0.005 with a $1 minimum). The conclusion is
unaffected.

**What the plan misses.** Today that 1% is also, by accident, what covers the gap between the price
buys are sized at and the price they are budgeted at. Remove it and move to sizing at the ask, and
nothing covers that gap any more. So item 1 is only safe together with the budget check in C2.

### Item 2 — Remove `cash_reserve_percent`

**Verdict: valid — and the case is stronger than the plan makes it.** Two verified defects:

- **The reserve is spent anyway.** The buy pass scales buys *up* to use all available cash, which
  spends the reserve. Probe: an all-cash account with a 10% reserve still invests more than 98%.
  The setting does not do what its name says today, for any account.
- **It suppresses trades it should cause** (the plan's example, reproduced): with a 1% reserve, a
  position at exactly its target share of NLV is never trimmed, because sizing uses one denominator
  and the threshold check uses another. The spec (`rebalancing-logic.md` §2.7.3) describes this
  backwards.

Only one paper account uses a non-zero value. Delete it. **Migration needed** — see decision D10.

### Item 3 — Remove PDT protection

**Verdict: delete it, but correct the reason.**

The FINRA facts in the plan are right: Regulatory Notice 26-10 replaces the day-trading margin
rules, effective June 4, 2026, with a phase-in for brokers ending October 20, 2027. But IBKR's own
help pages (PDT Reset; Available for Trading) say: *"Your account may still be subject to existing
PDT rules during FINRA's transition period, which ends in October 2027."* A September 2026
broker-by-broker tracker (third-party) notes that IBKR's page stops short of saying it has switched
over. So "the rule is gone; the guard cannot protect you from anything" is not established.

Why deletion is still right:

- Every account in your `accounts.yaml` has it off; the code path is dead for you.
- PDT only bites margin accounts under $25,000 that make four same-symbol buy-and-sell round trips in
  five days. A rebalance only creates a day trade when the same account rebalances twice in a day and
  reverses a symbol. IBKR enforces the rule itself, server-side.
- The guard is a blunt "once a day" switch that fails open on a bad state file.

What to do with the change: tell community users in the release notes and README. The example
`accounts.yaml` ships with `pdt_protection_enabled: true`, so some installs rely on it. Also delete
`add_account_to_schedule` — its only code caller is the PDT path (`tools/schedule.sh` writes the file
directly) — unless the retry feature reuses it (it should not; see item 10).

### Item 4 — A "supports fractional shares" capability

**Verdict: valid as a fixed fact about the broker; not as a config setting.**

The fact is right: IBKR's TWS API does not accept fractional share quantities for stocks (IBKR's order
documentation; community reports of error 10243, "Fractional-sized order cannot be placed via API").
The code already assumes whole shares.

Push back on two parts of the plan's design:

- **"Driven by config, so it becomes a config flip."** It cannot. Supporting fractions needs different
  order code (a cash-amount order, an account permission, different quantity types). A config value
  that can be set to `true` without that code would only produce rejected orders. Make it a constant
  property of the broker class (`False` for IBKR). With one broker, it is honestly just a documented
  constant the planner receives.
- **"A persistent per-account needs-manual-action list."** That is new state on disk to avoid repeating
  one line. Report the leftover fraction in each run's summary instead; it is true every run until a
  human fixes it.

Agree with: never round a sell up; keep quantities as integers; group "targets below one share" into
one line. Note one policy change hidden in the plan: it moves buys from "round to nearest share"
(0.6 → 1 share) to "round down" (0.6 → nothing). The README promises *at least one share of every
target*. That is decision D6.

### Item 5 — Remove scaling and max-utilization; drop orders instead

**Verdict: valid — delete scaling, the adjustment pass, the utilization cap, the "early skip", and the
separate affordability filter, and replace them with one rule. But the rule is the cash guard, not a
formality, and it must budget at the limit price.**

Verified in favour of the change:

- Four mechanisms constrain one thing, with documented edge cases (negative factor, one-share floor,
  residual overage).
- Preview and live disagree. Probe: six one-share buys costing more than the cash available appear in
  preview; the live path would drop several (`calculate_rebalance` never applies the filter).

Two corrections (details in §4):

1. **Buys do not fit "by construction."** The plan's identity — the buy deficits add up to the cash —
   only holds if every target is traded. Positions that are slightly over target but inside the
   threshold band are not sold, and their excess has to come from somewhere. The trimming loop can
   therefore be asked to remove most of an order, not "a share or two". Test it as the primary
   safeguard, and log loudly when it trims more than a small fraction.
2. **Budget at the limit price.** See C2.

Two consequences to accept knowingly:

- **The smallest targets absorb every shortfall.** "Trim the lowest-weight order share by share, then
  drop it" means that in a real shortage the smallest positions (often hedges or new additions) get
  nothing while the large ones are fully funded. Today's scaling spreads the shortfall across all buys.
  This is what "drop orders instead of scaling" means; confirm it is what you want (D5).
- **The README becomes wrong.** "Cash usage maximization … scales up the buy orders to use the extra
  funds" and "at least one share is owned for each symbol" both stop being true.

### Item 6 — Replacement sets as a system artifact

**Verdict: the goal (validated at startup, never silently ignored) is right. Where the sets live is a
product decision with a community cost.**

Verified: an unknown or missing set name silently returns the original allocations
(`replacement_service.py:76-78`; probe). A missing file only logs a warning. A bad scale passes with
one warning-level log line.

The plan overstates one consequence. If the IRA set silently stops applying, the outcome depends on
the instrument: IBKR typically blocks leveraged/inverse volatility products in IRAs, so those buys
would be *rejected* — after the sells have executed. For commodity-pool ETFs such as DBC and UUP
(substituted to avoid partnership tax forms in IRAs, as I understand the set), orders would go through
silently. Both outcomes are bad; failing at startup fixes both.

The trade-off the plan waves away ("one ops person, a handful of sets"): this is a public community
repository. The README tells users to edit `replacement-sets.yaml`, and `docker-compose.yaml` mounts
it. Moving the sets into code takes that ability away, and an upgraded install would silently ignore a
customised file. If the sets move into code, startup must **refuse to run when an old
`replacement-sets.yaml` is still mounted**, with a message saying why. The alternative with the same
safety is to keep the file and validate it strictly at startup. Decision D11.

### Item 7 — Replacement logic bugs

**Verdict: all three confirmed against the real service.**

| Case | Input | Today's output | Problem |
|---|---|---|---|
| (a) target already held directly | UVXY 20% → VXX ×2, VXX 10%, SPY 70% | VXX 50 / SPY 50 | The direct VXX weight escapes the compression; a consistent answer is VXX 47.5 / SPY 52.5 |
| (b) scale below 1 | SVXY 20% → USMV ×0.5, SPY 80% | USMV 11.1 / SPY 88.9 | Normalising undoes the halving |
| (c) excess larger than the rest | UVXY 20% → VXX ×9, SPY 80% | VXX 95.7 / SPY 4.3 | Over-allocation hidden by normalising |

Agree with: consolidate first, then work with two totals; disallow scales below 1.0; validate rules at
load; log weights before and after replacement on every run.

**Disagree with "fail the account when the excess exceeds the non-replaced total."** With your
production rule (UVXY → VXX ×1.5), that happens for any strategy allocation above 66.7% UVXY. That is
a legitimate volatility-hedge regime, not bad data. Failing means every IRA account keeps its old
portfolio exactly when the strategy hedges. Probe: at 70% UVXY, today's engine produces VXX 97.2 /
SPY 2.8; at 60% it produces VXX 90 / SPY 10 (correct). The closest you can get to the strategy's
intent without leverage is to cap the replacement at 100% and give the rest zero. Fail only for
definition errors, which startup validation catches anyway. Decision D4.

### Item 8 — Cancel open orders

**Verdict: no gateway-wide cancel — the plan is right that `reqGlobalCancel` would cancel other
accounts' and other clients' orders. But the plan's "the existing loop is already correct and safe"
is wrong, and a per-account "cancel and confirm" operation is exactly what is needed.**

What the current step actually does (`rebalancer.py:43-62`, `client.py:453-494`):

1. It runs **after** the snapshot and the plan. If an old order fills in between, the plan is stale.
2. It **does not wait** for IBKR to confirm. If the run has no sells, new buys go out right after the
   cancel requests, while the old orders may still fill.
3. It **swallows errors in three places.** `get_open_orders` returns an empty list on any exception
   (`client.py:471-473`), `cancel_order` only logs (`:493-494`), and the caller catches and continues
   (`rebalancer.py:319-320`). A failure looks like "no open orders".
4. On a timeout or failure, the run exits and **leaves its own DAY orders working** at IBKR.

Recommended: one broker operation, "cancel this account's working orders and wait (bounded) until
IBKR confirms." Call it first, before the snapshot; fail the run if anything is still working; call it
again on the way out after a timeout or failure. This is also the precondition that makes item 10's
retry safe.

### Item 9 — Use IBKR's cash field

**Verdict: valid — USD `CashBalance`, not `AvailableFunds` or `TotalCashBalance` — but the plan
missed how that field can disappear.**

IB Gateway 10.47 and later has a setting that renames per-currency values to `$LEDGER-CashBalance`.
IBKR's release notes say it is **on by default for new installations**. Your own upgrade research hit
this in paper on November 7 and fixed it by turning the setting off (`docs/research/ib-gateway-upgrade.md`).
The code still reads only the bare name and silently uses **0** when it is missing
(`client.py:173-185`). With cash 0, every buy is skipped while sells still run, so the account drifts
into cash one run at a time. Community users installing fresh get the setting on by default. Fix:
accept both names (choose one authoritatively), and **fail the run when cash cannot be read**.

Two smaller points:

- **Freshness.** IBKR updates account values "when there is a change in a position, or every 3
  minutes." Cash should update with the sell fills, but nothing proves it arrives within the 1-second
  `post_completion_delay`. Under the plan's computed total (cash + marks), a late cash update
  understates the total, so the engine under-buys — the safe direction. Acceptable; say so in the
  spec, or wait (bounded) for the cash update after the sell leg.
- **Account type.** The plan says "your accounts are IRAs (cash accounts)" in one place and
  "margin-enabled without using margin" in another. It matters: it decides whether IBKR rejects or
  lends when buys exceed cash (C2). Please confirm the account types (D13).

### Item 10 — Requeue critical failures

**Verdict: valid feature, correctly scoped by the plan** (typed failure reasons, a separate queue
file, a maximum attempt count, and no retry unless the previous run's orders are confirmed gone).
Three additions:

- **Timing.** Your live signals arrive about eight minutes before the close (the signal scheduler
  publishes at close minus 8 minutes; the November 9 events arrived at 15:50:57 ET). DAY orders still
  unfilled at 16:00 are cancelled by IBKR and the leg fails. A close-time retry must start early
  enough to finish before 16:00.
- **Fix N2 first.** Without a market-hours check, a retry that runs after the close queues market
  sells for the next morning.
- **This is timing and concurrency code** (a scheduler, signal events and retries interacting).
  Under your standing rule it needs your explicit go-ahead, and it belongs after the planner and
  execution changes.

### Item 11 — One notification per round

**Verdict: the goal is right; the plan's mechanism does not exist for signal-driven runs.** The plan
says "aggregate after the `asyncio.gather`." That gather exists only on the market-open scheduler
path. Signal events arrive one per strategy channel and run independently (November 9: five events
over five seconds). Grouping those into one message needs a waiting window — new timing code.

Simple version with no timing code: one message per strategy event, covering all its accounts with
the warnings folded in (today: one or two per account). Escalate "sells done, buys failed" immediately,
as the plan says. Decision D12.

### Item 12 — Enough logging to investigate

**Verdict: valid.** The plan's diagnosis is right: no run identifier, interleaved processes, no record
of why a symbol was not traded, no record of which prices were used.

Push back on the remedy's size. A JSON file per account per run on the subscriber's disk means a new
writer, a schema to maintain, and unbounded growth (`data/` is a bind mount; nothing prunes it). One
structured JSON log line per run (inputs, decisions with reasons, orders, outcome), a run id on every
line, and the one compact human-readable plan line you already approved give the same answers. Docker
already rotates the logs (100 MB × 10). If you want files, add a retention limit at the same time.

---

## 3. The proposed interfaces

| Proposal | Verdict | Why |
|---|---|---|
| Broker connection | **Keep one, reshape it** | It already exists, and tests need a fake of it — a present second user. The plan's changes are right: `connect()` raises; statuses are normalised; the cache flags go. Add: keep the order handle returned when an order is placed, instead of looking orders up by number (N8); check the account is one the gateway manages; connect narrowly (S2). |
| Quote cache | **Do not build** | The cache exists only because the post-sell snapshot re-prices positions. With the planner taking prices as an input, the run fetches prices once and passes the same set to both legs (or fetches again, deliberately). Delete the cache and `price_cache_ttl_seconds`; no new abstraction. |
| Account list | **Concrete, not an interface** | The settled decision (strict validation at startup) is a loader that returns typed accounts. "We may move accounts behind an API later" is a speculative extension point. |
| Notifications | **Concrete** | One report object and one ntfy sender. No interface until a second channel exists. |
| Config, logging | **Agree: no interfaces** | Pass the typed config object into constructors; use `logging`. |
| Trade planner | **A pure function, not an interface** | One implementation. Its value is that it is pure, not that it is swappable. |

---

## 4. Corrections to `refactor.md`

Each of these changes what would be implemented.

**C1 — "Buys fit in the cash by construction" (§7.1, §2.5).** False. The sum of *all* target gaps
equals the cash, but the planner buys only the *positive* gaps. Positions over target inside the
threshold band are not sold, so the positive gaps exceed the cash by those excesses — up to about 0.5
points per such position. Probe: four 25% targets; three sit 0.45 points over (not sold); one is 1.65
points under; cash is 0.30% of the account. The planner wants 32 shares ($1,600.32) against $200 of
spendable cash. The loop must remove 28 of the 32 shares. Consequence: the trimming loop is the
safety-critical rule; it needs its own tests and a loud log line when it trims heavily.

**C2 — "Sizing at the limit price is self-inflicted" (§7.1).** Partly. Sizing at the ask does land
closer to target. But a limit buy can fill anywhere up to its limit, and a broker checks funds against
what the order *could* cost — very likely the limit price for a limit order (verify with an IBKR
"what-if" order in paper before relying on this). If quantities are sized at the ask and the budget
is also checked at the ask, the total limit value can exceed cash by up to 0.5% of all buys. In an IRA
the last order would likely be rejected after the sells executed; in a margin account the account
would borrow slightly. The plan's remedy ("reserve ≥ 0.5% of the largest order") is the wrong size:
every order can fill at its limit, so it would have to be 0.5% of *all* buys. Recommended: size at the
ask, check the budget at the limit. Cost: roughly 0.5% of the buy amount stays in cash on runs where
cash is the binding limit. Decision D2.

**C3 — "A zero snapshot cannot happen; connect awaits the account-value request" (§5.5).** Wrong for
this deployment. `ib_async` only makes that request for a login with exactly one account. Your login
holds eleven enabled live accounts (more counting disabled ones), so every connection instead
subscribes to account values for **every** account on the login (`ib.py:2049-2074`) — up to 121
subscriptions when all of them rebalance together — and any request that times out is only logged:
`raiseSyncErrors` defaults to false, and the code does not set it (`ib.py:2076-2096`). Your November 7
preflight recorded exactly such timeouts on a new paper account. When the values are missing, the
client returns NLV 0 and cash 0 (`client.py:173-185`), and the current calculator then **sells every
position** (N1). The plan's identity check is still worth having, but it is not "the only guard
needed."

**C4 — "No mutual exclusion; a manual run and a scheduled round can overlap" (§5.9, §2.3).** Wrong.
All three triggers share one executor that refuses a strategy already running
(`strategy_executor.py:26-30`); the check and the mark happen with no `await` in between, so they
cannot interleave. Same-account overlap cannot happen inside one container. The real defect is the
opposite: the second request is dropped (N3).

**C5 — "The PDT rule is gone."** See item 3: IBKR says accounts may still be subject to it until
October 2027.

**C6 — Normalisation contradicts itself.** §2.2 says "if an account should hold cash, the strategy
says so with weights summing to less than 1." §5.14 records "always normalise every account to 100%."
Both cannot hold. The spec (§2.5.4) says plain allocations MUST NOT be normalised. Your signal engine
rounds weights through a fixed number policy, so totals like 0.9999 are expected. Recommended: scale
small deviations (for example within 1%) to 100% with a log line, and fail the run on anything larger
as a data error. That gives up "cash by under-allocating"; if a strategy needs cash, give it an
explicit cash-like symbol. Decision D3.

**C7 — §4 pseudocode reintroduces `AvailableFunds`** (`spendable = min(cash − reserve,
available_funds)`), which §2.9 rejects. Use cash minus reserve only.

**C8 — "Add `build/` to `.gitignore`" (§5.13).** It is already there (`.gitignore:7`), and nothing
under `packages/*/build` is tracked. They are local build leftovers; deleting them is housekeeping,
not a repository change.

**C9 — Conflicts with settled decisions.** §9.6 proposes constructing the app inside `main()`; the
settled decision is to keep the module-level `app` (a test patches it). §5.11 lists "one allocations
request per account" as a defect; the settled decision is that this is intended.

**C10 — "Fractional support becomes a config flip" (§2.4).** See item 4.

**C11 — "Notifications aggregate after the gather" (§2.11).** Only on the scheduler path; see item 11.

**C12 — Account type is described two ways (§2.3 vs §2.9).** See item 9; decision D13.

---

## 5. Defects the plan does not mention

Ordered by severity. "Verified" means reproduced against the real code; "from code" means read
directly with no run needed; "plausible" means the mechanism is in the code but the broker-side
precondition is not proven.

**N1 — Missing account data makes the engine sell the whole account. Critical. Verified.**
If the account-value feed has not arrived (C3) or the NLV tag is missing, `get_account_snapshot`
returns NLV 0 and cash 0 without complaint. The calculator then sets every target to $0 and every
position looks 100 points away from target, so it plans to sell everything — sized at the bid from a
mid-price valuation, so it asks for one share *more* than held on large positions (2,001 of 2,000 SPY,
5,001 of 5,000 TLT in the probe). In an IRA, those oversized orders would likely be rejected, but
smaller positions whose sell size rounds exactly would be sold. In a margin account, everything is
sold plus a small short. No buys follow, because cash reads 0. Probability: needs a startup request
timeout — observed once, on November 7 in paper. Fix (small, independent of the refactor): fail the
run when NLV (or, after the refactor, cash) is absent; connect with `raiseSyncErrors=True` and only
for the account being traded (S2); check the account is in the gateway's managed-accounts list.

**N2 — Live runs outside market hours queue market sells for the next morning. High. From code plus
IBKR documentation.** Nothing checks whether the market is open before a live run. Orders are DAY
and regular-hours only; IBKR documents that such an order "submitted after regular trading hours …
will be held … and begin to work at the start of the next trading day." The sells then sit in
`PreSubmitted` (not finished), the wait times out after 300 seconds, the run fails and disconnects,
and the queued *market* sells execute at the next open, with no buys. Manual runs and any signal or
retry after 16:00 hit this. Fix: refuse live runs outside regular hours using the exchange calendar
already in the dependencies (and, with item 10, schedule them for the next open). Decision D8.

**N3 — Signals are dropped while a strategy is running. High. From code.** A second request for a
running strategy returns `already_running` and is discarded with an info-level log line
(`strategy_executor.py:26-28`; Ably, manual and scheduler paths). Concrete cases: a manual
`rebalance` for one account blocks that strategy's real signal for **all** its accounts until the next
signal; a scheduled round that collides with a running strategy counts it as failed and then clears
the schedule file, so it is not retried. Fix options: keep one pending run per strategy and start it
when the current one finishes (concurrency code — needs your go-ahead), or at minimum notify when a
trigger is dropped. Decision D9.

**N4 — The plan's budget change can cause rejected buys after sells. High, introduced by the plan.**
See C2.

**N5 — The cancel step is unconfirmed, in the wrong place, and fails open. Medium. From code.**
See item 8.

**N6 — Deposits smaller than the threshold band are never invested. Medium. Verified.** Ten equal
targets and a $4,000 deposit (3.8% of a $104,000 account): each symbol is only 0.38 points under, so
every buy is suppressed, today and under the plan. With ten equal targets, any deposit under about
5% of the account stays in cash until prices drift; with five targets, under about 2.5%. The README
promises cash is deployed. Simple rule: when cash above the reserve exceeds the band (say 0.5% of the
account), ignore the band for buys in that run. Decision D7.

**N7 — Bad allocation data is accepted. Medium. Verified.** `AllocationService` accepts a "success"
response with an empty list — the planner then sells every position — and accepts missing weights
(as 0), negative weights and duplicate symbols. A target with weight 0 is sold at
`round(mid value / bid)`, i.e. one share more than held on a large position (probe: 2,001 of 2,000).
Fix: validate at the boundary (non-empty, unique symbols, weights between 0 and 1, total within
tolerance — C6); sell a zero-weight target as a liquidation (exactly the shares held).

**N8 — Order status is looked up by order number alone. Low–medium. Plausible.** `get_order_status`
and `cancel_order` scan every order in the session for a matching order number (`client.py:475-514`).
At connect, `ib_async` loads *today's completed orders from all clients* into the same list. Order
numbers are unique only per client, and each account uses its own client id, so another account's
filled order can carry the same number as the order just placed. Probe (real `ib_async` wrapper): a
working sell is reported as `Filled`. Caveat: community reports say IBKR often returns order number 0
for completed orders, which would avoid the clash; not verified live. Fix, simpler either way: keep the
order handle returned by `placeOrder` and read its status, and stop loading completed orders and
executions at connect (unused; S2).

**N9 — Positions are keyed by symbol with no instrument-type check. Low. From code.** An option or
other non-stock position on a target's ticker overwrites the stock entry in the position map; a
non-stock position on a non-target ticker would be priced and sold as the stock. Fix: accept only
stock/ETF positions, and fail the run (the plan's §5.1 policy) on anything else.

**N10 — A crashed worker process breaks every later run. Low. From documented Python behaviour.**
If a pool worker dies abruptly, `ProcessPoolExecutor` becomes permanently unusable; every later event
fails while the container still reports healthy (even with the heartbeat fix, because the main loop
keeps ticking). Fix: on `BrokenProcessPool`, exit so Docker restarts the container.

**N11 — Event handler tasks are not kept. Low. From code.** `ably_service.py:143` fires
`asyncio.create_task(...)` without keeping a reference; Python's documentation warns such tasks can be
garbage-collected mid-run. Keep them in a set until they finish.

---

## 6. Other simplification opportunities

- **S1 — Delete the price cache** (see §3). It removes `use_cached_prices`, `use_cache`,
  `CachedPrice`, a config value, and the "fresh cash, stale prices" caveat in the spec.
- **S2 — Connect narrowly and fail closed.** `connectAsync(..., account=account_id,
  raiseSyncErrors=True, fetchFields=…)` with positions, open orders, and this account's values only.
  It is faster (one subscription instead of one per account on the login), removes the completed-order
  cache behind N8 and the blank-status records described in the upgrade research, and turns N1's
  timeout into a clean failure.
- **S3 — Cancel first.** Order the run as: cancel and confirm → snapshot → plan → sells → snapshot →
  plan → buys. Every plan then reflects a clean book.
- **S4 — One typed account snapshot with required fields.** Missing cash, a missing total (if kept),
  or an unpriced position is an error, never a 0.
- **S5 — Validate at the edges, trust inside.** Accounts (settled), allocations (N7) and replacement
  rules (item 6) are validated once at the boundary; the planner then has no defensive branches.
- **S6 — Market-hours check** (N2), using the calendar the scheduler already uses.
- **S7 — Optional:** the broker name is switched on in two places (`broker_factory.py` and
  `_create_rebalancer`) for a single broker. Keep one.

---

## 7. Decisions needed

Ordered by impact. Each has a recommendation; none is treated as approved.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Fix N1 as a small standalone safety patch before the refactor? | Yes |
| D2 | Budget check for buys: at the limit price, or at the ask with a larger reserve | Limit price (verify IBKR's funds check in paper first) |
| D3 | Weights that do not total 100% | Scale small rounding differences; fail on larger ones; no "cash by under-allocating" |
| D4 | Replacement heavier than the rest of the portfolio (e.g. >66.7% UVXY) | Cap at 100% instead of failing the account |
| D5 | Cash shortfall: trim smallest targets first, or spread across all buys | Your call — the plan's rule starves small/hedge positions in a real shortage |
| D6 | Buy rounding: nearest share or round down; keep "at least one share"? | Nearest share, with the trim loop as the guard |
| D7 | Deposits inside the threshold band | Add the cash-deployment rule in N6 |
| D8 | Live runs outside market hours | Refuse; with item 10, schedule for the next open |
| D9 | A signal that arrives while its strategy is running | Hold one pending run per strategy (needs your go-ahead), or at least notify |
| D10 | Existing `accounts.yaml` files with the removed fields | Accept and warn for one release, then reject |
| D11 | Replacement sets: in code or a strictly validated file | Your stated intent is code; if so, refuse to start when an old file is mounted |
| D12 | Notification grouping | One message per strategy event first; a time window only if still too noisy |
| D13 | Confirm account types (IRA cash or IRA margin; individual margin?) | Needed to pin down D2 |

---

## 8. Revised order of work

1. **Safety patch** (D1): N1 fail-closed snapshot, `$LEDGER-` parsing, managed-account check. Small,
   independent, shippable now.
2. **Characterisation tests** (the plan's step 0), seeded with the probes in §9.
3. **Input boundary:** strict `accounts.yaml` with migration (D10); allocation validation (N7, D3);
   replacement rules and validation (items 6–7, D4, D11).
4. **Pure planner:** delete `phase`, commission, reserve percentage, scaling, utilization cap; use the
   computed total; the trim loop with the limit-price budget (D2, D5, D6); the cash-deployment rule (D7).
   Rewrite the spec and README in the same change.
5. **Execution and broker:** cancel-first-and-confirm (item 8); order handles (N8); normalised
   statuses; narrow connect (S2); market-hours check (N2); cancel own orders on timeout or failure;
   delete the price cache (S1).
6. **Notifications and logging** (items 11–12).
7. **Retry queue and dropped-signal handling** (item 10, D9) — after your explicit approval, because
   both are concurrency code.
8. **Deletions:** PDT service and state files, dead models and helpers (settled list),
   `SubprocessManager` (settled).

---

## 9. Evidence

**Probes.** `/opt/data/workspace/zehnlabs-rebalancer-validation/` (WSL path, outside the repository).
They import the repository's packages read-only from `packages/*/src`, load the repository's
`config.yaml`, and need no broker or network. Run instructions are in that folder's `README.md`.
Result: `20 passed` (first pass); second pass adds `decided_planner_test.py` (21 with the invariant test; run with `-s` in this environment because pytest's output capture fails here). One deviation: pydantic 2.14 instead of the pinned 2.11.7, which has no wheel for
the local Python 3.14; nothing probed depends on the difference.

| Probe | Shows |
|---|---|
| `test_spec_worked_example_reproduces` | Control: the spec's worked example matches the code exactly |
| `test_zero_netliquidation_sells_every_position_and_oversells`, `zero_snapshot_probe_test.py` | N1, through the real client and calculator |
| `test_zero_weight_target_oversells`, `test_empty_allocation_list_liquidates_account` | N7 |
| `test_cash_reserve_percent_is_defeated_by_scale_up`, `test_cash_reserve_threshold_uses_other_denominator` | Item 2 |
| `test_preview_returns_buys_live_would_drop` | Item 5, preview vs live |
| `test_doc_7_1_fit_by_construction_is_false_…` | C1 |
| `test_doc_7_1_ask_vs_limit_arithmetic` | The plan's §8.11 arithmetic is right |
| `test_replacement_*`, `test_unknown_replacement_set_is_silent_passthrough` | Items 6–7, including the 70% UVXY case |
| `test_empty_status_is_treated_as_filled`, `test_filled_plus_unknown_status_mix_returns_success`, `test_not_found_waits_for_full_timeout_instead_of_failing` | The wait loop treats an empty or missing status as filled (plan §5.3) |
| `orderid_probe_test.py` | N8 mechanism, using the real `ib_async` wrapper |
| `deposit_probe_test.py` | N6 |

**Library facts** (`ib_async` 2.1.0): multi-account connect behaviour and swallowed timeouts
(`ib.py:2049-2096`); completed orders loaded into the shared order list (`wrapper.py:722-730`);
order key is (client id, order number) or the permanent id (`wrapper.py:431-438`); an order reported
as an error is marked `Cancelled` (`wrapper.py:1660-1672`); finished states are `Filled`, `Cancelled`,
`ApiCancelled`, `Inactive` (`order.py:269-271`).

**External sources.** IBKR TWS API — Account Updates ("unless there is a position change this
information is updated at a fixed interval of three minutes"); Per-Currency Account Value Prefix and
the 2026 TWS API release notes (`$LEDGER-`, on by default for new users); Architecture ("Up to 32
clients can be connected to a single instance"); Classic TWS Order Ticket, Time in Force (DAY orders
submitted after hours are held for the next session); IBKR Guides, Pattern Day Trader Reset
("may still be subject to existing PDT rules during FINRA's transition period, which ends in October
2027"); FINRA Regulatory Notice 26-10 (effective June 4, 2026; phase-in ends October 20, 2027).
Community, not authoritative: TWS API groups.io threads on order numbers being per client and
completed orders returning order number 0; r/interactivebrokers on error 10243; DeskTested's broker
PDT tracker.

**Not verified** (would need a paper session during market hours): IBKR's funds check for limit buys
(C2); whether completed orders carry their original order numbers (N8); how quickly `CashBalance`
updates after a fill (item 9).
