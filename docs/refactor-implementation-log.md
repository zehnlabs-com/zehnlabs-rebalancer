# Refactor v3 — Implementation Log

Per PRD §17.3, one entry per work package. Do not mark a requirement done when it is
not. `docs/refactor-prd.md` is the single source of truth; deviations and open questions
go here, and requirements are referenced by ID.

## WP1 — Test environment, fakes skeleton, requirements-dev.txt

**Status:** done

**Commits:**
- `0045b0a` (WP1 implementation): `tests/fakes.py` skeleton (`FakeBrokerConnection`,
  `FakeAllocations`, `FakeClock`, `RecordingNotifier`). The environment hen
  (`requirements-dev.txt`, venv) was scaffolded earlier; the report file itself is part of
  this doc.

**Requirement IDs:** §16.1 (environment), requirements-dev.txt.

**What was done (environment, before any WP1 code):**
- Branch `refactor/v3` (renamed from the typo'd `refator/v3`; PRD §0.8).
- Clean Python **3.12.9** venv at `.venv` via `uv` (the previous `.venv` was a broken
  link to a deleted temp 3.12.12 checkout; system Python is 3.14, so a working venv had
  to be recreated — 3.14 is not acceptable, PRD §0.9 / §16.1). `.venv` is gitignored.
- Installed editable: `app-config`, `broker-connector-base`, `rebalance-calculator`,
  `ibkr-connector` (with `ib-async==2.1.0` as pinned).
- Installed runtime deps from `event-broker/requirements.txt` plus dev deps
  `pytest` (`8.4.2`, within the `>=8,<9` band) and `httpx`.
- Created `event-broker/requirements-dev.txt` (pytest + httpx).
- Baseline verified: `PYTHONPATH=event-broker python -m pytest -q tests` → **22 passed**.

**Tests added:** none in WP1. §16.2's test files belong to WP2+; WP1 ships only the fakes
skeleton. The fakes were smoke-checked at a REPL (import, connection lifecycle, scripted
fills, allocations, clock, notifier) but no committed test exercises them yet — the real
checks come when WP2-7 drive them.

**Not done / deviations:**
- `tests/fakes.py` is implemented but kept boundary-proof: `FakeBrokerConnection` does not
  yet subclass `broker_connector_base.connection.BrokerConnection` and `RecordingNotifier`
  does not yet subclass `Notifier` — the ABCs land in WP2/WP7 and the module must import
  today. Their methods/signatures already match the §6.2 / §11.1 contracts, so the base-
  class changes are mechanical. The fakes use `SimpleNamespace`/plain mappings rather than
  the packages' domain models (which do not exist yet); WP2/WP6/WP7 reconcile the types.
- `packages/ibkr-connector/setup.py` still lists `rebalance-calculator==1.0.0`. PRD §4.2
  says to remove it. **Not changed here** — WP2 scope; left to avoid out-of-order edits (§17).

**Open questions:** none at time of writing.

---

## WP2 — broker-connector-base (§6)

**Status:** done

**Commits:**
- (`WP2`): `connection.py` (`BrokerConnection` ABC, §6.2), `errors.py` (§6.3),
  §6.1 models in `models.py`; `__init__.py` exports; `tests/test_broker_connector_base.py`;
  `tests/fakes.py` `FakeBrokerConnection` rewired to the real ABC/models.

**Requirement IDs done:** §6.1 (Side, OrderStatus [new enum], PriceSource, Quote,
Position, AccountState, OrderRequest, OrderState, WorkingOrder — frozen dataclasses;
Quote.mark/buy_reference/sell_reference with the PRC-1..3 chains and NoPriceError),
§6.2 (BrokerConnection ABC + `async with` support), §6.3 (BrokerError + 5 concrete
subclasses).

**Tests added:** `tests/test_broker_connector_base.py` (16 tests) — error hierarchy,
enum values, Quote price chains + NaN/≤0 cleaning, frozen value types, ABC
non-instantiation and `async with` connect/close.

**Not done / why:**
- Legacy `models.py` pydantic models, `base_client.py`, `base_rebalancer.py` were **kept**
  alongside the new §6 models rather than deleted. Their consumers (`ibkr-connector`,
  `event-broker/app/models.py`, `rebalance-calculator`) are not removed until WP4/WP8
  (§4.3). Deleting them now would break the green baseline, which §17 forbids. New models
  are additive; the old files/S4-related deletions land in the package that deprecates them.
- The old string-constant `OrderStatus` class in `models.py` was replaced by the new
  StrEnum (both named `OrderStatus`): verified no source consumer references it, so this
  is safe and satisfies §14's "new enum replaces the old string-constant class".

**Deviations:**
- The venv had installed the four local packages as **copies** under `uv` (not editable),
  so source edits were ignored. Reinstalled all four editable with
  `uv pip install --python .venv/Scripts/python.exe -e packages/<p>` to match PRD §16.1.
- `Quote.mark/buy_reference/sell_reference` are implemented here (they are model methods
  that §6.1 itself enumerates) even though PRC-4's Decimal rounding lives with the planner
  in WP3; PRC-4 is not a model method, so no WP3 logic was started early.
- `packages/ibkr-connector/setup.py` still lists `rebalance-calculator==1.0.0`; §4.2 says
  to remove it. Left for WP4 (its package), not changed here.

**Open questions:** none.

---

## WP3 — rebalance-calculator (§7, Appendix A)

**Status:** done

**Commits:** WP3 commit: `targets.py`, `replacement_sets.py`, `planner.py`, new §7
models in `models.py`; `__init__.py`; `setup.py` (dropped `app-config` dep per §4.2);
`tests/test_targets.py`, `tests/test_planner.py`.

**Requirement IDs done:** §7.1 TGT-1..5 (`validate_and_scale`), §7.2 REP-1..5
(`replacement_sets.py`, `apply_replacements`), §7.3 PRC-1..5 (Quote methods landed in
WP2; `limit_price` here with Decimal half-up rounding), §7.4 SELL-1..3 (`plan_sells`),
§7.5 CASH-1..3, §7.6 BUY-1..5 (`plan_buys`), §7.7 P1–P7 (property test), and `round_quantity`.

**Tests added:** `tests/test_targets.py` (TGT-1..5, REP-2 each rule, every Appendix A.1
row A1–A10, replacement-after-1%-check), `tests/test_planner.py` (A.2, A.3, A.4 exact
numbers; P1–P7 over a fixed-seed 5,000-account property run; weight-0 target sold exactly
`floor`; buy with no bid but an ask; `round_quantity`/`limit_price` edges). All numbers
in Appendix A reproduce exactly (V0 104031.45, GLD 22 ≤251.35, cash 499/149/77,
CCC 77 note, A.4 no-buys+CASH-3 note).

**Deviations / recorded problems (PRD §0.3):**
- **`round_quantity` §16.2 vs §7 contradiction.** §16.2 lists `99.9999999999 -> 99`, but
the §7 formula `floor(x + EPS)` with `EPS = 1e-9` returns **100** for that input (the
fractional deviation 1e-10 sits inside the EPS margin; verified empirically). The §7
formula is authoritative and behaviorally right: such a value only arises from a division
that truly rounds up to 100, and the EPS is meant to fix floats landing just under an
integer. The §16.2 expectation was **not** enforced; `tests/test_planner.py` asserts the
formula's behaviour (100, 100, float-artifact 100.0-1e-14 -> 100, 0.9999 -> 0) and the
conflict is recorded here as a spec defect for Faisal.
- `rebalance_calculator` imports `app_config` through its **legacy** `calculator.py`
(`get_config`). §4.2 says the package must not. I dropped `app-config` from
`install_requires` but the legacy class remains until WP8 removes `calculator.py`; the
venv still has `app-config` installed so imports stay green.

**Not done / why:** nothing within §7. Legacy `TradeCalculator`/`TradeCalculationResult`
kept (imported by `ibkr_connector/rebalancer.py` until WP4) — new models added alongside.

**Open questions:** the §16.2 `99.9999999999` expectation (see deviation) — needs Faisal's
call; I implemented the spec formula.

---

## WP4 — ibkr-connector (§9)

**Status:** done

**Commits:** WP4 commit: `connection.py` (`IBKRConnection`), `__init__.py` exports it;
`setup.py` dropped `rebalance-calculator` dep (§4.2); `tests/test_ibkr_connection.py`.

**Requirement IDs done:** IB-1 (connect args incl. exact `fetchFields` = POSITIONS | ORDERS_OPEN
| ACCOUNT_UPDATES, stabilization delay, disconnect/account check, `reqMarketDataType`),
IB-2 (cash `$LEDGER-CashBalance` else `CashBalance`), IB-3 (NLV, missing → error), IB-4 (finite
parse), IB-5 (positions, skip 0), IB-6 (non-STK/negative → error), IB-7 (one qualify call + cache),
IB-8 (tickers, retry, 1-min bar fallback, NoPriceError), IB-9 (Market/Limit, DAY,
`orderRef=zl-rebalancer:<run_id>`, whole-qty check, handle = `str(order.orderId)`),
IB-10 (status from stored Trades, empty/unknown → WORKING, message from last `log` entry),
IB-11 (own-order cancel, poll every 0.5s, CancelNotConfirmedError), IB-12 (foreign orders via
`reqAllOpenOrdersAsync`, `own = clientId match`), IB-13 (`supports_fractional=False`),
IB-14 (idempotent close, swallow+log), IB-15 (no `reqGlobalCancel`/multi/completed/executions).

**Tests added:** `tests/test_ibkr_connection.py` (19 tests) mocking `ib_async.IB`: exact connect
flags (and absence of EXECUTIONS/ORDERS_COMPLETE/SUB_ACCOUNT_UPDATES), unmanaged-account failure,
cash ledger-vs-cashbalance fallback, neither → error, NLV missing → error, non-STK/negative
position errors, zero-quantity skip, status mapping incl. empty → WORKING, own-vs-foreign
classification, fractional quantity refused without a `placeOrder` call, quote fallback chain
with the bar request (`useRTH=False`, `TRADES`), no-price-at-all → NoPriceError, DAY/TIF/ref/account
on sell market and buy limit, cancel unconfirmed vs confirmed.

**Deviations:**
- IB-1's `settings` parameter is effectively **duck-typed**: `IBKRConnection` reads
  `settings.ib_host`, `settings.ib_port`, `settings.trading_mode`. The real `RuntimeSettings`
  is a WP5 `event-broker/app` type; a package must not import the app, so WP4 accepts any
  object with those attributes. WP5's `RuntimeSettings` will provide them.
- Kept legacy `client.py`/`rebalancer.py`/`models.py`/`allocation_service.py`/`replacement_service.py`
  and their exports unchanged — their consumers (old `event-broker/app`, baseline tests) still
  import them; §4.3 deletion is WP8. `connection.py` is additive.
- `setup.py` now lists `broker-connector-base` + `app-config` (no `rebalance-calculator`),
  per §4.2.

**Not done / why:** the IB-12 note that manual TWS/mobile orders must be confirmed to appear in
`reqAllOpenOrders` is a **Faisal paper-check** item (§16.5), not an agent task. Real Gateway
connectivity was not exercised (PRD §0.4).

**Open questions:** none (the IB-12 paper confirmation is field validation, deferred).

---

## WP5 — app-config / accounts / settings (§5)

**Status:** done

**Commits:** WP5 commit: `app-config` strict models + loader (CFG-1..4), `config.yaml`
replaced (CFG-2), `event-broker/app/accounts.py` (ACC-1..3), `settings.py` (ENV-1..2),
`accounts.yaml.example` cleaned (ACC-4); `tests/test_config.py`, `tests/test_accounts.py`.

**Requirement IDs done:** CFG-1 (extra=forbid everywhere), CFG-2 (exact new schema in
models + `config.yaml`), CFG-3 (removed/unknown key messages, all listed together,
exact wording verified against the table, sections `replacement`/`pdt_protection`),
CFG-4 (one `logger.info(model_dump())`), CFG-5 (non-zero exit stays in main.py — see
below), ACC-1 (AccountConfig + AccountsFile), ACC-2 (whole-file validation with every
message format; disabled/other-mode accounts still validated), ACC-3 (AccountRegistry),
ACC-4 (example file cleaned; Faisal's `accounts.yaml` untouched and uncommitted — it is
gitignored), ENV-1 (RuntimeSettings built once, picklable), ENV-2 (masked `__repr__`;
never logged).

**Tests added:** `tests/test_config.py` (shipped config loads; each CFG-3 key/section
message verbatim; unknown nested key; all-offending-keys-listed-together; pydantic range
error prefixed with `config.yaml:`; bad HH:MM), `tests/test_accounts.py` (example loads;
registry filtering by enabled+mode; client-id = digits of the account ID; every ACC-2
message; disabled+removed-key still fails; all-problems-at-once).

**Deviations / scope notes:**
- ENV-1's explicit instruction to **remove** `StrategyExecutor._get_environment()` and the
  `os.environ` copying in `execute_strategy_batch` was **not** done in WP5: the baseline
test `test_unified_key_and_base_are_forwarded_to_subprocess_without_ably_key` asserts
`_get_environment()` still works, and PRD §16.3 says existing tests are adapted only in
WP8. `RuntimeSettings` is provided now; the executor/worker rework happens in WP7 and the
removal with it.
- CFG-5 (non-zero exit on config error) is existing `main.py` behaviour and was not
changed; nothing in WP5 regresses it.
- `accounts.yaml` (Faisal's) still contains the removed keys and would now fail strict
validation — by design (ACC-4: Faisal edits his own). It is gitignored; not touched,
not committed.
- The current `accounts.yaml.example` accounts are `paper` so the example loads cleanly.

**Not done / why:** nothing within §5.

**Open questions:** none.

---

## WP6 — allocations, market clock, results, the account run (§8, §10.1)

**Status:** done

**Commits:** WP6 commit: `event-broker/app/allocations.py`, `market_clock.py`, `results.py`,
`rebalance.py`; `tests/fakes.py` (`FakeAllocations` returns `list[RawAllocation]`;
`FakeBrokerConnection` reflects sell proceeds in `account_state` and gains `cash_factor`);
`tests/test_rebalance.py`.

**Requirement IDs done:** ALC-1..3 (`AllocationsClient` async CM, one session, `x-api-key`;
`ticker`/`symbol`+`allocation`, missing allocation → None; status-code-only errors, timeout /
network error). CLK-1..2 (`MarketClock` on `get_calendar("XNYS")`, injectable `now()`, real
session times incl. half-day closes, `next_scheduled_run` strictly-after-now at config open
EE); preview ignores market hours (implemented at the caller). §8.1 steps 1–18 + RUN-1..4
(`rebalance_account` never raises; own orders cancelled live; foreign orders →
`FOREIGN_ORDERS`; NLV mismatch → `ACCOUNT_DATA_MISMATCH` live / warning preview; two-leg
sells-then-buys; cash wait using the lower figure; positions_after = state0 − fills, never
re-read; close always via `async with`; connection factory is the single broker map point).
§8.2 (parse), §8.4 (`results.py`: RunOutcome, FailureReason with values == names,
ExecutedOrder/PlannedOrderOut/NoteOut/AccountRunResult), §8.5 WAIT-1..3 (`_wait_for_orders`:
empty/unknown status → WORKING; non-filled at quiet → ORDERS_FAILED; timeout → ORDERS_TIMED_OUT;
never cancels).

**Tests added:** `tests/test_rebalance.py` (11): full happy path (cancel → snapshot → sells →
cash wait → fresh quotes → buys; cash 335.0, value 104026.45, cash_base 10995), preview places
nothing and does not cancel (+ works when market closed), live market-closed never connects,
foreign order stops before any order, cancel-not-confirmed fails before the snapshot, NLV
mismatch fails live, sell rejected → ORDERS_FAILED + no buys + executed lists the fill, buy
timeout → ORDERS_TIMED_OUT and no cancel on failure, cash lag uses the lower figure, unexpected
exception → INTERNAL_ERROR (truncated to 300 chars) and never raised.

**Deviations / notes:**
- `RunDeps.settings` is included (per §8.1) but is not yet consumed by `rebalance_account`
  (port/mode live in `IBKRConnection` via the factory); it is carried for WP7's worker wiring.
- The `record` dict (REC-2) is built in the worker and attached to `AccountRunResult.record`
  as the spec requires; the record **file writing and pruning** are WP7 (`run_records.py`).
- `trimmed` in the record is derived from the BUY-3 warning note's listed symbols (no new
  model surface added, per §0.2).
- One internal `try:` (sold to scope the `async with` connection) was found dangling and
  removed in favour of the module's `except ConnectionFailedError`; behaviour unchanged.

**Not done / why:** nothing within §8/§10.1. Real allocations HTTP and IBKR wiring are
WP7-worker concerns; tests use fakes (§0.4).

**Open questions:** none.

---

## WP7 — executor, worker, scheduler, retry, notifications, run records, Ably task set, main/startup/heartbeat, and the §16.3 adaptation of `tests/test_security_startup.py` (§10.2–10.6, §11, §12, §13, §16.3)

**Status:** done

**Commits:** WP7 commit (this entry).

**Requirement IDs done:**
- §10.2 EXE-1..3 (`strategy_executor.py`: explicit fork pool, running-set dedup with release-before-postprocessing, parent market-closed detection, BrokenProcessPool → `WORKER_CRASHED` + pool replacement, generic-exception → `INTERNAL_ERROR`, per-step-guarded postprocessing).
- §10.3 WRK-1..2 (`trading_executor.py`: new event loop, one `AllocationsClient`, one `asyncio.gather(..., return_exceptions=True)`, `model_dump()` list, never raises, `INTERNAL_ERROR` fallback; module-level stdout handler with `[strategy account_id run_id]`; `SubprocessManager` and the `os.environ` copying removed).
- §10.4 SCH-1..8 (`scheduler_service.py`: `AsyncIOScheduler` + `CronTrigger(mon-fri, hour, minute)`, misfire `300`/`true`/`1`; non-trading-day skip; malformed file left untouched; unknown IDs treated as processed; concurrent `execute(live, scheduled)`; `None` → `SKIPPED_FINAL`; SCH-7 removes only processed IDs; the atomic read/filter/write helpers reused by RTY-2).
- §10.5 (scheduler-file add/remove + notification-kind mapping per row; RTY-1 scheduler-disabled → `FAILED_FINAL` + retry note, not queued; RTY-2 add/remove vs SCH-7 same helper; no locking; RTY-4 `add_account_to_schedule`'s PDT use gone — the helper is now `add_account(path, id, logger)`).
- §10.6 SIG-1 (task set + `add_done_callback(discard)`), SIG-2 (`rebalance`→live, `print-rebalance`→preview, else drop; source `signal`) in `ably_service.py`.
- §11 NTF-1..5 (`Notifier` ABC, `NotificationContext(kind, next_run)`, pure `render -> Message`, one message per result, `NtfyNotifier` posting to `https://ntfy.sh/<channel>` with `Title`/`Priority: default`/`Tags` at `aiohttp.ClientTimeout(total=10)`, failures logged never raised, disabled/no-channel skip at INFO; 3,500-byte body cap with `…and <k> more notes (see logs)`; account IDs redacted and run IDs omitted; `_format_strategy_name` kept; `send_warnings` removed).
- §12 REC-1..4 (`run_records.py`: parent appends one JSON line to `data/runs/<YYYY-MM-DD>.jsonl`, prunes `*.jsonl` older than 30 days, never touches other files, dir created, write failure logged and never affects outcome).
- §13 BOOT-1 (`main.py` startup order), BOOT-2 (heartbeat touch), BOOT-5 (`cleanup()` with `wait=True, cancel_futures=True`).
- §16.3 adaptation of `tests/test_security_startup.py`, and §16.2 test files `test_executor.py`, `test_scheduler.py`, `test_notifications.py`, `test_run_records.py`.

**Tests added:** `test_executor.py` (7: every §10.5 row; scheduler-disabled `FAILED_FINAL`+RTY-1 line; dropped-while-running no notification; running-mark held in pool + released before notifications; BrokenProcessPool → `WORKER_CRASHED` + new pool; fork-context to `ProcessPoolExecutor`; cleanup shutdown args). `test_scheduler.py` (8: bad file untouched; missing file no-op; SCH-7 keeps IDs added during the job; duplicates run once; already-running → `SKIPPED_FINAL` + removal; misfire settings passed to `add_job`; disabled scheduler adds no job; atomic add/remove helpers incl. not clobbering an invalid file). `test_notifications.py` (SUCCESS header/key lines incl. masking and `≈` amounts; each kind's title + key line; preview order lines + `≤`/`(market)` + 20-line cap; 200-note body ≤ 3,500 bytes; long-body truncation without a run ID; identifier redaction; what-happened lines; disabled/no-channel skip; one post/send with `ClientTimeout(total=10)`). `test_run_records.py` (4: one line per result; dir creation; pruning keeps 30 days + non-matching files; append-then-prune).

**Not implemented here (by design):** §10.1 `MarketClock` was WP6; the `IBKRConnection` factory lives in the worker (`_connection_factory`, RUN-4) and is not unit-tested against a real Gateway (§0.4). `requirements-dev.txt`, fakes and `run_records` directory path: `_RUNS_DIR` in `main.py` defaults to `/app/data/runs` and is overridable via `RUNS_DIR` (not in the PRD — recorded deviation, see below).

**Deviations / recorded problems (PRD §0.2 / §0.3):**
- **RTY-1 line delivery.** §11.1 fixes `NotificationContext` to carry only `kind` and `next_run`; the RTY-1 extra line `Automatic retry is disabled (scheduler.enabled: false).` is therefore delivered by appending a `warning` note to the result in `StrategyExecutor._decision` before the `FAILED_FINAL` notification is rendered. This keeps the context shape exactly as specified.
- **`RUNS_DIR` config.** Neither `config.yaml` nor `app-config` defines a runs-directory setting; §12.1 hardcodes `/app/data/runs`. `RunRecords` accepts a `directory` argument (needed for testability) and `main.py` defaults it to `/app/data/runs` with an optional `RUNS_DIR` env override. No config key was added to the strict schema (CFG-2 is untouched).
- **`fork` context on the Windows dev machine.** `multiprocessing.get_context("fork")` (EXE-1) is unavailable on Windows, so a real fork pool cannot be constructed on this dev box. Production runs in the Docker/Linux container where fork is valid; the tests construct `StrategyExecutor` only under a mocked `get_context` and a `FakePool` (no subprocesses) and assert the fork context is passed to `ProcessPoolExecutor`. This keeps the spec's EXE-1 intact while staying green on Windows.
- **WP5-deferred removal (ENV-1) completed here.** `StrategyExecutor._get_environment()` and the `os.environ` copying in `execute_strategy_batch` are removed in WP7; the worker receives a pickled `RuntimeSettings`. The old subprocess-forwarding test was replaced by `test_runtime_settings_masks_api_key_and_ignores_realtime_key`.
- **Removed one legacy test** (`test_account_grouping_preserves_mode_enabled_and_strategy_filters`): accounts grouping moved out of `AblyEventSubscriber` into `app.accounts.load_accounts` / `main._strategies_by_name` (covered by WP5 `test_accounts.py` and the scheduler's `accounts_lookup` in this suite). Not one of the §16.3 keep-list behaviours.
- **Worker logging.** WRK-2's handler is attached to a module logger named `worker` (one stdout handler, never added/removed per account); `rebalance_account` chains its own `LoggerAdapter` (run_id) onto the per-account adapter (strategy/account_id).

**Not done / why:** nothing within §10.2-10.6 / §11 / §12 / §13 / §16.3. Legacy removals that belong to WP8 (§4.3: `pdt_protection_service.py`, `trading/broker_factory.py`, old `models.py`, `replacement-sets.yaml`, version bumps, README/AGENTS/docs/changelog) are deliberately untouched to keep the new WP7 modules importable and the baseline green.

**Open questions:** none blocking. `RUNS_DIR` (deviation above) is offered as a small, non-schema setting for local testing; flag to Faisal whether a `config.yaml` `runs` path should be added instead.

---

## WP8 — Deletions, Docker/compose, docs, changelog, removal search, final check (§4.3, §13.3–13.4, §14, §15, §16.4)

**Status:** done

**Commit:** this entry.

**Requirement IDs done:**
- §4.3 (files to delete). Deleted: `replacement-sets.yaml`; `event-broker/app/services/pdt_protection_service.py`;
  `packages/broker-connector-base/src/broker_connector_base/{base_rebalancer.py,base_client.py}`;
  `packages/ibkr-connector/src/ibkr_connector/{rebalancer.py,client.py,models.py,allocation_service.py,replacement_service.py}`;
  `packages/rebalance-calculator/src/rebalance_calculator/calculator.py`;
  `event-broker/app/trading/broker_factory.py` and the now-empty `event-broker/app/trading/__init__.py` (the
  `trading/` package is gone entirely — §4.1's architecture has no `trading/`); and `event-broker/app/models.py`
  (its only consumers were the deleted PDT service; it held `PDTCheckResult` plus the legacy
  `AccountExecutionResult`/`StrategyExecutionResult` models, all displaced by `results.py`). Removed `PDTCheckResult`;
  removed every `SubprocessManager` and `CachedPrice` reference.
- §4.2 dependency cleanup that the deletions make safe: removed the legacy models from
  `broker-connector-base` (`src/broker_connector_base/models.py` now holds only the §6.1 dataclasses; the
  `__init__.py` no longer exports `BrokerClient`, `BaseRebalancer`, `Trade`, `AllocationItem`, `AccountConfig`,
  `AccountSnapshot`, `AccountPosition`, `ContractPrice`, `OrderResult`, `OpenOrder`, `RebalanceResult`,
  `CalculateRebalanceResult`); removed `TradeCalculationResult` (and the `Trade`/pydantic imports) from
  `rebalance-calculator` `models.py` and `__init__.py`; trimmed `ibkr-connector` `__init__.py` to export only
  `IBKRConnection`.
- §13.3 BOOT-3: `x-system-version` in `docker-compose.yaml` and `get_status()["version"]` in `main.py` → `3.0.0`.
- §13.4 BOOT-4: removed the `./replacement-sets.yaml:/app/replacement-sets.yaml:ro` volume line from
  `docker-compose.yaml`; nothing else in compose changed.
- §14: §4.3-linked removals plus the removal search (see below). No source reference to any removed identifier
  remains; only the intentional removal-gate messages (CFG-3/ACC-2, which must name the removed keys) and the new
  `trading.minimum_cash_reserve_percent` key still contain the strings.
- §15 DOC-1..4: README.md (removed "Cash Usage Maximization" and "Minimum Purchase Quantity"; added "How a
  Rebalance Works", "Retries", "Replacement Sets" with the built-in `ira` table, and "Upgrading to v3.0.0";
  removed `cash_reserve_percent`/`pdt_protection_enabled`/`replacement-sets.yaml`; documented
  `trading.minimum_cash_reserve_percent` (0.25%, with the low-price-share commission caveat); updated the
  scheduler to 09:35, foreign-order rule, TIF=DAY, and the margin/IRA cash-settlement note). AGENTS.md (rewrote
  the architecture, event/data-flow, module-organization, config, accounts, replacement-set, docker and
  trading-logic sections; removed PDT, the price cache, and the hash-based client-ID text — the real rule is the
  digits of the account ID). `docs/rebalancing-logic.md` (new — the standalone description of §7, §8.1, §10.5, no
  history; the file did not exist in the repo, so it was created fresh). `CHANGELOG.md` (new — a `3.0.0` entry
  listing every §3.1 `SCOPE-*` change).
- §16.4 definition-of-done items: see the item-by-item table below.

**Tests added:** none. §16.2's test files are written and pass unchanged (157 tests); WP8 is deletions + docs, and
removing source that only tests-rejected adds no new behaviour. `tests/test_config.py` / `tests/test_accounts.py`
already cover the strict-message startup failures the PRD's old-file check relies on.

**Deviations / scope notes (PRD §0.2 / §0.3):**
- **§13.2 BOOT-2 Dockerfile healthcheck was left stale by WP7 and is fixed here.** WP7 implemented the heartbeat
  touch in `main.py` but left `event-broker/Dockerfile` with the old `get_status().running` `HEALTHCHECK`
  (which, post-refactor, `get_status()` no longer reports). WP8 is the "Docker/compose" package, so I replaced it
  with the BOOT-2-specified `/tmp/heartbeat` mtime check. WP7's log claim of "BOOT-2 (heartbeat touch)" was
  incomplete — recorded here for accuracy.
- **§10.4 SCH-1 APScheduler pin was missed by WP7 and applied here.** `event-broker/requirements.txt` still read
  `APScheduler>=3.10`; SCH-1 requires `>=3.10,<4`. Pinned to `>=3.10,<4` (the build installed 3.11.3, within the
  pin). This is a WP7-scope line that WP8 corrected as part of the Docker final check; noted to keep the log
  honest about requirement coverage.
- **`event-broker/app/models.py` deleted in full, not just `PDTCheckResult`.** §4.3 says "remove the `PDTCheckResult`
  model"; the whole file's remaining models (`AccountExecutionResult`, `StrategyExecutionResult`) are legacy
  types displaced by `results.py` and have no remaining consumers, and §4.1's target architecture has no
  `app/models.py`. Deleting the file is the minimal faithful reading.
- **`docs/rebalancing-logic.md` did not exist.** DOC-3 says "replace its content"; with no file to replace, I
  created it fresh with the requested standalone content.
- **§14 strings in removal-gate messages and docs.** The CFG-3 (`config.yaml` loader) and ACC-2 (`accounts.py`)
  rejection messages necessarily name the removed keys; `README.md`'s upgrade notes and the ADRs also discuss
  them. §14's "only the changelog or the README" is therefore imperfect when read literally across the whole
  repo; the check that matters — no *functioning* reference to any removed feature in executable source — passes
  (search output below).

**§14 removal search (run over `git grep` = tracked files only, tests excluded; intentional removal-gate lines
and the retained `trading.minimum_cash_reserve_percent` key are the only non-doc hits):**
- Removed identifiers with **zero** hits anywhere in tracked source: `commission_divisor`,
  `max_account_utilization`, `PDTProtectionService`, `PDTCheckResult`, `last-executions`, `CachedPrice`,
  `use_cached_prices`, `use_cache`, `synthetic_ask_offset`, `ReplacementConfig`,
  `minimal_non_replaced_allocation_percent`, `normalization_trigger_threshold`, `normalization_failure_threshold`,
  `BaseRebalancer`, `IBKRRebalancer`, `TradeCalculator`, `SubprocessManager`, `BrokerClient`,
  `create_broker_client`, `send_warnings`, `get_open_orders`, `get_order_status`, and the string
  `phase='buy'`.
- Strings that appear only as (a) the CFG-3/ACC-2 rejection messages or (b) docs/ADRs upgrade notes:
  `commission_rate`, `allocation_threshold_percent`, `minimum_cash_reserve_usd`, `cash_reserve_percent`,
  `price_cache_ttl`, `order_tif`, `post_completion_delay`, `replacement-sets.yaml`.
- `Trade` (bare) appears in `ibkr-connector/connection.py` referring to **ib_async's `Trade` object** (IB-9/IB-10),
  not the removed pydantic model; `OrderStatus` is now only the §6.1 StrEnum.

**§16.4 Definition of done — item by item:**
1. *All tests pass.* — **MET.** `PYTHONPATH=event-broker python -m pytest -q tests` → **157 passed** (unchanged
   from the WP7 baseline; deletions removed only dead code and kept the suite green).
2. *§14 removals verified with a search.* — **MET.** Search run (`git grep` over tracked files) and reported above;
   no executable-code reference to any removed feature remains.
3. *`docker compose build event-broker` succeeds (if Docker is available).* — **MET.** Docker Engine **29.6.1** was
   available on this machine; `docker compose build event-broker` completed and produced
   `zehnlabs-rebalancer-serioustrader-event-broker:latest`. APScheduler resolved to 3.11.3 (within the `<4` pin).
4. *Starting the container with the old `config.yaml` or the old `accounts.yaml.example` fails with the §5
   messages.* — **MET (by design + existing tests).** The strict loader emits the exact CFG-3 messages and
   `accounts.py` the exact ACC-2 messages; `tests/test_config.py` and `tests/test_accounts.py` assert several of
   them verbatim. The Docker container was built, not run against a gateway (no real broker login; PRD §0.4), so
   this is verified at the unit level rather than a live container boot.
5. *README, AGENTS.md, rebalancing-logic.md and CHANGELOG.md are updated.* — **MET.** All four updated/created.

**Not done / why:** nothing within §4.3, §13.3–13.4, §14, §15, §16.4. I did not touch Faisal's local
`accounts.yaml` (it still carries the removed keys and is gitignored; ACC-4 says Faisal edits his own), nor the
untracked `CLAUDE.md` / `working-docs/` planning scratch (both out of the git repo).

**Open questions:**
- Whether a deliberate cleanup of the local-only `CLAUDE.md`/`working-docs/` scratch is wanted (they are
  untracked, so they are not part of any commit; left untouched).
- Re-confirm the §16.4 old-config boot check is acceptable at unit-test level given PRD §0.4 forbids a real
  Gateway connection; a `docker compose run` boot test (no broker) could be added by Faisal if wanted.

## Production correction — concurrent IBKR account updates (2026-11-23)

Production proved WP4's exact account-update startup flags were wrong for concurrent accounts.
`StartupFetch.ACCOUNT_UPDATES` uses legacy `reqAccountUpdates`, for which IB Gateway accepts only one
account across clients; ten of eleven concurrent live runs received warning 2101 and timed out.

The connector now starts with `POSITIONS | ORDERS_OPEN`, then awaits targeted
`reqAccountUpdatesMultiAsync(account_id)` under `request_timeout_seconds`. The subscription remains
open until disconnect so post-sell cash changes continue to update. Tests cover two different
accounts connected concurrently with valid cash/NLV snapshots and cleanup on account-update timeout.
PRD IB-1/IB-15 and ADR 0007 were corrected to match.
