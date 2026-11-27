"""Dashboard acceptance coverage for the Railway implementation specification."""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.accounts import AccountConfig, AccountRegistry, export_accounts_yaml, parse_accounts_yaml
from app.application import RebalancerApplication
from app.results import (
    AccountRunResult,
    FailureReason,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
    WorkAction,
)
from app.services.account_manager import (
    AccountSaveResult,
    DiscoveryResult,
    ExternalReadiness,
    ReadinessState,
)
from app.services.strategy_executor import ExecutionDecision
from app.services.work_manager import WorkManager
from app.presentation import format_eastern_time
from app.settings import RuntimeSettings
from app.state.models import RunStart, WebSession, WorkSource, WorkStatus, utc_now_text
from app.state.sqlite import SQLiteState
from app.web.app import create_web_app
from app.web.auth import AuthenticationService, COOKIE_NAME


PASSWORD = "safe-dashboard-password-123"
ORIGIN = "https://testserver"
ACCOUNT_ID = "U300001"


def _hidden(response, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    if match is None:
        raise AssertionError(f"missing hidden field {name!r}")
    return match.group(1)


def _form_hidden(response, action: str, name: str) -> str:
    form = re.search(
        rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>',
        response.text,
        re.DOTALL,
    )
    if form is None:
        raise AssertionError(f"missing form action {action!r}")
    field = re.search(rf'name="{name}" value="([^"]+)"', form.group(1))
    if field is None:
        raise AssertionError(f"missing hidden field {name!r} in form {action!r}")
    return field.group(1)


def _request(*, peer: str, headers: list[tuple[bytes, bytes]]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 443),
            "client": (peer, 12345),
            "headers": headers,
        }
    )


class FakeClock:
    def __init__(self, *, market_open: bool = True):
        self.current = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.market_open = market_open

    def now(self) -> datetime:
        return self.current

    def is_open(self, _now: datetime | None = None) -> bool:
        return self.market_open

    def next_scheduled_run(self, now: datetime | None = None) -> datetime:
        return (now or self.current) + timedelta(days=1)

    def scheduled_time_has_passed(self, _now: datetime | None = None) -> bool:
        return True


class FakeSubscriber:
    def __init__(self):
        self.status_calls = 0

    async def get_status(self):
        self.status_calls += 1
        return {"ably_connected": False}


class FakeAccountManager:
    def __init__(
        self,
        *,
        state: SQLiteState,
        settings: RuntimeSettings,
        registry: AccountRegistry,
    ):
        self.state = state
        self.settings = settings
        self.registry = registry
        self.subscriber = FakeSubscriber()
        self.degraded_error = None
        self.discovery_calls = 0
        self.discovery_error: Exception | None = None
        self.save_error: Exception | None = None

    async def subscriber_status(self):
        return SimpleNamespace(ably_connected=False)

    async def discover(self) -> DiscoveryResult:
        self.discovery_calls += 1
        if self.discovery_error is not None:
            raise self.discovery_error
        return DiscoveryResult(accounts=(ACCOUNT_ID,), strategies=(), replacement_sets=())

    async def save(self, accounts) -> AccountSaveResult:
        if self.save_error is not None:
            raise self.save_error
        candidate = list(accounts)
        self.state.replace_accounts(self.settings.trading_mode, candidate)
        self.registry.refresh(candidate, self.settings.trading_mode)
        return AccountSaveResult(accounts=tuple(candidate))

    async def import_yaml(self, document: str) -> AccountSaveResult:
        accounts = parse_accounts_yaml(document)
        self.state.replace_all_accounts(accounts)
        self.registry.refresh(accounts, self.settings.trading_mode)
        return AccountSaveResult(accounts=tuple(accounts))

    def export_yaml(self) -> str:
        return export_accounts_yaml(self.state.list_accounts())


