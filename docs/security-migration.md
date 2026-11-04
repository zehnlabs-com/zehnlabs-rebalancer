# Unified-key subscriber migration

These are proposed operator/subscriber instructions for the coordinated security release.
No deployment, credential replacement, container recreation, notification, or trade is performed
by adding this document. The website security decision record is authoritative; the older
`docs/research/subscriber-runtime-requirements.md` describes a separate proposed hosting design.

## Subscriber steps at the announced cutover

1. Wait until the operator confirms the updated Worker and authenticated Telegram webhook are
   available. In the private `FintechZL_bot` chat, send `/api`. Copy the complete key into a safe
   place; it is shown once. An existing key cannot be redisplayed. Use `/api regen` if it was lost
   and update every installation/client using the previous key. Generation/regeneration retains
   paid eligibility; a linked customer can still use `/api revoke` after cancellation.
2. Arrange a maintenance window before the next expected strategy or scheduled event. Check
   whether an account operation is still running. Let admitted work finish and reconcile any
   uncertain broker result before updating. Keep the Gateway volume, `data/` directory,
   `accounts.yaml`, replacement sets, and trading configuration. Stop the old event-broker before
   starting its replacement; never run old and new trading workers together.
3. Install the operator-approved release artifact in the existing installation. Preserve private
   files locally. Set `ALLOCATIONS_API_KEY` in `.env` to the new unified Zehnlabs key, retain
   `ALLOCATIONS_BASE_URL=https://workers.fintech.zehnlabs.com/api/v1/strategies` unless the operator
   supplied a different API, and remove the obsolete `REBALANCE_EVENT_SUBSCRIPTION_API_KEY`.
   Merge the release's Compose/config changes, including `api.rebalance_auth_timeout_seconds: 10`.
   The application ignores `REALTIME_API_KEY`; there is no legacy credential fallback.
4. Build the approved image from a separate source-only context containing reviewed application
   source and packages, with no `.env`, account files, replacement sets, `data/`, or credentials.
   Do not send the private instance directory as a Docker build context. After authorization:

   ```bash
   docker build -f <source-only-context>/event-broker/Dockerfile -t <approved-release-tag> <source-only-context>
   ```

   Configure the deployment's reviewed Compose release artifact to use that image (or its
   source-only build context). If distributing a prebuilt image, install the approved image
   reference and pull/load that artifact instead. From the private instance directory, run:

   ```bash
   docker compose stop event-broker
   docker compose up -d --no-deps --force-recreate event-broker
   docker compose logs --since 10m event-broker
   ```

   A plain `docker compose restart` cannot apply new code, Compose, or `.env` values.
5. Check that the expected enabled accounts for `TRADING_MODE` are loaded, every expected strategy
   subscribes once, the Ably connection establishes, and the scheduler starts. Review the pending
   scheduled/manual event files before activation: automatic events can cause real orders once
   the service runs. The existing Docker healthcheck alone does not establish live subscription
   health. Any end-to-end trade/preview verification requires the subscriber's separate deliberate
   action and broker reconciliation; this implementation task performs none.

The release causes an interruption between Worker activation and client/key migration. Old
Hashids allocation keys and old raw Telegram identity clients fail authentication. Updating the
key without installing this code still leaves the old rebalancer missing its separate Ably key.
Keep a list of all installations using the customer key so regeneration reaches each one.

## Startup failures

| Failure | Action |
| --- | --- |
| Missing `ALLOCATIONS_API_KEY` | Obtain/save the key with `/api`, set it in the active environment, and recreate the container. |
| HTTP 401 | Check that the complete key was copied. If lost/revoked/replaced, use `/api regen`, update every installation, and recreate. The key itself must not appear in logs or support messages. |
| HTTP 403 | Check account/subscription eligibility with `/me`; contact support for an unresolved entitlement problem. Repeated regeneration does not create eligibility. |
| HTTP 503, timeout, transport/JSON error | Check Worker/API availability and configuration. Startup fails before scheduler/manual watchers run; Docker's existing restart policy may retry process startup. Do not add a refresh loop or bypass. |
| Invalid base URL | Use an HTTP(S) allocation base ending in `/strategies`, without URL credentials, query, or fragment. Prefer HTTPS remotely. `/rebalance-auth` belongs at the API root. |
| Invalid response | Stop and contact the operator. The client requires `status: "success"` and a nonempty Ably app key in `ablyApiKey`; it does not reinterpret tokens or reuse an old configured Ably key. |

No response body, Zehnlabs key, or returned Ably key is logged by the startup handoff. The HTTP
request does not follow redirects, so the credential cannot be forwarded to a redirect target.
The returned shared key stays in process memory and is supplied directly to `AblyRealtime`.
Ably remains pinned at 2.0.12. Its known normal-WebSocket-EOF reconnect limitation remains;
ordinary protocol disconnect/reconnect preservation is checked with simulated transports.

## Manual shared-Ably-key rotation

This is separate from `/api regen`, the Telegram webhook secret, and routine deployments.
The operator chooses the cutover window, obtains a replacement with the same read-only
capabilities through the Ably Dashboard, and publishes it through the Worker's existing
`ABLY_API_KEY_SUB` configuration. Never substitute the publisher/management credential.
Subscribers restart/recreate their event-broker at the announced time to fetch that replacement.
Only after confirmations should the operator retire the old Ably key; revocation disconnects
remaining sessions. A running SDK retains its old key and does not call the Worker again.
No rotation command or Ably management credential is added here.

## Recovery and release gates

Roll forward with the new key/code where possible. Restoring old client code does not make an
old allocation key valid on the new Worker. Revoked Zehnlabs keys cannot be recovered from their
digests, and retiring an Ably key disconnects remaining users. Never replay missed historical
events automatically or assume a failed/partial trade made no broker changes. Reconcile actual
positions/orders before any deliberate action.

Local tests use fake HTTP/Ably/broker boundaries, including the actual pinned SDK with simulated
socket reconnects. All 20 tests also passed in a source-only Linux image with networking disabled
on 2026-11-02, and its installed dependencies passed `pip check`. This establishes no live Telegram,
Worker, Ably-capability, Linux-network, notification-continuity, or brokerage readiness. Before
release, perform separately authorized nontrading deployed-client checks; record expected
subscriptions/listeners, account routing, startup errors, and reconnect behavior.
