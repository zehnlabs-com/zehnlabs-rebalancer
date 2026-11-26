# Portfolio-wide optimization for whole-share accounts

## 1. Goal

Replace the current whole-share, symbol-by-symbol sell rounding and buy removal/add-back logic with a portfolio-wide PuLP model solved by HiGHS.

For each planning snapshot, the selected broker-reachable portfolio must minimize total absolute dollar drift from the target allocations while respecting the fixed cash reserve and conservative order prices.

The execution shape remains:

1. Calculate one initial portfolio-wide plan.
2. Submit at most one sell wave and wait for it to finish.
3. Recalculate a buy-only plan from actual post-sell cash, holdings, and fresh quotes.
4. Submit at most one buy wave and wait for it to finish.

The second calculation is not another trade wave. The initial calculation's provisional buys must never be submitted.

## 2. Scope

### Required

- Use PuLP's in-process `HiGHS` adapter and the official `highspy` binding.
- Apply the new optimizer when `BrokerConnection.supports_fractional` is `False`.
- Preserve the existing fractional planner when `supports_fractional` is `True`.
- Preserve the existing broker connector, order placement, order types, order waiting, cash refresh, scheduling, retry, notification, and run-record architecture except where this specification explicitly changes planner output or failure reporting.
- Preserve one market sell leg followed by one limit buy leg.

### Not part of this change

- Do not design the quantity rules for a hypothetical fractional broker.
- Do not replace `supports_fractional` or change `BrokerConnection`.
- Do not add another sell or buy wave.
- Do not change IBKR order submission or fill handling.
- Do not add commissions to the optimization model.
- Do not run an independent optimizer in production.
- Do not add concurrency, locks, or solver work outside the existing account worker processes.

The whole-share restriction is an IBKR capability, not a universal portfolio rule. The whole-share optimizer must be isolated behind the existing `supports_fractional=False` branch so a future fractional broker is not forced through it.

## 3. Dependencies and configuration

### 3.1 Package dependencies

In `packages/rebalance-calculator/setup.py`:

- Add `pulp==4.3.1`.
- Add `highspy==1.16.0`.
- Change `python_requires` to `>=3.12`, matching PuLP 4 and the production image.

Use `pulp.HiGHS`, not `HiGHS_CMD` and not a subprocess.

### 3.2 Configuration

Add this strictly validated setting:

```yaml
trading:
  optimization_timeout_seconds: 10.0
```

Requirements:

- Default: `10.0` seconds.
- Minimum: `0.1` seconds.
- Maximum: `60.0` seconds.
- The value is the limit passed to each HiGHS solve invocation.
- Add it to `TradingConfig`, the loader's known-key tree, `config.yaml`, configuration tests, and test configuration builders.
- Pass it into `plan_sells` and `plan_buys`; the calculator package must not read application configuration.

### 3.3 Container verification

The event-broker image must run `pip check` during its build after all Python packages are installed. A dependency conflict or missing native wheel must fail the image build.

## 4. Calculator structure and public behavior

### 4.1 New optimizer module

Create `packages/rebalance-calculator/src/rebalance_calculator/optimizer.py`.

It must contain the whole-share PuLP/HiGHS model and a private typed result used by `planner.py`. Keep solver-specific objects and status handling out of `planner.py`.

Define and export an `OptimizationError` exception from `rebalance_calculator`.

The optimizer must accept all inputs explicitly. It must not read configuration, environment variables, files, the clock, or broker state.

### 4.2 Planner entry points

Keep `plan_sells` and `plan_buys` as the application-facing planner functions.

Change `plan_sells` so it receives quotes rather than only a mark mapping, plus buy slippage and the optimization timeout. It needs marks, sell references, and provisional buy limits to make the initial global sell decision.

The required behavior is:

```python
plan_sells(
    positions,
    targets,
    quotes,
    cash,
    reserve,
    slippage_pct,
    supports_fractional,
    optimization_timeout_seconds,
) -> SellPlan
```

