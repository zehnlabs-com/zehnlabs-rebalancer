# Refactor v3 — Implementation Review

**Reviewer:** independent offline review (Hermes agent), 2026-11-20.
**Scope:** `refactor/v3` (11 commits, branch point `main` @ `fcd8d48`) against `docs/refactor-prd.md`.
**Method:** PRD read in full; every changed file read; tests run in a clean Python 3.12
environment with ib_async 2.1.0; Appendix A recomputed independently; 6,000 random accounts
run against the implemented planner checking §7.7 P1–P7; 62 adversarial probes against a fake
IB and the fake broker connection (scripts in the validation workspace, not the repo).
No code was changed. No real Gateway was contacted; no real ntfy message was sent.

---

## Production correction, 2026-11-23

The review missed a production-blocking IBKR constraint. V3's
`StartupFetch.ACCOUNT_UPDATES` made every per-account connection use legacy
`reqAccountUpdates`; IB Gateway permits only one such account subscription across clients. A burst
of eleven live runs produced warning 2101 for ten accounts, followed by connection timeouts. The
connector now starts positions and open orders normally, then subscribes only its account through
`reqAccountUpdatesMultiAsync`. A two-account concurrency regression test models warning 2101 and
requires both connections to retain valid cash and NetLiquidation snapshots. Historical claims
below that the old exact flags were correct describe what this review checked at the time; they are
superseded by this correction and current PRD IB-1/IB-15.

---

## Follow-up fixes (Faisal-approved), 2026-11-20

| Item | Commit | Result |
|---|---|---|
| R-M3: request timeout vs IBKR's 11 s snapshot window | `e8b6a6f` | `ibkr.request_timeout_seconds` default **15 s** (code, `config.yaml`, AGENTS.md, PRD CFG-2); test `test_defaults_apply` |
| `ACCOUNT_DATA_MISMATCH` record lacked `state0`/`V0` | `e8b6a6f` | snapshot recorded before the 2% check; `test_nlv_mismatch_fails_live` asserts it (fails on the old code) |
| R-H1: `run_id` missing from worker log lines | `3f02623` | run adapter wraps the underlying logger; lines read `[strategy account run_id] PLAN …`; `test_run_lines_carry_strategy_account_and_run_id` (fails on the old code) |
| Duplicate worker log lines | `3f02623` | `_worker_logger.propagate = False`; probe: 0 root-handler copies; `test_worker_logger_does_not_propagate_to_root` |

Suite: **159 passed** (Python 3.12.16, ib_async 2.1.0, `--capture=sys`). Still open (low):
sell-leg failures record no `prices`; the earlier fix commit has no requirement IDs and no
implementation-log entry. Paper check still worth doing: real `reqTickers` snapshot timing on a
thin ETF and outside market hours.

---

## Re-validation of the fix commit `25597db` ("Refactor bug fixes"), 2026-11-20

**Result: not yet. Two of the fixes need another pass before paper checks.** H1 is only half
fixed: log lines are no longer dropped, but they still carry no run ID (`-`). The M3 fix (a 10 s
bound on IBKR requests, which this review suggested) is shorter than IBKR's documented 11-second
snapshot window, so it can fail runs that would otherwise have succeeded. M1, L1–L5 are fixed.
M2 is fixed for the buy-leg case. No money path changed for the worse: the planner, the cash
rules and the order lifecycle re-verified clean.

**Re-run evidence.** I exported the commit with `git archive refactor/v3` and ran it in the same clean
3.12.16 venv (ib_async 2.1.0). Results:
- `pytest -q --capture=sys tests`: **157 passed**.
- `verify_planner.py`: Appendix A exact, **0 failures in 6,000 random accounts**.
- `probe_v3.py`: 62/63 pass. The one "fail" is my probe's own quantity expectation; the reason is
  now `ORDER_PLACEMENT_FAILED` as required.
- `probe2.py`: 0 failures.
- `revalidate_25597db.py` (new): results below.

| Finding | Status after `25597db` | Evidence |
|---|---|---|
| **H1** logs dropped | **Partly fixed. New residual R-H1 (high).** No more logging errors, and bare worker lines get `-` defaults. But every run line still shows `[s U1 -]`, so **`run_id` is still missing**. | `revalidate_25597db.py`: `[INFO] [s U1 -] PLAN sells: XLE -100 …`. Cause: `rebalance.py:186` still wraps `deps.logger`, which is itself a `LoggerAdapter`. The inner adapter's `process()` replaces `extra` with its own `{strategy, account_id}` again. The new formatter defaults now hide that silently. |
| M1 `place()` crash classification | **Fixed** | Probe: mid-leg `RuntimeError` now gives `ORDER_PLACEMENT_FAILED`, and the fill is still recorded |
| M2 "BUY orders did not complete." / failed-run record | **Fixed for the buy leg** | Buy rejected after sells: record has `planned_sells`, `planned_buys`, `V1`, `prices`. The notification says `Already done: SOLD 100 XLE @ $84.95; BUY orders did not complete.` |
| M3 unbounded IBKR awaits | **Fixed as written. New risk R-M3 (high, needs a paper check).** | See R-M3 |
| L1 retry all symbols | **Fixed** | Probe: calls `[SPY, THIN] → [THIN] → [THIN]` |
| L2 duplicate client IDs | **Fixed** (all pairs collected) | Code read, `accounts.py:173-180` |
| L3 run-id suffix | **Fixed** (`new_run_id` used by parent, scheduler, worker) | Code read |
| L4 non-dict allocation items | **Fixed** (fetch fails "invalid response") | Code read, `allocations.py:83-86` |
| L5 boolean weights | **Fixed** | Code read, `targets.py:49` |

### R-H1 (high): `run_id` still missing from every worker log line

