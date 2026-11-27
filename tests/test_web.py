from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.allocations import StrategyCatalogEntry
from app.services.account_manager import (
    AccountSaveResult,
    DiscoveryResult,
    ReplacementSetInfo,
)
from app.settings import RuntimeSettings
from app.state.sqlite import SQLiteState
from app.web.app import create_web_app
from app.web.auth import AuthenticationService, COOKIE_NAME


PASSWORD = "safe-dashboard-password-123"
ORIGIN = "https://testserver"


class FakeApplication:
    def __init__(self, state, settings):
        self.state = state
        self.settings = settings


class ManagementFakeApplication(FakeApplication):
    def __init__(self, state, settings):
        super().__init__(state, settings)
        self.saved = []

    async def status(self):
        return SimpleNamespace(
            readiness=SimpleNamespace(
                gateway_configured=True,
                zehnlabs_configured=True,
            )
        )

    async def discover(self):
        return DiscoveryResult(
            accounts=("U300001",),
            strategies=(
                StrategyCatalogEntry(name="strategy-one", long_name="Strategy One"),
            ),
            replacement_sets=(ReplacementSetInfo(name="ira", rules=1),),
        )

    async def replace_accounts(self, accounts):
        self.saved = list(accounts)
        return AccountSaveResult(accounts=tuple(accounts))

    def list_accounts(self):
        return list(self.saved)

    def export_accounts(self):
        return "accounts: []\n"


def request_at_proxy_boundary(
    *,
    host: str,
    origin: str,
    forwarded_proto: str | None = None,
    forwarded_host: str | None = None,
) -> Request:
    headers = [(b"host", host.encode()), (b"origin", origin.encode())]
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    if forwarded_host is not None:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "root_path": "",
            "server": ("rebalancer.railway.internal", 18778),
            "client": ("10.0.0.5", 12345),
            "headers": headers,
        }
    )


def csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "web.db")
        self.state.initialize()
        self.settings = RuntimeSettings(dashboard_admin_password=PASSWORD)
        self.auth = AuthenticationService(self.state, self.settings)
        self.client = TestClient(
            create_web_app(FakeApplication(self.state, self.settings), self.auth),
            base_url=ORIGIN,
        )
        self.addCleanup(self.client.close)

    def login_form(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        return response, csrf(response)

    def test_health_and_login_start_without_external_credentials(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        response, _ = self.login_form()
        self.assertIn("ZEHNLABS", "ZEHNLABS")
        self.assertNotIn(PASSWORD, response.text)

    def test_html_security_headers_and_session_cookie_contract(self):
        response, _ = self.login_form()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        cookie = response.headers["set-cookie"]
        self.assertIn(COOKIE_NAME, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Domain=", cookie)

    def test_login_redirect_is_not_cacheable(self):
        response, token = self.login_form()
        logged_in = self.client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "csrf_token": token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(logged_in.status_code, 303)
        self.assertEqual(logged_in.headers["cache-control"], "no-store")

    def test_wrong_username_and_password_are_indistinguishable(self):
        response, token = self.login_form()
        wrong_user = self.client.post(
            "/login",
            data={"username": "other", "password": PASSWORD, "csrf_token": token},
            headers={"Origin": ORIGIN},
        )
        self.client.cookies.clear()
        response, token = self.login_form()
        wrong_password = self.client.post(
            "/login",
            data={"username": "admin", "password": "x", "csrf_token": token},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(wrong_user.status_code, wrong_password.status_code)
        self.assertIn("Invalid username or password.", wrong_user.text)
        self.assertIn("Invalid username or password.", wrong_password.text)

    def test_login_rotates_session_and_invalid_csrf_or_origin_changes_nothing(self):
        response, token = self.login_form()
        old = self.client.cookies.get(COOKIE_NAME)
        invalid = self.client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "csrf_token": "bad"},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(invalid.status_code, 403)
        invalid_origin = self.client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "csrf_token": token},
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(invalid_origin.status_code, 403)
        good = self.client.post(
            "/login",
            data={"username": "admin", "password": PASSWORD, "csrf_token": token},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(good.status_code, 303)
        self.assertNotEqual(old, self.client.cookies.get(COOKIE_NAME))

    def test_fifth_failure_is_throttled_and_persists(self):
        for _ in range(4):
            self.client.cookies.clear()
            _response, token = self.login_form()
            response = self.client.post(
                "/login",
                data={"username": "admin", "password": "wrong", "csrf_token": token},
                headers={"Origin": ORIGIN},
            )
            self.assertEqual(response.status_code, 401)
        self.client.cookies.clear()
        _response, token = self.login_form()
        fifth = self.client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(fifth.status_code, 429)
        self.assertEqual(fifth.headers["retry-after"], "900")
        self.assertIsNotNone(SQLiteState(self.state.path).get_login_failure(next(iter(self._keys()))))

    def _keys(self):
        import sqlite3

        connection = sqlite3.connect(self.state.path)
        try:
            return [row[0] for row in connection.execute("SELECT client_key FROM login_failures")]
        finally:
            connection.close()


class OnboardingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "web.db")
        self.state.initialize()
        self.settings = RuntimeSettings(dashboard_admin_password=PASSWORD)
        self.application = ManagementFakeApplication(self.state, self.settings)
        self.auth = AuthenticationService(self.state, self.settings)
        self.client = TestClient(
            create_web_app(self.application, self.auth),
            base_url=ORIGIN,
        )
        self.addCleanup(self.client.close)

        login_form = self.client.get("/login")
        logged_in = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": PASSWORD,
                "csrf_token": csrf(login_form),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(logged_in.status_code, 303)

    def test_fresh_discovery_renders_selectable_rows_and_save_parses_selection(self):
        onboarding = self.client.get("/onboarding")
        discovered = self.client.post(
            "/onboarding/discover",
            data={"csrf_token": csrf(onboarding)},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(discovered.status_code, 200)
        self.assertIn('name="selected_account_id" value="U300001"', discovered.text)
        self.assertIn('value="strategy-one"', discovered.text)
        self.assertEqual(self.application.saved, [])

        saved = self.client.post(
            "/onboarding/save",
            data={
                "csrf_token": csrf(discovered),
                "selected_account_id": "U300001",
                "strategy_U300001": "strategy-one",
                "replacement_U300001": "ira",
                "enabled_U300001": "on",
            },
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(len(self.application.saved), 1)
        self.assertEqual(self.application.saved[0].account_id, "U300001")
        self.assertEqual(self.application.saved[0].strategy_name, "strategy-one")
        self.assertEqual(self.application.saved[0].replacement_set, "ira")

    def test_authenticated_export_and_logout_redirect_are_not_cacheable(self):
        exported = self.client.get("/accounts/export")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.headers["cache-control"], "no-store")

        accounts = self.client.get("/accounts")
        logged_out = self.client.post(
            "/logout",
            data={"csrf_token": csrf(accounts)},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        self.assertEqual(logged_out.status_code, 303)
        self.assertEqual(logged_out.headers["cache-control"], "no-store")


class ProxyOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SQLiteState(Path(self.tmp.name) / "proxy.db")
        self.state.initialize()

    def auth(self, *, railway: bool) -> AuthenticationService:
        return AuthenticationService(
            self.state,
            RuntimeSettings(
                dashboard_admin_password=PASSWORD,
                railway_environment_id="railway-environment" if railway else "",
            ),
        )

    def test_railway_accepts_https_origin_over_internal_http(self):
        request = request_at_proxy_boundary(
            host="dashboard.up.railway.app",
            origin="https://dashboard.up.railway.app",
            forwarded_proto="https",
        )
        self.assertTrue(self.auth(railway=True).same_origin(request))
        self.assertFalse(self.auth(railway=False).same_origin(request))

    def test_forwarded_host_and_malformed_proto_are_never_trusted(self):
        forwarded_host = request_at_proxy_boundary(
            host="rebalancer.railway.internal:18778",
            origin="https://dashboard.up.railway.app",
            forwarded_proto="https",
            forwarded_host="dashboard.up.railway.app",
        )
        malformed_proto = request_at_proxy_boundary(
            host="dashboard.up.railway.app",
            origin="https://dashboard.up.railway.app",
            forwarded_proto="https,http",
        )
        railway_auth = self.auth(railway=True)
        self.assertFalse(railway_auth.same_origin(forwarded_host))
        self.assertFalse(railway_auth.same_origin(malformed_proto))


if __name__ == "__main__":
    unittest.main()
