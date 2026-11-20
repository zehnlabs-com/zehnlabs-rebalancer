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

_Append new entries below for WP3 onward._
