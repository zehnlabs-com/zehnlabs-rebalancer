"""Market-open job and schedule-file helpers (PRD §10.4–10.6, §12).

Only the parent process writes the scheduled file. `read_schedule` / `write_schedule`
/ `add_account` / `remove_accounts` are the atomic read-filter/append-write helpers used
by both SCH-7 and RTY-2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from typing import Callable, Collection, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app_config import AppConfig

from ..accounts import AccountConfig
from ..market_clock import MarketClock
from ..rebalance import new_run_id
from ..results import AccountRunResult, RunOutcome
from .notification_service import (
    Notifier,
    NotificationContext,
    NotificationKind,
)

_ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Schedule-file helpers (RTY-2 / SCH-7): atomic read-filter/append-write.
# --------------------------------------------------------------------------- #
def read_schedule(path: str, logger: logging.Logger) -> Optional[list[str]]:
    """Read the scheduled file. Missing -> []. Malformed -> None (leave untouched)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Invalid scheduled file %s: %s", path, exc)
        return None
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        logger.error("Scheduled file %s is not a JSON array of strings", path)
        return None
    return data


def write_schedule(path: str, ids: Collection[str]) -> None:
    """Atomic write: `<file>.tmp` then `os.replace`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)
    os.replace(tmp, path)


def add_account(path: str, account_id: str, logger: logging.Logger) -> None:
    """Add an account ID if not present; atomic write. Never clobber a malformed file."""
    ids = read_schedule(path, logger)
    if ids is None:
        logger.error("Not adding %s: scheduled file %s is invalid", account_id, path)
        return
    if account_id not in ids:
        ids.append(account_id)
        try:
            write_schedule(path, ids)
        except OSError as exc:
            logger.error("Failed to schedule %s: %s", account_id, exc)


def remove_accounts(path: str, account_ids: Collection[str], logger: logging.Logger) -> None:
    """Remove the given IDs if present; atomic write only when something changes."""
    ids = read_schedule(path, logger)
    if ids is None:
        logger.error("Not removing %s: scheduled file %s is invalid", list(account_ids), path)
        return
    drop = set(account_ids)
    remaining = [x for x in ids if x not in drop]
    if remaining == ids:
        return
    try:
        write_schedule(path, remaining)
    except OSError as exc:
        logger.error("Failed to update schedule: %s", exc)


# --------------------------------------------------------------------------- #
# Scheduler service
# --------------------------------------------------------------------------- #
class SchedulerService:
    """SCH-1..8: the market-open job over the scheduled-file accounts."""

    def __init__(
        self,
        *,
        config: AppConfig,
        strategy_executor,
        accounts_lookup: Callable[[str], Optional[AccountConfig]],
        clock: MarketClock,
        notifier: Notifier,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.strategy_executor = strategy_executor
        self.accounts_lookup = accounts_lookup
        self.clock = clock
        self.notifier = notifier
        self.logger = logger or logging.getLogger(__name__)
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.scheduled_file = config.scheduler.scheduled_file_path
        self.running = False

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not self.config.scheduler.enabled:
            self.logger.info("Scheduler is disabled in configuration")
            return
        self.scheduler = AsyncIOScheduler()
        hour, minute = self.config.scheduler.market_open_time.split(":")
        self.scheduler.add_job(
            self._execute_scheduled_rebalance,
            CronTrigger(day_of_week="mon-fri", hour=int(hour), minute=int(minute)),
            id="market_open_rebalance",
            name="Market Open Rebalance",
            misfire_grace_time=300,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        self.running = True
        self.logger.info("Scheduler started - market open rebalance at %s ET (Mon-Fri)",
                         self.config.scheduler.market_open_time)

    async def stop(self) -> None:
        if self.scheduler and self.running:
            self.scheduler.shutdown(wait=False)
            self.running = False
            self.logger.info("Scheduler stopped")

    # ------------------------------------------------------------------ #
    async def _execute_scheduled_rebalance(self) -> None:
        today = self.clock.now().date()
        if not self.clock.is_trading_day(today):  # SCH-2
            self.logger.info("%s is not a trading day; keeping scheduled entries", today)
            return
        await self._process_scheduled_accounts()

    async def _process_scheduled_accounts(self) -> None:
        original = read_schedule(self.scheduled_file, self.logger)
        if original is None:
            self.logger.error("Scheduled file invalid; leaving it untouched")
            return

        # SCH-3: remove duplicate IDs (keep first occurrence).
        ids: list[str] = []
        seen: set[str] = set()
        for x in original:
            if x not in seen:
                seen.add(x)
                ids.append(x)
        if not ids:
            return

        # SCH-4: unknown/disabled/other-mode IDs are warnings and treated as processed.
        groups: dict[str, list[AccountConfig]] = defaultdict(list)
        for aid in ids:
            acct = self.accounts_lookup(aid)
            if acct is None:
                self.logger.warning("Scheduled account %s not found; treating as processed", aid)
            else:
                groups[acct.strategy_name].append(acct)

        # SCH-5: run every strategy's scheduled batch concurrently.
        tasks = [
            self.strategy_executor.execute(s, accts, "live", "scheduled")
            for s, accts in groups.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (s, accts), res in zip(groups.items(), results):
            if isinstance(res, Exception):
                self.logger.error("Scheduled strategy %s crashed: %s", s, res)
                continue
            if res is None:  # SCH-6: already running -> SKIPPED_FINAL
                for acct in accts:
                    await self._skipped_final(acct)
                continue

        # SCH-7: remove only the processed IDs; IDs added while the job ran survive.
        processed = set(ids)
        current = read_schedule(self.scheduled_file, self.logger)
        if current is None:
            return
        remaining = [x for x in current if x not in processed]
        if remaining != current:
            try:
                write_schedule(self.scheduled_file, remaining)
            except OSError as exc:
                self.logger.error("Failed to write scheduled file: %s", exc)

    async def _skipped_final(self, acct: AccountConfig) -> None:
        """SCH-6: one SKIPPED_FINAL notification for a dropped scheduled account."""
        now = self.clock.now()
        run_id = new_run_id(now, acct.account_id)
        result = AccountRunResult(
            run_id=run_id,
            account_id=acct.account_id,
            strategy_name=acct.strategy_name,
            mode="live",
            source="scheduled",
            outcome=RunOutcome.FAILED,
            record={"run_id": run_id, "account_id": acct.account_id,
                    "strategy_name": acct.strategy_name,
                    "started_at": now.astimezone(_ET).isoformat(),
                    "outcome": RunOutcome.FAILED.value},
        )
        await self.notifier.send(
            result, NotificationContext(kind=NotificationKind.SKIPPED_FINAL, next_run=now)
        )

    def get_next_run_time(self) -> Optional[str]:
        if self.scheduler and self.running:
            job = self.scheduler.get_job("market_open_rebalance")
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
        return None