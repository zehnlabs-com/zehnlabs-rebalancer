"""Typed in-process façade used by the server-rendered dashboard."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict

from .accounts import AccountConfig
from .results import RunOutcome, RunSource, WorkAction
from .services.account_manager import (
    AccountManager,
    AccountSaveResult,
    DiscoveryResult,
    ExternalReadiness,
    ReadinessState,
)
from .services.strategy_executor import StrategyExecutor
from .services.work_manager import WorkManager
from .settings import RuntimeSettings
from .state.models import (
    OnboardingInitializationResult,
    StoredRun,
    WorkCreation,
    WorkItem,
    WorkSource,
    WorkStatus,
    WorkSubmissionReceipt,
)
from .state.sqlite import SQLiteState


class ReadinessStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_ready: bool
    gateway_configured: bool
    zehnlabs_configured: bool
    ably_ready: bool
    gateway_state: ReadinessState
    gateway_message: str
    zehnlabs_state: ReadinessState
    zehnlabs_message: str
    ably_state: ReadinessState
    ably_message: str
    degraded_error: Optional[str] = None


class DashboardStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_mode: str
    onboarded: bool
    accepting_mutations: bool
    pending_work: int
    recent_runs: tuple[StoredRun, ...]
    readiness: ReadinessStatus
    critical_storage_error: Optional[str] = None


class WorkConfirmation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: AccountConfig
    action: WorkAction
    execute_at: datetime
    scheduled: bool
    live_ack_required: bool


class RebalancerApplication:
    """Small typed surface; HTTP routes contain no domain orchestration."""

    def __init__(
        self,
        *,
        settings: RuntimeSettings,
        state: SQLiteState,
        accounts: AccountManager,
        work: WorkManager,
        executor: StrategyExecutor,
    ):
        self.settings = settings
        self.state = state
        self.accounts = accounts
        self.work = work
        self.executor = executor
        self.accepting_mutations = True

    async def status(self) -> DashboardStatus:
        subscriber_status = await self.accounts.subscriber_status()
        pending = self.state.list_work_items(statuses=[WorkStatus.PENDING], limit=1000)
        gateway = getattr(
            self.accounts,
            "gateway_readiness",
            ExternalReadiness(
                state=(
                    ReadinessState.UNVERIFIED
                    if self.settings.ib_host.strip()
                    else ReadinessState.MISSING
                ),
                message=(
                    "Gateway connectivity and authentication have not been verified."
                    if self.settings.ib_host.strip()
                    else "IB_HOST is missing."
                ),
            ),
        )
        zehnlabs = getattr(
            self.accounts,
            "zehnlabs_readiness",
            ExternalReadiness(
                state=(
                    ReadinessState.UNVERIFIED
                    if self.settings.zehnlabs_api_key.strip()
                    else ReadinessState.MISSING
                ),
                message=(
                    "Zehnlabs API-key access has not been verified."
                    if self.settings.zehnlabs_api_key.strip()
                    else "ZEHNLABS_API_KEY is missing."
                ),
            ),
        )
        subscriber_error = getattr(subscriber_status, "degraded_error", None)
        if subscriber_status.ably_connected:
            ably = ExternalReadiness(
                state=ReadinessState.READY,
                message="Ably Signal subscriptions are connected.",
            )
        elif subscriber_error or self.accounts.degraded_error:
            ably = ExternalReadiness(
                state=ReadinessState.DEGRADED,
                message="Ably Signal subscriptions are degraded and will retry automatically.",
            )
        else:
            ably = ExternalReadiness(
                state=ReadinessState.UNVERIFIED,
                message="Ably connectivity has not been verified.",
            )
        return DashboardStatus(
            trading_mode=self.settings.trading_mode,
            onboarded=self._onboarded(),
            accepting_mutations=self.accepting_mutations and self.executor.accepting,
            pending_work=len(pending),
            recent_runs=tuple(self.state.list_runs(limit=10)),
            readiness=ReadinessStatus(
                database_ready=self.state.health_check(),
                gateway_configured=bool(self.settings.ib_host),
                zehnlabs_configured=bool(self.settings.zehnlabs_api_key.strip()),
                ably_ready=subscriber_status.ably_connected,
                gateway_state=gateway.state,
                gateway_message=gateway.message,
                zehnlabs_state=zehnlabs.state,
                zehnlabs_message=zehnlabs.message,
                ably_state=ably.state,
                ably_message=ably.message,
                degraded_error=self.accounts.degraded_error or subscriber_error,
            ),
            critical_storage_error=self.executor.critical_storage_error,
        )

    async def discover(self) -> DiscoveryResult:
        self._require_mutations()
        return await self.accounts.discover()

    async def replace_accounts(
        self, accounts: Sequence[AccountConfig]
    ) -> AccountSaveResult:
        self._require_mutations()
        result = await self.accounts.save(accounts)
        self.work.account_configuration_changed()
        return result

    async def import_accounts(self, document: str) -> AccountSaveResult:
        self._require_mutations()
        result = await self.accounts.import_yaml(document)
        self.work.account_configuration_changed()
        return result

    def export_accounts(self) -> str:
        return self.accounts.export_yaml()

    def list_accounts(self) -> list[AccountConfig]:
        return self.state.list_accounts(self.settings.trading_mode)

    def submit_work(
        self,
        account_id: str,
        action: WorkAction,
        request_token: str,
        *,
        schedule_next_open: bool = False,
        source: WorkSource = WorkSource.DASHBOARD,
    ) -> WorkCreation:
        self._require_mutations()
        return self.work.submit(
            account_id,
            action,
            request_token,
            source=source,
            schedule_next_open=schedule_next_open,
        )

    def work_confirmation(
        self,
        account_id: str,
        action: WorkAction,
        *,
        schedule_next_open: bool = False,
    ) -> WorkConfirmation:
        self._require_mutations()
        plan = self.work.plan(
            account_id,
            action,
            schedule_next_open=schedule_next_open,
        )
        return WorkConfirmation(
            account=plan.account,
            action=action,
            execute_at=plan.execute_at,
            scheduled=plan.scheduled,
            live_ack_required=(
                self.settings.trading_mode == "live"
                and action == WorkAction.REBALANCE
            ),
        )

    def preview_all(self, request_token: str) -> WorkSubmissionReceipt:
        self._require_mutations()
        enabled = [account.account_id for account in self.accounts.registry.accounts]
        return self.work.submit_bulk(
            enabled,
            WorkAction.PREVIEW,
            request_token,
            source=WorkSource.INITIAL,
        )

    def onboarding_confirmations(self) -> list[WorkConfirmation]:
        confirmations: list[WorkConfirmation] = []
        for account in self.accounts.registry.accounts:
            plan = self.work.plan(
                account.account_id,
                WorkAction.REBALANCE,
                source=WorkSource.INITIAL,
            )
            confirmations.append(
                WorkConfirmation(
                    account=plan.account,
                    action=WorkAction.REBALANCE,
                    execute_at=plan.execute_at,
                    scheduled=plan.scheduled,
                    live_ack_required=self.settings.trading_mode == "live",
                )
            )
        return confirmations

    def finish_onboarding(self, request_token: str) -> OnboardingInitializationResult:
        self._require_mutations()
        enabled = self.accounts.registry.accounts
        if not enabled:
            raise ValueError("At least one enabled Account is required to finish onboarding.")
        fingerprint = self.state.current_onboarding_configuration_fingerprint(
            self.settings.trading_mode
        )
        existing = self.state.get_onboarding_initialization(
            self.settings.trading_mode,
            fingerprint,
            [account.account_id for account in enabled],
        )
        if existing is not None:
            return existing
        missing: list[str] = []
        for account in enabled:
            updated = self.state.account_updated_at(account.account_id)
            if updated is None or not self.state.has_successful_preview_since(
                account.account_id, updated
            ):
                missing.append(account.account_id)
        if missing:
            raise ValueError(
                "Every enabled Account needs a successful Preview after its latest configuration save."
            )
        return self.work.finish_onboarding(
            [account.account_id for account in enabled],
            request_token,
            trading_mode=self.settings.trading_mode,
            configuration_fingerprint=fingerprint,
        )

    def _onboarded(self) -> bool:
        accounts = self.state.list_accounts(self.settings.trading_mode)
        return bool(accounts) and all(
            not account.enabled or bool(account.strategy_name) for account in accounts
        )

    def cancel_work(self, work_id: str) -> Optional[WorkItem]:
        self._require_mutations()
        return self.work.cancel(work_id)

    def list_work(self) -> list[WorkItem]:
        return self.state.list_work_items(limit=200)

    def get_work(self, work_id: str) -> Optional[WorkItem]:
        return self.state.get_work_item(work_id)

    def get_work_submission(
        self, submission_id: str
    ) -> Optional[WorkSubmissionReceipt]:
        return self.state.get_work_submission(submission_id)

    def list_runs(
        self,
        *,
        account_id: Optional[str] = None,
        outcome: Optional[RunOutcome] = None,
        source: Optional[RunSource] = None,
        since: Optional[date] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[StoredRun]:
        since_text = f"{since.isoformat()}T00:00:00.000000Z" if since else None
        return self.state.list_runs(
            account_id=account_id,
            outcome=outcome,
            source=source,
            since=since_text,
            limit=limit,
            offset=offset,
        )

    def get_run(self, run_id: str) -> Optional[StoredRun]:
        return self.state.get_run(run_id)

    def stop_accepting(self) -> None:
        self.accepting_mutations = False
        self.work.stop_accepting()
        self.executor.stop_accepting()

    def _require_mutations(self) -> None:
        if not self.accepting_mutations or not self.executor.accepting:
            raise RuntimeError("The application is shutting down or durable state is unavailable.")
