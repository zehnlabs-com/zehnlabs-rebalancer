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

_Append new entries below for WP5 onward._
