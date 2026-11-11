"""Simplified Ably service for strategy-level subscription"""

import os
import json
import yaml
import asyncio
import logging
import aiohttp
from urllib.parse import urlsplit, urlunsplit
from typing import Dict, List, Optional, Any
from ably import AblyRealtime
from app_config import DEFAULT_ALLOCATIONS_BASE_URL, get_config

class AblyEventSubscriber:
    """Subscribes to Ably channels and routes to strategy executor"""

    def __init__(self, strategy_executor=None, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.strategy_executor = strategy_executor
        self.strategies = {}  # strategy_name -> List[accounts]
        self.ably = None
        self.config = get_config()
        self.zehnlabs_api_key = os.getenv('ALLOCATIONS_API_KEY')
        self.allocations_base_url = os.getenv('ALLOCATIONS_BASE_URL', DEFAULT_ALLOCATIONS_BASE_URL)
        self.api_key: Optional[str] = None

    async def start(self):
        """Start Ably subscription"""

        self.api_key = await self._fetch_ably_key()

        # Load and group accounts by strategy
        self._load_accounts()

        if not self.strategies:
            self.logger.warning("No strategies configured for event subscription")
            return

        # Subscribe once per strategy
        await self._subscribe_to_strategies()

        self.logger.info(f"Started Ably Event Subscriber for {len(self.strategies)} strategies")

    def _rebalance_auth_url(self) -> str:
        """The allocation base ends in /strategies; authentication uses its sibling route."""
        base = self.allocations_base_url
        try:
            parts = urlsplit(base)
            path = parts.path.rstrip('/')
            if (not base or any(char.isspace() for char in base) or
                parts.scheme not in ('https', 'http') or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or not path.endswith('/strategies')):
                raise ValueError
            parts.port  # Validate an explicitly configured port.
        except ValueError:
            raise ValueError(
                "ALLOCATIONS_BASE_URL must be an HTTP(S) URL ending in /strategies, "
                "without credentials, query parameters, or a fragment"
            ) from None
        return urlunsplit((parts.scheme, parts.netloc, path[:-len('/strategies')] + '/rebalance-auth', '', ''))

    async def _fetch_ably_key(self) -> str:
        """Fetch once per process startup; SDK reconnects retain the returned app key."""
        if not self.zehnlabs_api_key or not self.zehnlabs_api_key.strip():
            raise ValueError("ALLOCATIONS_API_KEY is required. Obtain your Zehnlabs key with /api in Telegram.")

        url = self._rebalance_auth_url()
        timeout = aiohttp.ClientTimeout(total=self.config.api.rebalance_auth_timeout_seconds)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={'X-API-Key': self.zehnlabs_api_key},
                    timeout=timeout,
                    allow_redirects=False
                ) as response:
                    if response.status == 401:
                        raise RuntimeError("Zehnlabs API key rejected. Check ALLOCATIONS_API_KEY; use /api regen if lost.")
                    if response.status == 403:
                        raise RuntimeError("Zehnlabs API access is not eligible. Check your subscription with /me in Telegram.")
                    if response.status != 200:
                        raise RuntimeError(f"Zehnlabs realtime startup failed (HTTP {response.status}). Try again later.")
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # Response bodies and transport errors can contain credentials; keep them out of logs.
            raise RuntimeError("Unable to retrieve the Ably key from Zehnlabs. Check API availability and try again.") from None

        key = data.get('ablyApiKey') if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get('status') != 'success' or
            not isinstance(key, str) or len(key.split(':')) != 2 or
            not all(key.split(':')) or any(char.isspace() for char in key)):
            raise RuntimeError("Zehnlabs returned an invalid realtime authentication response.")
        return key

    def _load_accounts(self):
        """Load accounts.yaml and group by strategy"""

        try:
            accounts_file = '/app/accounts.yaml'
            with open(accounts_file, 'r') as f:
                config = yaml.safe_load(f)

            trading_mode = os.getenv('TRADING_MODE', 'paper')
            self.logger.info(f"Loading accounts for trading mode: {trading_mode}")

            for account in config.get('accounts', []):
                if (account.get('type') == trading_mode and
                    account.get('enabled', True) and
                    account.get('strategy_name')):

                    strategy = account['strategy_name']
                    if strategy not in self.strategies:
                        self.strategies[strategy] = []
                    self.strategies[strategy].append(account)

            total_accounts = sum(len(accounts) for accounts in self.strategies.values())
            self.logger.info(f"Loaded {len(self.strategies)} strategies with {total_accounts} accounts")

            # Log strategy breakdown
            for strategy, accounts in self.strategies.items():
                account_ids = [acc['account_id'] for acc in accounts]
                self.logger.info(f"Strategy '{strategy}': {len(accounts)} accounts ({', '.join(account_ids)})")

        except Exception as e:
            self.logger.error(f"Failed to load accounts: {e}")
            raise

    async def _subscribe_to_strategies(self):
        """Subscribe to Ably channel for each strategy"""

        self.ably = AblyRealtime(self.api_key)

        # Set up connection monitoring
        self._setup_connection_monitoring()

        for strategy_name, accounts in self.strategies.items():
            channel = self.ably.channels.get(strategy_name)

            # Create handler for this strategy
            def create_handler(strat_name, strat_accounts):
                def handler(message, *args, **kwargs):
                    asyncio.create_task(self._handle_strategy_event(strat_name, strat_accounts, message))
                return handler

            handler = create_handler(strategy_name, accounts)
            await channel.subscribe(handler)

            self.logger.info(f"Subscribed to strategy '{strategy_name}' with {len(accounts)} accounts")

    async def _handle_strategy_event(self, strategy_name: str, accounts: List[dict], message):
        """Handle incoming strategy event"""

        try:
            self.logger.info(f"Received event for strategy '{strategy_name}': {message.data}")

            # Parse message
            event_data = message.data if isinstance(message.data, dict) else json.loads(message.data)

            # Validate event has required fields
            exec_command = event_data.get('exec')
            if not exec_command:
                self.logger.error(f"No 'exec' command in event data: {event_data}")
                return

            # Execute strategy using strategy executor
            if self.strategy_executor:
                result = await self.strategy_executor.execute_strategy(
                    strategy_name,
                    accounts,
                    event_data
                )

                # Log results summary
                if result.get('status') == 'already_running':
                    self.logger.info(f"Strategy {strategy_name} already running, skipped")
                elif result.get('status') == 'failed':
                    self.logger.error(f"Strategy {strategy_name} failed completely: {result.get('error')}")
                else:
                    results = result.get('results', [])
                    successful = sum(1 for r in results if r.get('success', False))
                    self.logger.info(f"Strategy {strategy_name}: {successful}/{len(accounts)} accounts successful")
            else:
                self.logger.error("No strategy executor available")

        except Exception as e:
            self.logger.error(f"Failed to handle event for strategy {strategy_name}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")

    def _setup_connection_monitoring(self):
        """Set up Ably connection state monitoring"""

        def on_connected(state_change, *args, **kwargs):
            self.logger.info("Ably connection established")

        def on_failed(state_change, *args, **kwargs):
            self.logger.error("Ably connection failed")

        def on_disconnected(state_change, *args, **kwargs):
            self.logger.warning("Ably connection lost")

        def on_suspended(state_change, *args, **kwargs):
            self.logger.warning("Ably connection suspended")

        # Set up connection event handlers
        self.ably.connection.on('connected', on_connected)
        self.ably.connection.on('failed', on_failed)
        self.ably.connection.on('disconnected', on_disconnected)
        self.ably.connection.on('suspended', on_suspended)

    async def stop(self):
        """Stop the Ably service and clean up resources"""

        if self.ably:
            try:
                # Unsubscribe from all channels
                for strategy_name in self.strategies.keys():
                    channel = self.ably.channels.get(strategy_name)
                    channel.unsubscribe()
                    self.logger.info(f"Unsubscribed from strategy '{strategy_name}'")

                # Close connection
                await self.ably.close()
                self.logger.info("Closed Ably connection")

            except Exception as e:
                self.logger.error(f"Error stopping Ably service: {e}")

    async def get_status(self) -> Dict[str, Any]:
        """Get current status of the event subscriber"""

        return {
            "running": self.ably is not None,
            "strategies_count": len(self.strategies),
            "total_accounts": sum(len(accounts) for accounts in self.strategies.values()),
            "ably_connected": self.ably.connection.state == 'connected' if self.ably else False,
            "strategies": {
                name: len(accounts) for name, accounts in self.strategies.items()
            }
        }
