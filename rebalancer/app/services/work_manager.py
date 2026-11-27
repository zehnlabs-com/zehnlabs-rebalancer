"""Durable dashboard work submission, dispatch, and retry decisions."""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, Sequence
from uuid import uuid4

from ..accounts import AccountConfig, AccountRegistry
from ..market_clock import MarketClock
from ..results import (
    AccountRunResult,
    FailureReason,
    NoteOut,
    RunMode,
    RunOutcome,
    RunSource,
    WorkAction,
)
from ..settings import RuntimeSettings
from ..state.models import (
    OnboardingInitializationResult,
    RetryDecision,
    RetryDisposition,
    RunStart,
    WorkCreation,
    WorkItem,
    WorkRunStart,
    WorkSubmissionReceipt,
    WorkSource,
    WorkStatus,
    parse_utc,
    utc_now_text,
)
from ..state.sqlite import SQLiteState, StateError
from .strategy_executor import ExecutionDecision, StrategyExecutor


@dataclass(frozen=True)
class WorkPlan:
    """Validated Account and timing decision shared by confirmation and submit."""

    account: AccountConfig
    source: WorkSource
    execute_at: datetime

    @property
    def scheduled(self) -> bool:
        return self.source == WorkSource.SCHEDULED


class SchedulerController(Protocol):
    def recalculate(self) -> None: ...


class UnknownAccountError(ValueError):
    """The requested Account does not exist in the active registry."""