**Smallest fix.** At `rebalance.py:186`, wrap the underlying logger, not the adapter:
`logging.LoggerAdapter(getattr(deps.logger, "logger", deps.logger), _extra)`.

**Related (low).** Every worker line is printed twice. The `worker` logger propagates to the root
handler that the parent's `basicConfig` sets up and the forked worker inherits. The probe counted
one copy in each handler. The first review missed this because the worker copy was being dropped.
Fix: `_worker_logger.propagate = False`.

### R-M3 (high, hypothesis backed by vendor documentation): the 10 s bound is shorter than IBKR's snapshot window

`quotes()` now wraps `reqTickersAsync` in `asyncio.wait_for(…, request_timeout_seconds)`
(default **10 s**, `connection.py:234-236`). In ib_async 2.1.0, `reqTickersAsync` only returns when
every snapshot request has received `tickSnapshotEnd` (`wrapper.py:1108-1109` → `_endReq`).
IBKR's TWS API documentation says a snapshot returns *"the currently available market data once
before a tickSnapshotEnd event is sent 11 seconds later … in some cases values may not be
returned for all tick types"* (interactivebrokers.github.io/tws-api/md_request.html).

**Hypothesis:** any snapshot that waits the full window can now time out at 10 s. That covers
thin ETFs with no bid, symbols missing a tick type, and previews outside market hours, which the
PRD allows at any time. The result would be `PRICING_FAILED`: the run fails, a live run is queued
for retry, and the last/close/bar fallback chain (IB-8) never gets a chance to run. This fails
safe (no orders), but it can block runs the PRD meant to succeed. It is the exact thin-ETF case
the fallback chain was designed for. The same 10 s bound also replaces ib_async's own 60 s
default on `reqHistoricalDataAsync`, the last-resort price source.

**This review recommended the bound in M3** without accounting for the 11-second snapshot window.
That is my miss, not the implementer's.

**Smallest fix (needs your call):** give the market-data calls a bound above IBKR's window. For
example, `request_timeout_seconds + 11` for `reqTickersAsync`, and leave
`reqHistoricalDataAsync` on ib_async's own 60 s timeout. Keep 10 s for qualification and open
orders. A paper check can confirm the real timing.

### Other residuals (low)

- **The fixes have no regression tests.** The commit touches no test file (157 tests before and
  after), so H1, M1, M2, M3 and L1–L5 are proven only by this review's workspace probes. Add at
  least: a log-format test through the real worker adapter (it would have caught R-H1), the
  mid-leg `place()` crash, the buy-leg failure record and notification line, and the
  bounded-timeout mapping.
- **`ACCOUNT_DATA_MISMATCH` runs record `state0: null, V0: null`.** The record is filled at step
  11, after the 2% check fails at step 10, so the one failure where you most need the snapshot
  doesn't record it. The first review missed this. Fix: move the `state0`/`V0` `record.update`
  above the check.
- **Sell-leg failures still record no `prices`.** `q0` is known but only stored at step 16.
- **The commit message has no requirement IDs** (PRD §0.6), and `docs/refactor-implementation-log.md`
  has no entry for the fix commit (§17.3).

**Needs a paper check (add to the list below):** how long `reqTickers` snapshots take on your
subscriptions for a thin ETF, and for a preview outside market hours. This decides the R-M3 bound.

---

## Result first (original review of `63568b9`)

**Ready for paper checks.** The money paths are solid: the planner reproduces every Appendix A
number exactly, P1–P7 and the BUY-1 cash invariant hold over 6,000 random accounts, the account
run fails closed on every missing-data case I could construct, cash after sells is budgeted
conservatively (`min` rule, reserve from V0, buys budgeted at the limit price), and all thirteen
"easy to get wrong" items from §17.2 check out in the code. One defect should be fixed before
you rely on the logs: **every log line a run produces fails to format and is thrown away**
(missing `run_id` field — finding H1), so the per-run investigation logging the PRD requires
does not actually reach `docker logs`. Three smaller deviations (failure-reason classification
for a mid-leg `place()` crash, the unreachable "BUY orders did not complete." line, and a dead
`request_timeout_seconds` key) are worth fixing or consciously accepting; none of them can lose
money. Tests: 157 passed in a clean 3.12.16 venv (command below). `docker compose build` could
not be re-verified here (no Docker daemon in this WSL session).

---

## Findings

Severity: **critical** = can lose money or trade wrongly; **high** = wrong behaviour a user
will notice; **medium**; **low**.

### H1 (high) — Every worker log line fails to format; the run's logs are lost

**What is wrong.** `rebalance_account` wraps the per-account adapter in a second
`LoggerAdapter` (`event-broker/app/rebalance.py:177`:
`adapter = logging.LoggerAdapter(deps.logger, {"run_id": run_id})`, where `deps.logger` is
already a `LoggerAdapter` carrying `strategy`/`account_id` — `trading_executor.py:96-99`).
`LoggerAdapter.process` **replaces** `extra`, it does not merge, so the record that reaches the
worker handler carries `strategy`+`account_id` but **no `run_id`**. The worker's formatter
requires all three (`trading_executor.py:26`:
`[%(strategy)s %(account_id)s %(run_id)s]`), so every `adapter.info/warning/exception` call
from a run — the raw/scaled/targets lines, `state0`, the `PLAN sells:`/`PLAN buys:` lines,
per-order states, the outcome — raises
`ValueError: Formatting field not found in record: 'run_id'`, the line is **dropped**, and a
~25-line traceback is printed instead. The same happens for the direct
`_worker_logger.error(...)` calls (`trading_executor.py:114,150`) and for
`IBKRConnection._log.warning` (`ibkr_connector/connection.py:144`).

**How I showed it.** `v3review/logging_probe.py` (workspace) reconstructs the exact adapter
nesting against the committed code; output:

```
--- inner.info (as in rebalance_account) ---
--- Logging error ---
  ...
KeyError: 'run_id'
...
ValueError: Formatting field not found in record: 'run_id'
```

