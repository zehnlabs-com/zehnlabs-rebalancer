# Failed runs retry once at the next market open; scheduled failures never retry

Every failing or market-closed **live** run (signal or manual source) queues the
account into `data/market-open/scheduled.json` to run once more at the next open,
with fresh data (PRD §10.5). A **scheduled** run that fails or finds
the market closed is final — it is removed from the file and never re-added, and the
user is sent a `FAILED_FINAL` notification telling them to fix the issue and run
manually. Retries are always live-market only, never in preview. When the scheduler
is disabled, failures are reported as `FAILED_FINAL` too. The queuing happens only
in the parent process; the occasional cleanup race where a manual retry is
accidentally removed is an accepted edge case (SCH-8).
