# Railway distribution and release guide

## Result

The Rebalancer will be distributed to Railway as two pinned container images:

1. `rebalancer` will use a public, Zehnlabs-owned image from GitHub Container Registry (GHCR).
2. `ibkr-gateway` will continue to use the approved stock Gateway image at its exact tag and digest.

Railway will not build the Rebalancer from the repository for subscriber deployments. The
template will refer to one tested Rebalancer release by both version and immutable image digest,
and Railway image auto-updates will be off. New installations will receive the release selected in
the template. Existing installations will remain on their current release until their owner
explicitly upgrades them.

The repository remains the source of the application. The published image is only the tested
release artifact. Development continues through feature branches and `main`; a protected
`release` branch controls what can become a subscriber release.

The first dashboard/SQLite release should be `4.0.0`. It replaces the v3 file-based operating
interface and is therefore a major, breaking release rather than another build of `3.0.0`.

**Status: not yet published.** The isolated VPS paper pilot passed on November 27, 2026. The GHCR
release workflow, Railway reference project, Railway template, clean template deployment, Railway
backup/restore drill, and README button still have to be completed in the order below.

| Publication record | Current value |
|---|---|
| First release | Proposed `4.0.0`; confirm before promotion |
| Stable GHCR manifest digest | Pending |
| AMD64 child digest | Pending |
| ARM64 child digest | Pending |
| Shareable Railway template URL | Pending |
| README button | Withheld until the distribution gate passes |

## Decisions

### Public image owned by Zehnlabs

The image name will be:

```text
ghcr.io/zehnlabs-com/zehnlabs-rebalancer
```

The GHCR package will be public and attached to the Zehnlabs GitHub repository. This avoids giving
Railway or subscribers a Zehnlabs registry credential and works for the intended Railway plan. It
does not disclose anything beyond the public MIT-licensed repository, although the image itself
can also be downloaded and inspected.

If the image must be private, stop before implementing the release workflow. Private registry
access changes the Railway plan and template design; it is not a switch to make after publication.

### Exact, immutable releases

Each stable release has all of the following tied to the same commit and image manifest digest:

- a semantic version such as `4.0.0`;
- a Git tag such as `v4.0.0`;
- a GitHub Release and changelog entry;
- a GHCR version tag such as `4.0.0`; and
- the immutable multi-platform GHCR manifest produced by the release build.

Railway will use a reference shaped like this:

```text
ghcr.io/zehnlabs-com/zehnlabs-rebalancer:4.0.0@sha256:<tested-manifest-digest>
```

Railway's public documentation shows tag references but does not promise that its source field
accepts the combined tag-plus-digest form. Before the release workflow is finalized, a disposable
service will test that exact syntax with a known public image. If Railway rejects it, the template
will use the digest-only form below and keep the human version in GHCR, the image metadata, and the
GitHub Release:

```text
ghcr.io/zehnlabs-com/zehnlabs-rebalancer@sha256:<tested-manifest-digest>
```

Both forms are immutable. The platform probe chooses the form Railway actually supports; it does
not weaken the pin.

The version tag makes the release understandable to a person; the digest guarantees the exact
artifact. We will not deploy `latest`, deploy a branch name, move an existing release tag, rebuild
an existing version, or delete an image still used by subscribers.

The release workflow will build one multi-platform candidate for `linux/amd64` and `linux/arm64`.
The Oracle VPS uses the ARM64 image; the reference project must record which platform Railway
selects. Both platform images belong to the same pinned manifest. Release evidence will record the
manifest digest and both child digests so each platform's exact bytes can be verified.

After that exact candidate passes acceptance on both platforms, the stable version tag will be
added to the same manifest. The stable image will not be rebuilt after testing.

The application version will be baked into the image. Railway and VPS users will not maintain a
separate `SERVICE_VERSION` variable that could disagree with the image actually running.

### Protected release branch

`release` is a permanent, protected branch. It is not a second development branch and it is not a
deployment reference. Its purpose is to make the selected release commit unambiguous.

