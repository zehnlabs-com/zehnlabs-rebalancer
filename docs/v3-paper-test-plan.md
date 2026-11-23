# Rebalancer v3 paper-acceptance plan

Status: staged and offline-validated; awaiting paper credentials and weekend order
Candidate: `refactor/v3` at `3f02623`
Primary window: Monday, November 23, 2026, 09:00–11:30 ET (08:00–10:30 CT)

## Result this plan must produce

The final report starts with exactly one decision:

- **Ready to deploy v3** — all safety-critical checks pass and every paper order is reconciled.
- **Ready with specific caveats** — safety checks pass, but a non-safety behavior such as slow cash
  propagation or unavailable outside-hours prices needs an explicit operational caveat.
- **Do not deploy v3** — a safety check fails, required evidence is missing, or any order remains
  unresolved.

The report then gives the evidence in plain English, lists any caveats, and identifies the exact
tested source commit and image. Account IDs and credentials are always written as `[REDACTED]`.

Every individual check and the aggregate machine-readable result uses exactly one status:

- **PASS** — the required behavior was observed and cleanup was confirmed.
- **FAIL** — observed behavior contradicted an acceptance criterion.
- **INCOMPLETE** — the condition was not observed, evidence is missing or ambiguous, or cleanup
  was not confirmed.

Any safety-critical `FAIL` or `INCOMPLETE` means **Do not deploy v3**. “Ready with specific
caveats” is available only for an explicitly non-safety limitation after the related cash,
order-state, and cleanup invariants have all passed.

## Decisions already made

- Production and paper run in parallel. Production is not stopped, restarted, or modified.
- The paper app uses its normal Ably subscriptions. The session ends hours before the expected
  production signal near 15:50 ET. Any unexpected signal is recorded and its paper orders are
  reconciled.
- A sell remains a market order even when the quote has no bid. This plan does not introduce a
  no-bid limit exception.
- The user places one realistic, non-marketable, RTH-only GTC order in the selected paper account
  over the weekend and supplies its exact details. TWS is preferred because IBKR explicitly
  documents client-ID-0 binding for TWS manual orders.
- The assistant prepares and runs the remaining checks. The user is needed Monday only if the
  supplied manual order cannot be matched and cancelled safely by automation.
- All test commands use the fixed test path, Compose project, ports, and evidence directory in this
  plan. No override is accepted. Production is read only during paper acceptance: no production
  file, container, image, volume, network, order, or notification setting is changed.

## Isolation and current preparation state

| Resource | Paper candidate | Production |
|---|---|---|
| VPS path | `/home/ubuntu/docker-projects/zehnlabs-rebalancer-test` | sibling production path, untouched |
| Compose project | `zehnlabs-rebalancer-test` | `zehnlabs-rebalancer-serioustrader` |
| API/VNC host ports | `4011`, `4012`, `5901`, loopback only | existing production ports |
| Gateway API port inside test network | paper `4004`; live `4003` is forbidden to probes | existing production setting |
| Source | clean archive of `3f02623` | existing v2 deployment |
| Event-broker image | `sha256:6fb87a2918449c6bde7867fcb58d62a329e7a904b532b2ae1ffd814b2c778196` | existing image, untouched |
| Config/accounts/data | test-directory copies only | untouched |
| Gateway settings volume | test project creates its own volume at runtime | existing volume, untouched |
| Probe evidence root | `/home/ubuntu/docker-projects/zehnlabs-rebalancer-test/data/paper-acceptance` | never mounted or written |

Preparation already completed:

- The test path, project labels, and ports were proved unused before staging.
- The reviewed v3 source was copied to the test path and built successfully on the VPS.
- The guarded validation probe and 13 offline safety tests pass with networking disabled.
- No test container, network, or volume exists yet; no Gateway login has been attempted.
- `.NOT_READY` prevents treating the directory as ready until paper credentials, the selected
  paper account, and the four user-observable manual-order fields are installed.
- Production remained `running/healthy` after staging and the offline build.

Before Monday the assistant will:

1. Before writing test configuration, read the production live username privately from the fixed
   production `.env` and compare it with the proposed paper username. If either is missing or they
   are equal, fail closed without printing either value and leave `.NOT_READY` in place. Production
   is never sourced or written.
2. Create a mode-`0600` test `.env` using the distinct paper username/password, a random VNC
   password, a random test-only ntfy topic, the existing allocations API key copied server-side
   without printing it, `TRADING_MODE=paper`, project `zehnlabs-rebalancer-test`, and host ports
   `4011`, `4012`, and `5901` exactly.
