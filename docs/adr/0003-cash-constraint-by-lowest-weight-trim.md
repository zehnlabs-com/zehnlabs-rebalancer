# Cash shortfalls are absorbed by trimming the lowest-weight buys

One rule replaces the old bundle of buy scaling, the adjustment pass,
`max_account_utilization` and the separate affordability filter (PRD §3.1 SCOPE-3,
§7.6). When buys are sized at the ask and limited at `limit = reference × 1.005`,
their total cost can exceed the budget; the algorithm removes whole shares from the
**lowest-weight** buys first (ties by symbol) until `Σ(qty × limit) <= budget`,
dropping a buy entirely at zero before moving up. Property P4 makes this the safety
invariant: even if every buy fills at its limit, cash stays at or above the reserve.
Buys are always budgeted at the limit price, never the ask.
