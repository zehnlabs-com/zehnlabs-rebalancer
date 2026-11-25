# The cash reserve is outside strategy capital

The configured reserve is a real cash allocation, not part of the capital divided
among strategy targets. At the start of each run:

1. `V0 = starting cash + marked holdings`.
2. `reserve = minimum_cash_reserve_percent / 100 × V0`.

That dollar reserve is calculated once and reused unchanged for the whole run. Sell
desired values use `weight × max(0, V0 - reserve)`. After sells, the planner computes
`V1 = cash_base + marked remaining holdings`; buy desired values use
`weight × max(0, V1 - reserve)`. A raw buy is the positive desired-value-minus-held-
value gap divided by the buy reference and rounded down to broker granularity.

The buy budget remains `cash_base - reserve`. It is only a cash constraint and is
not a second target-allocation base. This distinction ensures existing holdings are
included when sizing buys and makes a fully invested account sell enough broker-
granular quantity to establish the reserve where possible.