**What it breaks.** LOG-1 (per-run INFO logging with the bound `run_id`) does not work in
practice; t docker logs you get tracebacks instead of the plan lines. It does not affect
trading, notifications, or records (the run record in `data/runs/` is unaffected).

**Smallest fix.** Use one adapter per account carrying all three fields
(`{"strategy": ..., "account_id": ..., "run_id": ...}`) — i.e. build the run adapter inside
`rebalance_account` by copying `deps.logger.extra` and adding `run_id`, or give the worker
formatter defaults (`%(run_id)s` → a `logging.Formatter` subclass with defaults).

### M1 (medium) — `place()` raising a non-`OrderPlacementError` mid-leg is misclassified as `INTERNAL_ERROR`

**What is wrong.** ORD-2: "If `place()` raises during a leg … fail with
`ORDER_PLACEMENT_FAILED`." The run only catches `OrderPlacementError`
(`rebalance.py:428,521`), and `IBKRConnection.place` does not wrap transport errors
(`connection.py:286`: `ib.placeOrder(...)` unguarded). A `RuntimeError`/socket error raised by
`place()` falls through to the `except BaseException` handler and the run reports
`INTERNAL_ERROR` ("Internal error: RuntimeError: …").

**How I showed it.** `v3review/probe_v3.py` → check `RUN-place-midleg`: a fake connection whose
second `place()` raises `RuntimeError` yields
`outcome FAILED, failure_reason INTERNAL_ERROR`; the first order's fill **is** correctly
recorded in `executed` (RUN-1 holds).

**Impact.** Notification kind is `FAILED_WILL_RETRY` either way and the fills are recorded, so
no money risk; but the user-facing message says "Internal error" instead of a placement
problem, and the reason recorded in the run file is wrong.

**Smallest fix.** In the two leg loops, catch `Exception` and map anything that is not
`OrderPlacementError` to `ORDER_PLACEMENT_FAILED` — or wrap the body of `IBKRConnection.place`
in `try/except Exception → OrderPlacementError`.

### M2 (medium) — The failure notification can never say "BUY orders did not complete."; failed runs lose their planned-orders record

**What is wrong.** `_what_happened` appends `"; BUY orders did not complete."` only when
`result.planned_buys` is non-empty (`notification_service.py:128-129`). But `planned_buys` is
extended **only** on the preview and success paths (`rebalance.py:404-405, 534-535`); every
failure path leaves it empty. The PRD's §11.3 example ("Already done: SOLD 40 XLE @ $84.97 …
; BUY orders did not complete.") describes exactly the sells-filled-buys-failed case, which is
therefore unreachable. Related: on failure, `finish()` writes the still-empty
`planned_sells`/`planned_buys` into the run record (`rebalance.py:229-230`), and `prices`,
`V1`, `trimmed` are never set — so a failed run's record cannot tell you what the run was about
to do (REC-2 says the planned orders are recorded "for preview + record").

**How I showed it.** Code path read; probe `RUN-partial-timeout` shows a buy-leg timeout result
whose record has `planned_sells: []` even though a sell plan existed.

**Smallest fix.** In `finish()` (or just before each `fail()` after step 10), populate
`planned_sells`/`planned_buys`/`prices` from what the run has computed so far. The
"BUY orders did not complete." line then appears naturally for buy-leg failures.

### M3 (medium, hypothesis for the residual risk) — `ibkr.request_timeout_seconds` is a dead key; several awaits have no timeout

**What is wrong.** The PRD's CFG-2 defines `ibkr.request_timeout_seconds`, and the schema
accepts it, but nothing reads it (old code set `ib.RequestTimeout`).
`IBKRConnection` awaits `qualifyContractsAsync`, `reqTickersAsync`, `reqAllOpenOrdersAsync`
and `reqHistoricalDataAsync` without a timeout. In ib_async 2.1.0,
`reqTickersAsync` does `await asyncio.gather(*futures)` with no `wait_for`
(`ib.py:2196`), and `RequestTimeout` only applies to the *sync* `util.run` path
(`ib.py:428`). IBKR normally terminates these requests with an error message (which resolves
the future), so a hang is not the expected behaviour — but a silently stalled Gateway (e.g.
during its nightly restart with a half-open socket) is the one scenario where a worker could
hang forever: the strategy stays in `running`, all later signals are dropped without
notification (SCH-6 turns the morning run into SKIPPED_FINAL), and the heartbeat stays green.

**Evidence.** `git grep request_timeout_seconds` → only `loader.py`/`models.py`; ib_async
source cited above. The hang itself is a **hypothesis** (needs a paper/soak check; listed
below). Not a regression: the old code's `RequestTimeout` did not cover the async paths either.

**Smallest fix.** Wrap the four awaits in `asyncio.wait_for(..., cfg.request_timeout_seconds)`
and map `TimeoutError` to the existing error types.

### L1 (low) — `quotes()` re-requests **all** symbols on every retry round

IB-8 says "retry (only those symbols)". `connection.py:216-231` rebuilds the full ticker
request each attempt, so 12 symbols × 10 retries = 120 snapshot requests when one thin ETF has
no bid. Cost is request volume and log noise (IBKR pacing allows it, but it wastes data
credits). Fix: request only the still-missing contracts on later attempts.

### L2 (low) — Duplicate client-ID check reports only the first pair

`accounts.py:172-180` `break`s after the first duplicate-client-ID problem, so ACC-2's
"collects all problems and fails once, listing them all" is only partially met for this case.
Fix: collect all pairs.

### L3 (low) — Parent-built results use run IDs without the 4-hex suffix

