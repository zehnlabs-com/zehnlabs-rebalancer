"""Dashboard password, session, CSRF, and persistent login throttling."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request, Response

from ..settings import RuntimeSettings
from ..state.models import WebSession, parse_utc, utc_now_text
from ..state.sqlite import SQLiteState

COOKIE_NAME = "__Host-rebalancer-session"
IDLE_MINUTES = 30
ABSOLUTE_HOURS = 12


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    session: WebSession
    csrf_token: str
    set_cookie: bool = False


class AuthenticationService:
    def __init__(self, state: SQLiteState, settings: RuntimeSettings):
        self.state = state
        self.settings = settings
        self._csrf_tokens: dict[str, str] = {}

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _token() -> str:
        return secrets.token_urlsafe(32)

    def create_session(self, *, authenticated: bool) -> SessionContext:
        now = datetime.now(timezone.utc)
        session_id = self._token()
        csrf_token = self._token()
        session = WebSession(
            session_hash=self._hash(session_id),
            csrf_hash=self._hash(csrf_token),
            authenticated=authenticated,
            created_at=utc_now_text(now),
            last_seen_at=utc_now_text(now),
            absolute_expires_at=utc_now_text(now + timedelta(hours=ABSOLUTE_HOURS)),
        )
        self.state.create_session(session)
        self._csrf_tokens[session.session_hash] = csrf_token
        return SessionContext(session_id, session, csrf_token, True)

    def get_session(
        self,
        request: Request,
        *,
        ensure: bool = False,
        require_authenticated: bool = False,
        rotate_csrf: bool = False,
    ) -> Optional[SessionContext]:
        now = datetime.now(timezone.utc)
        self.state.prune_sessions(now, IDLE_MINUTES)
        raw = request.cookies.get(COOKIE_NAME, "")
        stored = self.state.get_session(self._hash(raw)) if raw else None
        if stored is None:
            if not ensure:
                return None
            context = self.create_session(authenticated=False)
            if require_authenticated:
                return None
            return context
        if require_authenticated and not stored.authenticated:
            return None
        if rotate_csrf or stored.session_hash not in self._csrf_tokens:
            csrf = self._token()
            self.state.rotate_session_csrf(stored.session_hash, self._hash(csrf), now)
            self._csrf_tokens[stored.session_hash] = csrf
            stored = self.state.get_session(stored.session_hash)
            assert stored is not None
        else:
            csrf = self._csrf_tokens[stored.session_hash]
            self.state.touch_session(stored.session_hash, now)
        return SessionContext(raw, stored, csrf, False)

    def require_post(self, request: Request, csrf_token: str) -> Optional[SessionContext]:
        context = self.get_session(request, require_authenticated=True)
        if context is None or not self.same_origin(request):
            return None
        if not secrets.compare_digest(
            context.session.csrf_hash,
            self._hash(csrf_token[:4096]),
        ):
            return None
        return context

    def validate_anonymous_post(
        self, request: Request, csrf_token: str
    ) -> Optional[SessionContext]:
        context = self.get_session(request)
        if context is None or not self.same_origin(request):
            return None
        if not secrets.compare_digest(
            context.session.csrf_hash,
            self._hash(csrf_token[:4096]),
        ):
            return None
        return context

    def login(
        self,
        context: SessionContext,
        *,
        username: str,
        password: str,
        client_key: str,
    ) -> tuple[Optional[SessionContext], int]:
        now = datetime.now(timezone.utc)
        self.state.prune_login_failures(now)
        failure = self.state.get_login_failure(client_key)
        if (
            failure is not None
            and failure.blocked_until is not None
            and parse_utc(failure.blocked_until) > now
        ):
            return None, 429
        bounded_username = username[:257]
        bounded_password = password[:1025]
        username_ok = secrets.compare_digest(bounded_username, "admin")
        password_ok = secrets.compare_digest(
            bounded_password, self.settings.dashboard_admin_password
        )
        valid_lengths = len(username) <= 256 and len(password) <= 1024
        if not (username_ok and password_ok and valid_lengths):
            recorded = self.state.record_login_failure(client_key, now)
            return None, 429 if recorded.blocked_until is not None else 401
        self.state.clear_login_failure(client_key)
        session_id = self._token()
        csrf = self._token()
        session = WebSession(
            session_hash=self._hash(session_id),
            csrf_hash=self._hash(csrf),
            authenticated=True,
            created_at=utc_now_text(now),
            last_seen_at=utc_now_text(now),
            absolute_expires_at=utc_now_text(now + timedelta(hours=ABSOLUTE_HOURS)),
        )
        self.state.rotate_session(context.session.session_hash, session)
        self._csrf_tokens.pop(context.session.session_hash, None)
        self._csrf_tokens[session.session_hash] = csrf
        return SessionContext(session_id, session, csrf, True), 303

    def logout(self, context: SessionContext) -> None:
        self.state.delete_session(context.session.session_hash)
        self._csrf_tokens.pop(context.session.session_hash, None)

    def client_key(self, request: Request) -> str:
        if self.settings.railway_environment_id:
            raw = request.headers.get("x-real-ip", "")
            try:
                identity = str(ipaddress.ip_address(raw))
            except ValueError:
                identity = "railway-unknown-client"
        else:
            identity = request.client.host if request.client is not None else "vps-unknown-client"
        return self._hash(identity)

    def same_origin(self, request: Request) -> bool:
        scheme = request.url.scheme
        if (
            self.settings.railway_environment_id
            and request.headers.get("x-forwarded-proto") == "https"
        ):
            scheme = "https"
        expected = f"{scheme}://{request.url.netloc}"
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            return f"{parsed.scheme}://{parsed.netloc}" == expected and not parsed.path.strip("/")
        referer = request.headers.get("referer")
        if referer:
            parsed = urlsplit(referer)
            return f"{parsed.scheme}://{parsed.netloc}" == expected
        return False

    @staticmethod
    def set_cookie(response: Response, context: SessionContext) -> None:
        if context.set_cookie:
            response.set_cookie(
                COOKIE_NAME,
                context.session_id,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )

    @staticmethod
    def clear_cookie(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
