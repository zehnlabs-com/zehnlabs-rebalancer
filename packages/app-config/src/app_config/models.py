"""Pydantic models for application configuration (PRD §5.1, CFG-1..2).

Every model is strict (`extra="forbid"`): unknown or removed keys fail startup.
"""

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_ALLOCATIONS_BASE_URL = "https://workers.fintech.zehnlabs.com/api/v1/strategies"


class IBKRPortsConfig(BaseModel):
    """IBKR port configuration."""

    model_config = ConfigDict(extra="forbid")

    live_internal: int = Field(
        default=4003, ge=1, le=65535,
        description="Internal port for IBKR Gateway live trading",
    )
    paper_internal: int = Field(
        default=4004, ge=1, le=65535,
        description="Internal port for IBKR Gateway paper trading",
    )


class IBKRConfig(BaseModel):
    """IBKR connection configuration (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    request_timeout_seconds: float = Field(
        default=10.0, ge=1, le=120, description="Timeout for IBKR API requests"
    )
    connection_timeout_seconds: int = Field(
        default=10, ge=1, le=120, description="Timeout for IBKR connection attempts"
    )
    connection_stabilization_delay_seconds: float = Field(
        default=0.5, ge=0, le=10,
        description="Wait after successful connection before first request",
    )
    market_data_type: Literal[1, 2, 3, 4] = Field(
        default=1, description="1 live, 2 frozen, 3 delayed, 4 delayed-frozen"
    )
    order_placement_delay_seconds: float = Field(
        default=1.0, ge=0, le=10, description="Pause after each placed order"
    )
    market_data_retry_delay_seconds: float = Field(
        default=2.0, ge=0.1, le=30, description="Wait between market-data retries"
    )
    market_data_max_retries: int = Field(
        default=10, ge=0, le=30, description="Maximum market-data retries"
    )
    ports: IBKRPortsConfig = Field(
        default_factory=IBKRPortsConfig, description="Live and paper internal ports"
    )


class TradingConfig(BaseModel):
    """Trading financial parameters (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    minimum_cash_reserve_percent: float = Field(
        default=0.25, ge=0, le=5,
        description="Cash floor as % of the account's total value",
    )
    buy_slippage_percent: float = Field(
        default=0.5, ge=0, le=5,
        description="Limit price = buy reference * (1 + this/100)",
    )
    order_timeout_seconds: int = Field(
        default=300, ge=10, le=3600, description="Per-leg order completion timeout"
    )
    order_status_check_interval_seconds: float = Field(
        default=2.0, ge=0.1, le=30, description="Delay between order status polls"
    )
    cancel_confirm_timeout_seconds: int = Field(
        default=30, ge=1, le=300, description="Timeout confirming own-order cancels"
    )
    cash_update_timeout_seconds: int = Field(
        default=30, ge=0, le=300, description="Timeout waiting for cash to update after fills"
    )


class ServiceConfig(BaseModel):
    """Background service timing and operational settings (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    heartbeat_interval_seconds: float = Field(default=1.0, ge=0.1, le=10)
    manual_event_check_interval_seconds: float = Field(default=1.0, ge=0.1, le=10)
    error_recovery_delay_seconds: float = Field(default=5.0, ge=1.0, le=60)
    manual_event_file_path: str = Field(
        default="/app/data/manual-rebalance/rebalance.json",
        description="File path watched for manual rebalance events",
    )


class SchedulerConfig(BaseModel):
    """Scheduler configuration (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True)
    market_open_time: str = Field(default="09:35", description="HH:MM, ET")
    scheduled_file_path: str = Field(
        default="/app/data/market-open/scheduled.json",
        description="JSON array of account-ID strings to rebalance at open",
    )

    @field_validator("market_open_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError(f"Invalid time format '{v}'. Must be HH:MM where HH is 00-23 and MM is 00-59")
        return v


class ExecutorConfig(BaseModel):
    """Executor (process pool) configuration (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    max_workers: int = Field(default=32, ge=1, le=100)


class APIConfig(BaseModel):
    """External API configuration (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    rebalance_auth_timeout_seconds: int = Field(
        default=10, ge=1, le=120,
        description="Startup timeout for the shared read-only Ably key",
    )
    allocation_timeout_seconds: int = Field(
        default=30, ge=5, le=120, description="Timeout for allocation requests"
    )


class AppConfig(BaseModel):
    """Root application configuration (PRD §5.1)."""

    model_config = ConfigDict(extra="forbid")

    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    api: APIConfig = Field(default_factory=APIConfig)