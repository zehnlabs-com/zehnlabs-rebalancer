# The 1% target-sum check runs before replacements

`validate_and_scale` (normalise symbols, reject missing/duplicate/negative weights,
require the sum within 1% of 100%, then scale to exactly 1.0) always runs on the
**raw** API weights, and `apply_replacements` runs only afterwards (PRD §7.1 TGT-5,
§7.2, and Appendix A examples). Running the sum check after replacements would fail
every IRA account, because replacement rules change the total on purpose (they are
the whole reason the account deviates from a plain strategy). All implementing
agents must keep this ordering; it is the most likely thing to get wrong.
