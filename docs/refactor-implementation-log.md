# Refactor v3 — Implementation Log

Per PRD §17.3, one entry per work package. Do not mark a requirement done when it is
not. `docs/refactor-prd.md` is the single source of truth; deviations and open questions
go here, and requirements are referenced by ID.

## WP1 — Test environment, fakes skeleton, requirements-dev.txt

**Status:** in progress (environment scaffolded; `tests/fakes.py` skeleton not yet written)

**Commits:** none for requirements-dev.txt / venv (environment files are untracked where
expected; the report file itself is part of this doc).

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

**Not done / deviations:**
- `tests/fakes.py` skeleton not yet written (WP1 implementation remains). The environment
  scaffolding above is setup only; WP1's deliverables (fakes skeleton) still need coding.
- Note: `packages/ibkr-connector/setup.py` still lists `rebalance-calculator==1.0.0`.
  PRD §4.2 says to remove it. **Not changed here** — that is a WP2/Early-WP scope change;
  left for its own work package to avoid out-of-order edits (§17).

**Open questions:** none at time of writing.

---

_Append new entries below for WP2 onward._
