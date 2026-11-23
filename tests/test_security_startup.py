"""Local security/regression checks (PRD §16.3). All network and trading boundaries
are fakes. The §16.3 behaviours are preserved, adapted to the WP7 module layout.
"""

import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, mock_open, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import httpx
import yaml
from ably import AblyRealtime
from ably.types.connectionstate import ConnectionState
from app_config import APIConfig, AppConfig, DEFAULT_ALLOCATIONS_BASE_URL
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "event-broker"))

from app.accounts import AccountConfig  # noqa: E402
from app.services import ably_service  # noqa: E402
from app.services.ably_service import AblyEventSubscriber  # noqa: E402
from app.services.scheduler_service import SchedulerService  # noqa: E402
from app.settings import build_settings  # noqa: E402
from fakes import FakeClock, RecordingNotifier  # noqa: E402

ZEHNLABS_KEY = "AbC123.0123456789abcdefghijklmnopqrstuv"
ABLY_KEY = "synthetic.readonly:synthetic-secret"
DEFAULT_RESPONSE = object()
ACCOUNT = AccountConfig(
    account_id="U100001", type="paper", enabled=True, strategy_name="strategy-one"
)
STRATEGIES = {"strategy-one": [ACCOUNT]}


def executor_stub():
    """A minimal StrategyExecutor stand-in (execute is what signal/manual call)."""
    return SimpleNamespace(execute=AsyncMock(return_value=None), running=set())


class Response:
    def __init__(self, status=200, data=DEFAULT_RESPONSE, error=None):
        self.status = status
        self.data = data if data is not DEFAULT_RESPONSE else {"status": "success", "ablyApiKey": ABLY_KEY}
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


