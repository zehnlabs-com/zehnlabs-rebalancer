# Cash shortfalls are allocated by target-weight priority

One rule replaces the old bundle of buy scaling, the adjustment pass,
`max_account_utilization` and the separate affordability filter (PRD §3.1 SCOPE-3,
§7.6). When buys are sized at the ask and limited at `limit = reference × 1.005`,
their total cost can exceed the budget. On a shortfall, the algorithm starts with
the full budget and funds raw buys by target weight descending. Equal weights use
reverse-alphabetical symbol priority, preserving the rule that alphabetically
earlier symbols absorb the shortfall first. Each final quantity is capped by its raw
desired quantity and by limit-price affordability, rounded down through
`round_quantity` (whole shares for IBKR, `0.000001` for a fractional broker).

Orders are emitted in normal symbol order, so funding priority does not change
placement order. Warnings and the run record's trimmed-symbol list are derived only
from final versus raw quantities. The planner recomputes final limit-price cost and
fails closed through an explicit runtime check if it exceeds `budget + EPS`.

The plan is maximal under the target-weight priority: no underfunded raw buy can use
the final remainder to buy one more broker-sized unit. This is not a global portfolio
optimizer. The safety invariant remains unchanged: even if every buy fills at its
limit, cash stays at or above the reserve. Buys are budgeted at the limit price,
never the expected fill price.