Change `plan_buys` to receive the optimization timeout:

```python
plan_buys(
    positions_after,
    targets,
    quotes,
    cash_base,
    reserve,
    slippage_pct,
    supports_fractional,
    optimization_timeout_seconds,
) -> BuyPlan
```

For `supports_fractional=False`:

- `plan_sells` runs the full buy-and-sell model and returns only its sell orders.
- `plan_buys` runs the same model with all sells prohibited and returns its buy orders.

For `supports_fractional=True`:

- Preserve the current fractional calculations, six-decimal quantity unit, warnings, affordability behavior, and tests.
- Do not invoke PuLP or HiGHS.

`SellPlan.total_value`, `BuyPlan.total_value`, `BuyPlan.cash_base`, `BuyPlan.reserve`, and `BuyPlan.budget` retain their current meanings.

## 5. Financial definitions

Use the existing quote-selection methods and limit-price rounding rules.

For the initial plan:

```text
V0 = initial cash + sum(initial quantity × initial mark)
fixed reserve = configured reserve percentage × V0
strategy capital = max(0, V0 − fixed reserve)
target dollars[symbol] = target weight[symbol] × strategy capital
```

For the post-sell buy-only plan:

```text
V1 = cash_base + sum(post-sell quantity × fresh mark)
strategy capital = max(0, V1 − the same fixed dollar reserve)
target dollars[symbol] = target weight[symbol] × strategy capital
```

Price roles are fixed:

- Mark price measures target drift.
- Buy limit price determines the maximum cash required by a buy.
- Sell reference price estimates proceeds only for the initial model's affordability constraint.
- Actual sell fills and the existing conservative `cash_base` calculation determine the live buy-only budget.

Do not use buy limits or sell references to measure target drift.

## 6. Money representation

Use exact integer money coefficients in units of `$0.0001`.

Requirements:

- Convert financial inputs with `Decimal(str(value))`.
- Round to `$0.0001` with `ROUND_HALF_UP` before converting to integer ticks.
- Use integer ticks for cash, reserve, target dollars, marks, buy limits, sell references, drift variables, and cash constraints.
- Reject non-finite or non-positive required prices.
- Reject a model before solving if any scaled coefficient, variable bound, product bound, or objective bound can exceed `2**53 - 1`.
- Do not expose the money scale as configuration.

The production formulation uses this one scale. It must not try alternate scales or solver parameters at runtime.

## 7. Whole-share model

### 7.1 Symbols

Model the sorted union of:

- every nonzero target symbol; and
- every currently held symbol.

Aggregate duplicate positions by symbol before building the model.

Reject negative holdings with `OptimizationError`; short-position optimization is not part of this change.

### 7.2 Existing fractional residues in IBKR accounts

IBKR orders remain whole shares even if an account already contains a fractional residue.

For each current quantity:

```text
held whole shares = floor(current quantity)
immutable residue = current quantity − held whole shares
final quantity = immutable residue + final whole shares
```

Buys and sells change only the whole-share component. Never round away, buy, or sell the residue.

### 7.3 Decision variables

For each symbol create:

- nonnegative integer `final_whole`;
- nonnegative integer `buy`;
- nonnegative integer `sell`;
- nonnegative continuous `error_ticks`;
- one binary direction variable.

Required relationships:

```text
final_whole = held_whole + buy − sell
sell <= held_whole
buy <= buy_upper_bound × direction
sell <= held_whole × (1 − direction)
```

The direction constraints must make buying and selling the same symbol impossible.

### 7.4 Tight quantity bounds

For each symbol calculate the first whole-share lattice point at or above its target value after accounting for the immutable residue. That is the largest final whole-share quantity that can improve or tie that symbol's drift.

For the full initial model:

```text
0 <= final_whole <= useful_upper_bound
```

For the buy-only model:

```text
held_whole <= final_whole <= max(held_whole, useful_upper_bound)
sell = 0
```

Do not use arbitrary large big-M values. Derive every upper bound from current holdings and the useful upper bound.