- Normal work is reviewed and merged to `main` first.
- A release candidate advances `release` to one selected commit from `main`.
- Direct, unreviewed pushes are disabled.
- Required validation must pass before `release` advances.
- Only a commit on `release` may be promoted to a stable version.

Publishing a candidate or promoting it to a stable release is a deliberate GitHub Actions action,
not a side effect of every push to `main`.

### No automatic subscriber upgrades

Railway image auto-updates will be disabled for both services. The template will also pin both
images by digest, so a registry change cannot silently replace a running release.

Updating the Railway template changes what a new subscriber installs; it does not change existing
subscriber projects. Railway does not currently provide template-update notifications for
Docker-image-based services. GitHub Releases will therefore be the canonical public release
record, and Zehnlabs must use its normal subscriber communication channel when an update is worth
announcing.

### Two Railway acceptance projects

Railway validation has two separate stages:

1. A **reference project** is assembled manually from the contract in this document. It proves
   that the services, images, volumes, networking, variables, shutdown settings, and backups work
   on Railway.
2. A **clean template acceptance project** is created from the finished template URL without
   inherited project variables or registry credentials. It proves the subscriber experience.

The clean project should be created in a separate test workspace if practical. Passing only the
reference project is insufficient because it cannot prove that the template captured every
required setting.

The reference project may be stopped or deleted after distribution without affecting the template
or subscriber projects. Keeping it as a paper-only staging project will make future release tests
easier.

### Company ownership rather than one personal account

The GHCR package, GitHub workflows, Railway template, and reference project should be owned by the
Zehnlabs GitHub organization and a Zehnlabs Railway workspace. The Railway workspace should have at
least two administrators. This prevents a personal account deletion or loss of access from making
the template unmaintainable. Moving a Railway template between workspaces is not a routine edit, so
ownership must be correct before distribution.

Railway currently requires Pro or Enterprise to create an organization workspace and add workspace
members. Zehnlabs therefore needs a Pro authoring workspace for this ownership model. Subscribers
still need only Hobby or higher for the two-volume deployment.

### Direct template link, not marketplace publication

The first release will keep the template out of Railway's public marketplace. Railway gives an
unpublished template a shareable URL, and that URL can power the README Deploy on Railway button.
This reaches Zehnlabs subscribers without opting into marketplace discovery and its public template
question queue.

Marketplace publication is a separate future decision. If Zehnlabs later wants it, the owner must
explicitly accept the additional public listing and support channel before publishing.

### Backups and rollback are separate from image pinning

Pinning prevents an unwanted upgrade; it does not make a database downgrade safe. SQLite
migrations are forward-only. Before every upgrade, the subscriber must back up both Railway
volumes. If a release changes the SQLite schema, recovery may require restoring the pre-upgrade
`rebalancer` volume as well as selecting the previous image. Release notes must say whether a
simple image rollback is supported.

## Who does what

### Actions only the Zehnlabs owner can complete

These actions require account ownership, secrets, billing authority, or a human approval:

- create or select the Zehnlabs Railway workspace and add a second administrator;
- connect the Zehnlabs GitHub organization to Railway if Railway asks for that connection;
- enable GitHub Actions/package permissions and approve the protected `release` branch rules;
- approve a protected GitHub release action if required-reviewer approval is configured;
- create or own the Railway projects and accept any Railway plan change;
- enter paper IBKR credentials, the Zehnlabs API key, and other secrets directly in Railway;
- approve IBKR authentication prompts;
- inspect paper Account activity and confirm that the resulting orders are correct;
- approve the shareable template URL and final README button; and
- decide when a tested release should be announced to subscribers.

No secret should be pasted into an issue, commit, workflow file, template default, release note, or
chat transcript.

### Repository and validation work I will do

Once authorized at each external distribution point, I will:

- implement and test the GitHub Actions candidate/promotion workflow;
- make the image carry its own version and source revision, removing the duplicate Railway version
  setting;
