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

_Append new entries below for WP2 onward._