### 7.5 Absolute drift

For each symbol:

```text
final value ticks = residue value ticks + final_whole × mark ticks
error_ticks >= final value ticks − target dollar ticks
error_ticks >= target dollar ticks − final value ticks
```

A held non-target symbol has target dollars of zero. The model must therefore sell every broker-tradable whole share while leaving any immutable residue.

### 7.6 Cash constraints

Full initial model:

```text
projected cash = cash
               + sum(sell × sell-reference ticks)
               − sum(buy × buy-limit ticks)

projected cash >= fixed reserve
```

Buy-only model:

```text
spendable cash = max(0, cash_base − fixed reserve)
sum(buy × buy-limit ticks) <= spendable cash
sell = 0
```

Cash already below reserve is a feasible buy-only state that produces no buys. The buy-only model must not require an account to create reserve cash when selling is prohibited.

### 7.7 Optimization hierarchy

Use sequential lexicographic solves. Every stage must retain the exact optimum established by earlier stages.

1. Minimize total `error_ticks` across all modeled symbols.
2. Order symbols by target weight descending, then symbol ascending. Held non-targets have weight zero and come last.
3. In that order, minimize each symbol's `error_ticks` while retaining all earlier optima. This preserves allocation priority only when total portfolio drift is exactly tied.
4. In the same order, minimize each symbol's `final_whole` while retaining all error optima. This chooses the lower quantity for an exact half-share tie and makes the result deterministic.

After each successful stage, constrain that integer-valued objective to its proven optimum before starting the next stage.

Read each stage's objective value as follows:

- it must be finite;
- it must be within `0.25` tick or unit of the nearest integer;
- round it to that nearest integer; and
- constrain the expression to be less than or equal to that integer for later stages.

If the value is outside that tolerance, raise `OptimizationError` rather than weakening the earlier optimum.

Do not use allocation priority to accept a portfolio with greater total drift.

## 8. Solver policy

Construct PuLP 4 variables with the PuLP 4 API and solve with:

```python
pulp.HiGHS(
    mip=True,
    msg=False,
    gapRel=0.0,
    gapAbs=0.0,
    threads=1,
    timeLimit=optimization_timeout_seconds,
    parallel="off",
    random_seed=0,
    mip_feasibility_tolerance=1e-7,
    primal_feasibility_tolerance=1e-7,
)
```

For every lexicographic stage, accept the result only when all are true:

- `stats.status == pulp.LpSolveStatus.Optimal`;
- `stats.has_solution` is true; and
- `problem.solverModel.getModelStatus() == highspy.HighsModelStatus.kOptimal`.

Any other status, unavailable solver, invalid option, solver exception, timeout, or missing solution raises `OptimizationError`.

Do not accept an incumbent from a time, node, iteration, solution, memory, gap, objective, interrupt, or numerical limit.

Instantiate and solve inside the existing account worker process. Do not initialize or smoke-solve HiGHS in the parent process before the process pool forks.

## 9. Result conversion and safety postconditions

Trust an accepted `Optimal` result. Do not run a second optimizer or exhaustive search in production.

Before creating `PlannedOrder` objects, perform these direct postconditions:

- Every solver value used by the plan is finite.
- Every whole-share variable is within `1e-6` of an integer before rounding.
- Final whole shares, buys, and sells are nonnegative.
- `final_whole == held_whole + buy − sell`.
- Sells do not exceed held whole shares.
- A symbol is not both bought and sold.
- Every order quantity is a positive whole number.
- Recomputed initial projected cash is at least the reserve.
- Recomputed buy-only cost does not exceed `max(0, cash_base − reserve)`.
- Recomputed values stay within the safe money bounds.

Use application arithmetic for these checks. They validate order safety; they do not re-evaluate whether HiGHS found the optimum.

Any failed postcondition raises `OptimizationError` and produces no orders from that planning call.

## 10. Buy-plan notes and trimmed symbols