class WorkManager:
    """The only service that turns dashboard requests into executor calls."""

    def __init__(
        self,
        *,
        state: SQLiteState,
        registry: AccountRegistry,
        settings: RuntimeSettings,
        clock: MarketClock,
        executor: StrategyExecutor,
        logger: Optional[logging.Logger] = None,
    ):
        self.state = state
        self.registry = registry
        self.settings = settings
        self.clock = clock
        self.executor = executor
        self.logger = logger or logging.getLogger(__name__)
        self.scheduler: Optional[SchedulerController] = None
        self.accepting = True

    def set_scheduler(self, scheduler: SchedulerController) -> None:
        self.scheduler = scheduler

    def account_configuration_changed(self) -> None:
        """Wake the scheduler after Account persistence cancels pending work."""

        self._recalculate()

    def stop_accepting(self) -> None:
        self.accepting = False

    def submit(
        self,
        account_id: str,
        action: WorkAction,
        request_token: str,
        *,
        source: WorkSource = WorkSource.DASHBOARD,
        schedule_next_open: bool = False,
    ) -> WorkCreation:
        submission_id = str(uuid4())
        item = self._new_item(
            account_id,
            action,
            request_token,
            submission_id=submission_id,
            source=source,
            schedule_next_open=schedule_next_open,
        )
        creation = self.state.create_work_item(item)
        self._recalculate()
        return creation

    def submit_bulk(
        self,
        account_ids: Sequence[str],
        action: WorkAction,
        form_token: str,
        *,
        source: WorkSource,
    ) -> WorkSubmissionReceipt:
        if not self.accepting:
            raise StateError("New dashboard mutations are disabled during shutdown.")
        account_ids = sorted(set(account_ids))
        if not account_ids:
            raise ValueError("At least one Account is required for a bulk submission.")
        submission_id = str(uuid4())
        items = [
            self._new_item(
                account_id,
                action,
                f"{form_token}:{account_id}:{action.value}",
                submission_id=submission_id,
                source=source,
            )
            for account_id in account_ids
        ]
        creations = self.state.create_work_items(items)
        persisted_submission_ids = {creation.item.submission_id for creation in creations}
        if len(persisted_submission_ids) != 1:
            raise StateError("Bulk Work Items did not resolve to one durable submission.")
        receipt = self.state.get_work_submission(persisted_submission_ids.pop())
        if receipt is None:
            raise StateError("The Work submission receipt could not be read.")
        self._recalculate()
        return receipt

    def finish_onboarding(
        self,
        account_ids: Sequence[str],
        form_token: str,
        *,
        trading_mode: str,
        configuration_fingerprint: str,
    ) -> OnboardingInitializationResult:
        """Create or replay the form token's complete initial-rebalance set."""

        if not self.accepting:
            raise StateError("New dashboard mutations are disabled during shutdown.")
        submission_id = str(uuid4())
        items = [
            self._new_item(
                account_id,
                WorkAction.REBALANCE,
                f"{form_token}:{account_id}:{WorkAction.REBALANCE.value}",
                submission_id=submission_id,
                source=WorkSource.INITIAL,
            )
            for account_id in sorted(set(account_ids))
        ]
        result = self.state.finish_onboarding_initialization(
            trading_mode,
            configuration_fingerprint,
            items,
            self.clock.now(),
        )
        self._recalculate()
        return result

    def _new_item(
        self,
        account_id: str,
        action: WorkAction,
        request_token: str,
        *,
        submission_id: str,
        source: WorkSource,
        schedule_next_open: bool = False,
        require_accepting: bool = True,
    ) -> WorkItem:
        if require_accepting and not self.accepting:
            raise StateError("New dashboard mutations are disabled during shutdown.")
        if not request_token or len(request_token) > 4096:
            raise ValueError("The request token is invalid.")
        plan = self.plan(
            account_id,
            action,
            source=source,
            schedule_next_open=schedule_next_open,
        )
        now = self.clock.now()
        return WorkItem(
            work_id=str(uuid4()),
            submission_id=submission_id,
            account_id=plan.account.account_id,
            action=action,
            source=plan.source,
            status=WorkStatus.PENDING,
            execute_at=utc_now_text(plan.execute_at),
            request_token_hash=self._hash_token(request_token),
            created_at=utc_now_text(now),
        )

    def plan(
        self,
        account_id: str,
        action: WorkAction,
        *,
        source: WorkSource = WorkSource.DASHBOARD,
        schedule_next_open: bool = False,
    ) -> WorkPlan:
        """Validate an Account and make the authoritative scheduling decision."""

        account = self.registry.get(account_id)
        if account is None:
            raise UnknownAccountError("The Account does not exist.")
        if not account.enabled or account.type != self.settings.trading_mode:
            raise ValueError("The Account is disabled, unavailable, or belongs to another mode.")
        now = self.clock.now()
        selected_source = source
        execute_at = now
        if action == WorkAction.REBALANCE:
            if source == WorkSource.RETRY:
                execute_at = self.clock.next_scheduled_run(now)
            elif schedule_next_open or not self.clock.is_open(now):
                execute_at = self.clock.next_scheduled_run(now)
                selected_source = WorkSource.SCHEDULED
        return WorkPlan(
            account=account,
            source=selected_source,
            execute_at=execute_at,
        )

    def cancel(self, work_id: str) -> Optional[WorkItem]:
        item = self.state.cancel_work_item(
            work_id,
            "Cancelled before broker execution. No already-sent broker order was cancelled.",
            self.clock.now(),
        )
        self._recalculate()
        return item

    def prepare_retry_decision(
        self, result: AccountRunResult, *, exclude_work_id: Optional[str] = None
    ) -> RetryDecision:
        """Plan retry persistence and attach only notes the transaction can make true."""

        if result.failure_reason == FailureReason.STRATEGY_BUSY:
            self._note_no_retry(
                result,
                "No automatic retry was created because busy dashboard work does not retry.",
            )
            return RetryDecision(disposition=RetryDisposition.NOT_CREATED)
        if result.mode == RunMode.PREVIEW:
            return RetryDecision(disposition=RetryDisposition.NOT_CREATED)
        if result.outcome == RunOutcome.SUCCEEDED:
            if result.source in (RunSource.SIGNAL, RunSource.DASHBOARD, RunSource.INITIAL):
                self.state.cancel_pending_retry(result.account_id, self.clock.now())
                self._recalculate()
            return RetryDecision(disposition=RetryDisposition.NOT_CREATED)
        if result.source not in (RunSource.SIGNAL, RunSource.DASHBOARD, RunSource.INITIAL):
            self._note_no_retry(
                result,
                "No automatic retry was created because Scheduled and Next-open retry Runs are final.",
            )
            return RetryDecision(disposition=RetryDisposition.NOT_CREATED)
        existing = self.state.find_pending_rebalance_for_retry(
            result.account_id,
            exclude_work_id=exclude_work_id,
        )
        if existing is not None:
            self._note_existing_retry(result, existing)
            return RetryDecision(
                disposition=RetryDisposition.REUSED,
                item=existing,
            )
        account = self.registry.get(result.account_id)
        if account is None:
            self._note_no_retry(
                result,
                "No automatic retry was created because the Account is unavailable or disabled.",
            )
            return RetryDecision(disposition=RetryDisposition.NOT_CREATED)
        token = f"retry:{result.run_id}:{result.account_id}"
        item = self._new_item(
            result.account_id,
            WorkAction.REBALANCE,
            token,
            submission_id=str(uuid4()),
            source=WorkSource.RETRY,
            require_accepting=False,
        )
        self._note_created_retry(result, item)
        return RetryDecision(disposition=RetryDisposition.CREATED, item=item)

    @staticmethod
    def _note_existing_retry(result: AccountRunResult, existing: WorkItem) -> None:
        text = f"Existing pending rebalance Work Item {existing.work_id} satisfies the retry."
        if not any(note.text == text for note in result.notes):
            result.notes.append(NoteOut(level="info", text=text))
            result.record.notes = result.notes

    @staticmethod
    def _note_created_retry(result: AccountRunResult, item: WorkItem) -> None:
        text = (
            f"Automatic retry Work Item {item.work_id} was created for {item.execute_at}."
        )
        if not any(note.text == text for note in result.notes):
            result.notes.append(NoteOut(level="info", text=text))
            result.record.notes = result.notes

    @staticmethod
    def _note_no_retry(result: AccountRunResult, text: str) -> None:
        if not any(note.text == text for note in result.notes):
            result.notes.append(NoteOut(level="info", text=text))
            result.record.notes = result.notes

    def retry_persisted(self, decision: RetryDecision) -> None:
        if decision.disposition == RetryDisposition.CREATED:
            self._recalculate()

    def recover_overdue(self) -> None:
        """Apply the restart-only missed-time rule before scheduling begins."""

        now = self.clock.now()
        for item in self.state.due_pending_items(utc_now_text(now)):
            if item.action != WorkAction.REBALANCE:
                continue
            if self.clock.is_open(now) and self.clock.scheduled_time_has_passed(now):
                continue
            next_run = self.clock.next_scheduled_run(now)
            self.state.reschedule_work_item(
                item.work_id,
                utc_now_text(next_run),
                "Missed execution time moved to the next configured market open.",
            )

    async def dispatch_due(self) -> None:
        if not self.accepting or not self.executor.accepting:
            return
        now = self.clock.now()
        due = self.state.due_pending_items(utc_now_text(now))
        selected: list[WorkItem] = []
        seen_accounts: set[str] = set()
        for item in due:
            if item.account_id not in seen_accounts:
                seen_accounts.add(item.account_id)
                selected.append(item)

        runnable: list[tuple[WorkItem, AccountConfig]] = []
        for item in selected:
            account = self.registry.get(item.account_id)
            if account is None or not account.enabled or account.type != self.settings.trading_mode:
                self.state.cancel_work_item(
                    item.work_id,
                    "Cancelled because the Account is deleted, disabled, invalid, or in another mode.",
                    now,
                )
                continue
            if item.action == WorkAction.REBALANCE and not self.clock.is_open(now):
                next_run = self.clock.next_scheduled_run(now)
                self.state.reschedule_work_item(
                    item.work_id,
                    utc_now_text(next_run),
                    "Missed execution time moved to the next configured market open.",
                )
                continue
            runnable.append((item, account))

        groups: "OrderedDict[tuple[WorkAction, str], list[tuple[WorkItem, AccountConfig]]]" = OrderedDict()
        for pair in runnable:
            item, account = pair
            groups.setdefault((item.action, account.strategy_name), []).append(pair)
        for (_action, strategy), group in groups.items():
            await self._dispatch_group(strategy, group)
            if not self.executor.accepting:
                break
        self.state.prune_terminal(now)
        self._recalculate()

    async def _dispatch_group(
        self,
        strategy: str,
        group: Sequence[tuple[WorkItem, AccountConfig]],
    ) -> None:
        decision = self.executor.try_accept(strategy)
        if decision == ExecutionDecision.BUSY:
            batch_id = str(uuid4())
            mode = (
                RunMode.PREVIEW
                if group[0][0].action == WorkAction.PREVIEW
                else RunMode.LIVE
            )
            starts = [
                self.executor.build_starts(
                    [account],
                    mode,
                    RunSource(item.source.value),
                    batch_id=batch_id,
                )[0]
                for item, account in group
            ]
            for (item, _account), start in zip(group, starts):
                result = self.executor.static_result(
                    start,
                    RunOutcome.FAILED,
                    reason=FailureReason.STRATEGY_BUSY,
                    error="The Strategy was already running; this request did not reach the broker.",
                )
                retry = self.prepare_retry_decision(result, exclude_work_id=item.work_id)
                self.state.complete_busy_work(item.work_id, start, result, result.error_message or "")
                await self.executor.postprocess(
                    result, retry_decision=retry
                )
            return
        if decision != ExecutionDecision.ACCEPTED:
            raise StateError("Trading work is disabled because durable state is unavailable.")
        completed: list[tuple[AccountRunResult, RetryDecision]] = []
        try:
            batch_id = str(uuid4())
            mode = (
                RunMode.PREVIEW
                if group[0][0].action == WorkAction.PREVIEW
                else RunMode.LIVE
            )
            starts = [
                self.executor.build_starts(
                    [account],
                    mode,
                    RunSource(item.source.value),
                    batch_id=batch_id,
                )[0]
                for item, account in group
            ]
            assignments = [
                WorkRunStart(work_id=item.work_id, run=start)
                for (item, _account), start in zip(group, starts)
            ]
            self.state.start_work_batch(assignments, utc_now_text(self.clock.now()))
            try:
                try:
                    results = await self.executor.execute_reserved(strategy, starts)
                except Exception:
                    results = [
                        self.executor.static_result(
                            start,
                            RunOutcome.FAILED,
                            reason=FailureReason.INTERNAL_ERROR,
                            error="The worker coordinator failed before returning a result.",
                        )
                        for start in starts
                    ]
                by_id = {result.run_id: result for result in results}
                for (item, _account), start in zip(group, starts):
                    result = by_id.get(start.run_id) or self.executor.static_result(
                        start,
                        RunOutcome.FAILED,
                        reason=FailureReason.INTERNAL_ERROR,
                        error="No result was returned for this Account.",
                    )
                    retry = self.prepare_retry_decision(
                        result, exclude_work_id=item.work_id
                    )
                    self.state.complete_work_item(
                        item.work_id,
                        result,
                        retry_item=(
                            retry.item
                            if retry.disposition == RetryDisposition.CREATED
                            else None
                        ),
                    )
                    self.retry_persisted(retry)
                    completed.append((result, retry))
            except Exception as exc:
                self.executor.record_storage_failure(exc)
                raise
        finally:
            self.executor.release(strategy)
        for result, retry in completed:
            await self.executor.postprocess(
                result, retry_decision=retry
            )

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _recalculate(self) -> None:
        if self.scheduler is not None:
            self.scheduler.recalculate()
