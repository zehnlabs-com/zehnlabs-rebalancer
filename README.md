# Zehnlabs Portfolio Rebalancer

The Rebalancer follows Zehnlabs Strategies in subscriber-owned Interactive Brokers Accounts. It runs as two containers:

- `rebalancer`: signal subscriber, scheduler, trade executor, FastAPI dashboard, and one parent-owned SQLite database;
- `ibkr-gateway`: the pinned stock Interactive Brokers Gateway image.

Zehnlabs does not receive brokerage credentials or operate subscriber deployments. IBKR credentials are passed only to the Gateway. The Zehnlabs API key is passed only to `rebalancer`.

> Live trading can lose money. Preview first, verify Account assignments and market data, and complete the paper acceptance plan before enabling live mode.

## Requirements

- Docker Engine with Docker Compose 5.3.0 or newer
- an IBKR Pro Account and the required market-data subscription
- a Zehnlabs API key
- a unique dashboard password of at least 16 characters

Railway requires Hobby or higher because this stack needs two persistent volumes. Railway Free is not supported.

## VPS installation

1. Copy the example environment file and set `IB_USERNAME`, `IB_PASSWORD`, `ZEHNLABS_API_KEY`, `TRADING_MODE`, and a unique `DASHBOARD_ADMIN_PASSWORD`:

   ```bash
   cp .env.example .env
   ```

2. Start the stack:

   ```bash
   docker compose up -d --build
   docker compose logs -f rebalancer
   ```

   On a new `ib_settings` volume, Compose runs a one-time initialization step
   from the pinned stock Gateway image to give UID 1000 ownership of its
   settings directory. The long-running Gateway container still runs as the
   stock image's unprivileged user.

3. Open `http://localhost:18778`, log in as `admin`, and complete onboarding. The supplied Compose file binds the dashboard, Gateway API ports, and VNC to host loopback. It does not expose them on a public interface.

For a remote VPS, reach the dashboard through an SSH tunnel, private VPN, or a separately configured HTTPS reverse proxy. Do not publish its plain HTTP port.

Onboarding discovers managed IBKR Accounts and available Strategies, validates every assignment, stores the complete Account set in SQLite, Previews every enabled Account, and only then offers initial rebalance work. No `accounts.yaml` or `config.yaml` edit is part of normal operation.

See [Docker operations](docs/docker.md) for backups, upgrades, and multi-instance port settings.

## Railway

The reviewed Railway template contract is documented in [Railway deployment](docs/railway.md). It creates exactly two one-replica services and two volumes, exposes only the dashboard through HTTPS, and uses private networking for Gateway access.

The public Deploy to Railway button is intentionally withheld until the external paper pilot and restore drill in [Paper acceptance](docs/paper-acceptance.md) pass. Publishing a button before those gates pass would present an unverified trading deployment as supported.

After any Railway variable change, click **Deploy** to apply the staged change. A `TRADING_MODE` change must redeploy both services from the same shared template input.

### Mandatory post-deploy checklist

Before relying on the installation:

1. In the `rebalancer` service **Backups** tab, enable daily and weekly schedules for the `/app/state` volume.
2. In the `ibkr-gateway` service **Backups** tab, enable daily and weekly schedules for its settings volume.
3. Take a manual backup of both volumes before every application or Gateway upgrade.
4. Restore a backup into paper mode and record a successful restore drill.

Railway provides backup tooling; templates do not guarantee that backup schedules are enabled.

## Dashboard operations

The dashboard is the management interface on both platforms. It supports Account discovery and assignment, Preview, immediate or next-open rebalance, pending-work cancellation, enablement changes, and 30-day Run history. A one-time import/export path preserves the legacy `accounts.yaml` document shape.

A Preview never places or cancels orders. Live confirmations name the Account, Strategy, Replacement Set, action, and execution time and require an explicit acknowledgement. Cancelling pending work or disabling an Account does not cancel broker orders already submitted by a running Run.

The fixed username is `admin`. Rotating `DASHBOARD_ADMIN_PASSWORD` and restarting `rebalancer` invalidates every old dashboard session.

## Durable state and restart behavior

`rebalancer_state:/app/state` contains `rebalancer.db`, which owns Accounts, Work submissions and Work Items, durable initial-onboarding receipts, Run history, sessions, and login throttles. `ib_settings` remains a separate Gateway volume. Replaying the same unchanged onboarding completion returns its original submission even after normal Work history expires.

The parent application process is the only SQLite owner. Process-pool workers receive immutable typed inputs and return typed results; they do not open SQLite. Work and Runs left `running` after a stop are marked `interrupted` on startup and are never replayed automatically. Pending work remains scheduled.

The dashboard stays available when Gateway, Zehnlabs, or Ably is unavailable and reports those conditions as readiness errors. `/healthz` checks only that the event loop and SQLite are responsive.

## Trading behavior

Each Run fetches allocations, validates/scales targets, applies an optional built-in Replacement Set, reads the Account, plans the whole portfolio, submits market sells, refreshes cash and prices, then submits limit buys. IBKR is whole-share-only in this application.

Safety behavior includes a fixed 0.25% cash reserve outside Strategy capital, 0.5% buy-limit slippage, own-order cancellation, refusal to trade over foreign working orders, no invented prices, a 2% value cross-check, deterministic portfolio-wide optimization, and one eligible next-open retry without retry chains.

See [Rebalancing logic](docs/rebalancing-logic.md) and the ADRs under `docs/adr/`.

## Configuration

Trading defaults are immutable typed Python configuration. The former `config.yaml` is deliberately removed and its shipped values are the code defaults. They are not dashboard-editable.

Deployment settings remain environment-backed:

- `DASHBOARD_ADMIN_PASSWORD` (required, 16+ characters, no known default);
- `TRADING_MODE` (`paper` or `live`);
- `ZEHNLABS_API_KEY` and `ALLOCATIONS_BASE_URL` for `rebalancer`;
- `IB_USERNAME` and `IB_PASSWORD` for `ibkr-gateway` only;
- optional notification, logging, Gateway restart, and loopback port settings in `.env.example`.

Account-to-Strategy assignments live in SQLite, not environment variables.

## Existing subscriber upgrade

This release intentionally does not import `config.yaml`, scheduled JSON, manual-request files, or JSONL Run history. Before activation, compare the old `config.yaml` with the former shipped defaults and explicitly accept that custom values will be replaced by code defaults. Review and recreate pending schedules in the dashboard. Never replay a legacy manual-request file because its execution state is ambiguous. Preserve old JSONL Runs in the pre-upgrade backup for their remaining retention period.

Follow [Dashboard upgrade](docs/upgrade-dashboard.md). Never operate old and new deployments against the same live Accounts at the same time.

## Development and validation

Application source is under `rebalancer/`. Run the complete release validation with:

```bash
./tools/validate.sh
```

The command runs the complete Python suite with `PYTHONPATH=rebalancer`, validates Compose with synthetic values, and runs an isolated production-image smoke test. It never reads the repository's `accounts.yaml` or developer credentials.

Reload Compose changes with `./tools/reload.sh`.

## License

MIT. See [LICENSE](LICENSE).