- update the Docker and deployment contract tests for GHCR distribution;
- run the repository validation suite and inspect the produced image;
- prepare the changelog and exact candidate commit;
- build the candidate through GitHub Actions and record its manifest and platform digests;
- provide or enter every non-secret Railway service setting from the contract below;
- validate Railway logs, process identity, networking, persistence, health, restarts, and recovery;
- compare the external template against this document;
- repeat the test/fix cycle until all distribution gates pass;
- replace placeholders in this document with the real release reference and template URL; and
- add and validate the README Deploy on Railway button.

I will not create a Git tag, publish/promote an image, change a Railway project, make a template
shareable, or add the public button without the corresponding owner authorization. Those are
durable external changes even when the underlying repository work is ready.

## Step-by-step path to the README button

Unmarked steps below are repository or validation work I perform after authorization. Steps marked
**Owner** require the Zehnlabs owner to act or approve the external change.

### Phase 1: Prepare GitHub ownership and release controls

1. **Owner:** Confirm that a public GHCR image is acceptable.
2. **Owner:** Create or select a Zehnlabs Pro Railway workspace, add a second administrator, and
   confirm that the intended template author is the company workspace rather than a personal one.
3. **Owner:** Confirm the GitHub repository is owned by `zehnlabs-com` and that the people who must
   maintain releases have organization access.
4. **Owner:** Enable GitHub Actions and GitHub Packages for the repository if either is restricted.
5. **I prepare; owner approves:** Create the protected `release` branch and its branch rules.
   Require the repository validation checks and prevent force pushes and deletion.
6. **I prepare; owner approves:** Add a `v*` Git tag ruleset that prevents released version tags
   from being moved or deleted outside the guarded release procedure.
7. **Owner:** Create a GitHub deployment environment for stable releases with a required reviewer.
   This gives the owner a final approval before a candidate becomes a subscriber release.
8. Create a disposable Railway service from a known public GHCR image and test whether Railway accepts a
   combined `tag@sha256:digest` source. Record whether the template must use that form or digest-only.

Phase 1 is complete when an ordinary change cannot silently publish a stable image and loss of one
personal account would not strand the package.

### Phase 2: Add the image release workflow

1. I add a candidate workflow that runs only for an explicitly selected commit on `release`.
2. The workflow runs `./tools/validate.sh` before publishing anything.
3. It builds one `linux/amd64` and `linux/arm64` manifest, stamps the commit and proposed version into
   the image, and publishes a unique candidate tag derived from the commit SHA and GitHub run ID.
4. It uses GitHub's short-lived repository token with only `contents: read` and `packages: write`;
   no personal access token is stored.
5. It prints and records the manifest and platform digests. Those digests, rather than a mutable tag,
   are the artifacts used in acceptance tests.
6. The workflow refuses to replace any candidate tag that already exists.
7. I add a separate, approval-gated promotion workflow. Promotion adds the stable version tag to the
   already-tested manifest and creates the matching Git tag and GitHub Release; it does not rebuild
   the image.
8. Before promotion, the workflow fails if the stable Git tag or GHCR tag already exists. Its GitHub
   token receives `contents: write` only for creating that tag and Release.
9. I update automated deployment-contract tests and documentation so they reject `latest`, a
   repository build source, an unpinned image, or a separate user-maintained application version.

Phase 2 is complete when the workflows and guardrails are validated without publishing a release
candidate.

### Phase 3: Build the first Railway candidate

1. I bring the intended, reviewed `main` commit onto `release` and run the complete validation suite.
2. I publish the dashboard/SQLite work in `CHANGELOG.md` as `4.0.0`. The owner approves the release.
3. The owner approves the candidate workflow if GitHub asks for approval.
4. The first candidate workflow run creates the GHCR package. **Owner:** Make the package public and
   connect it to this repository if GitHub did not inherit those settings automatically.
5. I verify unauthenticated pulls for both platforms and confirm their metadata identifies the exact
   source commit.
6. I record the candidate's full image reference, manifest digest, and both platform digests in the
   release evidence. No digest is copied from a local developer build.
7. I verify that the image contains no `.env` file, Account file, database, paper credential, build
   token, or other working-tree secret.

Phase 3 is complete when there is one candidate manifest that can be referred to exactly and all
later tests use that same manifest.

