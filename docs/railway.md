# Railway template contract

Railway Hobby or higher is required. Free is unsupported because this stack needs two volumes.

Railway's repository files cannot completely publish a multi-service template, so this document is the reproducible, reviewable definition used to create the external template.

**External acceptance status: PENDING.** No external template URL or README Deploy button is published. The production-image paper pilot, Railway project verification, backup schedules, and paper-mode restore drill require separately supplied paper credentials and Railway access and were not performed as part of this repository change.

## Template inputs

Define these shared template inputs before creating either service:

| Input | Required value |
|---|---|
| `TRADING_MODE` | Subscriber selects exactly `paper` or `live`; default `paper`. Both services reference the same value. |
| `IB_USERNAME` | Required subscriber input. |
| `IB_PASSWORD` | Required masked subscriber input. |
| `ZEHNLABS_API_KEY` | Required masked subscriber input. |
| `VNC_PASSWORD` | Required masked subscriber input, even though VNC is not public. |

## Services

### `rebalancer`

- source: `https://github.com/zehnlabs-com/zehnlabs-rebalancer`, repository root build context;
- Dockerfile: `rebalancer/Dockerfile`;
- replicas: `1`;
- volume: `rebalancer-state`, mounted at `/app/state`;
- public networking: one Railway-generated HTTPS domain targeting internal HTTP port `18778`; do not assign a fixed external URL in the template definition;
- private networking: enabled; the service is named exactly `ibkr-gateway`, so `IB_HOST=ibkr-gateway.railway.internal`;
- health-check path: `/healthz`;
- restart policy: `Always`;
- variables:
  - `PORT=18778`
  - `TRADING_MODE=${{shared.TRADING_MODE}}`
  - `DASHBOARD_ADMIN_PASSWORD=${{secret(32)}}` (ordinary revealable template variable, not a write-only external secret)
  - `ZEHNLABS_API_KEY=${{shared.ZEHNLABS_API_KEY}}`
  - `ALLOCATIONS_BASE_URL=https://workers.fintech.zehnlabs.com/api/v1/strategies`
  - `LOG_LEVEL=INFO`
  - `SERVICE_VERSION=3.0.0`
  - `IB_HOST=ibkr-gateway.railway.internal`
  - `USER_NOTIFICATIONS_ENABLED=false`
  - `USER_NOTIFICATIONS_CHANNEL=` (empty)
  - `TZ=America/New_York`
  - `RAILWAY_RUN_UID=0`
  - `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0`
  - `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=900`

`RAILWAY_RUN_UID=0` only lets the entrypoint create/chown `/app/state`; it immediately execs Python as the non-root `appuser`.

### `ibkr-gateway`

- image: `gnzsnz/ib-gateway:10.51.1e@sha256:85975f493bcacc8341cb42e6946c460868d165e5f64d9924b1958c8f03e7bee4`;
- replicas: `1`;
- volume: `ib-settings`, mounted at `/home/ibgateway/tws_settings`;
- public networking: none; do not publish Gateway API or VNC;
- private networking: enabled;
- restart policy: `Always`;
- variables:
  - `TWS_USERID=${{shared.IB_USERNAME}}`
  - `TWS_PASSWORD=${{shared.IB_PASSWORD}}`
  - `TWS_USERID_PAPER=${{shared.IB_USERNAME}}`
  - `TWS_PASSWORD_PAPER=${{shared.IB_PASSWORD}}`
  - `TWOFA_DEVICE=IB Key`
  - `TRADING_MODE=${{shared.TRADING_MODE}}`
  - `TWS_SETTINGS_PATH=/home/ibgateway/tws_settings`
  - `READ_ONLY_API=no`
  - `BYPASS_WARNING=yes`
  - `AUTO_RESTART_TIME=10:00 PM`
  - `TWOFA_TIMEOUT_ACTION=restart`
  - `RELOGIN_AFTER_TWOFA_TIMEOUT=yes`
  - `VNC_SERVER_PASSWORD=${{shared.VNC_PASSWORD}}`
  - `TIME_ZONE=America/New_York`
  - `TZ=America/New_York`
  - `JAVA_HEAP_SIZE=1024`

`RAILWAY_ENVIRONMENT_ID` is injected by Railway and must not be supplied by the template owner. The application uses only its presence to select Railway's trusted proxy boundary.

The template exposes one shared `TRADING_MODE` input referenced by both services. Editing it creates staged changes; the owner must click **Deploy** so both services restart with the same value.

## Shutdown behavior

Railway sends `SIGTERM`, waits up to the configured 900 seconds (15 minutes), and then force-stops a process that has not exited. This covers the configured normal order timeouts but does not promise every broker operation will finish. Startup marks unfinished Runs and Work Items `interrupted` and never replays them.

## Unavoidable post-deploy checklist

1. Enable daily and weekly backup schedules in the `rebalancer` Backups tab.
2. Enable daily and weekly backup schedules in the `ibkr-gateway` Backups tab.
3. Take a manual backup of both volumes before every application or Gateway upgrade.
4. Restore the state into a paper-mode project and record a successful restore drill.

Railway provides backup tooling; the template does not make backups automatic.

## Publication gate

Create the external template, capture its immutable template URL, and add the README Deploy button only after the complete production-image paper pilot and backup/restore drill pass. Verify two services, two volumes, one public domain, private Gateway networking, generated-password visibility, shared-mode redeployment, ownership repair, persistence, health checks, and restart policies in the actual project.

Until every item passes, report the template, paper pilot, restore drill, and README button as pending rather than substituting an example or guessed template URL.
