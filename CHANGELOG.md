# Changelog

## 3.0.0 — Rebalancer refactor

Breaking configuration changes; see §3.1 of `docs/refactor-prd.md` for the
full list and the upgrade notes in the README.

Behaviour changes:

- **SCOPE-1** The 0.5-point allocation band is removed. Every run aligns every
  symbol to its target as closely as whole shares allow. Deposits are invested
  on the next run.
- **SCOPE-2** The 1% commission divisor is removed.
- **SCOPE-3** Buy scaling, the adjustment pass, `max_account_utilization`, the
  "skip if cash is low and all symbols are held" shortcut, and the separate
  affordability filter are removed. One rule replaces them: when cash is short,
  reduce the **lowest-weight** buys first, share by share.
- **SCOPE-4** Share quantities are always rounded **down** to whole shares. A
  target smaller than one share is not bought.
- **SCOPE-5** `cash_reserve_percent` (per account) and
  `minimum_cash_reserve_usd` ($100) are replaced by one global setting,
  `trading.minimum_cash_reserve_percent` (default `0.25`).
- **SCOPE-6** PDT protection is removed completely.
- **SCOPE-7** Replacement sets move into code; `replacement-sets.yaml` is
  deleted. The replacement algorithm is fixed.
- **SCOPE-8** Target weights must total within 1%; they are then scaled to
  exactly 100%, or the run fails. An empty list means "sell everything".
- **SCOPE-9** A live run triggered while the market is closed places no orders
  and is queued for the next market open. A live run that fails is queued for
  the next market open. A failed market-open run is not retried.
- **SCOPE-10** The market-open job moves from 09:30 to **09:35 ET**.
- **SCOPE-11** At the start of every live run the app cancels this account's
  own working orders. If the account has foreign working orders, the run stops
  and the user is told.
- **SCOPE-12** `config.yaml` and `accounts.yaml` are validated strictly at
  startup; unknown or removed keys stop the service, naming each key.
- **SCOPE-13** One notification per account per run, with warnings folded in.
- **SCOPE-14** 30 days of per-run investigation records in `data/runs/`.
- **SCOPE-15** Missing account data (cash, NetLiquidation, positions feed)
  fails the run; it is never treated as zero.