### Phase 4: Create the Railway reference project

1. **Owner:** Create a paper-only project in the Zehnlabs Railway workspace. Do not reuse a live
   subscriber project.
2. Add `rebalancer` from the exact candidate GHCR digest and `ibkr-gateway` from the exact approved
   Gateway reference in the contract below.
3. Add one volume to each service at its documented mount path.
4. Configure private networking, one public HTTPS domain for the dashboard, health checks, restart
   policy, replica count, shutdown settings, and image auto-update settings exactly as documented.
5. **Owner:** Generate and set a strong random `DASHBOARD_ADMIN_PASSWORD`, enter the paper
   credentials and other secrets directly into Railway, and start in `paper` mode. The template's
   `${{secret(32)}}` function is not available in this manually assembled project.
6. Deploy both services and approve IBKR authentication when prompted.
7. Retrieve the configured dashboard password from Railway, log in as `admin`, complete onboarding,
   discover the paper Accounts and Strategies, and verify allocation access.
8. Run Previews and paper rebalances covering materially different Strategies and holdings. Inspect
   both the dashboard evidence and IBKR paper orders.
9. Redeploy and restart the services. Confirm SQLite state and Gateway settings survive, unfinished
   work is handled as documented, the Gateway has no public endpoint, and only the dashboard is
   reachable publicly.
10. Change a shared deployment variable in a controlled paper test and prove Railway stages and
    redeploys every affected service with a consistent value.
11. Enable daily and weekly Railway backups for both volumes. Take manual backups and restore both
    inside the same reference project and environment, which is the boundary Railway supports.
    Railway stages a replacement volume and leaves the former volume unmounted. Deploy the restored
    volumes, confirm the application can log in, read its state, connect to the restored Gateway
    settings, and run a Preview, then deliberately retain the accepted volume set.

Phase 4 is complete only when every item in the Railway distribution gate below has evidence from the
actual platform. A failure is fixed in the repository or project contract and then retested; it is
not waived because the VPS pilot passed.

### Phase 5: Create and test the template

1. Generate a Railway template from the working reference project in the Zehnlabs workspace.
2. Remove all reference-project secret values. Replace them with required subscriber inputs or the
   documented Railway-generated secret expression.
3. Pin the Rebalancer candidate by digest and the Gateway by its approved tag and digest. Disable
   image auto-updates for both template services.
4. Compare every service, volume, mount, variable, reference, domain, private-network name, health
   check, replica, restart policy, and shutdown value against the contract below.
5. Keep the template unpublished while testing. Use its private/template URL to deploy a completely
   new project, preferably in a separate test workspace.
6. **Owner:** Enter a fresh set of paper secrets as a subscriber would. Do not copy project-level
   variables or registry credentials from the reference project.
7. Confirm Railway generated a new revealable dashboard password, then repeat the fresh-install,
   login, onboarding, persistence, networking, and paper trading acceptance checks. Confirm both
   services expose Railway's backup controls; the destructive
   restore drill is performed in the reference project.
8. Delete and redeploy the clean test project once more from the template URL to prove the result is
   repeatable.

Phase 5 is complete when the template alone reproduces the accepted stack and the test project has
no dependency on the reference project or a Zehnlabs registry credential.

### Phase 6: Promote and distribute

1. The owner approves promotion of the tested manifest to `4.0.0`.
2. The promotion workflow adds the stable GHCR tag to the same manifest, creates `v4.0.0`, and
   creates the GitHub Release from the reviewed changelog.
3. Update the Railway template from the candidate reference to the stable reference using the pinned
   source format proved in Phase 1. This changes the name when tag-plus-digest is supported, not the
   bytes already tested.
4. Perform one final clean template deployment and compare its resolved manifest and selected
   platform image digests with the tested digests.
5. **Owner:** Approve the template name, description, and shareable direct URL in the Zehnlabs
   Railway workspace. Keep the template unpublished from the marketplace.
6. Record the permanent Railway template URL in this document.
7. I add Railway's official Deploy on Railway button to the README using that exact URL, run the
   documentation/deployment checks, and push the reviewed repository change.
