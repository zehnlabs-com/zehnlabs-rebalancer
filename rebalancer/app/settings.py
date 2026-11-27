"""Validated, environment-backed deployment settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app_config import DEFAULT_ALLOCATIONS_BASE_URL


DEFAULT_PORT = 18778
DOCUMENTED_PASSWORD_EXAMPLE = "replace-with-a-strong-random-password"
MIN_DASHBOARD_PASSWORD_LENGTH = 16
_UNSAFE_PASSWORDS = {
    "admin",
    "password",
    "changeme",
    DOCUMENTED_PASSWORD_EXAMPLE,
    "replace-with-a-unique-random-password",
    "your_dashboard_admin_password",
}
_UNSAFE_PASSWORDS_CASEFOLDED = frozenset(
    candidate.casefold() for candidate in _UNSAFE_PASSWORDS
)


def _validated_dashboard_password(value: str) -> str:
    if (
        not value.strip()
        or len(value) < MIN_DASHBOARD_PASSWORD_LENGTH
        or value.casefold() in _UNSAFE_PASSWORDS_CASEFOLDED
    ):
        raise ValueError(
            "DASHBOARD_ADMIN_PASSWORD is required, must be at least 16 characters, "
            "and must not be a known default"
        )
    return value


class RuntimeSettings(BaseModel):
    """Immutable parent settings; operational credentials may be absent.

    A missing Zehnlabs key is a readiness problem, not a web-process startup
    failure. The dashboard password is different: it protects the management
    surface and must be safe before the listener starts.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
    )

    trading_mode: Literal["paper", "live"] = "paper"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    dashboard_admin_password: str = Field(
        repr=False,
        min_length=MIN_DASHBOARD_PASSWORD_LENGTH,
        max_length=1024,
    )
    ib_host: str = "ibkr-gateway"
    ib_port: int | None = Field(default=None, ge=1, le=65535)
    allocations_base_url: str = DEFAULT_ALLOCATIONS_BASE_URL
    zehnlabs_api_key: str = Field(default="", repr=False)
    user_notifications_enabled: bool = False
    user_notifications_channel: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    railway_environment_id: str = ""

    def for_worker(self) -> "WorkerSettings":
        """Return only values required by broker worker subprocesses."""
        return WorkerSettings(
            trading_mode=self.trading_mode,
            ib_host=self.ib_host,
            ib_port=self.ib_port,
            allocations_base_url=self.allocations_base_url,
            zehnlabs_api_key=self.zehnlabs_api_key,
        )

    @field_validator("dashboard_admin_password")
    @classmethod
    def _safe_dashboard_password(cls, value: str) -> str:
        return _validated_dashboard_password(value)

    def __repr__(self) -> str:
        masked_key = "<redacted>" if self.zehnlabs_api_key else "<missing>"
        return (
            "RuntimeSettings("
            f"base_url={self.allocations_base_url!r}, api_key={masked_key!r}, "
            f"mode={self.trading_mode!r}, port={self.port!r})"
        )


class WorkerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    trading_mode: Literal["paper", "live"]
    ib_host: str
    ib_port: int | None = Field(default=None, ge=1, le=65535)
    allocations_base_url: str
    zehnlabs_api_key: str = Field(repr=False)


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _port(environ: Mapping[str, str], name: str, default: int | None) -> int | None:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer from 1 to 65535") from None
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be an integer from 1 to 65535")
    return value


def build_settings(environ: Mapping[str, str] | None = None) -> RuntimeSettings:
    """Read deployment settings once without disclosing rejected values."""

    env = os.environ if environ is None else environ
    password = _validated_dashboard_password(
        env.get("DASHBOARD_ADMIN_PASSWORD", "")
    )

    trading_mode = env.get("TRADING_MODE", "paper").strip().lower()
    if trading_mode not in ("paper", "live"):
        raise ValueError("TRADING_MODE must be 'paper' or 'live'")

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")

    return RuntimeSettings(
        trading_mode=trading_mode,
        port=_port(env, "PORT", DEFAULT_PORT),
        dashboard_admin_password=password,
        ib_host=env.get("IB_HOST", "ibkr-gateway"),
        ib_port=_port(env, "IB_PORT", None),
        allocations_base_url=env.get(
            "ALLOCATIONS_BASE_URL", DEFAULT_ALLOCATIONS_BASE_URL
        ),
        zehnlabs_api_key=env.get("ZEHNLABS_API_KEY", ""),
        user_notifications_enabled=_env_bool(
            env.get("USER_NOTIFICATIONS_ENABLED"), False
        ),
        user_notifications_channel=env.get("USER_NOTIFICATIONS_CHANNEL", ""),
        log_level=log_level,
        railway_environment_id=env.get("RAILWAY_ENVIRONMENT_ID", ""),
    )
