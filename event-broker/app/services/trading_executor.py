"""Worker entry point (PRD §10.3).

`execute_strategy_batch(...)` runs in a process-pool worker. It never raises:
any unexpected exception becomes `INTERNAL_ERROR` results for every account.
`SubprocessManager` and its signal-handler/-exit machinery were removed (WRK-1).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app_config import AppConfig

from ..accounts import AccountConfig
from ..allocations import AllocationsClient
from ..market_clock import MarketClock
from ..rebalance import RunDeps, rebalance_account
from ..results import AccountRunResult, FailureReason, RunOutcome
from ..settings import RuntimeSettings

_ET = ZoneInfo("America/New_York")
_WORKER_FORMAT = "[%(asctime)s] [%(levelname)s] [%(strategy)s %(account_id)s %(run_id)s] %(message)s"

_worker_logger = logging.getLogger("worker")
if not _worker_logger.handlers:  # WRK-2: one module-level stdout handler
    _stream = logging.StreamHandler(sys.stdout)
    _stream.setFormatter(logging.Formatter(_WORKER_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    _worker_logger.addHandler(_stream)


def _connection_factory(account: AccountConfig, config: AppConfig, settings: RuntimeSettings):
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
    account: AccountConfig, mode: str, source: str, exc: BaseException
) -> AccountRunResult:
    """A minimal FAILED/INTERNAL_ERROR result for a worker-level exception."""
    now = datetime.now(_ET)
    message = f"Internal error: {type(exc).__name__}: {exc}"[:300]
    record = {
        "run_id": f"{now:%Y%m%d-%H%M%S}-{account.account_id}",
        "account_id": account.account_id,
        "strategy_name": account.strategy_name,
        "mode": mode, "source": source,
        "started_at": now.isoformat(), "finished_at": now.isoformat(),
        "outcome": RunOutcome.FAILED.value,
        "failure_reason": FailureReason.INTERNAL_ERROR.value,
        "error_message": message,
    }
    return AccountRunResult(
        run_id=record["run_id"],
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
    accounts: list[AccountConfig],
    mode: str,
    source: str,
    config: AppConfig,
    settings: RuntimeSettings,
) -> list[AccountRunResult]:
    """WRK-1/2: one AllocationsClient, one gather over the accounts."""
    clock = MarketClock(config.scheduler.market_open_time)
    async with AllocationsClient(
        settings.allocations_base_url,
        settings.allocations_api_key,
        config.api.allocation_timeout_seconds,
    ) as allocations:
        tasks = []
        for account in accounts:
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
            tasks.append(rebalance_account(account, mode, source, deps))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    finalized: list[AccountRunResult] = []
    for account, res in zip(accounts, results):
        if isinstance(res, Exception):
            _worker_logger.error(
                "worker exception for %s %s: %s", strategy, account.account_id, res
            )
            finalized.append(_internal_error_result(account, mode, source, res))
        else:
            finalized.append(res)
    return finalized


def execute_strategy_batch(
    strategy: str,
    accounts: list[AccountConfig],
    mode: str,
    source: str,
    config: AppConfig,
    settings: RuntimeSettings,
) -> list[dict]:
    """Runs `rebalance_account` for every account and returns `list[dict]`.

    Never raises: anything unexpected produces `INTERNAL_ERROR` results for every
    account. Returns each result via `model_dump()` (it crosses the pool boundary).
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results = loop.run_until_complete(
                _run_batch(strategy, accounts, mode, source, config, settings)
            )
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
        return [r.model_dump() for r in results]
    except BaseException as exc:  # noqa: BLE001 - never raise from the worker
        _worker_logger.error("worker batch failed for %s: %s", strategy, exc)
        return [_internal_error_result(a, mode, source, exc).model_dump() for a in accounts]