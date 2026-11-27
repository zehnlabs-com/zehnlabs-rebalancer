"""Complete-set Account validation, persistence, discovery, and Signal reconciliation."""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Optional, Protocol, Sequence

from app_config import AppConfig
from pydantic import BaseModel, ConfigDict, Field
from rebalance_calculator import (
    REPLACEMENT_SETS,
    Targets,
    apply_replacements,
    validate_and_scale,
)

from ibkr_connector import AccountDiscoveryError, discover_managed_accounts

from ..accounts import (
    AccountConfig,
    AccountRegistry,
    export_accounts_yaml,
    parse_accounts_yaml,
    validate_account_set,
)
from ..allocations import (
    AllocationsAuthenticationError,
    AllocationsClient,
    AllocationsUnavailable,
    StrategyCatalogEntry,
)
from ..settings import RuntimeSettings
from ..state.sqlite import SQLiteState


_RECONCILIATION_DEGRADED_ERROR = (
    "Signal subscriptions are unavailable. The service will retry automatically."
)


class ReplacementSetInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    rules: int


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accounts: tuple[str, ...]
    strategies: tuple[StrategyCatalogEntry, ...]
    replacement_sets: tuple[ReplacementSetInfo, ...]


class AccountSaveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accounts: tuple[AccountConfig, ...]
    degraded_error: Optional[str] = None


