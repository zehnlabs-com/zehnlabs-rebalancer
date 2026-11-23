"""Ably service: strategy-level subscription and signal routing (PRD §10.6)."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from ably import AblyRealtime
from app_config import DEFAULT_ALLOCATIONS_BASE_URL, get_config

from ..accounts import AccountConfig


def _mode_for_exec(exec_command: Optional[str]) -> Optional[str]:
    """SIG-2: `rebalance` -> live, `print-rebalance` -> preview; anything else drops."""
    return {"rebalance": "live", "print-rebalance": "preview"}.get(exec_command)


class AblyEventSubscriber:
    """Subscribes to Ably channels and routes signals to the strategy executor."""

    def __init__(
        self,
        strategy_executor=None,
        logger: Optional[logging.Logger] = None,
        strategies: Optional[Dict[str, List[AccountConfig]]] = None,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.strategy_executor = strategy_executor
        self.strategies: Dict[str, List[AccountConfig]] = strategies or {}
        self.ably: Optional[AblyRealtime] = None
        self.config = get_config()
        self.zehnlabs_api_key = os.getenv("ZEHNLABS_API_KEY")
        self.allocations_base_url = os.getenv("ALLOCATIONS_BASE_URL", DEFAULT_ALLOCATIONS_BASE_URL)
        self.api_key: Optional[str] = None
        self._tasks: set[asyncio.Task] = set()  # SIG-1: keep a ref to every message task

    async def start(self):
        """Fetch the shared read-only key once, then subscribe per strategy."""
        self.api_key = await self._fetch_ably_key()

        if not self.strategies:
            self.logger.warning("No strategies configured for event subscription")
            return

        await self._subscribe_to_strategies()
        self.logger.info("Started Ably Event Subscriber for %d strategies", len(self.strategies))

    # ---------------- security: key fetch (unchanged behaviour) --------------- #
    def _rebalance_auth_url(self) -> str:
        """The allocation base ends in /strategies; authentication uses its sibling route."""
        base = self.allocations_base_url
        try:
            parts = urlsplit(base)
            path = parts.path.rstrip("/")
            if (not base or any(char.isspace() for char in base) or
                parts.scheme not in ("https", "http") or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or not path.endswith("/strategies")):
                raise ValueError
            parts.port  # Validate an explicitly configured port.
        except ValueError:
            raise ValueError(
                "ALLOCATIONS_BASE_URL must be an HTTP(S) URL ending in /strategies, "
                "without credentials, query parameters, or a fragment"
            ) from None
        return urlunsplit((parts.scheme, parts.netloc, path[: -len("/strategies")] + "/rebalance-auth", "", ""))

    async def _fetch_ably_key(self) -> str:
        """Fetch once per process startup; SDK reconnects retain the returned app key."""
        if not self.zehnlabs_api_key or not self.zehnlabs_api_key.strip():
            raise ValueError("ZEHNLABS_API_KEY is required. Obtain your Zehnlabs key with /api in Telegram.")

        url = self._rebalance_auth_url()
        timeout = aiohttp.ClientTimeout(total=self.config.api.rebalance_auth_timeout_seconds)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"X-API-Key": self.zehnlabs_api_key},
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    if response.status == 401:
                        raise RuntimeError("Zehnlabs API key rejected. Check ZEHNLABS_API_KEY; use /api regen if lost.")
                    if response.status == 403:
                        raise RuntimeError("Zehnlabs API access is not eligible. Check your subscription with /me in Telegram.")
                    if response.status != 200:
                        raise RuntimeError(f"Zehnlabs realtime startup failed (HTTP {response.status}). Try again later.")
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise RuntimeError("Unable to retrieve the Ably key from Zehnlabs. Check API availability and try again.") from None

        key = data.get("ablyApiKey") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("status") != "success" or
            not isinstance(key, str) or len(key.split(":")) != 2 or
            not all(key.split(":")) or any(char.isspace() for char in key)):
            raise RuntimeError("Zehnlabs returned an invalid realtime authentication response.")
        return key

    # ---------------- subscription ---------------- #
    async def _subscribe_to_strategies(self):
        self.ably = AblyRealtime(self.api_key)
        self._setup_connection_monitoring()

        for strategy_name in self.strategies:
            channel = self.ably.channels.get(strategy_name)

            def create_handler(strat_name):
                def handler(message, *args, **kwargs):
                    # SIG-1: keep a reference to the task and drop it when done.
                    task = asyncio.create_task(self._handle_strategy_event(strat_name, message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                return handler

            await channel.subscribe(create_handler(strategy_name))
            self.logger.info("Subscribed to strategy '%s' with %d accounts",
                             strategy_name, len(self.strategies[strategy_name]))

    async def _handle_strategy_event(self, strategy_name: str, message) -> None:
        """SIG-1/2: parse the event, map exec -> mode, route to the executor."""
        try:
            event_data = message.data if isinstance(message.data, dict) else json.loads(message.data)
            exec_command = event_data.get("exec")
            if not exec_command:
                self.logger.error("No 'exec' command in event data: %s", event_data)
                return
            mode = _mode_for_exec(exec_command)
            if mode is None:
                self.logger.error("Unknown exec command '%s'; dropping", exec_command)
                return
            accounts = self.strategies.get(strategy_name, [])
            if not accounts:
                self.logger.warning("No accounts for strategy '%s'; dropping signal", strategy_name)
                return
            if self.strategy_executor:
                await self.strategy_executor.execute(strategy_name, accounts, mode, "signal")
            else:
                self.logger.error("No strategy executor available")
        except Exception as e:  # noqa: BLE001 - a signal-handling error is logged, not fatal
            self.logger.error("Failed to handle event for strategy %s: %s", strategy_name, e)

    def _setup_connection_monitoring(self):
        def on_connected(state_change, *args, **kwargs):
            self.logger.info("Ably connection established")

        def on_failed(state_change, *args, **kwargs):
            self.logger.error("Ably connection failed")

        def on_disconnected(state_change, *args, **kwargs):
            self.logger.warning("Ably connection lost")

        def on_suspended(state_change, *args, **kwargs):
            self.logger.warning("Ably connection suspended")

        self.ably.connection.on("connected", on_connected)
        self.ably.connection.on("failed", on_failed)
        self.ably.connection.on("disconnected", on_disconnected)
        self.ably.connection.on("suspended", on_suspended)

    async def stop(self):
        if self.ably:
            try:
                for strategy_name in self.strategies:
                    channel = self.ably.channels.get(strategy_name)
                    channel.unsubscribe()
                    self.logger.info("Unsubscribed from strategy '%s'", strategy_name)
                await self.ably.close()
                self.logger.info("Closed Ably connection")
            except Exception as e:  # noqa: BLE001
                self.logger.error("Error stopping Ably service: %s", e)

    async def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.ably is not None,
            "strategies_count": len(self.strategies),
            "total_accounts": sum(len(accounts) for accounts in self.strategies.values()),
            "ably_connected": self.ably.connection.state == "connected" if self.ably else False,
            "strategies": {name: len(accs) for name, accs in self.strategies.items()},
        }