def subscriber(strategies=None):
    with patch.dict(os.environ, {"ZEHNLABS_API_KEY": ZEHNLABS_KEY}, clear=True), \
         patch.object(ably_service, "get_config", return_value=AppConfig()):
        return AblyEventSubscriber(
            strategy_executor=executor_stub(),
            strategies=dict(STRATEGIES) if strategies is None else strategies,
        )


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_fetches_key_once_then_subscribes_with_returned_key(self):
        sub = subscriber()
        session = Session(Response())
        channel = SimpleNamespace(subscribe=AsyncMock())
        sdk = SimpleNamespace(channels=SimpleNamespace(get=Mock(return_value=channel)),
                              connection=SimpleNamespace(on=Mock()))
        with patch.object(ably_service.aiohttp, "ClientSession", return_value=session), \
             patch.object(ably_service, "AblyRealtime", return_value=sdk) as constructor:
            await sub.start()
        self.assertEqual(sub.strategies, STRATEGIES)
        constructor.assert_called_once_with(ABLY_KEY)
        channel.subscribe.assert_awaited_once()
        self.assertEqual(session.requests[0][0], "https://workers.fintech.zehnlabs.com/api/v1/rebalance-auth")
        self.assertEqual(session.requests[0][1]["headers"], {"X-API-Key": ZEHNLABS_KEY})
        self.assertEqual(session.requests[0][1]["timeout"].total, 10)
        self.assertIs(session.requests[0][1]["allow_redirects"], False)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(sdk.connection.on.call_count, 4)

    async def test_missing_key_fails_before_sdk(self):
        sub = subscriber()
        sub.zehnlabs_api_key = None
        with patch.object(ably_service, "AblyRealtime") as sdk:
            with self.assertRaisesRegex(ValueError, "ZEHNLABS_API_KEY.* /api"):
                await sub.start()
        sdk.assert_not_called()
        self.assertIsNone(sub.ably)

    async def test_legacy_realtime_key_does_not_bypass_new_key(self):
        with patch.dict(os.environ, {"REALTIME_API_KEY": ABLY_KEY}, clear=True), \
             patch.object(ably_service, "get_config", return_value=AppConfig()):
            sub = AblyEventSubscriber(strategy_executor=executor_stub())
        with self.assertRaisesRegex(ValueError, "ZEHNLABS_API_KEY"):
            await sub.start()

    async def test_http_failures_stop_startup_and_exclude_response_body(self):
        for status, message in [(401, "key rejected"), (403, "not eligible"), (503, "HTTP 503"),
                                (302, "HTTP 302"), (500, "HTTP 500")]:
            with self.subTest(status=status):
                sub = subscriber()
                session = Session(Response(status, {"error": ZEHNLABS_KEY + ABLY_KEY}))
                with patch.object(ably_service.aiohttp, "ClientSession", return_value=session), \
                     patch.object(ably_service, "AblyRealtime") as sdk:
                    with self.assertRaisesRegex(RuntimeError, message) as error:
                        await sub.start()
                self.assertNotIn(ZEHNLABS_KEY, str(error.exception))
                self.assertNotIn(ABLY_KEY, str(error.exception))
                sdk.assert_not_called()
                self.assertEqual(len(session.requests), 1)

    async def test_transport_timeout_and_json_failures_are_sanitized(self):
        responses = [Response(error=asyncio.TimeoutError(ZEHNLABS_KEY)),
                     Response(error=aiohttp.ClientError(ABLY_KEY)),
                     Response(data=ValueError(ZEHNLABS_KEY + ABLY_KEY))]
        for response in responses:
            with self.subTest(response=response):
                sub = subscriber()
                with patch.object(ably_service.aiohttp, "ClientSession", return_value=Session(response)):
                    with self.assertRaisesRegex(RuntimeError, "Unable to retrieve") as error:
                        await sub.start()
                self.assertNotIn(ZEHNLABS_KEY, str(error.exception))
                self.assertNotIn(ABLY_KEY, str(error.exception))
                self.assertIsNone(sub.ably)

    async def test_invalid_payloads_never_construct_sdk(self):
        payloads = [None, [], "key", {}, {"status": "error", "ablyApiKey": ABLY_KEY},
                    {"ablyApiKey": ABLY_KEY}, {"status": "success", "ablyApiKey": True},
                    {"status": "success", "ablyApiKey": ""},
                    {"status": "success", "ablyApiKey": "token-without-colon"},
                    {"status": "success", "ablyApiKey": "key:"},
                    {"status": "success", "ablyApiKey": ":secret"},
                    {"status": "success", "ablyApiKey": "key:secret:extra"},
                    {"status": "success", "ablyApiKey": "key:secret\n"}]
        for data in payloads:
            with self.subTest(data=data):
                with patch.object(ably_service.aiohttp, "ClientSession", return_value=Session(Response(data=data))), \
                     patch.object(ably_service, "AblyRealtime") as sdk:
                    with self.assertRaisesRegex(RuntimeError, "invalid realtime authentication response"):
                        await subscriber().start()
                sdk.assert_not_called()

    async def test_no_strategies_fetches_key_once_then_skips_sdk(self):
        sub = subscriber({})
        session = Session(Response())
        with patch.object(ably_service.aiohttp, "ClientSession", return_value=session), \
             patch.object(ably_service, "AblyRealtime") as sdk:
            await sub.start()
        self.assertEqual(sub.strategies, {})
        sdk.assert_not_called()
        self.assertEqual(len(session.requests), 1)

    async def test_configured_timeout_is_used_for_total_startup_request(self):
        sub = subscriber()
        sub.config = AppConfig(api=APIConfig(rebalance_auth_timeout_seconds=7))
        session = Session(Response())
        with patch.object(ably_service.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await sub._fetch_ably_key(), ABLY_KEY)
        self.assertEqual(session.requests[0][1]["timeout"].total, 7)

    def test_api_root_derivation_and_invalid_bases(self):
        sub = subscriber()
        for base, expected in [
            (DEFAULT_ALLOCATIONS_BASE_URL, "https://workers.fintech.zehnlabs.com/api/v1/rebalance-auth"),
            ("https://example.invalid/custom/api/strategies/", "https://example.invalid/custom/api/rebalance-auth"),
            ("http://127.0.0.1:8787/api/v1/strategies", "http://127.0.0.1:8787/api/v1/rebalance-auth")]:
            sub.allocations_base_url = base
            self.assertEqual(sub._rebalance_auth_url(), expected)
        for base in ["", "relative/strategies", "https://example.invalid/api/v1", "ftp://example.invalid/strategies",
                     "https://user:password@example.invalid/strategies", "https://example.invalid/strategies?q=secret",
                     "https://example.invalid/strategies#proof", "https://example.invalid:bad/strategies",
                     "https://example.invalid/\nstrategies", "https://[invalid/strategies"]:
            with self.subTest(base=base):
                sub.allocations_base_url = base
                with self.assertRaisesRegex(ValueError, "ALLOCATIONS_BASE_URL") as error:
                    sub._rebalance_auth_url()
                self.assertNotIn("password", str(error.exception))

    def test_timeout_config_is_bounded_and_old_config_keeps_default(self):
        self.assertEqual(APIConfig().rebalance_auth_timeout_seconds, 10)
        self.assertEqual(APIConfig(rebalance_auth_timeout_seconds=1).rebalance_auth_timeout_seconds, 1)
        self.assertEqual(APIConfig(rebalance_auth_timeout_seconds=120).rebalance_auth_timeout_seconds, 120)
        for value in [0, 121, float("inf"), float("nan"), "bad"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                APIConfig(rebalance_auth_timeout_seconds=value)

    async def test_event_payload_maps_exec_to_executor_as_live(self):
        sub = subscriber()
        event = {"exec": "rebalance", "eventId": "synthetic-event"}
        await sub._handle_strategy_event("strategy-one", SimpleNamespace(data=json.dumps(event)))
        sub.strategy_executor.execute.assert_awaited_once_with(
            "strategy-one", [ACCOUNT], "live", "signal"
        )

    async def test_event_payload_maps_print_rebalance_to_preview(self):
        sub = subscriber()
        await sub._handle_strategy_event(
            "strategy-one",
            SimpleNamespace(data={"exec": "print-rebalance", "eventId": "p"}),
        )
        sub.strategy_executor.execute.assert_awaited_once_with(
            "strategy-one", [ACCOUNT], "preview", "signal"
        )

    async def test_invalid_event_payload_drops_without_executor_call(self):
        sub = subscriber()
        sub.strategy_executor.execute.reset_mock()
        await sub._handle_strategy_event("strategy-one", SimpleNamespace(data={"eventId": "invalid"}))
        sub.strategy_executor.execute.assert_not_called()
        await sub._handle_strategy_event("strategy-one", SimpleNamespace(data={"exec": "unknown"}))
        sub.strategy_executor.execute.assert_not_called()


class BuildSettingsTests(unittest.TestCase):
    def test_runtime_settings_masks_api_key_and_ignores_realtime_key(self):
        with patch.dict(os.environ, {
            "ZEHNLABS_API_KEY": ZEHNLABS_KEY,
            "REALTIME_API_KEY": ABLY_KEY,
            "TRADING_MODE": "paper",
        }, clear=True):
            settings = build_settings()
        self.assertEqual(settings.zehnlabs_api_key, ZEHNLABS_KEY)
        self.assertNotIn(ZEHNLABS_KEY, repr(settings))
        self.assertNotIn(ABLY_KEY, repr(settings))

    def test_missing_api_key_fails(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "ZEHNLABS_API_KEY"):
                build_settings()


class ExecutionPreservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app_config import load_config
        load_config(REPO / "config.yaml")

    def main_module(self):
        with patch.dict(os.environ, {"CONFIG_PATH": str(REPO / "config.yaml")}), \
             patch("app.services.strategy_executor.StrategyExecutor"):
            return importlib.import_module("app.main")

    def make_app(self):
        main = self.main_module()
        app = main.EventBrokerApp.__new__(main.EventBrokerApp)
        app.config = AppConfig()
        app.running = False
        app.settings = SimpleNamespace(
            trading_mode="paper", ib_port=None,
            allocations_base_url=DEFAULT_ALLOCATIONS_BASE_URL,
            zehnlabs_api_key=ZEHNLABS_KEY,
            user_notifications_enabled=False, user_notifications_channel="",
        )
        app.registry = SimpleNamespace(
            get=lambda i: ACCOUNT if i == "U100001" else None,
            for_strategy=lambda s: [ACCOUNT],
            strategies=lambda: ["strategy-one"],
        )
        app.clock = FakeClock(__import__("datetime").datetime(2026, 11, 20, 10, 0))
        app.notifier = RecordingNotifier()
        app.records = SimpleNamespace(prune=lambda: None, append=lambda r: None)
        app.strategy_executor = executor_stub()
        app.ably_subscriber = subscriber()
        app.scheduler_service = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        app._run_forever = AsyncMock()
        app._watch_manual_events = AsyncMock()
        app.manual_event_file = "/synthetic/manual/rebalance.json"
        app._check_strategies = AsyncMock()
        return app

    async def test_main_waits_for_signal_shutdown(self):
        main = self.main_module()
        handlers = {}
        finished = asyncio.Event()

        async def start():
            handlers[main.signal.SIGTERM](main.signal.SIGTERM, None)
            await asyncio.sleep(0)

        async def stop():
            await asyncio.sleep(0.01)
            finished.set()

        with patch.object(main, "app", SimpleNamespace(start=start, stop=stop)), \
             patch.object(main.signal, "signal", side_effect=handlers.__setitem__):
            await main.main()

        self.assertTrue(finished.is_set())

    async def test_startup_failure_does_not_start_scheduler_or_manual_watcher(self):
        app = self.make_app()
        app.ably_subscriber.start = AsyncMock(side_effect=RuntimeError("synthetic auth failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic auth failure"):
            await app.start()
        app.scheduler_service.start.assert_not_called()
        app._watch_manual_events.assert_not_called()
        self.assertFalse(app.running)

    async def test_accounts_load_before_scheduler_and_manual_watcher(self):
        app = self.make_app()
        order = []

        async def startup():
            order.append("subscriber")

        async def scheduler():
            self.assertIs(app._find_account_by_id("U100001"), ACCOUNT)
            order.append("scheduler")

        async def manual():
            self.assertIs(app._find_account_by_id("U100001"), ACCOUNT)
            order.append("manual")

        app.ably_subscriber.start = startup
        app.scheduler_service.start = scheduler
        app._watch_manual_events = manual
        await app.start()
        self.assertEqual(order, ["subscriber", "scheduler", "manual"])
        self.assertTrue(app.running)

    async def test_strategy_availability_check_stops_startup(self):
        main = self.main_module()
        app = self.make_app()
        del app._check_strategies  # use the real method

        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def fetch(self, strategy):
                raise LookupError(f"{strategy} unavailable")

        with patch.object(main, "AllocationsClient", FailingClient):
            with self.assertRaisesRegex(
                RuntimeError, "Strategy 'strategy-one' is not available " r".*Check the name"
            ):
                await app._check_strategies()

    async def test_manual_event_routes_loaded_account_to_executor(self):
        app = self.make_app()
        event = {"account_id": "U100001", "exec": "print-rebalance", "source": "manual"}
        with patch("builtins.open", mock_open(read_data=json.dumps(event))), \
             patch("app.main.os.remove") as remove:
            await app._process_manual_event()
        remove.assert_called_once_with(app.manual_event_file)
        app.strategy_executor.execute.assert_awaited_once_with(
            "strategy-one", [ACCOUNT], "preview", "manual"
        )

    async def test_scheduled_events_use_loaded_accounts_and_existing_payload(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            scheduled_file = os.path.join(tmp, "scheduled.json")
            Path(scheduled_file).write_text(json.dumps(["U100001", "missing"]), encoding="utf-8")
            scheduler = SchedulerService(
                config=AppConfig(), strategy_executor=app.strategy_executor,
                accounts_lookup=app.registry.get, clock=app.clock,
                notifier=app.notifier, logger=None,
            )
            scheduler.scheduled_file = scheduled_file
            await scheduler._process_scheduled_accounts()
        args = app.strategy_executor.execute.await_args.args
        self.assertEqual(args[:2], ("strategy-one", [ACCOUNT]))
        self.assertEqual(args[2], "live")
        self.assertEqual(args[3], "scheduled")

    def test_compose_and_examples_configure_one_key(self):
        compose = (REPO / "docker-compose.yaml").read_text(encoding="utf-8")
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        self.assertIn("ZEHNLABS_API_KEY: ${ZEHNLABS_API_KEY}", compose)
        self.assertIn("ZEHNLABS_API_KEY=", example)
        self.assertNotIn("REALTIME_API_KEY:", compose)
        self.assertNotIn("REBALANCE_EVENT_SUBSCRIPTION_API_KEY=", example)
        self.assertIn("ALLOCATIONS_BASE_URL=" + DEFAULT_ALLOCATIONS_BASE_URL, example)


class Socket:
    """Ably's real protocol reads synthetic CONNECTED/ATTACHED/DISCONNECTED frames."""
    def __init__(self):
        self.queue = asyncio.Queue()
        self.queue.put_nowait({"action": 4, "connectionId": "synthetic-connection",
                               "connectionDetails": {"connectionKey": "synthetic-resume", "connectionStateTtl": 120000}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.queue.get()
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["action"] == 10:
            self.queue.put_nowait({"action": 11, "channel": frame["channel"], "channelSerial": "synthetic", "flags": 4})
        elif frame["action"] == 7:
            self.queue.put_nowait({"action": 8})

    async def close(self):
        self.queue.put_nowait(None)


class SocketContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        return False


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.003)


class RealSDKReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_startup_and_three_sdk_reconnects_keep_key_channel_and_single_listener(self):
        sub = subscriber()
        sub.strategy_executor.execute.return_value = None
        session = Session(Response())
        sockets = []
        clients = []
        real_http_client = httpx.AsyncClient

        def reject_http(request):
            raise AssertionError("Unexpected SDK HTTP request; external network is disabled")

        def sdk_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(reject_http)
            return real_http_client(*args, **kwargs)

        def connect(url, **kwargs):
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params["key"], [ABLY_KEY])
            self.assertNotIn("accessToken", params)
            self.assertNotIn(ZEHNLABS_KEY, url + str(kwargs))
            socket = Socket()
            sockets.append(socket)
            return SocketContext(socket)

        def construct(key):
            client = AblyRealtime(key, fallback_hosts=[], use_binary_protocol=False,
                                  realtime_request_timeout=300, disconnected_retry_timeout=30,
                                  suspended_retry_timeout=40)
            clients.append(client)
            return client

        with patch.object(ably_service.aiohttp, "ClientSession", return_value=session), \
             patch.object(ably_service, "AblyRealtime", side_effect=construct), \
             patch("ably.transport.websockettransport.ws_connect", side_effect=connect), \
             patch("httpx.AsyncClient", side_effect=sdk_http):
            try:
                await asyncio.wait_for(sub.start(), 2)
                original_channel = sub.ably.channels.get("strategy-one")
                for _ in range(3):
                    previous = len(sockets)
                    sockets[-1].queue.put_nowait({"action": 6, "error": {"code": 80003, "statusCode": 400,
                                                                          "message": "synthetic disconnect"}})
                    await until(lambda: len(sockets) > previous)
                    await until(lambda: sub.ably.connection.state == ConnectionState.CONNECTED)
                self.assertEqual(len(session.requests), 1)
                self.assertEqual(len(clients), 1)
                self.assertEqual(len(sockets), 4)
                self.assertIs(sub.ably.channels.get("strategy-one"), original_channel)
                sub.strategy_executor.execute.assert_not_called()
                event = {"exec": "rebalance", "eventId": "synthetic-reconnect-event"}
                sockets[-1].queue.put_nowait({"action": 15, "channel": "strategy-one",
                                             "messages": [{"name": "any-event-name", "data": event}]})
                await until(lambda: sub.strategy_executor.execute.await_count == 1)
                await asyncio.sleep(0.02)
                sub.strategy_executor.execute.assert_awaited_once_with(
                    "strategy-one", [ACCOUNT], "live", "signal"
                )
            finally:
                if sub.ably:
                    await asyncio.wait_for(sub.ably.close(), 1)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsubscribes_and_closes_connection(self):
        sub = subscriber()
        sub.strategy_executor.execute.reset_mock()
        channel = SimpleNamespace(unsubscribe=Mock())
        sub.ably = SimpleNamespace(channels=SimpleNamespace(get=Mock(return_value=channel)), close=AsyncMock())

        await sub.stop()

        channel.unsubscribe.assert_called_once_with()
        sub.ably.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