8. Open the README button while signed out of the authoring workspace and prove that it starts the
   expected two-service deployment without exposing any Zehnlabs secret.

Only then may Railway be described as a supported deployment and the button remain in the README.
Marketplace publication is not part of this release.

## Railway template contract

Subscribers require Railway Hobby or higher because the stack needs two persistent volumes.
Zehnlabs requires a Pro authoring workspace so the template has company ownership and at least two
administrators. The template is a Railway-owned object; this repository document is its
reproducible review contract.

### Subscriber inputs

| Input | Required value |
|---|---|
| `TRADING_MODE` | Exactly `paper` or `live`; default `paper`. Both services reference the same input. |
| `IB_USERNAME` | Subscriber's IBKR username. |
| `IB_PASSWORD` | Subscriber's masked IBKR password. |
| `ZEHNLABS_API_KEY` | Subscriber's masked Zehnlabs API key. |
| `VNC_PASSWORD` | Subscriber's masked strong password, even though VNC is not public. |

### `rebalancer`

- source: `ghcr.io/zehnlabs-com/zehnlabs-rebalancer:<release>@sha256:<tested-manifest-digest>`,
  falling back to digest-only if the recorded Railway source-field probe requires it;
- image auto-updates: disabled;
- replicas: `1`;
- volume: `rebalancer-state`, mounted at `/app/state`;
- public networking: one Railway-generated HTTPS domain targeting internal HTTP port `18778`;
- private networking: enabled, with `IB_HOST=ibkr-gateway.railway.internal`;
- health-check path: `/healthz`;
- restart policy: `Always`;
- variables:
  - `PORT=18778`
  - `TRADING_MODE=${{shared.TRADING_MODE}}`
  - `DASHBOARD_ADMIN_PASSWORD=${{secret(32)}}`
  - `ZEHNLABS_API_KEY=${{shared.ZEHNLABS_API_KEY}}`
  - `ALLOCATIONS_BASE_URL=https://workers.fintech.zehnlabs.com/api/v1/strategies`
  - `LOG_LEVEL=INFO`
  - `IB_HOST=ibkr-gateway.railway.internal`
  - `USER_NOTIFICATIONS_ENABLED=false`
  - `USER_NOTIFICATIONS_CHANNEL=`
  - `TZ=America/New_York`
  - `RAILWAY_RUN_UID=0`
  - `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0`
  - `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=900`

`DASHBOARD_ADMIN_PASSWORD` is an ordinary revealable Railway variable because the project owner
needs it for first login. `RAILWAY_RUN_UID=0` only lets the entrypoint repair `/app/state`; the
entrypoint immediately starts Python as the non-root `appuser`.

### `ibkr-gateway`

- source:
  `gnzsnz/ib-gateway:10.51.1e@sha256:85975f493bcacc8341cb42e6946c460868d165e5f64d9924b1958c8f03e7bee4`;
- image auto-updates: disabled;
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

`RAILWAY_ENVIRONMENT_ID` is injected by Railway and must not be a template input. Editing the shared
`TRADING_MODE` creates staged changes; the owner must deploy all affected changes so the two
services restart with the same mode.

### Shutdown and backups

Railway sends `SIGTERM`, waits up to the configured 900 seconds, and then force-stops a process that
has not exited. This gives normal broker timeouts room to finish but cannot promise every broker
operation will finish. Startup marks unfinished Runs and Work Items `interrupted` and never replays
them automatically.

The subscriber must enable daily and weekly backup schedules for both volumes. Before every
application or Gateway upgrade, the subscriber must take a manual backup of both. Railway provides
the backup facility; creating a deployment from the template does not prove that a schedule is
enabled or that a restore works.

## Railway distribution gate

The shareable template and README button require all of the following:

- the exact candidate manifest passes `./tools/validate.sh`, the production-image VPS paper suite on
  ARM64, and the Railway paper suite on the platform Railway selects;
- the reference project and a clean template-generated project each create exactly two services and
  two volumes;