class SubscriberStatus(BaseModel):
    """Safe, dashboard-facing state reported by the Signal subscriber."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    running: bool
    ably_connected: bool
    strategies_count: int
    strategies: tuple[str, ...]
    degraded_error: Optional[str] = None


class ReadinessState(StrEnum):
    MISSING = "missing"
    UNVERIFIED = "unverified"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    DEGRADED = "degraded"


class ExternalReadiness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReadinessState
    message: str


class ReadinessError(ValueError):
    """Safe operational readiness error suitable for dashboard rendering."""


class SignalSubscriber(Protocol):
    async def reconcile(
        self, strategies: dict[str, list[AccountConfig]]
    ) -> None: ...

    async def get_status(self) -> SubscriberStatus: ...


class AccountManager:
    """Owns the boundary between submitted Account forms and committed state."""

    def __init__(
        self,
        *,
        config: AppConfig,
        settings: RuntimeSettings,
        state: SQLiteState,
        registry: AccountRegistry,
        subscriber: SignalSubscriber,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.settings = settings
        self.state = state
        self.registry = registry
        self.subscriber = subscriber
        self.logger = logger or logging.getLogger(__name__)
        self.degraded_error: Optional[str] = None
        self._reconcile_task: Optional[asyncio.Task[None]] = None
        self._reconcile_generation = 0
        self._reconcile_waiters: list[tuple[int, asyncio.Future[bool]]] = []
        self._stopping = False
        self._discovered_accounts: set[str] = set()
        self.gateway_readiness = self._initial_gateway_readiness()
        self.zehnlabs_readiness = self._initial_zehnlabs_readiness()

    def load_committed(self) -> None:
        self.registry.refresh(
            self.state.list_accounts(self.settings.trading_mode),
            self.settings.trading_mode,
        )

    async def start(self) -> None:
        self.load_committed()
        if self.registry.accounts:
            await self._reconcile_or_degrade()

    async def stop(self) -> None:
        self._stopping = True
        task, self._reconcile_task = self._reconcile_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for _generation, waiter in self._reconcile_waiters:
            if not waiter.done():
                waiter.set_result(False)
        self._reconcile_waiters.clear()

    async def discover(self) -> DiscoveryResult:
        port = self.settings.ib_port or (
            self.config.ibkr.ports.live_internal
            if self.settings.trading_mode == "live"
            else self.config.ibkr.ports.paper_internal
        )
        if not self.settings.ib_host.strip():
            self.gateway_readiness = ExternalReadiness(
                state=ReadinessState.MISSING,
                message="IB_HOST is missing.",
            )
            raise ReadinessError(self.gateway_readiness.message)
        try:
            accounts = await discover_managed_accounts(
                host=self.settings.ib_host,
                port=port,
                timeout_seconds=self.config.ibkr.connection_timeout_seconds,
            )
        except AccountDiscoveryError:
            self.gateway_readiness = ExternalReadiness(
                state=ReadinessState.UNAVAILABLE,
                message="IBKR Gateway is unreachable or unauthenticated.",
            )
            raise ReadinessError(self.gateway_readiness.message) from None
        self.gateway_readiness = ExternalReadiness(
            state=ReadinessState.READY,
            message="IBKR Gateway connectivity and authentication are verified.",
        )
        if not self.settings.zehnlabs_api_key.strip():
            self.zehnlabs_readiness = ExternalReadiness(
                state=ReadinessState.MISSING,
                message="ZEHNLABS_API_KEY is missing.",
            )
            raise ReadinessError(self.zehnlabs_readiness.message)
        try:
            async with AllocationsClient(
                self.settings.allocations_base_url,
                self.settings.zehnlabs_api_key,
                self.config.api.allocation_timeout_seconds,
            ) as client:
                strategies = await client.fetch_catalog()
        except AllocationsAuthenticationError:
            self.zehnlabs_readiness = ExternalReadiness(
                state=ReadinessState.REJECTED,
                message="Zehnlabs rejected the configured API key.",
            )
            raise ReadinessError(self.zehnlabs_readiness.message) from None
        except AllocationsUnavailable:
            self.zehnlabs_readiness = ExternalReadiness(
                state=ReadinessState.UNAVAILABLE,
                message="Zehnlabs connectivity could not be verified.",
            )
            raise ReadinessError(self.zehnlabs_readiness.message) from None
        self.zehnlabs_readiness = ExternalReadiness(
            state=ReadinessState.READY,
            message="Zehnlabs API-key access is verified.",
        )
        replacements = tuple(
            ReplacementSetInfo(name=name, rules=len(rules))
            for name, rules in sorted(REPLACEMENT_SETS.items())
        )
        self._discovered_accounts = set(accounts.accounts)
        return DiscoveryResult(
            accounts=accounts.accounts,
            strategies=tuple(strategies),
            replacement_sets=replacements,
        )

    async def save(self, accounts: Sequence[AccountConfig]) -> AccountSaveResult:
        candidate = list(accounts)
        validate_account_set(candidate, expected_mode=self.settings.trading_mode)
        existing_ids = {
            account.account_id
            for account in self.state.list_accounts(self.settings.trading_mode)
        }
        undiscovered = sorted(
            account.account_id
            for account in candidate
            if account.account_id not in existing_ids
            and account.account_id not in self._discovered_accounts
        )
        if undiscovered:
            raise ValueError(
                "Discover managed IBKR Accounts before adding: " + ", ".join(undiscovered)
            )
        await self._verify_allocations(candidate)
        self.state.replace_accounts(self.settings.trading_mode, candidate)
        self.load_committed()
        await self._reconcile_or_degrade()
        return AccountSaveResult(
            accounts=tuple(candidate), degraded_error=self.degraded_error
        )

    async def import_yaml(self, document: str) -> AccountSaveResult:
        accounts = parse_accounts_yaml(document)
        validate_account_set(accounts)
        await self._verify_allocations(accounts)
        self.state.replace_all_accounts(accounts)
        self.load_committed()
        await self._reconcile_or_degrade()
        return AccountSaveResult(accounts=tuple(accounts), degraded_error=self.degraded_error)

    def export_yaml(self) -> str:
        return export_accounts_yaml(self.state.list_accounts())

    async def subscriber_status(self) -> SubscriberStatus:
        return await self.subscriber.get_status()

    async def _verify_allocations(self, accounts: Sequence[AccountConfig]) -> None:
        if accounts and not self.settings.zehnlabs_api_key.strip():
            self.zehnlabs_readiness = ExternalReadiness(
                state=ReadinessState.MISSING,
                message="ZEHNLABS_API_KEY is missing.",
            )
            raise ValueError("Cannot verify Strategies because ZEHNLABS_API_KEY is not configured.")
        selected = sorted({account.strategy_name for account in accounts})
        fetched: dict[str, Targets] = {}
        if selected:
            async with AllocationsClient(
                self.settings.allocations_base_url,
                self.settings.zehnlabs_api_key,
                self.config.api.allocation_timeout_seconds,
            ) as client:
                for strategy in selected:
                    try:
                        fetched[strategy] = validate_and_scale(await client.fetch(strategy))
                    except AllocationsAuthenticationError:
                        self.zehnlabs_readiness = ExternalReadiness(
                            state=ReadinessState.REJECTED,
                            message="Zehnlabs rejected the configured API key.",
                        )
                        raise ValueError(
                            f"Strategy '{strategy}' could not be validated because the Zehnlabs key was rejected."
                        ) from None
                    except AllocationsUnavailable:
                        self.zehnlabs_readiness = ExternalReadiness(
                            state=ReadinessState.UNAVAILABLE,
                            message="Zehnlabs connectivity could not be verified.",
                        )
                        raise ValueError(
                            f"Strategy '{strategy}' could not be validated."
                        ) from None
                    except Exception as exc:
                        raise ValueError(f"Strategy '{strategy}' could not be validated.") from exc
        if selected:
            self.zehnlabs_readiness = ExternalReadiness(
                state=ReadinessState.READY,
                message="Zehnlabs API-key access is verified.",
            )
        for account in accounts:
            targets = fetched[account.strategy_name]
            if account.replacement_set is not None:
                try:
                    apply_replacements(
                        targets.weights, REPLACEMENT_SETS[account.replacement_set]
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Strategy '{account.strategy_name}' is invalid with Replacement Set "
                        f"'{account.replacement_set}'."
                    ) from exc

    def _desired(self) -> dict[str, list[AccountConfig]]:
        return {
            strategy: self.registry.for_strategy(strategy)
            for strategy in self.registry.strategies()
        }

    async def _reconcile_or_degrade(self) -> None:
        if self._stopping:
            return
        self._reconcile_generation += 1
        generation = self._reconcile_generation
        waiter = asyncio.get_running_loop().create_future()
        self._reconcile_waiters.append((generation, waiter))
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        await waiter

    async def _reconcile_loop(self) -> None:
        try:
            while not self._stopping:
                generation = self._reconcile_generation
                try:
                    self.load_committed()
                    await self.subscriber.reconcile(self._desired())
                    self.degraded_error = None
                    succeeded = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.degraded_error = _RECONCILIATION_DEGRADED_ERROR
                    self.logger.warning(
                        "Signal subscription still degraded: %s", self.degraded_error
                    )
                    succeeded = False
                self._resolve_reconcile_waiters(generation, succeeded)
                if succeeded:
                    if self._reconcile_generation == generation:
                        return
                    continue
                await asyncio.sleep(self.config.service.error_recovery_delay_seconds)
        finally:
            waiters, self._reconcile_waiters = self._reconcile_waiters, []
            for _generation, waiter in waiters:
                if not waiter.done():
                    waiter.set_result(False)
            if self._reconcile_task is asyncio.current_task():
                self._reconcile_task = None

    def _resolve_reconcile_waiters(self, generation: int, succeeded: bool) -> None:
        pending: list[tuple[int, asyncio.Future[bool]]] = []
        for requested, waiter in self._reconcile_waiters:
            if requested <= generation:
                if not waiter.done():
                    waiter.set_result(succeeded)
            else:
                pending.append((requested, waiter))
        self._reconcile_waiters = pending

    def _initial_gateway_readiness(self) -> ExternalReadiness:
        if not self.settings.ib_host.strip():
            return ExternalReadiness(
                state=ReadinessState.MISSING,
                message="IB_HOST is missing.",
            )
        return ExternalReadiness(
            state=ReadinessState.UNVERIFIED,
            message="Gateway connectivity and authentication have not been verified.",
        )

    def _initial_zehnlabs_readiness(self) -> ExternalReadiness:
        if not self.settings.zehnlabs_api_key.strip():
            return ExternalReadiness(
                state=ReadinessState.MISSING,
                message="ZEHNLABS_API_KEY is missing.",
            )
        return ExternalReadiness(
            state=ReadinessState.UNVERIFIED,
            message="Zehnlabs API-key access has not been verified.",
        )
