"""Parent-owned process-pool execution and Signal persistence."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from multiprocessing import get_context
from typing import Optional, Protocol, Sequence
from uuid import uuid4

from app_config import AppConfig

from ..accounts import AccountConfig
from ..market_clock import MarketClock
from ..rebalance import new_run_id
from ..results import (
    AccountRunResult,
    FailureReason,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
)
from ..settings import RuntimeSettings
from ..state.models import (
    RetryDecision,
    RetryDisposition,
    RunStart,
    utc_now_text,
)
from ..state.sqlite import SQLiteState, StateError
from .notification_service import Notifier, NotificationContext, NotificationKind
from .trading_executor import WorkerRunRequest, execute_strategy_batch


class ExecutionDecision(StrEnum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    STORAGE_UNAVAILABLE = "storage_unavailable"


@dataclass(frozen=True)
class ExecutionBatch:
    decision: ExecutionDecision
    results: tuple[AccountRunResult, ...] = ()


class RetryCoordinator(Protocol):
    """Narrow retry-persistence seam used by the execution coordinator."""

    def prepare_retry_decision(
        self, result: AccountRunResult, *, exclude_work_id: Optional[str] = None
    ) -> RetryDecision: ...

    def retry_persisted(self, decision: RetryDecision) -> None: ...


class StrategyExecutor:
    """Keeps the existing per-Strategy gate and explicit Linux `fork` pool."""

    def __init__(
        self,
        *,
        config: AppConfig,
        settings: RuntimeSettings,
        clock: MarketClock,
        notifier: Notifier,
        state: SQLiteState,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.settings = settings
        self.clock = clock
        self.notifier = notifier
        self.state = state
        self.logger = logger or logging.getLogger(__name__)
        self.running: set[str] = set()
        self.accepting = True
        self.critical_storage_error: Optional[str] = None
        self.retry_coordinator: Optional[RetryCoordinator] = None
        self._pool = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.config.executor.max_workers,
            mp_context=get_context("fork"),
        )

    def set_retry_coordinator(self, coordinator: RetryCoordinator) -> None:
        self.retry_coordinator = coordinator

    def try_accept(self, strategy: str) -> ExecutionDecision:
        """Synchronous gate used before durable state or any worker submission."""
        if not self.accepting:
            return ExecutionDecision.STORAGE_UNAVAILABLE
        if strategy in self.running:
            return ExecutionDecision.BUSY
        self.running.add(strategy)
        return ExecutionDecision.ACCEPTED

    def release(self, strategy: str) -> None:
        self.running.discard(strategy)

    def build_starts(
        self,
        accounts: Sequence[AccountConfig],
        mode: RunMode,
        source: RunSource,
        *,
        batch_id: Optional[str] = None,
        requested_at: Optional[str] = None,
    ) -> list[RunStart]:
        now = self.clock.now()
        selected_batch_id = batch_id or str(uuid4())
        selected_requested_at = requested_at or utc_now_text(now)
        return [
            RunStart(
                run_id=new_run_id(now, account.account_id),
                batch_id=selected_batch_id,
                account=account,
                trading_mode=self.settings.trading_mode,
                run_mode=mode,
                source=source,
                requested_at=selected_requested_at,
            )
            for account in accounts
        ]

    async def execute(
        self,
        strategy: str,
        accounts: Sequence[AccountConfig],
        mode: RunMode,
        source: RunSource,
    ) -> ExecutionBatch:
        """Execute a Signal batch; dashboard Work Items use `execute_reserved`."""
        decision = self.try_accept(strategy)
        if decision != ExecutionDecision.ACCEPTED:
            if decision == ExecutionDecision.BUSY:
                self.logger.warning("Dropped %s for busy Strategy %s", source.value, strategy)
            return ExecutionBatch(decision)
        completed: list[tuple[AccountRunResult, RetryDecision]] = []
        try:
            batch_id = str(uuid4())
            starts = self.build_starts(accounts, mode, source, batch_id=batch_id)
            self.state.start_runs(starts)
            try:
                results = await self._execute_reserved_no_release(strategy, starts)
                for result in results:
                    retry = (
                        self.retry_coordinator.prepare_retry_decision(result)
                        if self.retry_coordinator is not None
                        else RetryDecision(disposition=RetryDisposition.NOT_CREATED)
                    )
                    self.state.finish_run(
                        result,
                        retry_item=(
                            retry.item
                            if retry.disposition == RetryDisposition.CREATED
                            else None
                        ),
                    )
                    if self.retry_coordinator is not None:
                        self.retry_coordinator.retry_persisted(retry)
                    completed.append((result, retry))
            except Exception as exc:
                self.record_storage_failure(exc)
                raise
        finally:
            self.release(strategy)
        for result, retry in completed:
            await self.postprocess(result, retry_decision=retry)
        self.state.prune_terminal()
        return ExecutionBatch(ExecutionDecision.ACCEPTED, tuple(results))

    async def execute_reserved(
        self, strategy: str, starts: Sequence[RunStart]
    ) -> list[AccountRunResult]:
        """Execute after a caller accepted the gate; that caller releases it after persistence."""
        return await self._execute_reserved_no_release(strategy, starts)

    async def _execute_reserved_no_release(
        self, strategy: str, starts: Sequence[RunStart]
    ) -> list[AccountRunResult]:
        if not starts:
            return []
        mode = starts[0].run_mode
        source = starts[0].source
        if mode == RunMode.LIVE and not self.clock.is_open(self.clock.now()):
            return [
                self.static_result(
                    start,
                    RunOutcome.MARKET_CLOSED,
                    error="The market was closed.",
                )
                for start in starts
            ]
        return await self._run_in_pool(strategy, starts)

    async def _run_in_pool(
        self, strategy: str, starts: Sequence[RunStart]
    ) -> list[AccountRunResult]:
        requests = [
            WorkerRunRequest(start.account, start.run_id, start.run_mode, start.source)
            for start in starts
        ]
        current_pool = self._pool
        try:
            worker_results = await asyncio.get_running_loop().run_in_executor(
                current_pool,
                execute_strategy_batch,
                strategy,
                requests,
                self.config,
                self.settings.for_worker(),
            )
        except BrokenProcessPool:
            self.logger.error("Worker pool broke for Strategy %s", strategy)
            if self._pool is current_pool:
                self._pool = self._new_pool()
            return [
                self.static_result(
                    start,
                    RunOutcome.FAILED,
                    reason=FailureReason.WORKER_CRASHED,
                    error=(
                        "The worker process stopped unexpectedly. Orders may have been placed; "
                        "check the Account."
                    ),
                )
                for start in starts
            ]
        except Exception as exc:
            self.logger.error(
                "Worker submission failed for Strategy %s (%s)",
                strategy,
                type(exc).__name__,
            )
            return [
                self.static_result(
                    start,
                    RunOutcome.FAILED,
                    reason=FailureReason.INTERNAL_ERROR,
                    error="The worker could not be started for this request.",
                )
                for start in starts
            ]
        try:
            if not isinstance(worker_results, list) or not all(
                isinstance(result, AccountRunResult) for result in worker_results
            ):
                raise TypeError("worker returned an untyped result")
            by_id = {result.run_id: result for result in worker_results}
            if len(by_id) != len(worker_results) or set(by_id) != {
                start.run_id for start in starts
            }:
                raise ValueError("worker result IDs did not match submitted Runs")
            return [by_id[start.run_id] for start in starts]
        except Exception as exc:
            self.logger.error("Malformed worker results for Strategy %s: %s", strategy, exc)
            return [
                self.static_result(
                    start,
                    RunOutcome.FAILED,
                    reason=FailureReason.INTERNAL_ERROR,
                    error="The worker returned an invalid result.",
                )
                for start in starts
            ]

    def static_result(
        self,
        start: RunStart,
        outcome: RunOutcome,
        *,
        reason: Optional[FailureReason] = None,
        error: Optional[str] = None,
    ) -> AccountRunResult:
        now = utc_now_text(self.clock.now())
        record = RunRecord(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=start.run_mode,
            source=start.source,
            started_at=now,
            finished_at=now,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
            replacement_set=start.account.replacement_set,
        )
        return AccountRunResult(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=start.run_mode,
            source=start.source,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
            record=record,
        )

    async def postprocess(
        self,
        result: AccountRunResult,
        *,
        retry_decision: Optional[RetryDecision] = None,
    ) -> None:
        decision = retry_decision or RetryDecision(
            disposition=RetryDisposition.NOT_CREATED
        )
        try:
            await self.notifier.send(
                result,
                self.notification_context(result, retry_decision=decision),
            )
        except Exception as exc:
            self.logger.error("Notification failed for Run %s: %s", result.run_id, exc)

    def notification_context(
        self,
        result: AccountRunResult,
        *,
        retry_decision: Optional[RetryDecision] = None,
    ) -> NotificationContext:
        decision = retry_decision or RetryDecision(
            disposition=RetryDisposition.NOT_CREATED
        )
        if result.failure_reason == FailureReason.STRATEGY_BUSY:
            return NotificationContext(kind=NotificationKind.FAILED_FINAL)
        if result.mode == RunMode.PREVIEW:
            kind = (
                NotificationKind.PREVIEW
                if result.outcome == RunOutcome.PREVIEWED
                else NotificationKind.PREVIEW_FAILED
            )
            return NotificationContext(kind=kind)
        if result.outcome == RunOutcome.SUCCEEDED:
            return NotificationContext(kind=NotificationKind.SUCCESS)
        if result.source in (RunSource.SCHEDULED, RunSource.RETRY):
            return NotificationContext(kind=NotificationKind.FAILED_FINAL)
        if decision.disposition not in (
            RetryDisposition.CREATED,
            RetryDisposition.REUSED,
        ):
            return NotificationContext(kind=NotificationKind.FAILED_FINAL)
        assert decision.item is not None
        if result.outcome == RunOutcome.MARKET_CLOSED:
            return NotificationContext(
                kind=NotificationKind.QUEUED_MARKET_CLOSED,
                retry_decision=decision,
            )
        return NotificationContext(
            kind=NotificationKind.FAILED_WILL_RETRY,
            retry_decision=decision,
        )

    def record_storage_failure(self, exc: BaseException) -> None:
        self.accepting = False
        self.critical_storage_error = "Critical storage error; trading requests are disabled."
        self.logger.critical("Terminal Run persistence failed: %s", exc)

    def stop_accepting(self) -> None:
        self.accepting = False

    def cleanup(self) -> None:
        self.accepting = False
        try:
            self._pool.shutdown(wait=True, cancel_futures=True)
        except Exception as exc:
            self.logger.error("Error during worker-pool cleanup: %s", exc)