Keep `BuyPlan.trimmed_symbols` and the run record's `trimmed` field.

For the whole-share optimizer, define each target's baseline as the independently closest broker-reachable final quantity when cash is ignored but the buy-only lower bound `final quantity >= current quantity` is enforced. Compare the reachable quantities immediately below and above the target where both satisfy that lower bound; exact error ties choose the lower quantity. If the current holding is already above target, the baseline is the current holding because the buy-only phase cannot sell.

A symbol is `trimmed` when the optimized final quantity is below that individually closest buy-only baseline because the global cash constraint allocates the budget elsewhere.

Requirements:

- `trimmed_symbols` is sorted alphabetically.
- The insufficient-cash warning names every and only final trimmed symbol.
- A symbol restored to its baseline is not trimmed.
- If `cash_base <= reserve`, preserve the existing single cash-at-or-below-reserve warning, place no buys, and leave `trimmed_symbols` empty.
- Preserve the existing informational note when an unheld target is smaller than one broker-tradable share.
- Preserve the existing warning for an untradeable fractional residue in a non-target holding.

The old removal order, add-back order, and raw-floor quantity are no longer part of whole-share behavior and must not appear in messages or tests.

## 11. Application integration

### 11.1 Initial planning

In `rebalance_account`:

- Keep fetching `q0` for the union of targets and holdings.
- Keep calculating `V0` and the fixed dollar reserve before planning.
- Pass `q0`, slippage, broker fractional capability, and optimization timeout to `plan_sells`.
- The initial model may calculate provisional buys internally, but only its sell orders enter `planned_sells` or order placement.
- A required missing mark, sell reference, or provisional buy reference is a pricing failure, not an optimization failure.

### 11.2 Preview

Preview must:

- Use the optimized initial sell orders.
- Simulate those sells at `q0.sell_reference()` exactly as today.
- Build post-sell positions exactly as today.
- Run the buy-only optimizer using simulated cash, the same fixed reserve, and `q0`.
- Report only the final buy-only orders as `planned_buys`.
- Place no orders.

### 11.3 Live execution

Live execution must:

- Place the optimized sell orders once and wait for completion.
- Preserve the existing actual-fill and conservative `cash_base` calculation.
- Preserve the existing construction of `positions_after`.
- Fetch fresh `q1` exactly as today.
- Run the buy-only optimizer using `positions_after`, `q1`, actual conservative `cash_base`, and the same fixed reserve.
- Place the resulting buys once and wait for completion.

Never submit the initial model's provisional buys.

### 11.4 Failure handling

Add `FailureReason.OPTIMIZATION_FAILED`.

Catch `OptimizationError` around every planner call:

- Initial-plan failure: return `FAILED / OPTIMIZATION_FAILED`; place no orders.
- Preview buy-plan failure: return `FAILED / OPTIMIZATION_FAILED`; place no orders.
- Live buy-plan failure after completed sells: return `FAILED / OPTIMIZATION_FAILED`; preserve the executed sell states in the result and place no buys.

Existing parent retry behavior applies without special cases.

Catch `NoPriceError` raised while building either optimization model and report `PRICING_FAILED`.

## 12. Required tests

### 12.1 Configuration and dependency tests

- Default optimization timeout is `10.0`.
- Values below `0.1` and above `60.0` are rejected.
- The strict configuration loader accepts the new key and still rejects unknown keys.
- The built event-broker image imports PuLP and highspy, reports the pinned versions, and passes `pip check`.

### 12.2 Exact whole-share planner examples

Every example must assert final quantities, sells, buys, ending projected cash, per-symbol drift, and total drift where applicable.

Exercise the optimizer's private typed result directly for complete full-model assertions and exercise `plan_sells` / `plan_buys` for caller-visible planner behavior. Solver-specific objects must remain private to `optimizer.py`.

#### A. Global sell decision

```text
Cash: $0
Reserve: $0
Holdings: A 1 at $100; B 90 at $10
Targets: A 6%; B 94%
Buy limits and sell references equal marks
```

