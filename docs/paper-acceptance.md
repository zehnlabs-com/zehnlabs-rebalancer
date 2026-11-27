# Production-image paper acceptance

Use newly supplied paper credentials and synthetic/temporary local state. Never use the repository's existing `accounts.yaml`, a developer `.env`, or live credentials.

Record each result:

1. `./tools/validate.sh` passes, including the Linux production-image fork/descriptor and non-root probes.
2. Fresh Compose starts exactly two containers; `/login` and `/healthz` work before onboarding; dashboard and Gateway/VNC host bindings remain loopback-only.
3. Gateway MFA completes and read-only discovery lists only managed paper Accounts without order calls.
4. Strategy discovery, allocation-access validation, and complete-set save succeed; invalid assignments save nothing.
5. Every enabled Account Previews successfully with zero broker place/cancel calls.
6. Initial work runs immediately during market hours or is stored for the exact next configured XNYS time when closed.
7. Repeated dashboard submissions create one Work Item and one broker execution; pending Preview and rebalance work for one Account dispatch sequentially.
8. A signal reaches all enabled Accounts for its Strategy and produces SQLite Run history; retry-once behavior remains intact.
9. Restart preserves Accounts, pending Work Items, and terminal Runs; interrupted work is visible and never replayed.
10. Disablement, assignment changes, and pending-work cancellation are accurate and do not claim to cancel submitted broker orders.
11. A SQLite/volume backup restores successfully into paper mode.
12. Railway-specific deployment proves the exact template contract, generated password visibility, shared staged mode deployment, private Gateway, two backup schedules per volume, manual backups, and a recorded restore drill.

Do not present the Railway button as supported for live trading until this report is complete. If the Linux fork/descriptor-release probe fails, stop and request authorization before considering any process-start redesign.
