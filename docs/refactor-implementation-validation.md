# Handoff prompt: validate the rebalancer refactor implementation

Paste everything below the line into a fresh session.

---

You are reviewing a finished implementation of the Zehnlabs rebalancer refactor. This system places
real orders in real brokerage accounts, so a missed bug costs real money. Your job is to find out,
with evidence, whether the implementation does what the PRD requires: no more, no less, and without
new bugs. **Do not change any code in this session** unless I explicitly ask. Write findings only.

## Where things are

- Repository: `C:\Users\mharo\source\zehnlabs\zehnlabs-rebalancer` (WSL:
  `/mnt/c/Users/mharo/source/zehnlabs/zehnlabs-rebalancer`). The implementation is on branch
  `refactor/v3`. Compare it against the branch point on `main`.
- The specification: `docs/refactor-prd.md`. It is the source of truth. Every requirement has an
  ID (e.g. `PLAN-…`, `BUY-1`, `IB-10`).
- The decisions behind it: `docs/refactor-validation.md` §0, §0A (including §0A.0 and §0A.6).
  Read them for intent; the PRD wins where they differ.
- The implementers' log: `docs/refactor-implementation-log.md`. It lists their claims, deviations and
  open questions. Treat it as claims to verify, not facts.
- Load the `zehnlabs-rebalancer` skill first (settled decisions; do not re-open them).
- My offline validation workspace: `/opt/data/workspace/zehnlabs-rebalancer-validation/`.
  `prd_reference.py` computes the PRD's Appendix A numbers; `decided_planner.py` and
  `decided_planner_test.py` are a prototype of the planning rules and their invariant tests;
  `harness.py` and the `*_probe_test.py` files reproduced the old bugs against the old code.

## What to do, in order

1. **Read before judging.** Read the PRD fully, then the diff (`git diff main...refactor/v3 --stat`,
   then every changed file), then the implementation log. Do not base a conclusion on the log or
   the PRD alone. Check the code.
2. **Run the tests** in a clean Python 3.12 environment, as PRD §16.1 describes, with ib_async
   **2.1.0**. Record the exact command and result. If Python 3.12 is not available, say so and use
   what is. Note any difference that could matter.
3. **Traceability.** Build a table with one row for every requirement ID in the PRD:
   implemented? (yes / partly / no / deviates), where (file:line), a test that proves it (test name, or
   "none"), and a comment. Unproven requirements are findings.
4. **Recompute independently.** Run `prd_reference.py` and compare its Appendix A numbers with the
   implementation's tests. Then write your own small scripts (in the workspace, not the repository)
   that call the implemented planner on the Appendix A inputs and on several thousand random
   accounts. Check PRD §7.7 P1–P7 yourself: do not trust the implementers' property test without
   reading it.
5. **Adversarial review of the high-risk areas.** For each item, read the code path end to end and
   try to break it with a test script or a fake:
   - PRD §17.2: all thirteen "easy to get wrong" items.
   - Missing or partial account data: missing cash tag, missing NetLiquidation, both cash tags
     present, positions feed empty while NetLiquidation is large (the 2% cross-check), a non-STK
     position, a negative position.
   - Order lifecycle: empty status, unknown status, partial fill then timeout, rejection mid-leg,
     `place()` raising after some orders were placed, own vs foreign orders, cancel not confirmed.
     Confirm the exact `fetchFields` passed to `connectAsync`.
   - Cash: the post-sell `cash_base = min(...)` rule, the reserve computed once from V0, a budget ≤ 0,
     and buys budgeted at the limit price, so that cash ≥ reserve even if every buy fills at its limit.
   - Targets: the 1% check runs before replacements; the empty list sells everything; zero weights;
     duplicates after upper-casing; the overflow case (UVXY 70 / SPY 30 → VXX 100); two rules mapping
     to one target.
   - Retry and scheduling: every row of PRD §10.5; scheduled runs are final; SCH-3 and SCH-7;
     misfire settings; scheduler disabled; a strategy that is already running.
   - Executor: the running mark is released before notifications; a broken pool is replaced and
     reported; the `fork` context; workers never read `get_config()` or the environment.
   - Notifications: exactly one per account per run; size cap; no secrets; masking; the right kind
     for each outcome.
   - Startup: strict `config.yaml` and `accounts.yaml` messages; old files are refused; the strategy
     check; the heartbeat healthcheck can actually fail.
6. **Scope check.** List every change in the diff that no PRD requirement asks for (new settings,
   abstractions, locks, retries, dependencies, changed tools, changed Dockerfile Python version,
   moved `app = EventBrokerApp()`). For each: harmless, risky, or must revert.
7. **Removals.** Run the PRD §14 search and report anything still present.
8. **Docs.** Check README, AGENTS.md, `docs/rebalancing-logic.md` and CHANGELOG against the code (not
   against the PRD). Anything the docs promise that the code does not do is a finding.
9. **Old bugs.** Confirm each old defect from `docs/refactor-validation.md` §5 (N1–N11) and §0A
   (R1–R15) is fixed in the new code, or is no longer reachable. Give evidence for each.

## Rules

- **Never connect to a real IB Gateway (live or paper) and never send a real ntfy message.** If a
  question can only be answered against a broker, list it under "needs a paper check" and stop. A
  paper login may disconnect production. I must be told before any paper test, and I decide.
- Claims must come from code you read, a command you ran, or vendor documentation you cite. Label
  hypotheses as hypotheses.
- Write scratch scripts only under `/opt/data/workspace/zehnlabs-rebalancer-validation/` (or the
  scratch directory), never inside the repository.
- Never copy account numbers or credentials from `accounts.yaml` or `.env` into any output. Write
  `[REDACTED]`.

## Output

1. Write `docs/refactor-implementation-review.md` in the repository. Draft it in the workspace and
   copy it in, because direct writes under `/mnt/c` may be refused. It contains:
   - **Result first:** ready for paper checks / ready after listed fixes / not ready, in one paragraph.
   - **Findings**, ordered by severity (critical = can lose money or trade wrongly; high = wrong
     behaviour a user will notice; medium; low). Each finding gives what is wrong, where (file:line),
     how you showed it (command or script and its output), the PRD requirement it breaks, and the
     smallest fix.
   - The traceability table (step 3).
   - Out-of-scope changes (step 6), removal leftovers (step 7) and doc mismatches (step 8).
   - Old-bug status (step 9).
   - "Needs a paper check" list, merged with PRD §16.5.
2. In chat: a short plain-English summary that leads with the result. Then explain the
   single most important finding with enough context for me to decide, and ask one question at a
   time. Use plain words, explain any jargon, and give a concrete example.
