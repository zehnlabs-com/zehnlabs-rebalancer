# v3 intentionally changes rebalancing behaviour

Refactor 3.0.0 breaks behaviour on purpose (PRD §3.1): the 0.5-point "close enough"
allocation band is removed so every run aligns every symbol to its target; buys are
never scaled and the adjustment pass, `max_account_utilization` and the separate
affordability filter are gone; share quantities always round down (a target smaller
than one share is not bought, invalidating the old "at least one share" promise);
and the fixed $100 cash reserve plus per-account `cash_reserve_percent` are replaced
by one percentage cash floor (`trading.minimum_cash_reserve_percent`, default 0.25%).
The first run after upgrading may trade more than usual precisely because small
drifts are now corrected. A future maintainer must not "restore" the band or
rounding-up thinking it is a regression.