§8.1 step 1 defines `run_id = <stamp>-<account>-<4 hex>`. The parent-side synthetic results
(`strategy_executor.py:377`, `scheduler_service.py:210`, `trading_executor.py:57`) omit the
hex part. Cosmetic/traceability only; the run file dedupes on `run_id` anyway. Fix: append
4 hex chars.

### L4 (low) — Allocations client silently drops non-dict allocation items

`allocations.py:527-528` `continue`s when an item is not a dict. A malformed body would then
produce a short weight list; the 1% check usually catches that, but a single small dropped
item could pass validation and silently skew the targets. Failing the fetch (like a missing
`allocation`) would be stricter. (The parse-level errors — non-numeric `allocation`,
non-JSON body — do fail the run correctly.)

### L5 (low) — `validate_and_scale` accepts `True`/`False` as weights

`targets.py:233` `isinstance(weight, (int, float))` — `bool` passes. Only reachable if the
API ever sends JSON booleans; harmless today, one-line fix (`type(w) is not bool`).

### Environment note (not a code finding)

Pytest's default output capture is broken in this WSL review environment for *any* suite
(reproduced with a two-test dummy suite; `FileNotFoundError` in `capture.py` at teardown).
`--capture=sys` (or `-s`) is unaffected. The recorded run below uses `--capture=sys`.

---

## Test environment and results (step 2)

- Python **3.12.16** (installed via `uv`, since the system Python here is 3.14 and the repo's
  own `.venv` is a Windows venv). Clean venv at `/opt/data/cache/scratch/rb312`; the four local
  packages installed editable from the committed tree (exported with `git archive refactor/v3`
  to `/opt/data/cache/scratch/rb_v3`, so the review ran against the commit, not the working
  dir); runtime deps from `event-broker/requirements.txt` + `pytest==8.4.2` (`>=8,<9`) + httpx.
- **ib_async 2.1.0** verified (`ib_async.__version__`), APScheduler 3.11.3, pydantic 2.11.7,
  exchange_calendars 4.13.3.
- Command: `PYTHONPATH=event-broker python -m pytest -q --capture=sys tests`
  → **157 passed, 63 subtests passed** (30.5 s). (With default capture the run dies at teardown
  in this environment — see the note above; with `-s` it is also fully green.)
- `docker compose build event-broker`: **not verified here** — Docker is installed in this WSL
  session but the daemon is not running and the compose plugin is absent. The implementers
  report a successful build; treat as their claim.

## Independent recomputation (step 4)

- `prd_reference.py` run: A1–A6, B (V0 104,031.45; sells XLE 100; reserve 260.0786;
  cash_base 10,995; V1 104,026.45; buys GLD 22 ≤ 251.35 / SPY 8 ≤ 603.10 / TLT 4 ≤ 90.50;
  trimmed GLD 1) and C (499/149/77) all match the PRD and the implementation's tests.
