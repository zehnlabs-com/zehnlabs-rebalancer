"""Pydantic models for application configuration with validation."""

import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class IBKRPortsConfig(BaseModel):
    """IBKR port configuration."""

    live_internal: int = Field(
        default=4003,
        ge=1024,
        le=65535,
        description="Internal port for IBKR Gateway live trading"
    )
    paper_internal: int = Field(
        default=4004,
        ge=1024,
        le=65535,
        description="Internal port for IBKR Gateway paper trading"
    )


class IBKRConfig(BaseModel):
    """IBKR connection configuration."""

    request_timeout_seconds: float = Field(
        default=10.0,
        ge=5.0,
        le=60.0,
        description="Timeout for IBKR API requests"
    )
    connection_timeout_seconds: int = Field(
        default=10,
        ge=5,
        le=60,
        description="Timeout for IBKR connection attempts"
    )
    connection_stabilization_delay_seconds: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        description="Wait after successful connection before making requests"
    )
    price_cache_ttl_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="How long price data is cached before refresh"
    )
    market_data_type: Literal[1, 2, 3, 4] = Field(
        default=1,
        description="Market data type: 1=live, 2=frozen, 3=delayed, 4=delayed-frozen"
    )
    synthetic_ask_offset_usd: float = Field(
        default=1.00,
        ge=0.01,
        le=10.0,
        description="When market closed, synthetic ask = bid + this offset"
    )
    order_placement_delay_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=5.0,
        description="Wait after placing order to allow processing"
    )

    # Market data retry settings (handles bid=nan when IBKR hasn't populated data yet)
    market_data_retry_delay_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="Wait time between retries when market data returns nan"
    )
    market_data_max_retries: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Maximum number of retries when market data returns nan"
    )

    ports: IBKRPortsConfig = Field(
        default_factory=IBKRPortsConfig,
        description="Port configuration for live and paper trading"
    )


class TradingConfig(BaseModel):
    """Trading financial parameters."""

    # Cash Management
    minimum_cash_reserve_usd: float = Field(
        default=100.0,
        ge=0.0,
        le=10000.0,
        description="Minimum cash reserve in USD before calculating available funds"
    )
    commission_rate: float = Field(
        default=0.01,
        ge=0.0001,
        le=0.1,
        description="Commission rate as decimal (e.g., 0.01 for 1%)"
    )
    max_account_utilization: float = Field(
        default=0.995,
        ge=0.90,
        le=0.999,
        description="Maximum percentage of account value to use (safety limit)"
    )

    # Order Execution
    order_tif: Literal["DAY", "GTC"] = Field(
        default="DAY",
        description="Time In Force for all orders. DAY=day only, GTC=good til canceled."
    )
    buy_slippage_percent: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="Slippage percentage added to ask price for buy orders"
    )
    allocation_threshold_percent: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="Skip trades if allocation within this percent of target"
    )
    order_timeout_seconds: int = Field(
        default=300,
        ge=60,
        le=600,
        description="Maximum time to wait for order completion"
    )
    order_status_check_interval_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="Delay between order status checks in wait loop"
    )
    post_completion_delay_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="Wait after all orders complete before returning"
    )

    @property
    def commission_divisor(self) -> float:
        """Convert commission rate to divisor (1 + rate)."""
        return 1.0 + self.commission_rate

    @property
    def buy_slippage_multiplier(self) -> float:
        """Convert slippage percent to multiplier (1 + percent/100)."""
        return 1.0 + (self.buy_slippage_percent / 100.0)


class ReplacementConfig(BaseModel):
    """ETF replacement settings."""

    normalization_trigger_threshold: float = Field(
        default=0.0001,
        ge=0.00001,
        le=0.001,
        description="Triggers normalization if total allocation differs from 100%"
    )
    normalization_failure_threshold: float = Field(
        default=0.01,
        ge=0.001,
        le=0.1,
        description="Warns if normalization failed to get within this threshold of 100%"
    )
    minimal_non_replaced_allocation_percent: float = Field(
        default=0.1,
        ge=0.01,
        le=0.5,
        description="Minimum allocation for non-replaced holdings (as fraction)"
    )


class PDTProtectionConfig(BaseModel):
    """PDT (Pattern Day Trader) protection settings."""

    next_execution_time: str = Field(
        default="09:30",
        description="Next allowed execution time in HH:MM format (24-hour, ET timezone)"
    )

    @field_validator("next_execution_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format is HH:MM where HH is 00-23 and MM is 00-59."""
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError(
                f"Invalid time format '{v}'. Must be HH:MM where HH is 00-23 and MM is 00-59"
            )
        return v


class ServiceConfig(BaseModel):
    """Service configuration."""

    heartbeat_interval_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Service heartbeat interval"
    )
    manual_event_check_interval_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="How often to check for manual rebalance files"
    )
    error_recovery_delay_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Wait time after error in manual event watcher"
    )
    manual_event_file_path: str = Field(
        default="/app/data/manual-rebalance/rebalance.json",
        description="File path watched for manual rebalance events"
    )


class SchedulerConfig(BaseModel):
    """Scheduler configuration for market-open rebalancing."""

    enabled: bool = Field(
        default=True,
        description="Enable/disable scheduled rebalancing"
    )
    market_open_time: str = Field(
        default="09:30",
        description="Market open time in HH:MM format (24-hour, ET timezone)"
    )
    scheduled_file_path: str = Field(
        default="/app/data/market-open/scheduled.json",
        description="File path for scheduled accounts list"
    )

    @field_validator("market_open_time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format is HH:MM where HH is 00-23 and MM is 00-59."""
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError(
                f"Invalid time format '{v}'. Must be HH:MM where HH is 00-23 and MM is 00-59"
            )
        return v


class ExecutorConfig(BaseModel):
    """Executor configuration."""

    max_workers: int = Field(
        default=32,
        ge=1,
        le=100,
        description="Maximum concurrent strategy executions"
    )


class APIConfig(BaseModel):
    """API configuration."""

    allocation_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Timeout for allocation API requests"
    )


class AppConfig(BaseModel):
    """Root application configuration."""

    ibkr: IBKRConfig = Field(
        default_factory=IBKRConfig,
        description="IBKR connection settings"
    )
    trading: TradingConfig = Field(
        default_factory=TradingConfig,
        description="Trading financial parameters"
    )
    replacement: ReplacementConfig = Field(
        default_factory=ReplacementConfig,
        description="ETF replacement settings"
    )
    pdt_protection: PDTProtectionConfig = Field(
        default_factory=PDTProtectionConfig,
        description="PDT protection settings"
    )
    service: ServiceConfig = Field(
        default_factory=ServiceConfig,
        description="Service configuration"
    )
    scheduler: SchedulerConfig = Field(
        default_factory=SchedulerConfig,
        description="Scheduler configuration for market-open rebalancing"
    )
    executor: ExecutorConfig = Field(
        default_factory=ExecutorConfig,
        description="Executor configuration"
    )
    api: APIConfig = Field(
        default_factory=APIConfig,
        description="API configuration"
    )
