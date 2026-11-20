"""StrategyExecutor (parent process) (PRD §10.2).

Owns the process pool, the running-strategy set, and post-run handling:
scheduled-file changes (§10.5), run records (§12), and notifications (§11).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context
from typing import Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app_config import AppConfig

from ..accounts import AccountConfig, AccountRegistry
from ..market_clock import MarketClock
from ..rebalance import new_run_id
from ..results import AccountRunResult, FailureReason, NoteOut, RunOutcome
from ..settings import RuntimeSettings
from .notification_service import (
    Notifier,
    NotificationContext,
    NotificationKind,
)
from .run_records import RunRecords
from .scheduler_service import add_account, remove_accounts
from .trading_executor import execute_strategy_batch

_ET = ZoneInfo("America/New_York")


class StrategyExecutor:
    """EXE-1..3: the pool, the dedup set, and post-process for each result."""

    def __init__(
        self,
        *,
        config: AppConfig,
        settings: RuntimeSettings,
        registry: AccountRegistry,
        clock: MarketClock,
        notifier: Notifier,
        records: RunRecords,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.settings = settings
        self.registry = registry
        self.clock = clock
        self.notifier = notifier
        self.records = records
        self.logger = logger or logging.getLogger(__name__)
        self.running: set[str] = set()  # strategies currently running (dedup)
        # EXE-1: explicit fork context (PRD R12)
        self._pool = ProcessPoolExecutor(
            max_workers=config.executor.max_workers, mp_context=get_context("fork")
        )

    # ------------------------------------------------------------------ #
    async def execute(
        self,
        strategy: str,
        accounts: Sequence[AccountConfig],
        mode: str,
        source: str,
    ) -> Optional[list[AccountRunResult]]:
        """EXE-2. Runs a strategy batch and post-processes each result.

        Returns `None` when the trigger is dropped because the strategy is
        already running (no notification for signal/manual; scheduled callers
        turn that into a `SKIPPED_FINAL` notification, SCH-6).
        """
        if strategy in self.running:
            self.logger.warning(
                "Dropped %s trigger for strategy %s: a run is already in progress.",
                source, strategy,
            )
            return None

        # EXE-3: detect a closed market in the parent before starting a worker.
        if mode == "live" and not self.clock.is_open(self.clock.now()):
            results = [
                self._static_result(
                    a, mode, source, RunOutcome.MARKET_CLOSED,
                    error="The market was closed.",
                )
                for a in accounts
            ]
            await self._postprocess(results)
            return results

        self.running.add(strategy)
        try:
            results = await self._run_in_pool(strategy, accounts, mode, source)
        finally:
            # EXE-2 step 3: release the mark before any post-processing.
            self.running.discard(strategy)

        await self._postprocess(results)
        return results

    async def _run_in_pool(
        self, strategy: str, accounts: Sequence[AccountConfig], mode: str, source: str
    ) -> list[AccountRunResult]:
        the_pool = self._pool
        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                the_pool, execute_strategy_batch, strategy, list(accounts), mode, source,
                self.config, self.settings,
            )
        except BrokenProcessPool:
            self.logger.error("broker worker pool broke for strategy %s", strategy)
            if self._pool is the_pool:  # EXE-2 step 4: replace the broken pool
                self.logger.warning("replacing broken process pool")
                self._pool = ProcessPoolExecutor(
                    max_workers=self.config.executor.max_workers,
                    mp_context=get_context("fork"),
                )
            return [
                self._static_result(
                    a, mode, source, RunOutcome.FAILED,
                    reason=FailureReason.WORKER_CRASHED,
                    error="The worker process stopped unexpectedly. "
                    "Orders may have been placed; check the account.",
                )
                for a in accounts
            ]
        except Exception as exc:  # pickling error, etc. (EXE-2 step 4)
            self.logger.error("failed to run strategy %s: %s", strategy, exc)
            return [
                self._static_result(
                    a, mode, source, RunOutcome.FAILED,
                    reason=FailureReason.INTERNAL_ERROR,
                    error=f"Internal error: {type(exc).__name__}: {exc}"[:300],
                )
                for a in accounts
            ]
        try:
            return [AccountRunResult(**d) for d in raw]
        except Exception as exc:  # worker returned malformed data
            self.logger.error("malformed worker results for %s: %s", strategy, exc)
            return [
                self._static_result(
                    a, mode, source, RunOutcome.FAILED,
                    reason=FailureReason.INTERNAL_ERROR,
                    error=f"Internal error: {type(exc).__name__}: {exc}"[:300],
                )
                for a in accounts
            ]

    # ------------------------------------------------------------------ #
    async def _postprocess(self, results: Sequence[AccountRunResult]) -> None:
        """EXE-2 step 5: each post-processing step is independently guarded."""
        for result in results:
            kind, action = self._decision(result)
            try:
                self.records.append(result.record)
            except Exception as exc:  # noqa: BLE001 - one failure stops nothing
                self.logger.error("run-record write failed for %s: %s", result.run_id, exc)
            try:
                self._apply(action, result)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("schedule update failed for %s: %s", result.account_id, exc)
            try:
                await self._send(result, kind)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("notification failed for %s: %s", result.account_id, exc)

    def _decision(self, result: AccountRunResult) -> Tuple[NotificationKind, Optional[str]]:
        """PRD §10.5: map a result to (notification kind, schedule action)."""
        if result.mode == "preview":
            kind = (NotificationKind.PREVIEW if result.outcome == RunOutcome.PREVIEWED
                    else NotificationKind.PREVIEW_FAILED)
            return kind, None

        if result.source == "scheduled":
            kind = (NotificationKind.SUCCESS if result.outcome == RunOutcome.SUCCEEDED
                    else NotificationKind.FAILED_FINAL)
            return kind, None

        # signal / manual, live
        if result.outcome == RunOutcome.SUCCEEDED:
            return NotificationKind.SUCCESS, "remove"

        if not self.config.scheduler.enabled:  # RTY-1
            result.notes.append(NoteOut(
                level="warning",
                text="Automatic retry is disabled (scheduler.enabled: false).",
            ))
            return NotificationKind.FAILED_FINAL, None

        if result.outcome == RunOutcome.MARKET_CLOSED:
            return NotificationKind.QUEUED_MARKET_CLOSED, "add"
        return NotificationKind.FAILED_WILL_RETRY, "add"

    def _apply(self, action: Optional[str], result: AccountRunResult) -> None:
        """RTY-2: add/remove the account from the scheduled file (parent only)."""
        if action is None:
            return
        path = self.config.scheduler.scheduled_file_path
        if action == "add":
            add_account(path, result.account_id, self.logger)
        else:
            remove_accounts(path, [result.account_id], self.logger)

    async def _send(self, result: AccountRunResult, kind: NotificationKind) -> None:
        next_run = None
        if kind in (NotificationKind.QUEUED_MARKET_CLOSED, NotificationKind.FAILED_WILL_RETRY):
            next_run = self.clock.next_scheduled_run(self.clock.now())
        await self.notifier.send(result, NotificationContext(kind=kind, next_run=next_run))

    def _static_result(
        self,
        account: AccountConfig,
        mode: str,
        source: str,
        outcome: RunOutcome,
        *,
        reason: Optional[FailureReason] = None,
        error: Optional[str] = None,
    ) -> AccountRunResult:
        """Build a minimal parent-side result (market-closed, worker-crash, internal)."""
        now = self.clock.now().astimezone(_ET)
        run_id = new_run_id(now, account.account_id)
        record = {
            "run_id": run_id,
            "account_id": account.account_id,
            "strategy_name": account.strategy_name,
            "mode": mode, "source": source,
            "started_at": now.isoformat(), "finished_at": now.isoformat(),
            "outcome": outcome.value,
            "failure_reason": reason.value if reason else None,
            "error_message": error,
            "notes": [],
        }
        return AccountRunResult(
            run_id=run_id,
            account_id=account.account_id,
            strategy_name=account.strategy_name,
            mode=mode, source=source,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
            record=record,
        )

    def cleanup(self) -> None:
        """BOOT-5: shut the pool down with wait + cancel_futures."""
        try:
            self._pool.shutdown(wait=True, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("error during pool cleanup: %s", exc)