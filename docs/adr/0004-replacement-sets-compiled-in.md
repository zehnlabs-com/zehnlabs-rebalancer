# Replacement sets live in code, not configuration

`replacement-sets.yaml` and its Docker mount are deleted; replacement rules are a
code constant `REPLACEMENT_SETS` with definition validation run at import time (PRD
§3.1 SCOPE-7, §7.2). The rules encode which volatile symbols an IRA-type account
cannot hold and what it holds instead (e.g. UVXY → VXX with scale 1.5), and any rule
broken at import stops the service from starting. Moving them to code makes them
versioned with the software they affect rather than an unversioned mounted file, and
the replacement weight is protected when it conflicts with ordinary targets because
shrinking the volatile exposure moves the account furthest from the strategy — do
not "balance" replacements back down.