3. Create a v3 `accounts.yaml` containing exactly one enabled paper account and no live accounts.
4. Create empty test-local `data/manual-rebalance`, `data/market-open`, `data/runs`, and
   `data/paper-acceptance` paths; initialize `scheduled.json` to `[]`.
5. Only after the username and fixed-project checks pass, create mode-`0600` probe sentinels:
   - `data/paper-acceptance/.project`, content exactly `zehnlabs-rebalancer-test 3f02623`;
   - `data/paper-acceptance/.paper-username-distinct`, content exactly
     `zehnlabs-rebalancer-test`.
   The wizard removes any stale `data/paper-acceptance/.ready` at startup. Only after the four
   weekend-order fields are written, paper TWS logout is confirmed, and `.NOT_READY` is removed
   does it recreate `.ready`, mode `0600`, with content exactly `zehnlabs-rebalancer-test`.
   At completion, `accounts.yaml`, the order-details file, and test `data/` are owned by the exact
   candidate image's non-root UID/GID `999:999`; files remain mode `0600` and directories mode
   `0700`. Host-side triggers and evidence collection therefore use `sudo`. This preserves the
   candidate runtime identity without making account or evidence files world-readable.
   The setup wizard never creates `.event-broker-stopped`.
6. Validate Compose mounts, project name, account mode, v3 config, internal paper port `4004`, and
   the 09:35 ET schedule
   without starting services.
7. Record production container/image/health state read-only so Monday checks can prove it was not
   changed.

## Weekend manual order

The user places exactly one manual order in the chosen paper account:

- a realistic limit that should not execute;
- quantity small enough to be harmless in paper;
- `GTC`, regular-hours only;
- status working, submitted, or pre-submitted;
- no other working order in that paper account and no other bindable manual TWS order anywhere on
  the paper login.

The private mode-`0600` order-details file contains the four fields the user can reliably supply:
symbol, side, quantity, and limit. The wizard fixes the remaining values as `LMT`, `GTC`, and `TWS`,
adds the creation time, and keeps `.NOT_READY` until those four user fields are valid. It does not
ask the user for `conId` or `permId`; Monday's read-only capture discovers them from IBKR. The wizard
never prints the paper or production username, account ID, or credentials.

After the GTC and its four fields are saved, the user logs out of paper TWS before Monday so the
isolated paper Gateway can use that username. No TWS or other paper session using that username may
remain open during Monday's test.

If the order fills, is rejected, or disappears before Monday, the foreign-order test is not run
against an ambiguous replacement. The user places a new order or the test is postponed.

## Monday execution

### 1. 09:00 ET — production and isolation gate

Automated checks:

- production event-broker and Gateway are still healthy on the recorded images;
- no test Compose container, network, or volume predates this run;
- test mounts resolve only under the test directory;
- paper mode, one active paper account, unique ports, and `.NOT_READY` removal are confirmed;
- `.project`, `.paper-username-distinct`, and `.ready` exist with the exact content above;
- paper and production usernames are rechecked privately as distinct without printing either;
- time is within the agreed morning window.

Pass: all checks agree and no production resource would be targeted.
Fail: stop before any paper login. Production continues unchanged.

### 2. 09:02 ET — start only the paper stack

Start the isolated paper Gateway, wait for its API port, verify the configured paper account is the
only selected account, then start the isolated v3 event-broker. Normal Ably subscription remains
enabled by decision.

Immediately recheck production health. If its live session becomes unhealthy or disconnected,
stop the paper project and report the conflict; do not continue.

Pass evidence:

- paper Gateway login and API connection succeed;
- event-broker reports successful allocations checks, Ably subscriptions, scheduler at 09:35 ET,
  manual watcher, and healthy heartbeat;
- production stays healthy.

### 3. 09:05 ET — outside-hours pricing and preview

The guarded probe times raw `reqTickersAsync` snapshots for a liquid control ETF and a selected
thin ETF, then calls the real v3 `IBKRConnection.quotes()` chain. Separately, the normal manual
tool runs a preview for the paper account.

Capture:

- raw snapshot duration and whether it completed inside 15 seconds;
- bid, ask, last, and close presence, recording NaN/non-positive values as missing;
- connector total duration, retries, and final mark/buy/sell sources;
- historical one-minute bar fallback when bid, ask, last, and close are all absent;
- fallback notification note when last, close, or bar supplies the price;
- preview plan, zero placed orders, and the manual GTC shown as foreign information;
- `V0`, NetLiquidation, and their percentage difference.

Pass:

- every raw request either completes within 15 seconds or produces the configured bounded timeout;
- the liquid symbol has a usable price;
- the connector returns a usable allowed fallback or a specific `NoPriceError`, never hangs;
- preview places no orders;
- normal account `abs(V0 − NetLiquidation) / NetLiquidation <= 2%`.

