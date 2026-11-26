"""Configuration loader with strict removed/unknown-key checks (PRD §5.1 CFG-3..4)."""

import logging
import yaml
from pathlib import Path
from typing import Optional

from .models import AppConfig

logger = logging.getLogger(__name__)

# Global config singleton
_config: Optional[AppConfig] = None

REMOVED_KEY_MESSAGES = {
    "ibkr.price_cache_ttl_seconds": "(the price cache was removed). Delete this line.",
    "ibkr.synthetic_ask_offset_usd": "(prices now fall back to last/close/bar). Delete this line.",
    "trading.minimum_cash_reserve_usd": "replaced by 'trading.minimum_cash_reserve_percent'. Delete this line.",
    "trading.commission_rate": "(commissions are covered by 'trading.minimum_cash_reserve_percent'). Delete this line.",
    "trading.max_account_utilization": "(removed with buy scaling). Delete this line.",
    "trading.allocation_threshold_percent": "(every run now aligns all symbols to target). Delete this line.",
    "trading.order_tif": "(orders are always DAY). Delete this line.",
    "trading.post_completion_delay_seconds": "replaced by 'trading.cash_update_timeout_seconds'. Delete this line.",
}

REMOVED_SECTION_MESSAGES = {
    "replacement": "section 'replacement' is no longer supported (replacement sets are built in). Delete this section.",
    "pdt_protection": "section 'pdt_protection' is no longer supported (PDT protection was removed). Delete this section.",
}

# Known config structure as a nested dict: section -> allowed children.
# A child whose value is a dict is a nested section (e.g. ibkr.ports).
_KNOWN_TREE: dict[str, dict] = {
    "ibkr": {
        "request_timeout_seconds": False,
        "connection_timeout_seconds": False,
        "connection_stabilization_delay_seconds": False,
        "market_data_type": False,
        "order_placement_delay_seconds": False,
        "market_data_retry_delay_seconds": False,
        "market_data_max_retries": False,
        "ports": {"live_internal": False, "paper_internal": False},
    },
    "trading": {
        "minimum_cash_reserve_percent": False, "buy_slippage_percent": False,
        "optimization_timeout_seconds": False,
        "order_timeout_seconds": False, "order_status_check_interval_seconds": False,
        "cancel_confirm_timeout_seconds": False, "cash_update_timeout_seconds": False,
    },
    "service": {
        "heartbeat_interval_seconds": False, "manual_event_check_interval_seconds": False,
        "error_recovery_delay_seconds": False, "manual_event_file_path": False,
    },
    "scheduler": {"enabled": False, "market_open_time": False, "scheduled_file_path": False},
    "executor": {"max_workers": False},
    "api": {"rebalance_auth_timeout_seconds": False, "allocation_timeout_seconds": False},
}


def _check_tree(raw: dict, tree: dict, prefix: str, problems: list[str]) -> None:
    for key, value in raw.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if dotted in REMOVED_KEY_MESSAGES:
            problems.append(
                f"config.yaml: '{dotted}' is no longer supported {REMOVED_KEY_MESSAGES[dotted]}"
            )
            continue
        expected = tree.get(key)
        if expected is None:
            problems.append(f"config.yaml: unknown setting '{dotted}'. Check the spelling or delete it.")
            continue
        if isinstance(expected, dict):
            if isinstance(value, dict):
                _check_tree(value, expected, dotted, problems)
            else:
                problems.append(f"config.yaml: unknown setting '{dotted}'. Check the spelling or delete it.")


def _check_keys(raw: dict) -> list[str]:
    """Return a list of error message lines for removed/unknown keys (CFG-3)."""
    problems: list[str] = []
    for top, value in raw.items():
        if top in REMOVED_SECTION_MESSAGES:
            problems.append(f"config.yaml: {REMOVED_SECTION_MESSAGES[top]}")
            continue
        expected = _KNOWN_TREE.get(top)
        if expected is None:
            problems.append(f"config.yaml: unknown setting '{top}'. Check the spelling or delete it.")
            continue
        if isinstance(value, dict):
            _check_tree(value, expected, top, problems)
        else:
            problems.append(f"config.yaml: unknown setting '{top}'. Check the spelling or delete it.")
    return problems


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate configuration from a YAML file (CFG-4, CFG-3).

    Raises ValueError with one line per offending removed/unknown key before the
    model is built; pydantic range errors surface as `config.yaml: <message>`.
    """
    global _config
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r") as f:
        raw_config = yaml.safe_load(f) or {}

    problems = _check_keys(raw_config)
    if problems:
        raise ValueError("\n".join(problems))

    try:
        _config = AppConfig(**raw_config)
    except Exception as e:
        raise ValueError(f"config.yaml: {e}") from e

    # CFG-4: log the validated model once.
    logger.info("Configuration loaded: %s", _config.model_dump())
    return _config


def get_config() -> AppConfig:
    """Return the currently loaded configuration (parent process only)."""
    if _config is None:
        raise RuntimeError("Configuration not loaded. Call load_config() first.")
    return _config
