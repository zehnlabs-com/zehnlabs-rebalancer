# Railway and VPS deployment plan

Date: 2026-11-26

## Result

The Rebalancer can support a practical **Deploy to Railway** experience without Zehnlabs operating any subscriber infrastructure and without changing the core trading architecture.

The target stack has two containers:

1. **rebalancer** — signal subscription, scheduling, rebalance execution, notifications, and a new FastAPI dashboard in one application container;
2. **ibkr-gateway** — the same stock, pinned third-party Gateway image used today.

The dashboard and signal-processing components live inside `rebalancer` and share one application layer and one durable SQLite database. The existing process-pool workers remain an internal implementation detail of that container; they are not split into additional Railway services.

The stack has two persistent volumes:

- `/app/state` for `rebalancer.db`, which owns Account configuration, scheduled work, action status, dashboard sessions, and Run history;
- the existing `ib_settings` volume for Gateway settings and session state.

PostgreSQL is unnecessary for this design. There is one application instance and one parent database writer, and Railway prevents replicas and overlapping deployments for a service that has a mounted volume. There is also no need for a new management CLI: the dashboard becomes the management interface for Railway and VPS subscribers.

This design is feasible on Railway Hobby or higher and on an ordinary Docker Compose VPS. Railway Free permits only one volume, while this stack needs two; this is a platform-capability constraint, not a cost estimate ([Free plan limits](https://docs.railway.com/pricing/free-plan), [volume limits](https://docs.railway.com/volumes/reference)). Railway removes the need to manage the host, TLS certificate, container restart policy, and volume attachment, and it provides backup tooling. It does not remove the subscriber's responsibility to enable and test backups, satisfy IBKR authentication requirements, or accept that this is a single-instance application.

## Ownership and responsibility

Everything runs in the subscriber's Railway project or on the subscriber's VPS. Zehnlabs publishes the repository, application image, pinned Gateway image reference, Railway template, Compose file, and documentation. Zehnlabs does not receive a Railway token, operate a shared control plane, hold IBKR credentials, or log in to subscriber deployments.

The deployment environment owns secrets. IBKR credentials are supplied only to `ibkr-gateway`. The Zehnlabs API key, dashboard password, trading mode, and notification settings are supplied only to `rebalancer` as appropriate. Subscriber Account assignments and trading history stay in the subscriber's SQLite volume.

## Architecture

```text
Browser
   |
   | Railway HTTPS, or the subscriber's VPS tunnel/proxy
   v
+-----------------------------------------------------------+
| rebalancer container                                      |
|                                                           |
| FastAPI dashboard                                         |
| application layer                                         |
| signal subscriber + scheduler + execution workers       |
|                         |                                 |
|                         v                                 |
|              /app/state/rebalancer.db                    |
+-----------------------------------------------------------+
              |                 |
              |                 +----> Ably and Zehnlabs API
              v
+-----------------------------------------------------------+
| stock ibkr-gateway                                        |
| private IBKR API + separate ib_settings volume            |
+-----------------------------------------------------------+
```

The dashboard is not a second trading application. It asks the same application layer used by the scheduler and signal handler to validate configuration, Preview an Account, request a rebalance, or schedule work. The browser never talks directly to SQLite, a worker subprocess, or Gateway.

Only the parent application process reads and writes SQLite. Existing execution workers continue to receive immutable Account and configuration inputs and return results to the parent. This keeps database access out of subprocesses and preserves the current trading boundary.

The dashboard always starts. An empty installation opens onboarding. A Gateway login problem, invalid Zehnlabs key, or Ably outage appears as a clear degraded status instead of making the recovery interface unavailable. Trading stays inactive until its prerequisites are valid.

## What changes in the repository

### One unified application container

Use `rebalancer` consistently for the Compose service, image, and source directory, and add FastAPI to that image. FastAPI owns application startup and shutdown, while the existing signal subscriber, scheduler, and executor keep their current responsibilities inside the application.

The supplied production configuration runs one Uvicorn parent process and one Railway replica. No dashboard, scheduler, worker, database, or internal API container is added.

### SQLite replaces mutable application files

The application currently uses separate files for Account configuration, manual requests, next-open work, and Run records. Those concepts should become typed SQLite records:

- Account ID, paper/live type, enabled state, Strategy, Replacement Set, and broker;
- requested Previews and rebalances, including whether they are pending, running, complete, failed, cancelled, or interrupted;
- next-market-open and retry work;
- Run outcome and detail, retained for the current 30-day period; and
- dashboard session and login-throttling state.

Saving Account configuration is one validated transaction. The UI submits the complete Account set for the current trading mode, the backend validates the whole set and every selected Strategy, and SQLite either saves all of it or none of it. There is no draft/revision system and no partially active Account configuration.

`accounts.yaml` is no longer live configuration. The dashboard provides a one-time import for existing subscribers and an optional export in the same format. After import, normal operation no longer depends on that file.

The manual-rebalance file, scheduled JSON file, JSONL Run files, and their shared `data` mount are removed. Dashboard actions create durable work directly through the application layer.

### `config.yaml` becomes typed code

In the target product, `config.yaml` is removed and its current validated values become immutable, strongly typed defaults in Python. They are injected into the modules that need them. This intentionally ends the repository's currently documented advanced YAML customization; it does not move those trading settings into the dashboard.

Secrets and deployment choices stay in environment variables. This includes the existing environment variables that still apply and the new dashboard administrator password. Stable trading behavior such as timeouts, retry intervals, slippage, and market-open time is not edited in the dashboard.

### Dynamic Account configuration

Today the rebalancer reads Accounts once at startup and holds fixed Strategy subscriptions. The new Account manager loads configuration from SQLite and refreshes the in-memory registry after a successful dashboard save. It subscribes newly used Strategies, unsubscribes Strategies no enabled Account uses, and routes the next signal using the latest committed Account configuration.

A Run already in progress keeps the Account configuration it started with. Disabling an Account or changing its Strategy or Replacement Set cancels that Account's pending work so an old confirmation cannot execute a new assignment. It does not mutate an active Run or claim to cancel orders already sent to IBKR.

## Dashboard experience

### Authentication and exposure

The dashboard has one local user:

- username: `admin`;
- password: `DASHBOARD_ADMIN_PASSWORD` from the deployment environment.

The Railway template generates a strong random password with Railway's template `secret()` function. The subscriber retrieves it from the Railway Variables screen and uses it for the single dashboard login. There is no second Railway login in the browser flow and no Zehnlabs identity provider. Railway project access does not protect a public application domain, so the application login is still required. Railway documents public domains as internet exposure and private domains as service-to-service networking ([domains](https://docs.railway.com/networking/domains/working-with-domains)).

On a VPS, the subscriber sets the password in `.env`. Changing or recovering the password means replacing that environment value and restarting the container.

The login uses secure, short-lived server-side sessions, CSRF protection, login throttling, safe response headers, and generic errors. State-changing operations require a confirmation form. A live-trading confirmation clearly names the Account, Strategy, action, and timing, but does not ask the user to log in again.

### Onboarding

After login, the wizard:

1. shows paper/live mode and the readiness of SQLite, Gateway, IBKR authentication, Zehnlabs API access, and Ably;
2. discovers the IBKR Accounts visible to the authenticated Gateway without placing or cancelling orders;
3. reads the existing Zehnlabs Strategy catalog and the built-in Replacement Sets;
4. lets the subscriber enable Accounts and assign one Strategy and optional Replacement Set to each enabled Account;
5. fetches allocations for every selected Strategy, proving that the configured API key can use those Strategies;
6. saves the complete valid Account configuration;
7. Previews every enabled Account; and
8. asks for one clear confirmation before creating the initial rebalance work.

The Strategy catalog is available from the existing `GET /api/v1/strategies/names/all` route. Access is verified separately through the existing authenticated allocation request for each selected Strategy. No new Zehnlabs control plane or subscriber-credential service is required.

If the market is open, confirmed initial work can run immediately. If it is closed, the work is stored for the next configured market-open time and that exact time is shown. Saving Account configuration alone never places orders.

### Routine use

The dashboard provides the actions a subscriber currently needs scripts or file edits for:

- view system, Gateway, signal, API, and scheduler status;
- discover Accounts and change Strategy or Replacement Set assignments;
- enable and disable Accounts;
- Preview an Account without placing orders;
- rebalance now;
- schedule a rebalance for the next market open;
- view and cancel work that has not started; and
- inspect recent and historical Run outcomes and details.

The dashboard is not something the subscriber must keep open. Closing the browser does not stop signals, schedules, or active work.

## Railway experience

The README's Deploy to Railway button opens a template containing exactly the two services. The
Rebalancer service uses the public Zehnlabs GHCR release image at an exact stable version and
immutable digest; Railway does not rebuild it from the repository. The Gateway uses its separately
pinned stock image. Railway templates can attach volumes, define variables, generate a public
domain, and configure a healthcheck ([creating templates](https://docs.railway.com/templates/create)).

The subscriber supplies the current deployment variables, including IBKR credentials, Zehnlabs API key, and one paper/live mode value referenced by both services. The template generates `DASHBOARD_ADMIN_PASSWORD`, creates one public HTTPS domain for `rebalancer`, attaches the two volumes, sets both long-running services to Railway's `Always` restart policy, and connects them over Railway private networking. Gateway API and VNC are not public. Railway provides private service DNS for this connection ([private networking](https://docs.railway.com/networking/private-networking), [restart policy](https://docs.railway.com/deployments/restart-policy)).

The application listens on Railway's `PORT`, defaulting to `18778` outside Railway. Its healthcheck confirms that the web process and SQLite are usable. Gateway, Ably, or Zehnlabs outages appear inside the dashboard and do not make the web healthcheck fail, because that would remove the interface needed to diagnose them. Railway healthchecks are deployment checks rather than continuous trading monitoring ([healthchecks](https://docs.railway.com/deployments/healthchecks)).

Railway image auto-updates are disabled for both services. Updating the template changes new
installations only; an existing subscriber remains on the old digest until explicitly upgrading
after a backup. The canonical release and upgrade procedure is [Railway distribution and release](../railway.md).

Railway variable changes are staged and then deployed, not applied inside an existing container ([variables](https://docs.railway.com/variables), [staged changes](https://docs.railway.com/deployments/staged-changes)). For example, switching from paper to live means changing `TRADING_MODE` and deploying. The new process reads the new mode and activates only Accounts stored for that mode; if none exist, the dashboard returns to onboarding.

### Volumes, backups, and redeploys

Railway does provide persistent volumes. This design deliberately uses one on each service:

- `rebalancer` needs its volume for SQLite;
- Gateway keeps `ib_settings` because those files belong to the third-party Gateway runtime and are not application data.

Railway does not allow replicas for a service with a volume and stops the old deployment before mounting the volume into the replacement. That gives SQLite one application owner without adding a lease or lock, but it also means rebalancer deployments have brief downtime ([volume behavior](https://docs.railway.com/volumes/reference)).

Railway volume backups include SQLite databases and support manual and scheduled backups. Backup schedules are configured for each service in Railway's Backups UI; the template does not silently enable them. The post-deploy checklist therefore requires daily and weekly schedules for both volumes and a manual backup before upgrades. Restoring a backup creates a replacement volume as a staged change and requires a deployment ([backups](https://docs.railway.com/volumes/backups)). Before release, a backup taken during realistic activity must be restored and verified in paper mode; documentation alone is not proof that the chosen SQLite journal and backup procedure behave correctly.

Railway can also initiate a deployment for host maintenance, faults, security patches, or platform upgrades ([Railway-initiated deployments](https://docs.railway.com/deployments/reference#railway-initiated-deployments)). Railway does offer a graceful shutdown period: it sends `SIGTERM`, waits for `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`, and then force-stops the process if it has not exited ([deployment teardown](https://docs.railway.com/deployments/deployment-teardown)). The template sets overlap to zero and the drain period to 900 seconds. The application uses those 15 minutes to stop accepting new work and shut down cleanly. It cannot guarantee that a multi-step broker operation always finishes before a forced stop, so a Run left in progress is marked interrupted on the next startup and is never automatically replayed.

The mounted volume exists only while the service is running, so schema creation and migration happen during application startup, not image build or a pre-deploy command ([using volumes](https://docs.railway.com/volumes)). Railway mounts volumes as root, so the container needs a narrow root entrypoint that fixes ownership only under `/app/state` and then drops permanently to the existing non-root application user before Python starts. The paper deployment must prove both the ownership change and the final process UID ([volume permissions](https://docs.railway.com/volumes#permissions)).

## VPS experience

The same release runs through Docker Compose with the same two containers and volumes. Compose publishes the dashboard only on loopback:

```yaml
ports:
  - "127.0.0.1:18778:18778"
```

The repository does not require or configure Cloudflare, Tailscale, Caddy, or another access product. The subscriber chooses how to reach the loopback service securely—for example, an SSH tunnel, an existing VPN, or an HTTPS reverse proxy. Plain HTTP is never exposed directly to the internet by the supplied Compose file.

There is no VPS-only CLI. Account import/export, configuration changes, Previews, rebalances, schedules, and history use the dashboard. Docker and Compose commands remain available for infrastructure operations and logs.

VPS backup instructions must create a consistent SQLite backup rather than copying a live database file blindly, and must back up `ib_settings` separately. Restore, upgrade, and rollback instructions use the same paper-mode verification as Railway.

## Gateway treatment

Gateway remains the pinned stock image already used by the repository. Its settings volume remains mounted at the upstream path. The image documents that path for persistent settings and exposes distinct internal live and paper API ports ([upstream Gateway image](https://github.com/gnzsnz/ib-gateway-docker)).

Gateway authentication and IBKR-required user interaction behave exactly as they do in the current product. The Railway work does not replace or bypass them. Gateway API and support-only VNC access remain private; if VNC is needed on Railway, it can be reached temporarily through Railway SSH port forwarding rather than a public TCP proxy ([Railway SSH port forwarding](https://docs.railway.com/cli/ssh#forward-a-port-with-ssh--l)).

Use an exact tested Gateway tag and digest and do not enable automatic image updates. A Gateway upgrade is a Rebalancer release and must pass the paper acceptance suite first.

## Existing subscriber migration

Migration is intentionally simple:

1. stop the old stack when no Run or broker order is active and back up `.env`, `accounts.yaml`, `data`, and `ib_settings`;
2. start the new release with the new SQLite volume;
3. log in and import `accounts.yaml` through the dashboard;
4. rediscover Accounts, resolve validation errors, verify Strategy access, and Preview every enabled Account;
5. recreate any still-needed pending schedule in the dashboard rather than importing ambiguous work;
6. confirm the initial rebalance in paper mode; and
7. keep the stopped old installation as a rollback copy until the new deployment and its backup/restore procedure are verified.

Old Run JSONL files remain part of the backup but do not need to be loaded into the new dashboard. They expire after 30 days today and importing two historical formats adds risk without helping the new runtime. `config.yaml` is also not imported: the new release deliberately uses the code defaults. An existing subscriber who changed the documented YAML settings must review those differences and accept the loss of customization before activation. Old and new deployments must never trade the same IBKR Accounts simultaneously.

## Release gates

The Railway button is ready to publish only after all of these pass:

- a fresh template deployment creates exactly two services, two volumes, and only one public HTTPS domain;
- Railway generates a recoverable dashboard password, and one dashboard login is sufficient;
- the application and Gateway can write and reuse their volumes after restart and redeploy;
- the subscriber can follow the post-deploy checklist to enable backup schedules for both volumes;
- Account and Strategy discovery, validation, Preview, initial scheduling, signals, retries, notifications, and Run history work with separately supplied paper credentials;
- dashboard actions are authenticated, CSRF-protected, idempotent, and incapable of bypassing paper/live or enabled-Account checks;
- a SQLite backup restores successfully in paper mode;
- redeploy and forced-stop tests never produce duplicate work or automatically replay an interrupted Run;
- the VPS stack exposes only its documented loopback ports and works through a representative secure tunnel or HTTPS proxy; and
- the current calculation, broker, execution, and safety test suite remains green.

## Main limitations

| Limitation | Effect | Treatment |
| --- | --- | --- |
| One volume-backed rebalancer instance | No replicas or zero-downtime deployment | Accept brief deployment downtime; verify restart behavior and backups |
| Forced interruption during broker work | A Run may end with an uncertain broker outcome | Mark it interrupted, show it clearly, and never replay it automatically |
| SQLite volume loss or corruption | Configuration, pending work, and history are unavailable | Fail closed; maintain and test platform plus portable backups |
| Public Railway dashboard | Internet users can reach the login page | Strong generated password, HTTPS, secure sessions, CSRF defense, throttling, and explicit confirmations |
| Non-root volume permissions | The current image may be unable to initialize SQLite | Prove the volume ownership strategy before publishing the template |
| IBKR authentication requirements | Subscriber attention may still be required | Report Gateway status clearly; retain private support access without changing Gateway |

## Recommendation

Proceed with the two-container design. It is the smallest coherent change that supports Railway and improves the VPS experience at the same time:

- one `rebalancer` container instead of splitting existing responsibilities;
- one stock Gateway container;
- SQLite instead of PostgreSQL or mutable application files;
- one application state volume plus the existing Gateway settings volume;
- fixed typed application defaults plus environment-backed secrets;
- one FastAPI dashboard for onboarding and occasional management; and
- the same product behavior on Railway and Docker Compose.

Publish the Railway button only after the paper, restart, interruption, permissions, and backup/restore release gates pass.

## Primary sources

### Repository

- [Current Compose topology](../../docker-compose.yaml)
- [Current application entry point](../../rebalancer/app/main.py)
- [Current Account configuration](../../rebalancer/app/accounts.py)
- [Current Strategy execution](../../rebalancer/app/services/strategy_executor.py)

### Railway

- [Templates](https://docs.railway.com/templates/create)
- [Variables](https://docs.railway.com/variables)
- [Staged changes](https://docs.railway.com/deployments/staged-changes)
- [Public and private domains](https://docs.railway.com/networking/domains/working-with-domains)
- [Private networking](https://docs.railway.com/networking/private-networking)
- [Healthchecks](https://docs.railway.com/deployments/healthchecks)
- [Volumes](https://docs.railway.com/volumes)
- [Volume behavior and caveats](https://docs.railway.com/volumes/reference)
- [Volume backups](https://docs.railway.com/volumes/backups)
- [Deployment teardown](https://docs.railway.com/deployments/deployment-teardown)
- [Restart policy](https://docs.railway.com/deployments/restart-policy)
- [Railway-initiated deployments](https://docs.railway.com/deployments/reference#railway-initiated-deployments)
- [Free plan limits](https://docs.railway.com/pricing/free-plan)
- [Railway SSH port forwarding](https://docs.railway.com/cli/ssh#forward-a-port-with-ssh--l)

### Gateway

- [Stock IB Gateway container](https://github.com/gnzsnz/ib-gateway-docker)
