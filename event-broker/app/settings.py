"""Runtime settings read once from environment variables (PRD §5.3)."""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app_config import DEFAULT_ALLOCATIONS_BASE_URL


class RuntimeSettings(BaseModel):
    """Parent-process settings passed to workers (picklable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_mode: str = "paper"  # "paper" | "live"
    ib_host: str = "ibkr-gateway"
    ib_port: Optional[int] = None
    allocations_base_url: str = DEFAULT_ALLOCATIONS_BASE_URL
    allocations_api_key: str
    user_notifications_enabled: bool = False
    user_notifications_channel: str = ""

    def __repr__(self) -> str:
        masked = self.allocations_api_key[:3] + "..." if self.allocations_api_key else ""
        fields = self.allocations_base_url, masked, self.trading_mode
        return f"RuntimeSettings(base_url={fields[0]!r}, api_key={fields[1]!r}, mode={fields[2]!r})"


def _env_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def build_settings() -> RuntimeSettings:
    """Build RuntimeSettings from the environment (ENV-1).

    Fails if ALLOCATIONS_API_KEY is missing or blank.
    """
    api_key = os.environ.get("ALLOCATIONS_API_KEY", "")
    trading_mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if trading_mode not in ("paper", "live"):
        trading_mode = "paper"
    ib_port_raw = os.environ.get("IB_PORT")
    ib_port: Optional[int] = None
    if ib_port_raw:
        ib_port = int(ib_port_raw)

    settings = RuntimeSettings(
        trading_mode=trading_mode,
        ib_host=os.environ.get("IB_HOST", "ibkr-gateway"),
        ib_port=ib_port,
        allocations_base_url=os.environ.get("ALLOCATIONS_BASE_URL", DEFAULT_ALLOCATIONS_BASE_URL),
        allocations_api_key=api_key,
        user_notifications_enabled=_env_bool(os.environ.get("USER_NOTIFICATIONS_ENABLED"), False),
        user_notifications_channel=os.environ.get("USER_NOTIFICATIONS_CHANNEL", ""),
    )
    if not settings.allocations_api_key or not settings.allocations_api_key.strip():
        raise ValueError(
            "ALLOCATIONS_API_KEY is required (get it from /api); set it in the environment."
        )
    return settings