- My own script (`v3review/verify_planner.py`) drives the **implemented** planner:
  - Appendix A.1 rows A1–A10, A.2, A.3, A.4 — all exact (including the 0.3700-share fractional
    note, reserve 260.078625, budget 10,734.92, and A.4's CASH-3 note).
  - **6,000 random accounts** (1–12 targets, prices $2–$1,200, spreads 0.01%–1%, fractional
    leftovers, non-target holdings, cash $0–$200k, cash figure lagging at 98% of proceeds):
    P1 (no sell exceeds held whole shares), P2 (positions never negative), P3 (quantities
    whole and > 0), P4/BUY-1 (`Σ qty×limit ≤ budget + EPS` and post-fill cash ≥ reserve −
    0.01), P5 (sell plan has no buys and vice versa), P6 (targets sum to 1 within 1e-9),
    P7 (identical inputs → identical plans) — **0 failures**.
  - Weight-0 target held → sold exactly `floor(held)`; buy with no bid but an ask → bought;
    `round_quantity(99.9999999999) → 100`, `99.999999 → 99`, `0.9999 → 0`.

## Adversarial probes (step 5) — summary

`v3review/probe_v3.py` (62 checks, fake `ib_async.IB` + the repo's fakes) and
`v3review/probe2.py` (executor/scheduler/notifications/records) — all pass except the
documented findings:

- **§17.2, all thirteen items:** verified (1% before replacements [A1 passes at all];
  `fetchFields` exactly `POSITIONS|ORDERS_OPEN|ACCOUNT_UPDATES` with `account=` and
  `raiseSyncErrors=True`; empty/unknown status → WORKING; statuses read only from stored
  `Trade` objects; missing cash/NLV never defaulted to 0; buys budgeted at the limit; running
  mark released before notifications; SCH-7 removes only processed IDs; scheduled failures
  final; `positions_after` = state0 − fills; round-down everywhere; workers never call
  `get_config`/`os.getenv` (grep clean); no cancel on failure, no `reqGlobalCancel`).
- **Missing/partial account data:** missing cash tag, missing NLV, non-finite values →
  `AccountDataError` (fail closed); both cash tags present → `$LEDGER-` preferred; empty
  positions with large NLV → 2% cross-check fails live; NLV ≤ 0 with V0 > 0 → mismatch;
  non-STK and negative positions → `AccountDataError`; negative cash (margin) parses.
- **Order lifecycle:** rejection mid-leg → `ORDERS_FAILED` with message, no buys; partial fill
  then timeout → `ORDERS_TIMED_OUT`, fills listed ("SOLD 30 of 40"-style data present in the
  result); `place()` crash mid-leg → fills recorded, reason misclassified (M1); own vs foreign
  classification correct incl. own orders from an *earlier* connection (same client id);
  cancel not confirmed → `CancelNotConfirmedError` before the snapshot.
- **Cash:** `cash_base = min(reported, state0+fills)` verified with a lagging cash feed;
  reserve computed once from V0; budget ≤ 0 → no buys + CASH-3 note (A.4).
- **Targets:** 1% check on raw weights before replacements; empty list → sells everything;
  zero weights dropped and held symbol sold; duplicates after upper-casing rejected; UVXY 70 /
  SPY 30 → VXX 100%; VIXY+UVXY both map to VXX and add (A.4).
- **Retry/scheduling:** every §10.5 row mapped correctly (probe + `test_each_10_5_row`);
  scheduled failure → `FAILED_FINAL`, never re-queued; SCH-3 malformed file untouched; SCH-7
  keeps IDs added during the job; misfire 300/coalesce/max_instances passed; scheduler
  disabled → `FAILED_FINAL` + RTY-1 line, nothing queued; running strategy → dropped trigger,
  no notification (signal/manual), `SKIPPED_FINAL` (scheduled).
- **Executor:** BrokenProcessPool → `WORKER_CRASHED` results + pool replaced; other exceptions
  → `INTERNAL_ERROR`, pool kept; fork context asserted; mark released before notifications
  (asserted inside a notifier fake).
- **Notifications:** one per account per run; 3,500-byte cap with the run-id trailer; masking
  (`U***456`); correct title/tags/kind; 10 s timeout; failures logged not raised. No secrets in
  any message (error messages carry host:port and status codes only).
- **Startup:** strict `config.yaml`/`accounts.yaml` messages (verbatim per §5.1/§5.2, tested);
  old keys refused incl. disabled accounts; strategy check stops startup; heartbeat file
  touched every loop and the Dockerfile healthcheck **can fail** (stale mtime → exit 1,
  verified by running the exact healthcheck command).
- **Market clock:** real session times; 15:59 open / 16:00 closed; half days close 13:00 ET
  (2026-11-27, 2026-12-24 verified); Thanksgiving closed; `next_scheduled_run` crosses
  weekends correctly (Fri 20:00 → Mon 09:35; Fri 09:00 → same-day 09:35).

## Scope check (step 6) — changes no PRD requirement asks for

| Change | Verdict |
|---|---|
| `RUNS_DIR` env override + `RunRecords(directory)` param | Harmless (testability); recorded deviation; no new config schema key |
| RTY-1 line delivered by appending a note to the result | Harmless; documented deviation; keeps `NotificationContext` shape |
| Extra docs: `docs/adr/0001-0008`, `docs/agents/*`, `CONTEXT.md` | Harmless (documentation) |
| `event-broker/app/models.py` deleted in full (PRD asked only for `PDTCheckResult`) | Harmless; remaining models had no consumers; logged |
| `event-broker/app/trading/` package removed entirely (incl. empty `__init__.py`) | Harmless; §4.1 has no `trading/` |
| `ib.RequestTimeout` no longer set (old code set it) | See M3 — the key is dead; residual hang risk is hypothetical |
| Dead helpers `_find_account_by_id` / `_get_strategy_accounts` in `main.py` | Harmless dead code; could be deleted |
| Legacy test `test_account_grouping_preserves_mode_enabled_and_strategy_filters` removed | Acceptable; not in §16.3's keep-list; replacement coverage exists |
| Dockerfile: only the healthcheck changed (BOOT-2), Python version untouched ✓ | As required |

Nothing rises to "must revert".

## Removals (step 7)

PRD §14 search re-run over tracked source (excluding CHANGELOG/README/docs): every name is
gone. Remaining hits are only (a) the CFG-3/ACC-2 rejection messages that must name the keys,
(b) the new `minimum_cash_reserve_percent` key (substring match), and (c) upgrade-note prose in
AGENTS.md/CONTEXT.md/accounts.yaml.example. `Trade` in `ibkr-connector/connection.py` refers
to ib_async's `Trade` object (IB-9/10), not the deleted pydantic model. **No leftovers.**

## Docs (step 8)

README, AGENTS.md, `docs/rebalancing-logic.md` and CHANGELOG checked against the code: no
material mismatches found. The two-line "How It Works" intro still says "while maximizing the
use of available cash" (pre-v3 phrasing; the reserve and trimming rules above it are correct) —
cosmetic. README's "Notifications include your account numbers" is true but they are masked
(`U***456`) except the full ID in the FAILED_FINAL rebalance command, which §11.3 itself
specifies.

## Old bugs (step 9) — status

| Bug | Status | Evidence |
|---|---|---|
| N1 missing data → sells everything | **Fixed** | IB-2/3/4 raise on missing/non-finite cash/NLV (probes `IB2-*`, `IB3-*`, `IB4-*`); run fails `ACCOUNT_DATA_UNAVAILABLE`; no 0-defaults anywhere |
| N2 live runs outside hours | **Fixed** | Step 4 parent check + EXE-3 + in-worker guard; MARKET_CLOSED queues, no orders placed (probe `RUN-closed`) |
| N3 signals dropped while running | **As decided** | Dropped trigger logged, no notification (signal/manual); scheduled → `SKIPPED_FINAL` (probe `EXE-dropped`, `test_skipped_when_strategy_already_running…`) |
| N4 buys rejected after sells | **Fixed** | Budget at limit price; P4/BUY-1 verified over 6,000 accounts |
| N5 cancel unconfirmed/wrong place/fails open | **Fixed** | Cancel first (live), waits for confirmation, fails the run otherwise (probe `RUN-cancel`, `IB11-*`); `ORDERS_OPEN` fetched (probe `IB1-fetchFields`) |
| N6 deposits never invested | **Fixed** | Threshold band removed; happy-path probe buys to full target |
| N7 bad allocation data accepted | **Fixed** | TGT-1 validation; empty list = intended sell-all; weight-0 sold at exactly held shares |
| N8 order status by order number | **Fixed** | Statuses read only from stored `Trade` objects (IB-10); completed orders/executions never fetched (IB-15) |
| N9 non-stock positions | **Fixed** | IB-6 fails the run on non-STK / negative (probes) |
| N10 crashed worker breaks pool | **Fixed (per PRD)** | BrokenProcessPool → `WORKER_CRASHED` + new pool (probe `EXE-broken`) |
| N11 unkept event tasks | **Fixed** | SIG-1 task set + `add_done_callback(discard)` (`ably_service.py:39,115-117`) |
| R1 1% check before replacements | **Fixed** | `rebalance.py` step 3 order; A1 passes only because of it |
| R2 overflow fitting | **As specified** | REP-3 implemented verbatim (protect, then shrink ordinary); A1–A4 exact |
| R3 slow notification drops signals | **Fixed** | Mark released before post-processing (probe); ntfy 10 s timeout (NTF-3) |
| R4 two messages / size limit | **Fixed** | One message per result; 3,500-byte cap with trailer |
| R5 retry list handling | **As specified** | Single `scheduled.json`, atomic add/remove helpers, only processed IDs removed, success removes (probe `SCH7-survives`, `RTY2-*`) |
| R6 misfire/grace | **Fixed** | `misfire_grace_time=300, coalesce=True, max_instances=1`; APScheduler `<4` pinned |
| R7 own-order visibility | **Fixed** | `ORDERS_OPEN` in `fetchFields` (probe); own = client-id match; own orders from an earlier connection are found and cancelled (probe `IB11-earlier-conn`) |
| R8 post-sell cash | **Fixed** | `min(reported, state0+fills)` (probe `RUN-cash-lag`), bounded wait, WARNING only |
| R9 fresh prices for buys | **Fixed** | Step 15 fetches `quotes1` before `plan_buys` |
| R10 second pass buys only | **Fixed** | `plan_buys` cannot emit sells (P5) |
| R11 real close times; previews exempt | **Fixed** | exchange_calendars session close incl. half days (verified); preview ignores market hours |
| R12 fork context; Python 3.12 | **Fixed** | Explicit `get_context("fork")` (probe); Dockerfile Python untouched |
| R13 failure says what happened | **Fixed** (with M2's unreachable line) | RUN-1 fills recorded and rendered (`SOLD n of m` verified) |
| R14 strict accounts checks | **Fixed** | Whole-file validation incl. disabled/other-mode; duplicate account IDs; duplicate client IDs among active (one pair only — L2); Faisal's own file still to edit (ACC-4) |
| R15 NLV cross-check | **Fixed** | 2% check, live fails / preview warns (probes `RUN-nlv-mismatch`, `RUN-nlv-zero`) |

## Traceability table (step 3)

Legend: ✔ implemented · ◐ partly/deviates · test = repo test proving it (probe = my workspace
probe). "none" = no automated proof.

| Req | Status | Where | Test | Comment |
|---|---|---|---|---|
| SCOPE-1 | ✔ | planner (no band), config gate | `test_each_removed_key_message` | |
| SCOPE-2 | ✔ | removed | `test_each_removed_key_message` | |
| SCOPE-3 | ✔ | `planner.plan_buys` trim | `test_cash_shortfall_trims_lowest_weight` | |
| SCOPE-4 | ✔ | `round_quantity` | `test_edges` | README promise removed ✓ |
| SCOPE-5 | ✔ | `TradingConfig`, `rebalance.py:364` | `test_reserve_and_buys` | |
| SCOPE-6 | ✔ | deletions | §14 search | |
| SCOPE-7 | ✔ | `replacement_sets.py`; compose mount removed | `test_removed_sections`, Appendix A | |
| SCOPE-8 | ✔ | `targets.validate_and_scale` | `test_each_...`/A7-A10 | |
| SCOPE-9 | ✔ | steps 4/12/17, EXE-3, scheduler file | §10.5 row tests | |
| SCOPE-10 | ✔ | `config.yaml`, `SchedulerConfig` | `test_shipped_config_loads` | |
| SCOPE-11 | ✔ | steps 6-7, `IBKRConnection` | `test_cancel_*`, `test_foreign_order_stops_before_orders` | |
| SCOPE-12 | ✔ | `loader.py`, `accounts.py` | config/accounts suites | |
| SCOPE-13 | ✔ | one send per result | `test_one_post_per_send_with_timeout_and_headers` | |
| SCOPE-14 | ✔ | `run_records.py` | `test_append_*`, `test_prune_*` | |
| SCOPE-15 | ✔ | `IBKRConnection.account_state` | `test_no_cash_reported_is_error` etc. | probes confirm |
| CFG-1 | ✔ | all models `extra="forbid"` | `test_unknown_nested_key` | |
| CFG-2 | ✔ | `models.py`, `config.yaml` | `test_shipped_config_loads`, `test_defaults_apply` | `request_timeout_seconds` accepted but unused → M3 |
| CFG-3 | ✔ | `loader.py:270-348` | `test_each_removed_key_message`, `test_removed_sections`, `test_all_offending_keys_listed_together` | messages verbatim |
| CFG-4 | ✔ | `loader.py:376` | `test_shipped_config_loads` (indirect) | no secrets in model ✓ |
| CFG-5 | ✔ | `main.py:40-42` (exit 1) | `test_startup_failure_does_not_start_scheduler_or_manual_watcher` | |
| ACC-1 | ✔ | `accounts.py:27-67` | `test_invalid_account_id`, `test_client_id_is_digits_of_account_id` | |
| ACC-2 | ✔ | `accounts.py:96-185` | accounts suite | duplicate client-ID reports one pair (L2); pydantic message embedded verbatim (multi-line) |
| ACC-3 | ✔ | `accounts.py:70-88` | `test_registry_filters_active_mode` | |
| ACC-4 | ✔ | `accounts.yaml.example` | `test_example_file_loads` | Faisal's own file untouched (his action) |
| ENV-1 | ✔ | `settings.py`, `trading_executor.py` | `test_missing_api_key_fails` | `_get_environment`/env copying removed ✓ |
| ENV-2 | ✔ | `RuntimeSettings.__repr__` | `test_runtime_settings_masks_api_key_and_ignores_realtime_key` | `str()` would show the key but nothing logs the object |
| §6.1 | ✔ | `broker_connector_base/models.py` | `test_broker_connector_base.py` | `slots=True` from the prose omitted; harmless |
| §6.2 | ✔ | `connection.py` ABC + `async with` | `test_abc_cannot_be_instantiated_directly`, `test_async_with_connects_and_closes` | |
| §6.3 | ✔ | `errors.py` | `test_all_errors_are_broker_errors...` | |
| TGT-1 | ✔ | `targets.py:211-257` | A7-A9, `test_targets.py` | |
| TGT-2 | ✔ | `targets.py:242-248` | A10 | |
| TGT-3 | ✔ | `targets.py:254`; log in `rebalance.py:270` | none (log line only) | log currently lost — H1 |
| TGT-4 | ✔ | `targets.py:256` | `test_weight_zero_target_sold_floor_not_ceil`, probe | |
| TGT-5 | ✔ | `rebalance.py:253-267` | A1 (only passes pre-check) | |
| REP-1 | ✔ | `replacement_sets.py` | Appendix A | |
| REP-2 | ✔ | `validate_replacement_set` + import-time check | `test_targets.py` REP-2 rules | |
| REP-3 | ✔ | `targets.py:264-306` | A1-A4 | PRD's assert kept as a real `assert` |
| REP-4 | ✔ | `rebalance.py:262` | `test_full_happy_path` (no set) | |
| REP-5 | ✔ | `rebalance.py:269-275` | none | logged lines lost — H1; record has the weights ✓ |
| PRC-1..3 | ✔ | `Quote.mark/buy_reference/sell_reference` | `test_mark_*`, `test_buy_reference_*`, `test_sell_reference_bid` | |
| PRC-4 | ✔ | `planner.limit_price` | `test_rounds_half_up...`, `test_4_decimals_below_one` | Decimal ROUND_HALF_UP |
| PRC-5 | ✔ | `rebalance.py:339-344, 482-487`; record sources | none (notes/record) | record `prices` lost on failure — M2 |
| SELL-1 | ✔ | `plan_sells` | P1 property, probe | |
| SELL-2 | ✔ | `connection.place` (MKT/DAY) | `test_sell_market_day_and_ref` | |
| SELL-3 | ✔ | plan computed once, step 10 | `test_full_happy_path` | |
| CASH-1 | ✔ | `rebalance.py:364` | `test_reserve_and_buys` | |
| CASH-2 | ✔ | `rebalance.py:459-461` | `test_cash_lag_uses_lower_number`, probe | |
| CASH-3 | ✔ | `planner.py:137-148` | `test_cash_below_reserve_no_buys`, A.4 | |
| BUY-1 | ✔ | trim loop | P4 property over 6,000 accounts | |
| BUY-2 | ✔ | trim loop | `test_cash_shortfall_trims_lowest_weight`, A.2/A.3 | fractional-broker cut branch omitted (IBKR is whole-share) |
| BUY-3 | ✔ | `planner.py:161-171` | A.2/A.3 notes | |
| BUY-4 | ✔ | `plan_buys` | P5 property | |
| BUY-5 | ✔ | `connection.place` (LMT/DAY) | `test_buy_is_limit` | |
| P1–P7 | ✔ | planner | `test_properties_hold_over_random_accounts`, `test_p7_...`; independently re-verified | |
| §8.1 steps | ✔ | `rebalance.py` | `test_rebalance.py` + probes | step 1 run-id suffix missing on parent-built results (L3) |
| RUN-1 | ✔ | `fail()`/`_fills` | `test_sell_rejected_means_no_buys...`, probes | |
| RUN-2 | ✔ | no cancel on failure | `test_buy_timeout_does_not_cancel`, probe | |
| RUN-3 | ✔ | `async with` | `test_full_happy_path` | |
| RUN-4 | ✔ | `trading_executor._connection_factory` | none (factory trivial) | |
| ALC-1 | ✔ | `allocations.py` | `test_security_startup.py` (client reuse implicit) | |
| ALC-2 | ✔ | `allocations.py:514-536` | `test_rebalance.py` (missing allocation → invalid) | non-dict items silently skipped (L4) |
| ALC-3 | ✔ | `allocations.py:493-512` | `test_http_failures_stop_startup_and_exclude_response_body` | |
| ORD-1 | ✔ | leg order | `test_full_happy_path` | |
| ORD-2 | ◐ | `rebalance.py:428,521` | probe `RUN-place-midleg` | non-`OrderPlacementError` → `INTERNAL_ERROR` (M1) |
| §8.4 | ✔ | `results.py` | `test_rebalance.py` | |
| §8.4 msg | ✔ | `rebalance.py:558-562` | `test_unexpected_exception_becomes_internal_error...` | |
| WAIT-1..3 | ✔ | `_wait_for_orders` | probes + `test_buy_timeout_does_not_cancel` | |
| IB-1 | ✔ | `connection.py:99-135` | `test_connect_args_and_fetchfields`, probe | exact flags verified; sync-errors semantics per ib_async 2.1.0 |
| IB-2..4 | ✔ | `connection.py:147-174` | cash/NLV tests, probes | |
| IB-5..6 | ✔ | `connection.py:176-194` | `test_zero_position_skipped`, `test_non_stk_position_is_error` | |
| IB-7 | ✔ | `_qualify` | `test_ibkr_connection.py` | None-placeholder alignment matches ib_async 2.1.0 docs |
| IB-8 | ✔ | `quotes()` | `test_quote_fallback_chain_including_bar`, probes | retries all symbols (L1) |
| IB-9 | ✔ | `place()` | `test_sell_market_day_and_ref`, `test_buy_is_limit`, `test_fractional_quantity_refused` | |
| IB-10 | ✔ | `order_states()` | `test_status_mapping_unknown_is_working`, probes | |
| IB-11 | ✔ | `cancel_own_orders` | `test_cancel_*`, probes | |
| IB-12 | ✔ | `working_orders()` | `test_working_orders_own_vs_foreign` | manual-order visibility = paper check |
| IB-13..15 | ✔ | `supports_fractional=False`; grep clean | probes | |
| CLK-1..2 | ✔ | `market_clock.py` | clock probes; half-day verified | |
| EXE-1..3 | ✔ | `strategy_executor.py` | `test_pool_uses_fork_context`, `test_broken_pool...`, probes | |
| WRK-1 | ✔ | `trading_executor.py` | probes (never raises) | |
| WRK-2 | ◐ | `trading_executor.py:26-32` | none | format requires `run_id`; adapter nesting loses it → H1 |
| SCH-1 | ✔ | `scheduler_service.start` | `test_misfire_settings_passed_to_add_job` | |
| SCH-2 | ✔ | `_execute_scheduled_rebalance` | `test_scheduler.py` | |
| SCH-3 | ✔ | `read_schedule` + dedupe | `test_bad_scheduled_file_left_untouched`, `test_duplicate_ids_run_once` | |
| SCH-4 | ✔ | `_process_scheduled_accounts` | none explicit | warning + treated as processed |
| SCH-5 | ✔ | gather over strategies | `test_scheduler.py` | |
| SCH-6 | ✔ | `_skipped_final` | `test_skipped_when_strategy_already_running_sends_skipped_final` | |
| SCH-7 | ✔ | end-of-job filter | `test_schedule_removes_only_processed_keeps_id_added_during_job`, probe | |
| SCH-8 | ✔ | accepted edge, not coded around | — | |
| RTY-1 | ✔ | `_decision` | `test_scheduler_disabled_gives_failed_final_with_retry_note`, probe | note appended to result (documented deviation) |
| RTY-2 | ✔ | `add_account`/`remove_accounts` | `test_add_and_remove_are_atomic...`, probe | parent-only writers ✓ |
| RTY-3 | ✔ | `_decision` (any failure → add) | `test_each_10_5_row` | |
| RTY-4 | ✔ | helper without PDT | — | |
| SIG-1..3 | ✔ | `ably_service.py`, `main.py` | `test_security_startup.py` | Ably key path reads env directly in the parent (unchanged behaviour) |
| NTF-1 | ✔ | `notification_service.py` | notification suite | |
| NTF-2 | ✔ | one send per result | probes | |
| NTF-3 | ✔ | `_post` | `test_one_post_per_send_with_timeout_and_headers` | |
| NTF-4 | ✔ | `_cap_body` | `test_body_capped_at_3500_bytes_with_200_notes` | |
| NTF-5 | ✔ | mask/format helpers | `test_success_title_and_key_lines` | |
| §11.3 kinds | ✔ | `_TITLE_TAGS`/`_content` | `test_each_kind_title_and_key_line` | "BUY orders did not complete." unreachable on real failures (M2) |
| LOG-1 | ◐ | `rebalance.py` logging | none | lines are produced but dropped by the formatter (H1) |
| LOG-2 | ✔ | old banners gone | §14 search | |
| REC-1..4 | ✔ | `run_records.py` | `test_run_records.py` | failed runs record thin/empty plans (M2) |
| BOOT-1 | ✔ | `main.start()` order | `test_accounts_load_before_scheduler_and_manual_watcher`, `test_strategy_availability_check_stops_startup` | prune runs just before scheduler start |
| BOOT-2 | ✔ | `main._run_forever`, Dockerfile | probe (healthcheck fails on stale mtime) | |
| BOOT-3..4 | ✔ | compose/main | — | |
| BOOT-5 | ✔ | `cleanup()` | `test_cleanup_shuts_down_with_wait_and_cancel_futures` | |
| §14 | ✔ | — | search re-run | clean |
| DOC-1..4 | ✔ | README/AGENTS/rebalancing-logic/CHANGELOG | read against code | cosmetic "maximizing the use of available cash" remnant |
| §16.1 | ✔ | venv/requirements-dev | this review's run | |
| §16.2 | ✔ | all listed test files | 157 passed | property test read and independently replicated |
| §16.3 | ✔ | `test_security_startup.py` adapted | suite green; keep-list assertions present | |
| §16.4 | ◐ | — | build not re-runnable here | daemon absent; rest met |

## Needs a paper check (merged with PRD §16.5 — Faisal decides and runs these)

1. How IBKR checks funds for a limit buy (does the reserve logic match Gateway behaviour).
2. How quickly cash updates after a fill (the 30 s wait and 0.98 threshold).
3. Orders left working by this app's client ID in an earlier connection are visible and
   cancellable after reconnecting (`ORDERS_OPEN` path).
4. Manual TWS/mobile orders appear in `reqAllOpenOrders` for the foreign-order rule (IB-12).
5. A preview of every live account before the first live signal.
6. *(from M3, only if you want the residual risk closed)* whether a stalled Gateway can leave
   `reqTickersAsync`/`qualifyContractsAsync` pending indefinitely; if observable, add the
   `request_timeout_seconds` wrapper.
7. *(from L1)* whether the all-symbols retry generates data-permission warning spam for your
   subscriptions.

## Verification artifacts (review workspace, outside the repo)

- `v3review/pytest_312.txt` — full test run output.
- `v3review/verify_planner.py` — Appendix A + 6,000-account property run (0 failures).
- `v3review/probe_v3.py` / `probe_out.txt` — 62 connector/run-level probes.
- `v3review/probe2.py` — executor/scheduler/notification/record/healthcheck probes (0 failures).
- `v3review/logging_probe.py` — H1 reproduction.
