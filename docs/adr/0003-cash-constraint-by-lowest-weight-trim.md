# Cash shortfalls use removal and add-back priority

When raw buy quantities cost more than `cash_base - reserve` at their limit prices,
the planner starts with the complete raw plan and uses one priority order for two
passes: target weight ascending, then symbol descending for equal weights.

The removal pass visits each candidate in that order. It removes a candidate
completely when the remaining plan is still over budget; otherwise it retains the
maximum broker-granular quantity that makes the complete plan fit and stops. The
add-back pass visits candidates once in the same order and restores the maximum
removed quantity that fits, never above the raw quantity. An unaffordable next unit
does not block a later, cheaper candidate. Both passes use bulk unit arithmetic, not
one loop iteration per share or microshare.

Orders are emitted in normal symbol order, so priority does not change placement
order. Warnings and the run record's trimmed-symbol list are derived only from final
versus raw quantities; a fully restored candidate is not reported as trimmed.

The planner fails closed unless final limit-price cost is at most `budget + EPS`,
every final quantity is within `[0, raw]` and respects broker granularity, and the
final remainder is insufficient to restore one more broker unit of every still-
trimmed candidate. This is a deterministic priority rule, not a global cash-use or
portfolio optimizer.
