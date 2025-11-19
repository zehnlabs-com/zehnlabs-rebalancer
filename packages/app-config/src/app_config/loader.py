"""Configuration loader with validation and singleton access."""

import logging
import yaml
from pathlib import Path
from typing import Optional

from .models import AppConfig

logger = logging.getLogger(__name__)

# Global config singleton
_config: Optional[AppConfig] = None


def load_config(config_path: str | Path) -> AppConfig:
    """
    Load and validate configuration from YAML file.

    Args:
        config_path: Path to config.yaml file

    Returns:
        Validated AppConfig instance

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config validation fails
        yaml.YAMLError: If YAML parsing fails
    """
    global _config

    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    logger.info(f"Loading configuration from: {config_path}")

    with open(config_path, 'r') as f:
        raw_config = yaml.safe_load(f)

    if raw_config is None:
        raw_config = {}

    try:
        _config = AppConfig(**raw_config)
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        raise ValueError(f"Invalid configuration: {e}") from e

    # Log loaded configuration for audit trail
    logger.info("Configuration loaded successfully:")
    logger.info(f"  IBKR request timeout: {_config.ibkr.request_timeout_seconds}s")
    logger.info(f"  IBKR connection timeout: {_config.ibkr.connection_timeout_seconds}s")
    logger.info(f"  Price cache TTL: {_config.ibkr.price_cache_ttl_seconds}s")
    logger.info(f"  Market data type: {_config.ibkr.market_data_type}")
    logger.info(f"  Live trading port: {_config.ibkr.ports.live_internal}")
    logger.info(f"  Paper trading port: {_config.ibkr.ports.paper_internal}")
    logger.info(f"  Minimum cash reserve: ${_config.trading.minimum_cash_reserve_usd}")
    logger.info(f"  Commission rate: {_config.trading.commission_rate * 100}%")
    logger.info(f"  Buy slippage: {_config.trading.buy_slippage_percent}%")
    logger.info(f"  Allocation threshold: {_config.trading.allocation_threshold_percent}%")
    logger.info(f"  Order timeout: {_config.trading.order_timeout_seconds}s")
    logger.info(f"  Max account utilization: {_config.trading.max_account_utilization * 100}%")
    logger.info(f"  PDT next execution time: {_config.pdt_protection.next_execution_time}")
    logger.info(f"  Max workers: {_config.executor.max_workers}")

    return _config


def get_config() -> AppConfig:
    """
    Get the current loaded configuration.

    Returns:
        Current AppConfig instance

    Raises:
        RuntimeError: If config hasn't been loaded yet
    """
    if _config is None:
        raise RuntimeError(
            "Configuration not loaded. Call load_config() first."
        )
    return _config
