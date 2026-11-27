"""Dynamic Ably subscription and Signal routing."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from ably import AblyRealtime
from app_config import AppConfig

from ..accounts import AccountConfig
from ..results import RunMode, RunSource
from ..settings import RuntimeSettings
from .account_manager import SubscriberStatus
from .strategy_executor import ExecutionBatch


class SignalExecutor(Protocol):
    async def execute(
        self,
        strategy: str,
        accounts: Sequence[AccountConfig],
        mode: RunMode,
        source: RunSource,
    ) -> ExecutionBatch: ...


def _mode_for_exec(exec_command: Optional[str]) -> Optional[RunMode]:
    return {
        "rebalance": RunMode.LIVE,
        "print-rebalance": RunMode.PREVIEW,
    }.get(exec_command)


class AblyEventSubscriber:
    """Keeps the actual channel set equal to the committed Account registry."""

    def __init__(
        self,
        *,
        config: AppConfig,
        settings: RuntimeSettings,
        strategy_executor: SignalExecutor,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.settings = settings
        self.strategy_executor = strategy_executor
        self.logger = logger or logging.getLogger(__name__)
        self.ably: Optional[AblyRealtime] = None
        self.api_key: Optional[str] = None
        self.strategies: dict[str, list[AccountConfig]] = {}
        self.actual_channels: set[str] = set()
        self.last_error: Optional[str] = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    async def start(self, strategies: dict[str, list[AccountConfig]]) -> None:
        await self.reconcile(strategies)

    async def reconcile(self, strategies: dict[str, list[AccountConfig]]) -> None:
        """Subscribe/unsubscribe to match a fresh committed registry snapshot."""
        if self._stopping:
            return
        desired = {name: list(accounts) for name, accounts in strategies.items() if accounts}
        self.strategies = desired
        if not desired and self.ably is None:
            self.last_error = None
            return
        try:
            if self.ably is None:
                self.api_key = await self._fetch_ably_key()
                self.ably = AblyRealtime(self.api_key)
                self._setup_connection_monitoring()
            for name in sorted(self.actual_channels - set(desired)):
                channel = self.ably.channels.get(name)
                maybe_awaitable = channel.unsubscribe()
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable
                self.actual_channels.discard(name)
                self.logger.info("Unsubscribed from Strategy '%s'", name)
            for name in sorted(set(desired) - self.actual_channels):
                channel = self.ably.channels.get(name)
                await channel.subscribe(self._handler(name))
                self.actual_channels.add(name)
                self.logger.info("Subscribed to Strategy '%s'", name)
            self.last_error = None
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            self.logger.error("Ably reconciliation failed: %s", self.last_error)
            raise

    @staticmethod
    def _safe_error(_exc: BaseException) -> str:
        return "Signal subscriptions are unavailable. The service will retry automatically."

    def _handler(self, strategy_name: str):
        def handler(message: Any, *args: Any, **kwargs: Any) -> None:
            task = asyncio.create_task(self._handle_strategy_event(strategy_name, message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return handler

    def _rebalance_auth_url(self) -> str:
        base = self.settings.allocations_base_url
        try:
            parts = urlsplit(base)
            path = parts.path.rstrip("/")
            if (
                not base
                or any(char.isspace() for char in base)
                or parts.scheme not in ("https", "http")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or not path.endswith("/strategies")
            ):
                raise ValueError
            parts.port
        except ValueError:
            raise ValueError(
                "ALLOCATIONS_BASE_URL must be an HTTP(S) URL ending in /strategies, "
                "without credentials, query parameters, or a fragment"
            ) from None
        return urlunsplit(
            (parts.scheme, parts.netloc, path[: -len("/strategies")] + "/rebalance-auth", "", "")
        )

    async def _fetch_ably_key(self) -> str:
        key = self.settings.zehnlabs_api_key or ""
        if not key.strip():
            raise RuntimeError("ZEHNLABS_API_KEY is not configured.")
        timeout = aiohttp.ClientTimeout(total=self.config.api.rebalance_auth_timeout_seconds)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._rebalance_auth_url(),
                    headers={"X-API-Key": key},
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    if response.status == 401:
                        raise RuntimeError("Zehnlabs API key rejected. Check ZEHNLABS_API_KEY.")
                    if response.status == 403:
                        raise RuntimeError("Zehnlabs API access is not eligible.")
                    if response.status != 200:
                        raise RuntimeError(
                            f"Zehnlabs realtime startup failed (HTTP {response.status})."
                        )
                    payload = await response.json()
        except RuntimeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise RuntimeError("Unable to retrieve the Ably key from Zehnlabs.") from None
        result = payload.get("ablyApiKey") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "success"
            or not isinstance(result, str)
            or len(result.split(":")) != 2
            or not all(result.split(":"))
            or any(char.isspace() for char in result)
        ):
            raise RuntimeError("Zehnlabs returned an invalid realtime authentication response.")
        return result

    async def _handle_strategy_event(self, strategy_name: str, message: Any) -> None:
        try:
            payload = message.data if isinstance(message.data, dict) else json.loads(message.data)
            command = payload.get("exec") if isinstance(payload, dict) else None
            mode = _mode_for_exec(command)
            if mode is None:
                self.logger.error("Unknown or missing Signal exec command; dropping")
                return
            accounts: Sequence[AccountConfig] = self.strategies.get(strategy_name, ())
            if not accounts:
                self.logger.warning("No Accounts for Strategy '%s'; dropping Signal", strategy_name)
                return
            await self.strategy_executor.execute(
                strategy_name,
                list(accounts),
                mode,
                RunSource.SIGNAL,
            )
        except Exception as exc:
            self.logger.error("Failed to handle Signal for Strategy %s: %s", strategy_name, exc)

    def _setup_connection_monitoring(self) -> None:
        assert self.ably is not None
        self.ably.connection.on("connected", lambda *_a, **_k: self.logger.info("Ably connected"))
        self.ably.connection.on(
            "failed", lambda *_a, **_k: self.logger.error("Ably connection failed")
        )
        self.ably.connection.on(
            "disconnected", lambda *_a, **_k: self.logger.warning("Ably disconnected")
        )
        self.ably.connection.on(
            "suspended", lambda *_a, **_k: self.logger.warning("Ably connection suspended")
        )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        client, self.ably = self.ably, None
        if client is not None:
            for name in sorted(self.actual_channels):
                try:
                    maybe_awaitable = client.channels.get(name).unsubscribe()
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
                except Exception as exc:
                    self.logger.warning("Could not unsubscribe from %s: %s", name, exc)
            self.actual_channels.clear()
            try:
                await client.close()
            except Exception as exc:
                self.logger.warning("Could not close Ably: %s", exc)
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def get_status(self) -> SubscriberStatus:
        return SubscriberStatus(
            running=self.ably is not None,
            ably_connected=(
                self.ably.connection.state == "connected" if self.ably else False
            ),
            strategies_count=len(self.actual_channels),
            strategies=tuple(sorted(self.actual_channels)),
            degraded_error=self.last_error,
        )
