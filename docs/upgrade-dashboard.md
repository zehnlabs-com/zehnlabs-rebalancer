# Upgrade to SQLite and the dashboard

This is a breaking state-interface migration.

1. Ensure no Run or broker order is active. Stop the old stack.
2. Back up `.env`, the legacy `accounts.yaml`, `config.yaml`, `data/`, and `ib_settings`.
3. Compare the legacy `config.yaml` with the shipped v3 defaults. The new release does not import it and uses exact immutable code defaults. Record explicit acceptance of every lost customization before activation.
4. Start the new stack with a fresh `rebalancer_state` volume and set a strong `DASHBOARD_ADMIN_PASSWORD`.
5. Log in and use the one-time Account YAML import. Import validates both modes and saves nothing unless every selected Strategy and Replacement Set is valid.
6. Rediscover Accounts and Preview every enabled Account with separately supplied paper credentials.
7. Review old pending schedules and recreate only intended work in the dashboard.
8. Do not replay a legacy manual-request file: whether it executed is ambiguous.
9. Keep legacy JSONL Run files unchanged in the pre-upgrade backup for their remaining retention period; they are not imported.
10. Complete the paper acceptance and restore drill before live activation. Include a transition between Strategies that requires whole-share sells and buys. Compare the execution quantities and cash reserve against the stored Run.

Never let the stopped old deployment and new deployment trade the same Account simultaneously. Rollback means stopping the new stack, confirming no broker work is active, restoring the complete old deployment and volumes, and re-verifying in paper mode.