If the thin quote contains a stale last but no ask, record the calculated buy limit and compare it
with the first regular-hours ask. A limit below the offer documents the real non-fill risk. If the
market supplies a complete quote, report the missing-quote condition as **not observed**, backed by
the already-passing offline fallback tests; do not manufacture a trade solely to create it.

### 4. 09:20 ET — closed-market queue

Run `tools/rebalance.sh` with live execution for `[REDACTED]` before 09:30 ET, using `sudo` because
the isolated data directory is deliberately owned by the candidate container user.

Pass:

- parent returns `MARKET_CLOSED` without starting a worker or connecting to IBKR for that run;
- no order is placed;
- the account appears exactly once in test-local `scheduled.json`;
- exactly one queued notification and one run-record line agree on account, strategy, time, and outcome.

Leave the schedule entry in place for the real 09:35 job.

### 5. 09:35 ET — scheduled retry and manual foreign-order rule

Let the real scheduler fire; do not call its method directly. The weekend GTC is still working.

Expected result: the scheduled retry connects, leaves the manual order untouched, classifies it
as foreign, and ends `FAILED / FOREIGN_ORDERS`. Scheduled failures are final, so the account is
removed from `scheduled.json` and is not re-added.

Pass:

- the retry occurs once with `source=scheduled`;
- the exact manual order is visible and foreign;
- no new app order is placed and the manual order remains working;
- one final scheduled notification and one run record match;
- the schedule entry is removed.

Fail if the scheduler does not run, the order is invisible, the app cancels/modifies it, or another
order is placed.

### 6. 09:38 ET — targeted manual-order cleanup

Stop only the test event-broker. The paper Gateway and production remain running.

Using the fixed test Compose project, host orchestration first verifies that the test
`event-broker` is stopped. Only then it creates
`data/paper-acceptance/.event-broker-stopped` with content exactly
`zehnlabs-rebalancer-test`. The probe requires that marker to be no more than 300 seconds old.
The marker is removed before any event-broker start. The wizard never creates it.

First, `capture-manual` performs a read-only all-open-orders query. It requires exactly one working
order across the whole paper login, matches the private user-supplied symbol, side, quantity, limit,
`LMT`, `GTC`, and selected account exactly, requires nonzero broker `conId` and `permId`, and writes
those observed IDs into a separate mode-`0600` private captured-order JSON. It never prints the
account or either login username.

Only then may `cancel-manual` consume that captured JSON. It refuses missing/zero `conId` or
`permId`, re-queries and repeats the full account, symbol, `conId`, side, quantity, type, limit, TIF,
and `permId` match before binding. Client ID 0 calls `reqOpenOrders`, repeats the same complete match
on the bound order, cancels only that order, waits for terminal `Cancelled`/`ApiCancelled`, and
performs a fresh open-order query proving the order is absent.

It never calls `reqGlobalCancel`. If there are zero or multiple matches, another working order, a
Mobile order that does not bind, or no confirmed cancellation, automation stops. The user then
cancels the order manually at about 09:15 CT, their agreed fallback availability.

Pass: exact order cancelled once and absent from a fresh open-order query.

### 7. 09:42 ET — regular-hours pricing comparison

Repeat the liquid/thin raw snapshots and connector quote chain during the core session.

Pass:

- each raw snapshot completes under 15 seconds;
- the liquid control has live bid/ask;
- the thin result is usable or fails explicitly and within its bound;
- premarket and regular-hours timings and fields are preserved for the report.

### 8. 09:48 ET — normal full v3 paper run

Restart the test event-broker and trigger one manual live rebalance through
`tools/rebalance.sh` under `sudo`. This is the unmodified end-to-end application path; elevated
host access is needed only to write the container-owned test event file.
Remove `.event-broker-stopped` before the restart.

Pass:

- own-order cancellation starts from zero and foreign-order check is clear;
- the 2% value check passes;
- planned market sells and limit buys agree with the preceding preview;
- a normal limit buy is accepted if the plan contains one;
- every placed order reaches a terminal recorded state;
- exactly one notification and one JSONL record agree on account, strategy, time, and outcome;
- raw private worker lines contain strategy, account, and the real run ID, with no duplicate copy;
  only the sanitized export replaces the account with `[REDACTED]`.

If the current account produces no buy or sell, that is not used as evidence for the funds/cash
checks; the controlled micro-cycle below supplies that evidence.

### 9. 10:00 ET — limit-funds and cash-update micro-cycle

