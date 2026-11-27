"""Worker entry point (PRD §10.3).

`execute_strategy_batch(...)` runs in a process-pool worker. It never raises:
any unexpected exception becomes `INTERNAL_ERROR` results for every account.
The old per-worker signal/machinery layer and its `sys.exit` are gone (WRK-1).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from app_config import AppConfig

from ..accounts import AccountConfig
from ..allocations import AllocationsClient
from ..market_clock import MarketClock
from ..rebalance import RunDeps, rebalance_account
from ..results import (
    AccountRunResult,
    FailureReason,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
)
from ..settings import WorkerSettings

_WORKER_FORMAT = "[%(asctime)s] [%(levelname)s] [%(strategy)s %(account_id)s %(run_id)s] %(message)s"

# Bare `_worker_logger` calls (worker exceptions, IBKR close warnings) have no
# strategy/account_id/run_id injected, so give the formatter defaults instead of
# letting `%(run_id)s` raise "Formatting field not found" and drop the line (H1).
_WORKER_LOG_DEFAULTS = {"strategy": "-", "account_id": "-", "run_id": "-"}


class _WorkerFormatter(logging.Formatter):
    """Worker formatter that fills missing context fields with defaults (H1)."""

    def format(self, record: logging.LogRecord) -> str:
        for key, value in _WORKER_LOG_DEFAULTS.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return super().format(record)


_worker_logger = logging.getLogger("worker")
if not _worker_logger.handlers:  # WRK-2: one module-level stdout handler
    _stream = logging.StreamHandler(sys.stdout)
    _stream.setFormatter(_WorkerFormatter(_WORKER_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    _worker_logger.addHandler(_stream)
# The forked worker inherits the parent's root handler (main.py basicConfig);
# without this every worker line would be printed twice.
_worker_logger.propagate = False


@dataclass(frozen=True)
class WorkerRunRequest:
    """The complete immutable per-Account input accepted by a worker."""

    account: AccountConfig
    run_id: str
    mode: RunMode
    source: RunSource


def _connection_factory(account: AccountConfig, config: AppConfig, settings: WorkerSettings):
    """RUN-4: the single place where `account.broker` maps to a connection class."""
    from ibkr_connector import IBKRConnection  # lazy: keeps the worker import light

    if account.broker != "ibkr":
        raise ValueError(f"Unsupported broker: {account.broker}")
    return IBKRConnection(
        account_id=account.account_id,
        client_id=account.client_id,
        ibkr_config=config.ibkr,
        settings=settings,
        logger=_worker_logger,
    )


def _internal_error_result(
    request: WorkerRunRequest,
    mode: RunMode,
    source: RunSource,
    exc: BaseException,
) -> AccountRunResult:
    """A minimal FAILED/INTERNAL_ERROR result for a worker-level exception."""
    account = request.account
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    message = f"Internal error: {type(exc).__name__}: {exc}"[:300]
    record = RunRecord(
        run_id=request.run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=mode,
        source=source,
        started_at=now,
        finished_at=now,
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.INTERNAL_ERROR,
        error_message=message,
        replacement_set=account.replacement_set,
    )
    return AccountRunResult(
        run_id=request.run_id,
        account_id=account.account_id,
        strategy_name=account.strategy_name,
        mode=mode,
        source=source,
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.INTERNAL_ERROR,
        error_message=message,
        record=record,
    )


async def _run_batch(
    strategy: str,
    requests: list[WorkerRunRequest],
    config: AppConfig,
    settings: WorkerSettings,
) -> list[AccountRunResult]:
    """WRK-1/2: one AllocationsClient, one gather over the accounts."""
    clock = MarketClock(config.scheduler.market_open_time)
    async with AllocationsClient(
        settings.allocations_base_url,
        settings.zehnlabs_api_key,
        config.api.allocation_timeout_seconds,
    ) as allocations:
        tasks = []
        for request in requests:
            account = request.account
            adapter = logging.LoggerAdapter(
                _worker_logger,
                {"strategy": strategy, "account_id": account.account_id},
            )
            deps = RunDeps(
                app_config=config,
                settings=settings,
                allocations=allocations,
                connection_factory=lambda acc: _connection_factory(acc, config, settings),
                clock=clock,
                logger=adapter,
            )
            tasks.append(
                rebalance_account(
                    account,
                    request.mode,
                    request.source,
                    deps,
                    run_id=request.run_id,
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)

    finalized: list[AccountRunResult] = []
    for request, res in zip(requests, results):
        if isinstance(res, Exception):
            account = request.account
            _worker_logger.error(
                "worker exception for %s %s: %s", strategy, account.account_id, res
            )
            finalized.append(_internal_error_result(request, request.mode, request.source, res))
        else:
            finalized.append(res)
    return finalized


def execute_strategy_batch(
    strategy: str,
    requests: list[WorkerRunRequest],
    config: AppConfig,
    settings: WorkerSettings,
) -> list[AccountRunResult]:
    """Run ``rebalance_account`` for every Account in the Strategy batch.

    Never raises: anything unexpected produces `INTERNAL_ERROR` results for every
    Account. The typed results are pickled directly across the process-pool boundary.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(
                _run_batch(strategy, requests, config, settings)
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        return results
    except BaseException as exc:  # noqa: BLE001 - never raise from the worker
        _worker_logger.error("worker batch failed for %s: %s", strategy, exc)
        return [
            _internal_error_result(request, request.mode, request.source, exc)
            for request in requests
        ]