- only the dashboard has a public HTTPS domain;
- the Gateway is reachable from `rebalancer` only through Railway private networking;
- Railway generates a revealable dashboard password and login requires only that application login;
- both services run one replica, image auto-updates are off, and the resolved image digests equal
  the approved digests;
- volume ownership works on a fresh deployment and both volumes survive restart and redeploy;
- shared mode changes, health checks, restart policies, and the shutdown period behave as documented;
- Account and Strategy discovery, allocation access, Preview, paper rebalances, schedules, Run
  history, and restart recovery work with separately supplied paper credentials;
- daily and weekly schedules are enabled for both reference-project volumes;
- manual backups of both volumes restore successfully inside the paper-only reference project and
  environment; and
- a second deployment made only from the template URL repeats the result without inherited secrets
  or registry access.

If any gate fails, the button remains withheld while the implementation or template is corrected
and the affected tests are repeated.

## Future release process

Every future release follows the same sequence:

1. **Prepare.** Merge reviewed work to `main`; update the changelog; decide the semantic version;
   document any database migration, required operator action, Gateway change, and rollback limit.
2. **Select.** Advance protected `release` to the exact intended commit. Run the complete validation
   suite. Do not publish directly from an unreviewed feature branch.
3. **Build once.** Publish a commit-addressed multi-platform candidate through GitHub Actions and
   record its manifest and child digests. Never reuse a candidate or stable tag for different bytes.
4. **Back up staging.** Take manual backups of both reference-project volumes before deploying the
   candidate.
5. **Test the exact manifest.** Deploy it to the paper-only Railway reference project and the VPS
   paper installation. At minimum, verify startup, login, readiness, Preview, one representative
   paper rebalance, persistence, and clean restart. Trading, storage, Gateway, authentication,
   scheduling, or migration changes require the corresponding deeper acceptance and restore tests.
6. **Promote.** After owner approval, add the stable version tag to that same manifest and create the
   Git tag and GitHub Release. Do not rebuild.
7. **Update the template.** Change the template to the new stable pinned reference. Keep image
   auto-updates off. Deploy a fresh project from the updated template and verify its resolved digest
   and basic paper flow.
8. **Save and announce.** Save the template revision and notify subscribers through the normal
   subscriber channel, linking to the GitHub Release and upgrade instructions.

The README button URL normally stays the same. It changes only if the Railway template itself must
be replaced rather than updated.

### What happens to existing subscribers

Nothing changes automatically. An existing project continues running its old digest. The release
announcement tells the owner what changed, whether the update is recommended or required, whether
rollback is supported, and the new exact image reference.

To upgrade, the subscriber should:

1. choose a time outside market hours and confirm there is no running work or working broker order;
2. take manual backups of both volumes;
3. change only the `rebalancer` image source to the announced pinned reference, unless the
   release explicitly includes a Gateway update;
4. leave image auto-updates off and deploy the staged change;
5. verify health, dashboard login, Account readiness, and Run history;
6. run a Preview before the next live rebalance; and
7. retain the pre-upgrade backups until the new release is accepted.

If startup or validation fails, stop rather than repeatedly redeploying. A release without a schema
change may permit selecting the previous image. A release with a forward-only schema change may
require restoring the pre-upgrade state volume. The release notes are authoritative for that
release.

A Gateway update is never hidden inside an application update. It receives its own pinned digest,
paper authentication test, settings-volume backup, restore check, and explicit release note.

## Primary platform references

- [Create a Railway template](https://docs.railway.com/templates/create)
- [Publish and share a Railway template](https://docs.railway.com/templates/publish-and-share)
- [Railway template updates and Docker-image limitation](https://docs.railway.com/templates/updates)
- [Railway image auto-updates](https://docs.railway.com/deployments/image-auto-updates)
- [Railway private registry plan requirement](https://docs.railway.com/builds/private-registries)
- [Railway template best practices](https://docs.railway.com/templates/best-practices)
- [Railway workspaces](https://docs.railway.com/projects/workspaces)
- [Railway volume backups and restore boundary](https://docs.railway.com/volumes/backups)
- [GitHub Container Registry billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages)
- [Publishing packages with GitHub Actions](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions)