The user reports an existing QQQ position. Preflight must confirm at least one whole QQQ share; if
confirmed, use QQQ so the probe can sell and rebuy one existing paper share without a preparation
buy. Otherwise use the guarded preparation path described below.

Stop only the test event-broker to prevent an app-client collision. Run the guarded probe using
that liquid ETF. If the paper account does not hold a whole share, the probe first buys one share
at the same `ask + 0.5%` DAY limit formula as v3. It then:

After the fixed project reports the service stopped, create the same short-lived
`.event-broker-stopped` marker. The cash probe refuses a missing, wrong-content, or older-than-five-
minutes marker. Remove it before any later event-broker start.

1. records affordable and deliberately oversized IBKR what-if margin/funds results without
   transmitting the oversized order;
2. sells exactly one paper share through the production connector's market-order path;
3. records the fill time and polls the same USD cash field once per second;
4. records when cash reaches `cash_before_sell + 98% × gross proceeds`;
5. restores the original paper position and confirms it.

Pass:

- the affordable limit order is accepted and fills;
- the oversized what-if is never transmitted and exposes IBKR's funds/margin response;
- cash reaches the 98% threshold within 30 seconds;
- the starting position is restored and no order remains working.

If cash takes over 30 seconds but the app uses the lower reported cash and never overspends, this
is **safe degraded behavior** and leads to “Ready with specific caveats,” not an automatic unsafe
verdict. A rejected affordable buy, overstated cash, or unresolved position is a failure.

### 10. 10:12 ET — own order across reconnect

With the test event-broker still stopped, the probe requires zero working orders and places one
one-share DAY limit buy several percent below the current bid using the account-derived app client
ID and app `orderRef`. It confirms `WORKING`, records `permId`, and deliberately disconnects.

Immediately before this probe, reverify the fixed test event-broker is stopped and recreate
`.event-broker-stopped` so its content and age pass the same strict gate.

Restart the normal test event-broker and trigger a manual live run.
Remove `.event-broker-stopped` before restarting it.

Pass:

- the earlier order is visible after reconnect under the same client ID;
- the app confirms its cancellation before snapshot/planning;
- the result note says one earlier app order was cancelled;
- the order is terminal in IBKR and no own working order remains;
- the continuing run has one notification and one record.

Fail if the order is invisible, classified foreign, unconfirmed after 30 seconds, or still working.

### 11. 10:25 ET — evidence and cleanup

Stop the test event-broker first, then perform a fresh paper open-order audit. Every deliberate or
unexpected Ably paper order must have a terminal state. Unknown orders are never cancelled
automatically.

Create the short-lived `.event-broker-stopped` marker only after verifying the fixed test service
is stopped; the final audit rejects any other marker. Remove it when the audit ends.

Collect raw evidence privately under mode-`0700`
`/home/ubuntu/docker-projects/zehnlabs-rebalancer-test/data/paper-acceptance/private`; individual
files are mode `0600`. Raw app logs, run records, and run IDs contain account identifiers;
notification bodies contain private financial details. None are pasted, attached, committed, or
copied to a shared location.

Collect there:

- timestamped Gateway and worker logs;
- probe JSON for outside/RTH snapshots, manual cancellation, cash/funds, reconnect, and final audit;
- test-local `data/runs/*.jsonl`;
- test-only ntfy messages, matched to run records by account, strategy, time, and outcome;
- paper broker order states and fills;
- exact v3 source commit and image ID;
- before/after production health and image IDs.

Generate a separate sanitized export under `data/paper-acceptance/sanitized`. It replaces the full
account ID everywhere, including inside run IDs, with `[REDACTED]`; excludes credentials and the
private manual-order file; and fails if a known account ID or secret remains. Only that sanitized
export and its plain-English verdict may be shared.

Stop the paper Gateway after the final audit. Leave production running throughout. Preserve the
stopped test project and evidence until the report is accepted; then remove only resources bearing
the exact test Compose project label. Never run a global Docker prune.

Hard stop: no new test order after 11:15 ET. By 11:30 ET, work is cleanup and production-health
verification only.

## Acceptance matrix

The aggregate JSON contains the tested commit/image, one `PASS`/`FAIL`/`INCOMPLETE` result per row,
cleanup state, and the final deployment decision. Missing files, an unobserved required condition,
or an unresolved order are `INCOMPLETE`, never `PASS`. The plain-English verdict is generated from
that same aggregate rather than reinterpreting individual probe output.

