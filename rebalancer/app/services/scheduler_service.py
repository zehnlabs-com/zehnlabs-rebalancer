"""One date job targeting the earliest durable pending Work Item."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from ..state.models import parse_utc
from ..state.sqlite import SQLiteState


class WorkDispatcher(Protocol):
    def recover_overdue(self) -> None: ...

    async def dispatch_due(self) -> None: ...


class SchedulerService:
    def __init__(
        self,
        *,
        state: SQLiteState,
        work_manager: WorkDispatcher,
        logger: Optional[logging.Logger] = None,
    ):
        self.state = state
        self.work_manager = work_manager
        self.logger = logger or logging.getLogger(__name__)
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._active_dispatch: Optional[asyncio.Task[None]] = None
        self.running = False

    async def start(self) -> None:
        if self.running:
            return
        self.scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self.scheduler.start()
        self.running = True
        self.work_manager.recover_overdue()
        self.recalculate()

    async def stop(self) -> None:
        if not self.running:
            return
        scheduler, self.scheduler = self.scheduler, None
        self.running = False
        if scheduler is not None:
            scheduler.pause()
        active_dispatch = self._active_dispatch
        if active_dispatch is not None and active_dispatch is not asyncio.current_task():
            await active_dispatch
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    def recalculate(self) -> None:
        if not self.running or self.scheduler is None:
            return
        existing = self.scheduler.get_job("pending_work")
        if existing is not None:
            self.scheduler.remove_job("pending_work")
        earliest = self.state.earliest_pending_execution_time()
        if earliest is None:
            return
        run_date = parse_utc(earliest)
        now = datetime.now(timezone.utc)
        if run_date < now:
            run_date = now
        self.scheduler.add_job(
            self._dispatch,
            DateTrigger(run_date=run_date),
            id="pending_work",
            name="Earliest pending rebalancer work",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=None,
            max_instances=1,
        )

    async def _dispatch(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_dispatch = task
        try:
            await self.work_manager.dispatch_due()
        except Exception as exc:
            self.logger.error("Pending Work dispatch failed: %s", exc)
        finally:
            try:
                self.recalculate()
            finally:
                if self._active_dispatch is task:
                    self._active_dispatch = None

    def get_next_run_time(self) -> Optional[str]:
        if self.scheduler is None:
            return None
        job = self.scheduler.get_job("pending_work")
        return job.next_run_time.isoformat() if job and job.next_run_time else None