Required initial result:

```text
Sell A 1
Provisional buy B 4
Final A 0, B 94
Cash $60
Total drift $60
```

The $80-drift result that retains A is a failure.

#### B. Unconstrained nearest quantities

```text
Cash: $1,000
Reserve: $100
Targets: H 80%; M 15%; L 5%
Marks and limits: H $100; M $20; L $10
No holdings
```

Required final quantities: `H 7, M 7, L 4`. Total drift: `$30`. The exact half-share tie for L chooses 4.

#### C. Nearest quantities do not fit

```text
Cash: $1,000
Reserve: $250
Targets: H 80%; M 15%; L 5%
Marks: H $100; M $20; L $10
Limits: H $100.50; M $20.10; L $10.05
No holdings
```

The individually nearest `6/6/4` costs `$763.80` and does not fit the `$750` budget.

Required result: `H 6, M 5, L 4`; cost `$743.70`; total drift `$15`.

#### D. Closeness overrides old removal priority

```text
Cash: $1,000
Reserve: $0
Targets: H 90%; L 10%
Marks and limits: H $20; L $111.11
No holdings
```

Required result: `H 44, L 1`; total drift `$31.11`.

The old `H 45, L 0` result with `$100` drift is a failure.

#### E. Multiple shares below floor

```text
Cash: $1,000
Reserve: $0
Target: A 100%
Mark: $1
Limit: $1.01
No holdings
```

Required result: buy 990, not 1,000 and not 999.

#### F. Actual sell proceeds change buys

Use example A's initial sell decision, but simulate an actual A fill of `$80`.

The post-sell snapshot is cash `$80`, A 0, B 90, with marks unchanged.

Required buy-only result: buy B 2. No second sell is allowed.

#### G. Fractional residue on a whole-share broker

```text
Cash: $0
Reserve: $80
Holding: A 10.4 at $100
Target: A 100%
```

Required initial result: sell exactly 1 and end at 9.4 shares. The 0.4 residue is unchanged.

#### H. Empty targets

With A 2.4 shares and no targets, sell exactly 2 and retain only 0.4.

#### I. Cash below reserve in buy-only mode

With cash below reserve, assert no buys, no sells, no infeasibility, the existing warning, and an empty `trimmed_symbols`.

### 12.3 Tie and determinism tests

- Higher-weight tie: cash `$100`, targets A 60% and B 40%, marks/limits A `$80` and B `$40`; only one combination fits. Required result is A 1, B 0.
- Equal-weight tie: cash `$100`, targets A 50% and B 50%, both marks/limits `$60`; required result is A 1, B 0 because symbol A has priority.
- Exact half-share quantity tie: cash `$100`, targets A 50% and B 50%, A mark/limit `$100`, B mark/limit `$1,000`, and no holdings. A's target is exactly half a share, B remains at zero, and both A 0 and A 1 produce the same total drift. Required result is A 0, B 0 because the lower reachable A quantity wins.
- Reversing every input dictionary's insertion order produces identical plans.
- Repeating the same model at least 100 times produces identical plans.
- Planned orders remain emitted in symbol order even though priority is used internally for exact ties.

### 12.4 Independent model tests

In tests only, implement a small exhaustive whole-share oracle that does not import optimizer helpers.

Requirements:

- Deterministic random seed.
- At least 500 generated portfolios.
- One to four symbols.
- Small bounded quantities so complete enumeration is feasible.
- Test both full buy-and-sell mode and buy-only mode.
- Include cash, reserve, spreads, existing holdings, non-target holdings, and exact ties.
- Rank candidates by the optimization hierarchy in section 7.7.
- Compare the complete selected final quantities, buy quantities, sell quantities, projected cash feasibility, per-symbol errors, and total error.

This oracle is a test of the model definition. It must not be imported by production code.

### 12.5 Solver and postcondition failures

Provide a narrow private solve seam that tests can patch without recreating the model.

Assert `OptimizationError` for:

