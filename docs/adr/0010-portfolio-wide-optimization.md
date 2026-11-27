# Whole-share accounts use portfolio-wide optimization

For a broker with `supports_fractional=False`, use PuLP 4.3.1 with its in-process
`HiGHS` adapter and the official highspy 1.16.0 binding. The model selects final
whole-share quantities for the entire account at once and minimizes total absolute
dollar drift from target allocations. Existing fractional residues are immutable;
buys and sells change only whole shares.

Cash feasibility uses conservative prices: sell references in the initial model
and buy limits in both models. The fixed reserve from the start-of-run value remains
outside strategy capital. Money coefficients use integer $0.0001 ticks.
HiGHS scales its internal bounds by a power of two near the tick-to-dollar
conversion. This keeps large monetary bounds numerically tractable across
the optimization stages while preserving the discrete share units.

Ties are resolved without accepting greater total drift: first minimize total drift,
then each symbol's drift in target-weight-descending and symbol-ascending order,
then final share quantities in that order. The final step chooses the lower quantity
for an exact half-share tie and makes results deterministic.

Execution remains one sell wave followed by one buy wave. The initial model makes
the global sell decision, but its provisional buys are never submitted. After sells
complete, a buy-only model uses actual conservative cash, remaining holdings, fresh
quotes, and the same reserve. Preview follows the same shape with simulated sell
proceeds.

Every lexicographic stage must be proven optimal by both PuLP and native HiGHS.
Timeouts, limits, unavailable solvers, numerical failures, unsafe coefficients, or
failed result postconditions produce `OPTIMIZATION_FAILED` and no orders from that
planning call. A live buy-plan failure may occur after the completed sell wave; the
run preserves those sell executions and submits no buys.

The fractional-capable broker path remains the existing six-decimal planner and does
not invoke PuLP or HiGHS. ADR 0010 supersedes ADR 0003 for whole-share accounts and
the raw floor-sizing portion of ADR 0009 while preserving ADR 0009's reserve decision.
