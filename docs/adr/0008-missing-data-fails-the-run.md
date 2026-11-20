# Missing account data fails the run; nothing is ever defaulted to zero

Account data that is missing — cash, NetLiquidation, or the positions feed — fails
the run with an error instead of being treated as zero (PRD §3.1 SCOPE-15, §9 IB-2/3).
Defaulting a missing cash or NetLiquidation value to zero would value the account as
worth nothing and could sell the entire portfolio; defaulting a missing position as
zero would silently drop holdings. Every quote field is likewise converted to `None`
when NaN or non-positive, and a symbol with no price at all raises `NoPriceError`, so
a run cannot guess a price with a fixed dollar offset (the old `bid + $1.00` rule is
deleted). The reserve is always a percentage of total value, never a fixed dollar floor.