- solver unavailable;
- `Infeasible`;
- `TimeLimit`, including a feasible incumbent;
- `NodeLimit`;
- `GapLimit`;
- `NumericalError`;
- `stats.has_solution == False`;
- PuLP `Optimal` with underlying HiGHS status not `kOptimal`;
- solver exception;
- non-finite result;
- integer variable outside the `1e-6` integrality tolerance;
- buy and sell on the same symbol;
- sell exceeding holdings;
- projected cash below reserve;
- buy-only cost above its spendable cash;
- unsafe scaled numeric bounds;
- negative holdings.

No failure test may produce a `PlannedOrder`.

### 12.6 Fractional regression tests

For `supports_fractional=True`:

- Assert PuLP/HiGHS is not invoked.
- Keep the existing six-decimal rounding, affordability, reserve, deterministic, warning, and randomized differential tests passing.

### 12.7 Rebalance orchestration tests

- Preview and live use identical initial optimized sells for the same initial snapshot.
- Preview uses only its simulated post-sell buy-only result.
- Live uses only its actual post-sell buy-only result.
- Provisional initial buys are never placed or recorded as final planned buys.
- Sells are all placed before any buy.
- There is at most one sell leg and one buy leg.
- An initial optimization failure places no orders and returns `OPTIMIZATION_FAILED`.
- A preview buy optimization failure places no orders and returns `OPTIMIZATION_FAILED`.
- A live post-sell optimization failure preserves completed sell executions, places no buys, and returns `OPTIMIZATION_FAILED`.
- A missing required optimizer price returns `PRICING_FAILED`.
- Existing order rejection, partial fill, timeout, market-close, foreign-order, cash-refresh, and success behavior remains passing.
- Run records contain only final post-sell `planned_buys` and the final `trimmed` symbols.

## 13. Documentation changes

Update every current-behavior document that describes floor rounding or removal/add-back priority:

- `README.md`: describe portfolio-wide whole-share optimization, the initial global sell decision, and the post-sell buy-only recalculation.
- `docs/rebalancing-logic.md`: replace the old selling and buying algorithms with the objective, constraints, broker-dependent quantity branch, execution sequence, and optimization failure behavior from this specification.
- `AGENTS.md`: update the package summary, trading logic, configuration example, and cash-reserve section so future agents do not restore the old algorithm.
- `CHANGELOG.md`: replace the stale Unreleased removal/add-back statement with this behavior change and the new optimization timeout setting.
- Create `docs/adr/0010-portfolio-wide-optimization.md`. It must record the decision to use PuLP with native HiGHS for whole-share accounts, the total-absolute-dollar-drift objective, exact-tie hierarchy, one-sell/one-buy execution shape, post-sell buy-only recalculation, and fractional-path scope.
- Add a clear `Superseded by ADR 0010` notice to `docs/adr/0003-cash-constraint-by-lowest-weight-trim.md`.
- Add a note to `docs/adr/0009-reserve-outside-strategy-capital.md` that ADR 0010 supersedes its raw-buy floor-sizing implementation while preserving its fixed-reserve and target-base decision.

Historical refactor research, implementation logs, reviews, and the 3.0.0 changelog section describe past behavior and must not be rewritten.

## 14. Acceptance criteria

The change is accepted only when all are true:

1. Every whole-share planner and orchestration requirement above is implemented.
2. The fractional planner remains behaviorally unchanged and does not invoke the solver.
3. The complete existing test suite plus all new tests passes.
4. The 500-case exhaustive differential test passes for both full and buy-only modes.
5. `git diff --check` passes.
6. The event-broker Docker image builds successfully on Docker Desktop.
7. The built image passes `pip check` and imports exactly PuLP 4.3.1 and highspy 1.16.0.
8. A tiny integer model solved inside the built image returns PuLP `Optimal`, `has_solution=True`, and HiGHS `kOptimal`.
9. No test or implementation uses production accounts, production credentials, or live order submission.
10. No commit, push, deployment, or production change is part of this implementation.