| Check | Pass | Deployment-blocking failure |
|---|---|---|
| Parallel isolation | Production unchanged and healthy | Any production file/resource change or unhealthy session |
| Snapshot bound | Each raw request completes `<15s` | Hangs or valid data consistently arrives after the bound |
| Thin fallback | Usable allowed source or explicit bounded error | Invented price, unbounded request, wrong source |
| Normal NLV cross-check | Difference `<=2%` | Normal paper account trips live failure |
| Manual foreign order | Seen as foreign, untouched, blocks run | Invisible, treated own, cancelled, or trading continues |
| 09:35 retry | Queued run attempted once and removed | No attempt, duplicate, or stale schedule entry |
| Limit funds | Affordable app-style limit accepted; what-if recorded | Affordable buy rejected because app overstated funds |
| Cash update | 98% within 30s | Safe degraded is caveat; overstated cash is failure |
| Own reconnect | Visible and cancelled by next run | Invisible or cancellation unconfirmed |
| Logs | Worker lines carry strategy/account/run ID once | Missing run ID or duplicate lines |
| Records | One JSONL line per result | Missing or duplicate record |
| Notifications | One test-topic message per account/run | Missing, duplicate, or mismatched account/strategy/time/outcome |
| Cleanup | Every order terminal; test stack stopped | Any unknown or working order remains |

## Cutover after a passing paper report

Cutover is a separate explicitly approved operation.

Nothing in paper acceptance performs these steps or changes production. Cutover begins only after
the user approves a sanitized passing report and separately authorizes the production change.

1. Pin the exact tested v3 image and publish/preserve the exact source commit before touching
   production.
2. Back up production `config.yaml`, `accounts.yaml`, `.env`, both Compose files,
   `replacement-sets.yaml`, current schedule file, and v2 image ID. Preserve `data/` in place.
3. Prepare v3 configuration separately:
   - start from the complete v3 `config.yaml` schema;
   - keep the global `minimum_cash_reserve_percent`;
   - remove v2-only config sections and keys;
   - remove `cash_reserve_percent` and `pdt_protection_enabled` from every account, including
     disabled/other-mode entries;
   - retain each account's strategy, enabled flag, type, broker, and built-in replacement-set name;
   - validate the entire files with v3 before installation;
   - confirm the market-open job is 09:35 ET.
4. Stop only the production event-broker; leave the authenticated live Gateway running.
5. Before any v3 Ably subscription starts, run a one-off v3 preview directly against every enabled
   live account. Review planned market sells—including any no-bid market sell—limit buys, price
   sources, reserve, foreign orders, and the 2% NLV comparison. No account proceeds without an
   approved preview.
6. Install the approved v3 config/accounts and tested image, then start only event-broker with
   `--no-deps`.
7. Verify allocations auth, Ably subscriptions, scheduler, manual watcher, health, notifications,
   and run-record writes.
8. Keep all v2 rollback assets until a normal production Ably signal completes successfully and
   every resulting broker order is reconciled. A preview or zero-trade manual run does not satisfy
   this gate.

The first v3 run can trade more than v2 because v3 removed the drift band and buy scaling. That is
why every live preview is a mandatory cutover gate.

## Rollback

On any cutover failure:

1. Stop v3 event-broker only.
2. Reconcile any possibly placed order before allowing another run.
3. Restore the exact v2 config, accounts, Compose override, replacement-set file, and recorded v2
   image.
4. Start the v2 event-broker with `--no-deps`; do not recreate the still-authenticated Gateway.
5. Verify the old image ID, health, subscriptions, schedule file, notifications, and account access.
6. Preserve the v3 evidence and report the failed phase, redacted error, timestamps, and confirmed
   order outcomes.

## Sources

- Project source of truth: `docs/refactor-prd.md`, especially §16.5.
- Offline review: `docs/refactor-implementation-review.md`, “Needs a paper check” and its top
  re-validation results.
- IBKR confirms live and paper sessions can coexist:
  https://www.interactivebrokers.com/support/faq/content/live-paper-accounts-open-different-23298934
- Paper data-session caveats:
  https://www.interactivebrokers.com/support/faq/content/market-data-considerations-paper-trading-1136176599
- Snapshot data covers the preceding 11 seconds:
  https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data
- Closed-market live/frozen behavior:
  https://www.interactivebrokers.com/docs/tws-api/doc/md/delayed-data
- Open-order binding and same-client reconnect behavior:
  https://www.interactivebrokers.com/docs/tws-api/doc/orders/modify-order
- Individual cancellation restriction:
  https://www.interactivebrokers.com/docs/tws-api/doc/orders/cancel-order
- Account update timing:
  https://www.interactivebrokers.com/docs/tws-api/doc/accounts/updates
- NYSE 2026 calendar and hours:
  https://www.nyse.com/trade/hours-calendars