class FakeExecutor:
    def __init__(self):
        self.accepting = True
        self.critical_storage_error = None
        self.broker_calls = 0
        self.place_calls = 0
        self.cancel_calls = 0
        self.execution_calls: list[tuple[str, tuple[RunStart, ...]]] = []
        self.postprocessed: list[AccountRunResult] = []
        self.running: set[str] = set()

    def try_accept(self, strategy: str) -> ExecutionDecision:
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
        accounts,
        mode: RunMode,
        source: RunSource,
        *,
        batch_id: str | None = None,
        requested_at: str | None = None,
    ) -> list[RunStart]:
        return [
            RunStart(
                run_id=f"acceptance-{account.account_id}-{uuid4()}",
                batch_id=batch_id or str(uuid4()),
                account=account,
                trading_mode=account.type,
                run_mode=mode,
                source=source,
                requested_at=requested_at or utc_now_text(datetime.now(timezone.utc)),
            )
            for account in accounts
        ]

    def static_result(
        self,
        start: RunStart,
        outcome: RunOutcome,
        *,
        reason: FailureReason | None = None,
        error: str | None = None,
    ) -> AccountRunResult:
        finished_at = utc_now_text(datetime.now(timezone.utc))
        record = RunRecord(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=start.run_mode,
            source=start.source,
            started_at=finished_at,
            finished_at=finished_at,
            outcome=outcome,
            failure_reason=reason,
            error_message=error,
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

    async def execute_reserved(
        self, strategy: str, starts: list[RunStart]
    ) -> list[AccountRunResult]:
        captured = tuple(starts)
        self.execution_calls.append((strategy, captured))
        self.broker_calls += 1
        if starts and starts[0].run_mode == RunMode.LIVE:
            self.place_calls += len(starts)
        return [
            self.static_result(
                start,
                (
                    RunOutcome.PREVIEWED
                    if start.run_mode == RunMode.PREVIEW
                    else RunOutcome.SUCCEEDED
                ),
            )
            for start in starts
        ]

    async def postprocess(
        self, result: AccountRunResult, *, retry_decision=None
    ) -> None:
        self.postprocessed.append(result)

    def record_storage_failure(self, _exc: BaseException) -> None:
        self.accepting = False

    def stop_accepting(self) -> None:
        self.accepting = False


class DashboardHarness:
    def __init__(
        self,
        *,
        trading_mode: str = "paper",
        include_account: bool = True,
        market_open: bool = True,
        railway_environment_id: str = "",
    ):
        self.temp = tempfile.TemporaryDirectory()
        self.state = SQLiteState(Path(self.temp.name) / "state.db")
        self.state.initialize()
        self.settings = RuntimeSettings(
            trading_mode=trading_mode,
            dashboard_admin_password=PASSWORD,
            railway_environment_id=railway_environment_id,
        )
        self.account = AccountConfig(
            account_id=ACCOUNT_ID,
            type=trading_mode,
            enabled=True,
            strategy_name="strategy-one",
        )
        if include_account:
            self.state.replace_accounts(trading_mode, [self.account])
        self.registry = AccountRegistry.from_accounts(
            self.state.list_accounts(trading_mode), trading_mode
        )
        self.clock = FakeClock(market_open=market_open)
        self.executor = FakeExecutor()
        self.work = WorkManager(
            state=self.state,
            registry=self.registry,
            settings=self.settings,
            clock=self.clock,
            executor=self.executor,
        )
        self.accounts = FakeAccountManager(
            state=self.state,
            settings=self.settings,
            registry=self.registry,
        )
        self.application = RebalancerApplication(
            settings=self.settings,
            state=self.state,
            accounts=self.accounts,
            work=self.work,
            executor=self.executor,
        )
        self.auth = AuthenticationService(self.state, self.settings)
        self.web = create_web_app(self.application, self.auth)
        self.clients: list[TestClient] = []

    def client(self) -> TestClient:
        client = TestClient(self.web, base_url=ORIGIN)
        self.clients.append(client)
        return client

    def login(self, client: TestClient | None = None) -> tuple[TestClient, str]:
        active = client or self.client()
        login = active.get("/login")
        response = active.post(
            "/login",
            data={
                "username": "admin",
                "password": PASSWORD,
                "csrf_token": _hidden(login, "csrf_token"),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        if response.status_code != 303:
            raise AssertionError(response.text)
        page = active.get("/accounts")
        return active, _hidden(page, "csrf_token")

    def record_successful_preview(
        self,
        account: AccountConfig | None = None,
        *,
        run_id: str | None = None,
    ) -> str:
        selected = account or self.account
        requested = self.clock.now() + timedelta(seconds=1)
        started = utc_now_text(requested)
        finished = utc_now_text(requested + timedelta(seconds=1))
        selected_run_id = run_id or f"acceptance-preview-{selected.account_id}"
        start = RunStart(
            run_id=selected_run_id,
            batch_id="acceptance-preview-batch",
            account=selected,
            trading_mode=self.settings.trading_mode,
            run_mode=RunMode.PREVIEW,
            source=RunSource.INITIAL,
            requested_at=started,
        )
        self.state.start_runs([start])
        record = RunRecord(
            run_id=selected_run_id,
            account_id=selected.account_id,
            strategy_name=selected.strategy_name,
            mode=RunMode.PREVIEW,
            source=RunSource.INITIAL,
            started_at=started,
            finished_at=finished,
            outcome=RunOutcome.PREVIEWED,
        )
        self.state.finish_run(
            AccountRunResult(
                run_id=selected_run_id,
                account_id=selected.account_id,
                strategy_name=selected.strategy_name,
                mode=RunMode.PREVIEW,
                source=RunSource.INITIAL,
                outcome=RunOutcome.PREVIEWED,
                record=record,
            )
        )
        return selected_run_id

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.temp.cleanup()


class DashboardRouteAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.harness = DashboardHarness()

    def tearDown(self):
        self.harness.close()

    def test_route_authentication_and_method_contract(self):
        client = self.harness.client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/login").status_code, 200)

        protected_gets = (
            "/",
            "/onboarding",
            "/accounts",
            f"/accounts/{ACCOUNT_ID}/preview",
            f"/accounts/{ACCOUNT_ID}/rebalance",
            f"/accounts/{ACCOUNT_ID}/schedule",
            "/accounts/export",
            "/work",
            "/work/submissions/not-a-submission-id",
            "/work/not-a-work-id",
            "/runs",
            "/runs/not-a-run-id",
        )
        for path in protected_gets:
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/login")

        protected_posts = (
            "/logout",
            "/onboarding/discover",
            "/onboarding/save",
            "/onboarding/preview",
            "/onboarding/finish",
            "/accounts/discover",
            "/accounts/save",
            "/accounts/import",
            f"/accounts/{ACCOUNT_ID}/preview",
            f"/accounts/{ACCOUNT_ID}/rebalance",
            f"/accounts/{ACCOUNT_ID}/schedule",
            "/work/not-a-work-id/cancel",
        )
        for path in protected_posts:
            with self.subTest(path=path):
                self.assertEqual(client.post(path, follow_redirects=False).status_code, 403)

        self.harness.login(client)
        for method, path in (
            ("GET", "/logout"),
            ("POST", "/healthz"),
            ("POST", "/accounts"),
            ("POST", "/work"),
            ("POST", "/runs"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(client.request(method, path).status_code, 405)

    def test_authenticated_html_security_headers_and_no_cors(self):
        client, _ = self.harness.login()
        response = client.get("/accounts", headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertIn("camera=()", response.headers["permissions-policy"])
        self.assertNotIn("access-control-allow-origin", response.headers)
        status = client.get("/")
        self.assertIn("Gateway", status.text)
        self.assertIn("Zehnlabs", status.text)
        self.assertIn("Ably", status.text)

    def test_readiness_states_are_specific_and_safe(self):
        client, _ = self.harness.login()
        initial = client.get("/")
        self.assertIn("unverified", initial.text)
        self.assertIn("ZEHNLABS_API_KEY is missing", initial.text)

        self.harness.accounts.gateway_readiness = ExternalReadiness(
            state=ReadinessState.UNAVAILABLE,
            message="IBKR Gateway is unreachable or unauthenticated.",
        )
        self.harness.accounts.zehnlabs_readiness = ExternalReadiness(
            state=ReadinessState.REJECTED,
            message="Zehnlabs rejected the configured API key.",
        )
        self.harness.accounts.degraded_error = (
            "Signal subscriptions are unavailable. The service will retry automatically."
        )
        response = client.get("/")
        self.assertIn("unavailable", response.text)
        self.assertIn("unreachable or unauthenticated", response.text)
        self.assertIn("rejected", response.text)
        self.assertIn("configured API key", response.text)
        self.assertIn("degraded", response.text)
        self.assertNotIn("upstream-secret", response.text)
        self.assertNotIn("SELECT *", response.text)

    def test_get_routes_do_not_mutate_domain_state_or_call_broker(self):
        preview = self.harness.application.submit_work(
            ACCOUNT_ID, WorkAction.PREVIEW, "purity-preview-token"
        ).item
        rebalance = self.harness.application.submit_work(
            ACCOUNT_ID, WorkAction.REBALANCE, "purity-rebalance-token"
        ).item
        run_id = self.harness.record_successful_preview()
        client, _ = self.harness.login()

        before_accounts = [row.model_dump() for row in self.harness.state.list_accounts()]
        before_work = [row.model_dump() for row in self.harness.state.list_work_items(limit=200)]
        before_runs = [row.model_dump() for row in self.harness.state.list_runs(limit=200)]
        before_broker_calls = self.harness.executor.broker_calls
        before_discovery_calls = self.harness.accounts.discovery_calls

        paths = (
            "/",
            "/onboarding",
            "/accounts",
            f"/accounts/{ACCOUNT_ID}/preview",
            f"/accounts/{ACCOUNT_ID}/rebalance",
            f"/accounts/{ACCOUNT_ID}/schedule",
            "/accounts/export",
            "/work",
            f"/work/submissions/{preview.submission_id}",
            f"/work/{preview.work_id}",
            f"/work/{rebalance.work_id}",
            "/runs",
            f"/runs/{run_id}",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

        self.assertEqual(
            [row.model_dump() for row in self.harness.state.list_accounts()], before_accounts
        )
        self.assertEqual(
            [row.model_dump() for row in self.harness.state.list_work_items(limit=200)],
            before_work,
        )
        self.assertEqual(
            [row.model_dump() for row in self.harness.state.list_runs(limit=200)], before_runs
        )
        self.assertEqual(self.harness.executor.broker_calls, before_broker_calls)
        self.assertEqual(self.harness.accounts.discovery_calls, before_discovery_calls)

    def test_cross_session_csrf_and_cross_origin_posts_create_no_work(self):
        first, first_csrf = self.harness.login()
        second, _ = self.harness.login()

        response = second.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": first_csrf, "request_token": "cross-session-token"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

        response = first.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": first_csrf, "request_token": "cross-origin-token"},
            headers={"Origin": "https://attacker.invalid"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

    def test_onboarding_requires_accounts_and_a_current_successful_preview(self):
        client, csrf_token = self.harness.login()
        blocked = client.post(
            "/onboarding/finish",
            data={"csrf_token": csrf_token, "request_token": "finish-before-preview"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

        self.harness.record_successful_preview()
        csrf_token = _hidden(client.get("/onboarding"), "csrf_token")
        accepted = client.post(
            "/onboarding/finish",
            data={"csrf_token": csrf_token, "request_token": "finish-after-preview"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303)
        work = self.harness.state.list_work_items(limit=200)
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0].action, WorkAction.REBALANCE)
        self.assertEqual(work[0].source, WorkSource.INITIAL)

        empty = DashboardHarness(include_account=False)
        try:
            empty_client, empty_csrf = empty.login()
            response = empty_client.post(
                "/onboarding/finish",
                data={"csrf_token": empty_csrf, "request_token": "empty-onboarding"},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(empty.state.list_work_items(limit=200), [])
        finally:
            empty.close()

    def test_immediate_onboarding_confirmation_says_now_on_both_pages(self):
        client, csrf_token = self.harness.login()
        expected_time = format_eastern_time(self.harness.clock.now())

        onboarding = client.get("/onboarding")
        for expected in (
            ACCOUNT_ID,
            "strategy-one",
            "Replacement Set",
            expected_time,
            "(now)",
        ):
            self.assertIn(expected, onboarding.text)
        self.assertNotIn("as soon as XNYS is open", onboarding.text)

        saved = client.post(
            "/onboarding/save",
            data={
                "csrf_token": csrf_token,
                "selected_account_id": ACCOUNT_ID,
                f"enabled_{ACCOUNT_ID}": "on",
                f"strategy_{ACCOUNT_ID}": "strategy-one",
            },
            headers={"Origin": ORIGIN},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertIn("Continue onboarding", saved.text)
        self.assertIn(expected_time, saved.text)
        self.assertIn("(now)", saved.text)
        self.assertNotIn("as soon as XNYS is open", saved.text)

    def test_duplicate_submission_is_idempotent_or_links_to_existing_receipt(self):
        client, csrf_token = self.harness.login()
        first_confirmation = client.get(f"/accounts/{ACCOUNT_ID}/preview")
        second_confirmation = client.get(f"/accounts/{ACCOUNT_ID}/preview")
        first_token = _hidden(first_confirmation, "request_token")
        second_token = _hidden(second_confirmation, "request_token")
        self.assertNotEqual(first_token, second_token)

        first = client.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": csrf_token, "request_token": first_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        receipt = first.headers["location"]

        replay = client.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": csrf_token, "request_token": first_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay.headers["location"], receipt)

        conflict = client.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": csrf_token, "request_token": second_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertIn(receipt, conflict.text)
        self.assertEqual(len(self.harness.state.list_work_items(limit=200)), 1)
        asyncio.run(self.harness.work.dispatch_due())
        self.assertEqual(len(self.harness.executor.execution_calls), 1)
        self.assertEqual(self.harness.executor.place_calls, 0)
        self.assertEqual(self.harness.executor.cancel_calls, 0)

    def test_closed_market_rebalance_confirmation_and_receipt_are_scheduled(self):
        closed = DashboardHarness(market_open=False)
        try:
            client, csrf_token = closed.login()
            confirmation = client.get(f"/accounts/{ACCOUNT_ID}/rebalance")
            expected_time = format_eastern_time(
                closed.clock.next_scheduled_run(closed.clock.now())
            )
            self.assertIn(expected_time, confirmation.text)
            self.assertIn("next market open", confirmation.text)
            submitted = client.post(
                f"/accounts/{ACCOUNT_ID}/rebalance",
                data={
                    "csrf_token": csrf_token,
                    "request_token": _hidden(confirmation, "request_token"),
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(submitted.status_code, 303)
            item = closed.state.list_work_items(limit=1)[0]
            self.assertEqual(item.source, WorkSource.SCHEDULED)
            receipt = client.get(submitted.headers["location"])
            self.assertIn("Scheduled for", receipt.text)
            self.assertIn(expected_time, receipt.text)
            self.assertIn("next market open", receipt.text)
        finally:
            closed.close()

    def test_preview_and_rebalance_coexist_with_durable_cancellation_receipt(self):
        client, csrf_token = self.harness.login()
        preview_token = _hidden(
            client.get(f"/accounts/{ACCOUNT_ID}/preview"), "request_token"
        )
        rebalance_token = _hidden(
            client.get(f"/accounts/{ACCOUNT_ID}/rebalance"), "request_token"
        )
        preview = client.post(
            f"/accounts/{ACCOUNT_ID}/preview",
            data={"csrf_token": csrf_token, "request_token": preview_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        rebalance = client.post(
            f"/accounts/{ACCOUNT_ID}/rebalance",
            data={"csrf_token": csrf_token, "request_token": rebalance_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual((preview.status_code, rebalance.status_code), (303, 303))
        outstanding = self.harness.state.list_work_items(limit=200)
        self.assertEqual({item.action for item in outstanding}, {WorkAction.PREVIEW, WorkAction.REBALANCE})

        rebalance_id = rebalance.headers["location"].rsplit("/", 1)[-1]
        receipt = client.get(f"/work/{rebalance_id}")
        self.assertIn(ACCOUNT_ID, receipt.text)
        self.assertIn("rebalance", receipt.text)
        self.assertIn("pending", receipt.text)

        cancelled = client.post(
            f"/work/{rebalance_id}/cancel",
            data={"csrf_token": csrf_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(cancelled.status_code, 303)
        stored = self.harness.state.get_work_item(rebalance_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, WorkStatus.CANCELLED)
        self.assertIn("No already-sent broker order was cancelled", stored.message or "")
        self.assertIn(
            "No already-sent broker order was cancelled",
            client.get(f"/work/{rebalance_id}").text,
        )
        self.assertEqual(self.harness.executor.broker_calls, 0)

    def test_preview_and_rebalance_dispatch_sequentially_in_priority_order(self):
        self.harness.application.submit_work(
            ACCOUNT_ID,
            WorkAction.PREVIEW,
            "sequential-preview",
        )
        self.harness.application.submit_work(
            ACCOUNT_ID,
            WorkAction.REBALANCE,
            "sequential-rebalance",
        )

        asyncio.run(self.harness.work.dispatch_due())
        first_cycle = self.harness.state.list_work_items(limit=200)
        statuses = {item.action: item.status for item in first_cycle}
        self.assertEqual(statuses[WorkAction.REBALANCE], WorkStatus.COMPLETED)
        self.assertEqual(statuses[WorkAction.PREVIEW], WorkStatus.PENDING)
        self.assertEqual(
            [call[1][0].run_mode for call in self.harness.executor.execution_calls],
            [RunMode.LIVE],
        )

        asyncio.run(self.harness.work.dispatch_due())
        self.assertTrue(
            all(
                item.status == WorkStatus.COMPLETED
                for item in self.harness.state.list_work_items(limit=200)
            )
        )
        self.assertEqual(
            [call[1][0].run_mode for call in self.harness.executor.execution_calls],
            [RunMode.LIVE, RunMode.PREVIEW],
        )
        self.assertEqual(self.harness.executor.place_calls, 1)
        self.assertEqual(self.harness.executor.cancel_calls, 0)

    def test_disabled_account_is_cancelled_before_executor_invocation(self):
        creation = self.harness.application.submit_work(
            ACCOUNT_ID,
            WorkAction.PREVIEW,
            "disable-before-dispatch",
        )
        disabled = self.harness.account.model_copy(update={"enabled": False})
        self.harness.state.replace_accounts("paper", [disabled])
        self.harness.registry.refresh([disabled], "paper")

        asyncio.run(self.harness.work.dispatch_due())

        stored = self.harness.state.get_work_item(creation.item.work_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, WorkStatus.CANCELLED)
        self.assertIn("Account configuration changed", stored.message or "")
        self.assertEqual(self.harness.executor.execution_calls, [])
        self.assertEqual(self.harness.executor.place_calls, 0)
        self.assertEqual(self.harness.executor.cancel_calls, 0)

    def test_readiness_assignment_and_allocation_failures_are_safe_and_atomic(self):
        client, csrf_token = self.harness.login()
        secret = "upstream-secret C:\\private\\state.db SELECT * FROM accounts"
        self.harness.accounts.discovery_error = RuntimeError(secret)
        discovery = client.post(
            "/onboarding/discover",
            data={"csrf_token": csrf_token},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(discovery.status_code, 503)
        self.assertIn("discovery is unavailable", discovery.text)
        self.assertNotIn(secret, discovery.text)
        self.harness.accounts.discovery_error = None

        invalid_assignment = client.post(
            "/onboarding/save",
            data={
                "csrf_token": csrf_token,
                "selected_account_id": ACCOUNT_ID,
                f"enabled_{ACCOUNT_ID}": "on",
                f"strategy_{ACCOUNT_ID}": "",
            },
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(invalid_assignment.status_code, 422)
        self.assertIn("strategy_name", invalid_assignment.text)

        preserved = client.post(
            "/onboarding/save",
            data={
                "csrf_token": csrf_token,
                "selected_account_id": ACCOUNT_ID,
                f"strategy_{ACCOUNT_ID}": "<script>alert(1)</script>",
                f"replacement_{ACCOUNT_ID}": "submitted-choice",
            },
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(preserved.status_code, 422)
        self.assertIn('aria-invalid="true"', preserved.text)
        self.assertIn('aria-describedby="strategy-error-', preserved.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", preserved.text)
        self.assertNotIn("<script>alert(1)</script>", preserved.text)
        self.assertIn('value="submitted-choice" selected', preserved.text)
        self.assertNotIn(
            f'name="enabled_{ACCOUNT_ID}" checked', preserved.text
        )

        before = [account.model_dump() for account in self.harness.state.list_accounts()]
        unsafe_error = "<script>alert(1)</script> Strategy could not be validated."
        self.harness.accounts.save_error = ValueError(unsafe_error)
        allocation = client.post(
            "/onboarding/save",
            data={
                "csrf_token": csrf_token,
                "selected_account_id": ACCOUNT_ID,
                f"enabled_{ACCOUNT_ID}": "on",
                f"strategy_{ACCOUNT_ID}": "strategy-one",
            },
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(allocation.status_code, 422)
        self.assertNotIn("<script>", allocation.text)
        self.assertIn("&lt;script&gt;", allocation.text)
        self.assertNotIn("Traceback", allocation.text)
        self.assertNotIn(PASSWORD, allocation.text)
        self.assertEqual(
            [account.model_dump() for account in self.harness.state.list_accounts()],
            before,
        )

    def test_one_missing_preview_blocks_every_initial_rebalance(self):
        second_account = AccountConfig(
            account_id="U300002",
            type="paper",
            enabled=True,
            strategy_name="strategy-two",
        )
        accounts = [self.harness.account, second_account]
        self.harness.state.replace_accounts("paper", accounts)
        self.harness.registry.refresh(accounts, "paper")
        self.harness.record_successful_preview(self.harness.account)
        client, csrf_token = self.harness.login()

        response = client.post(
            "/onboarding/finish",
            data={"csrf_token": csrf_token, "request_token": "all-or-nothing"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Every enabled Account", response.text)
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

    def test_two_account_preview_submission_shows_every_result_and_replays(self):
        second_account = AccountConfig(
            account_id="U300002",
            type="paper",
            enabled=True,
            strategy_name="strategy-two",
        )
        accounts = [self.harness.account, second_account]
        self.harness.state.replace_accounts("paper", accounts)
        self.harness.registry.refresh(accounts, "paper")
        client, csrf = self.harness.login()
        page = client.get("/onboarding")
        token = _form_hidden(page, "/onboarding/preview", "request_token")

        first = client.post(
            "/onboarding/preview",
            data={"csrf_token": csrf, "request_token": token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        self.assertTrue(first.headers["location"].startswith("/work/submissions/"))
        pending = client.get(first.headers["location"])
        self.assertEqual(pending.status_code, 200)
        self.assertIn(self.harness.account.account_id, pending.text)
        self.assertIn(second_account.account_id, pending.text)
        self.assertEqual(pending.text.count("Awaiting result"), 2)

        replay = client.post(
            "/onboarding/preview",
            data={"csrf_token": csrf, "request_token": token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(replay.headers["location"], first.headers["location"])

        asyncio.run(self.harness.work.dispatch_due())

        completed = client.get(first.headers["location"])
        self.assertEqual(completed.text.count("PREVIEWED"), 2)
        self.assertLess(
            completed.text.index(self.harness.account.account_id),
            completed.text.index(second_account.account_id),
        )
        self.assertEqual(self.harness.executor.place_calls, 0)
        self.assertEqual(self.harness.executor.cancel_calls, 0)
        self.assertEqual(len(self.harness.state.list_work_items(limit=200)), 2)

    def test_preview_requested_before_account_edit_does_not_qualify(self):
        requested = self.harness.clock.now()
        start = RunStart(
            run_id="overlapping-preview",
            batch_id="overlapping-preview-batch",
            account=self.harness.account,
            trading_mode="paper",
            run_mode=RunMode.PREVIEW,
            source=RunSource.INITIAL,
            requested_at=utc_now_text(requested),
        )
        self.harness.state.start_runs([start])
        changed = self.harness.account.model_copy(update={"strategy_name": "strategy-two"})
        self.harness.state.replace_accounts(
            "paper", [changed], now=requested + timedelta(seconds=1)
        )
        self.harness.registry.refresh([changed], "paper")
        finished = utc_now_text(requested + timedelta(seconds=2))
        record = RunRecord(
            run_id=start.run_id,
            account_id=start.account.account_id,
            strategy_name=start.account.strategy_name,
            mode=RunMode.PREVIEW,
            source=RunSource.INITIAL,
            started_at=utc_now_text(requested),
            finished_at=finished,
            outcome=RunOutcome.PREVIEWED,
        )
        self.harness.state.finish_run(
            AccountRunResult(
                run_id=start.run_id,
                account_id=start.account.account_id,
                strategy_name=start.account.strategy_name,
                mode=RunMode.PREVIEW,
                source=RunSource.INITIAL,
                outcome=RunOutcome.PREVIEWED,
                record=record,
            )
        )
        client, csrf = self.harness.login()

        response = client.post(
            "/onboarding/finish",
            data={"csrf_token": csrf, "request_token": "overlap-finish"},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("after its latest configuration save", response.text)
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

    def test_different_token_onboarding_finish_returns_the_original_receipt(self):
        second_account = AccountConfig(
            account_id="U300002",
            type="paper",
            enabled=True,
            strategy_name="strategy-two",
        )
        accounts = [self.harness.account, second_account]
        self.harness.state.replace_accounts("paper", accounts)
        self.harness.registry.refresh(accounts, "paper")
        self.harness.record_successful_preview()
        self.harness.record_successful_preview(second_account)
        client, csrf = self.harness.login()
        page = client.get("/onboarding")
        token = _form_hidden(page, "/onboarding/finish", "request_token")
        first = client.post(
            "/onboarding/finish",
            data={"csrf_token": csrf, "request_token": token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        asyncio.run(self.harness.work.dispatch_due())
        replay_page = client.get("/onboarding")
        replay_token = _form_hidden(
            replay_page, "/onboarding/finish", "request_token"
        )
        self.assertNotEqual(replay_token, token)
        replay = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": replay_token,
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay.headers["location"], first.headers["location"])
        initial = [
            item
            for item in self.harness.state.list_work_items(limit=200)
            if item.action == WorkAction.REBALANCE
        ]
        self.assertEqual(len(initial), 2)
        self.assertEqual(
            {item.account_id for item in initial},
            {self.harness.account.account_id, second_account.account_id},
        )
        self.assertEqual(len({item.request_token_hash for item in initial}), 2)

    def test_pruned_initial_work_keeps_archived_submission_receipt(self):
        self.harness.record_successful_preview()
        client, csrf = self.harness.login()
        page = client.get("/onboarding")
        first = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": _form_hidden(
                    page, "/onboarding/finish", "request_token"
                ),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(first.status_code, 303)
        asyncio.run(self.harness.work.dispatch_due())
        self.harness.state.prune_terminal(
            datetime.now(timezone.utc) + timedelta(days=31)
        )
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

        archived = client.get(first.headers["location"])

        self.assertEqual(archived.status_code, 200)
        self.assertIn("Current Work and Run history has expired", archived.text)
        self.assertIn(ACCOUNT_ID, archived.text)
        self.assertIn("History expired", archived.text)

        replay_page = client.get("/onboarding")
        replay = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": _form_hidden(
                    replay_page, "/onboarding/finish", "request_token"
                ),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(replay.status_code, 303)
        self.assertEqual(replay.headers["location"], first.headers["location"])
        self.assertEqual(self.harness.state.list_work_items(limit=200), [])

    def test_real_account_configuration_change_requires_new_preview_and_initialization(self):
        self.harness.record_successful_preview()
        client, csrf = self.harness.login()
        page = client.get("/onboarding")
        first = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": _form_hidden(
                    page, "/onboarding/finish", "request_token"
                ),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        asyncio.run(self.harness.work.dispatch_due())
        changed_at = self.harness.clock.now() + timedelta(minutes=1)
        changed = self.harness.account.model_copy(
            update={"strategy_name": "strategy-two"}
        )
        self.harness.state.replace_accounts("paper", [changed], now=changed_at)
        self.harness.registry.refresh([changed], "paper")

        blocked_page = client.get("/onboarding")
        blocked = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": _form_hidden(
                    blocked_page, "/onboarding/finish", "request_token"
                ),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("successful Preview", blocked.text)

        self.harness.clock.current = changed_at + timedelta(minutes=1)
        self.harness.record_successful_preview(changed, run_id="changed-preview")
        ready_page = client.get("/onboarding")
        second = client.post(
            "/onboarding/finish",
            data={
                "csrf_token": csrf,
                "request_token": _form_hidden(
                    ready_page, "/onboarding/finish", "request_token"
                ),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        self.assertEqual(second.status_code, 303)
        self.assertNotEqual(second.headers["location"], first.headers["location"])
        initial = [
            item
            for item in self.harness.state.list_work_items(limit=200)
            if item.action == WorkAction.REBALANCE
        ]
        self.assertEqual(len(initial), 2)

    def test_closed_market_onboarding_finish_persists_scheduled_receipt(self):
        closed = DashboardHarness(market_open=False)
        try:
            client, csrf = closed.login()
            page = client.get("/onboarding")
            next_run = utc_now_text(
                closed.clock.next_scheduled_run(closed.clock.now())
            )
            expected_time = format_eastern_time(next_run)
            self.assertIn(expected_time, page.text)
            self.assertIn("(next configured XNYS time)", page.text)
            self.assertNotIn("(now)", page.text)

            saved = client.post(
                "/onboarding/save",
                data={
                    "csrf_token": csrf,
                    "selected_account_id": ACCOUNT_ID,
                    f"enabled_{ACCOUNT_ID}": "on",
                    f"strategy_{ACCOUNT_ID}": "strategy-one",
                },
                headers={"Origin": ORIGIN},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertIn(expected_time, saved.text)
            self.assertIn("(next configured XNYS time)", saved.text)
            self.assertNotIn("(now)", saved.text)

            closed.record_successful_preview()
            token = _form_hidden(saved, "/onboarding/finish", "request_token")

            response = client.post(
                "/onboarding/finish",
                data={"csrf_token": csrf, "request_token": token},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 303)
            items = closed.state.list_work_items(limit=200)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].source, WorkSource.SCHEDULED)
            self.assertEqual(items[0].execute_at, next_run)
            self.assertTrue(items[0].execute_at.endswith("Z"))
            receipt = client.get(response.headers["location"])
            self.assertIn(expected_time, receipt.text)
            self.assertIn("next market open", receipt.text)
        finally:
            closed.close()

    def test_onboarding_status_follows_current_mode_accounts(self):
        client, _csrf = self.harness.login()
        before = client.get("/")
        self.assertIn("Onboarding: complete", before.text)

        self.harness.state.replace_accounts("paper", [])
        self.harness.registry.refresh([], "paper")

        after = client.get("/")
        self.assertIn("Onboarding: required", after.text)
        self.assertIn('href="/onboarding"', after.text)

    def test_unknown_submission_is_404_without_state_leak(self):
        client, _csrf = self.harness.login()
        response = client.get("/work/submissions/unknown-submission")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.text, "Not found.")

    def test_unknown_account_action_post_is_404_without_state_change(self):
        client, csrf = self.harness.login()
        before = self.harness.state.list_work_items(limit=200)

        response = client.post(
            "/accounts/U999999/preview",
            data={"csrf_token": csrf, "request_token": "unknown-account-action"},
            headers={"Origin": ORIGIN},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.text, "Not found.")
        self.assertEqual(self.harness.state.list_work_items(limit=200), before)

    def test_account_save_recalculates_after_pending_work_is_cancelled(self):
        class RecordingScheduler:
            def __init__(self):
                self.calls = 0

            def recalculate(self):
                self.calls += 1

        scheduler = RecordingScheduler()
        self.harness.work.set_scheduler(scheduler)
        self.harness.work.submit(
            ACCOUNT_ID,
            WorkAction.REBALANCE,
            "pending-before-disable",
            schedule_next_open=True,
        )
        scheduler.calls = 0

        disabled = self.harness.account.model_copy(update={"enabled": False})
        asyncio.run(self.harness.application.replace_accounts([disabled]))

        self.assertEqual(scheduler.calls, 1)
        self.assertEqual(
            self.harness.state.list_work_items(statuses=[WorkStatus.PENDING]),
            [],
        )

    def test_full_onboarding_flow_saves_previews_and_creates_initial_work(self):
        fresh = DashboardHarness(include_account=False)
        try:
            client, csrf_token = fresh.login()
            discovery = client.post(
                "/onboarding/discover",
                data={"csrf_token": csrf_token},
                headers={"Origin": ORIGIN},
            )
            self.assertEqual(discovery.status_code, 200)

            saved = client.post(
                "/onboarding/save",
                data={
                    "csrf_token": csrf_token,
                    "selected_account_id": ACCOUNT_ID,
                    f"enabled_{ACCOUNT_ID}": "on",
                    f"strategy_{ACCOUNT_ID}": "strategy-one",
                },
                headers={"Origin": ORIGIN},
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(
                [account.account_id for account in fresh.state.list_accounts("paper")],
                [ACCOUNT_ID],
            )

            onboarding = client.get("/onboarding")
            preview = client.post(
                "/onboarding/preview",
                data={
                    "csrf_token": _hidden(onboarding, "csrf_token"),
                    "request_token": _form_hidden(
                        onboarding, "/onboarding/preview", "request_token"
                    ),
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(preview.status_code, 303)
            asyncio.run(fresh.work.dispatch_due())
            self.assertEqual(fresh.executor.place_calls, 0)
            self.assertEqual(fresh.executor.cancel_calls, 0)
            self.assertEqual(fresh.state.list_runs(limit=1)[0].outcome, RunOutcome.PREVIEWED)

            finish_page = client.get("/onboarding")
            finished = client.post(
                "/onboarding/finish",
                data={
                    "csrf_token": _hidden(finish_page, "csrf_token"),
                    "request_token": _form_hidden(
                        finish_page, "/onboarding/finish", "request_token"
                    ),
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(finished.status_code, 303)
            pending = [
                item
                for item in fresh.state.list_work_items(limit=200)
                if item.status == WorkStatus.PENDING
            ]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].action, WorkAction.REBALANCE)
            self.assertEqual(pending[0].source, WorkSource.INITIAL)
        finally:
            fresh.close()


class DashboardSessionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.harness = DashboardHarness()

    def tearDown(self):
        self.harness.close()

    def test_logout_expiry_and_restart_invalidate_session_cookie(self):
        client, csrf_token = self.harness.login()
        old_cookie = client.cookies.get(COOKIE_NAME)
        self.assertIsNotNone(old_cookie)
        response = client.post(
            "/logout",
            data={"csrf_token": csrf_token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        replay = self.harness.client()
        replay.cookies.set(COOKIE_NAME, old_cookie)
        self.assertEqual(replay.get("/accounts", follow_redirects=False).status_code, 303)

        now = datetime.now(timezone.utc)
        for label, last_seen, absolute_expires in (
            ("idle", now - timedelta(minutes=31), now + timedelta(hours=1)),
            ("absolute", now, now - timedelta(seconds=1)),
        ):
            raw = f"expired-{label}-session"
            session_hash = hashlib.sha256(raw.encode()).hexdigest()
            self.harness.state.create_session(
                WebSession(
                    session_hash=session_hash,
                    csrf_hash=hashlib.sha256(b"unused-csrf").hexdigest(),
                    authenticated=True,
                    created_at=utc_now_text(now - timedelta(hours=1)),
                    last_seen_at=utc_now_text(last_seen),
                    absolute_expires_at=utc_now_text(absolute_expires),
                )
            )
            expired_client = self.harness.client()
            expired_client.cookies.set(COOKIE_NAME, raw)
            with self.subTest(expiry=label):
                self.assertEqual(
                    expired_client.get("/accounts", follow_redirects=False).status_code, 303
                )
                self.assertIsNone(self.harness.state.get_session(session_hash))

        restarted, _ = self.harness.login()
        restart_cookie = restarted.cookies.get(COOKIE_NAME)
        self.assertEqual(self.harness.state.invalidate_sessions(), 1)
        after_restart = self.harness.client()
        after_restart.cookies.set(COOKIE_NAME, restart_cookie)
        self.assertEqual(
            after_restart.get("/accounts", follow_redirects=False).status_code, 303
        )

    def test_stale_session_and_login_failure_rows_are_pruned_by_requests(self):
        now = datetime.now(timezone.utc)
        stale_raw = "stale-unrelated-session"
        stale_hash = hashlib.sha256(stale_raw.encode()).hexdigest()
        self.harness.state.create_session(
            WebSession(
                session_hash=stale_hash,
                csrf_hash=hashlib.sha256(b"stale-csrf").hexdigest(),
                authenticated=False,
                created_at=utc_now_text(now - timedelta(days=2)),
                last_seen_at=utc_now_text(now - timedelta(days=2)),
                absolute_expires_at=utc_now_text(now - timedelta(days=1)),
            )
        )
        stale_client_key = "f" * 64
        self.harness.state.record_login_failure(stale_client_key, now - timedelta(hours=26))

        client = self.harness.client()
        login = client.get("/login")
        self.assertIsNone(self.harness.state.get_session(stale_hash))
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": PASSWORD,
                "csrf_token": _hidden(login, "csrf_token"),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self.harness.state.get_login_failure(stale_client_key))


class DashboardDeploymentSecurityTests(unittest.TestCase):
    def test_railway_uses_only_valid_real_ip_and_vps_uses_socket_peer(self):
        railway = DashboardHarness(railway_environment_id="railway-environment")
        vps = DashboardHarness()
        try:
            valid = _request(
                peer="10.0.0.5",
                headers=[
                    (b"x-real-ip", b"203.0.113.9"),
                    (b"x-forwarded-for", b"198.51.100.1"),
                ],
            )
            self.assertEqual(
                railway.auth.client_key(valid),
                hashlib.sha256(b"203.0.113.9").hexdigest(),
            )
            invalid = _request(
                peer="10.0.0.6",
                headers=[
                    (b"x-real-ip", b"not-an-ip"),
                    (b"x-forwarded-for", b"203.0.113.10"),
                ],
            )
            missing = _request(
                peer="10.0.0.7", headers=[(b"x-forwarded-for", b"203.0.113.11")]
            )
            unknown = hashlib.sha256(b"railway-unknown-client").hexdigest()
            self.assertEqual(railway.auth.client_key(invalid), unknown)
            self.assertEqual(railway.auth.client_key(missing), unknown)

            spoofed = _request(
                peer="192.0.2.44",
                headers=[
                    (b"x-real-ip", b"203.0.113.12"),
                    (b"x-forwarded-for", b"203.0.113.13"),
                ],
            )
            self.assertEqual(
                vps.auth.client_key(spoofed),
                hashlib.sha256(b"192.0.2.44").hexdigest(),
            )
        finally:
            railway.close()
            vps.close()

    def test_live_rebalance_requires_explicit_acknowledgement(self):
        harness = DashboardHarness(trading_mode="live")
        try:
            client, csrf_token = harness.login()
            token = _hidden(
                client.get(f"/accounts/{ACCOUNT_ID}/rebalance"), "request_token"
            )
            rejected = client.post(
                f"/accounts/{ACCOUNT_ID}/rebalance",
                data={"csrf_token": csrf_token, "request_token": token},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(harness.state.list_work_items(limit=200), [])

            accepted = client.post(
                f"/accounts/{ACCOUNT_ID}/rebalance",
                data={
                    "csrf_token": csrf_token,
                    "request_token": token,
                    "live_acknowledgement": "on",
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(accepted.status_code, 303)
            self.assertEqual(len(harness.state.list_work_items(limit=200)), 1)
        finally:
            harness.close()

    def test_live_onboarding_finish_requires_ack_and_paper_does_not(self):
        live = DashboardHarness(trading_mode="live")
        paper = DashboardHarness(trading_mode="paper")
        try:
            live.record_successful_preview()
            client, csrf = live.login()
            page = client.get("/onboarding")
            self.assertIn(ACCOUNT_ID, page.text)
            self.assertIn("Initial rebalance", page.text)
            self.assertIn("live broker orders", page.text)
            token = _form_hidden(page, "/onboarding/finish", "request_token")
            rejected = client.post(
                "/onboarding/finish",
                data={"csrf_token": csrf, "request_token": token},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(live.state.list_work_items(limit=200), [])
            accepted = client.post(
                "/onboarding/finish",
                data={
                    "csrf_token": csrf,
                    "request_token": token,
                    "live_acknowledgement": "on",
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(accepted.status_code, 303)

            paper.record_successful_preview()
            paper_client, paper_csrf = paper.login()
            paper_page = paper_client.get("/onboarding")
            self.assertNotIn("live broker orders", paper_page.text)
            paper_result = paper_client.post(
                "/onboarding/finish",
                data={
                    "csrf_token": paper_csrf,
                    "request_token": _form_hidden(
                        paper_page, "/onboarding/finish", "request_token"
                    ),
                },
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            self.assertEqual(paper_result.status_code, 303)
        finally:
            live.close()
            paper.close()

    def test_run_history_filters_and_reaches_the_second_page(self):
        harness = DashboardHarness()
        try:
            for index in range(55):
                harness.record_successful_preview(run_id=f"history-{index:03d}")
            client, _csrf = harness.login()
            query = (
                f"account_id={ACCOUNT_ID}&outcome=PREVIEWED&source=initial"
                "&since=2026-11-01"
            )

            first = client.get(f"/runs?{query}")
            self.assertEqual(first.status_code, 200)
            for label in ("Account", "Outcome", "Source", "Date on or after"):
                self.assertIn(label, first.text)
            self.assertIn('name="account_id" value="U300001"', first.text)
            self.assertIn('value="PREVIEWED" selected', first.text)
            self.assertIn('value="initial" selected', first.text)
            self.assertIn('name="since" value="2026-11-01"', first.text)
            self.assertIn("page=2", first.text)
            self.assertNotIn("history-000", first.text)

            second = client.get(f"/runs?{query}&page=2")
            self.assertEqual(second.status_code, 200)
            self.assertIn("Page 2", second.text)
            self.assertIn("history-000", second.text)
            self.assertIn("account_id=U300001", second.text)
            self.assertIn("outcome=PREVIEWED", second.text)
            self.assertIn("source=initial", second.text)
            self.assertIn("since=2026-11-01", second.text)
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
