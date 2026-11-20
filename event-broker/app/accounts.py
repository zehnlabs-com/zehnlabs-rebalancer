"""Strict accounts.yaml parsing (PRD §5.2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rebalance_calculator import REPLACEMENT_SETS

_ACCOUNT_ID_RE = re.compile(r"^[A-Z0-9]+$")
_STRATEGY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_REMOVED_ACCOUNT_KEYS = {
    "cash_reserve_percent": "'cash_reserve_percent' is no longer supported. Delete it; "
    "the cash floor is now 'trading.minimum_cash_reserve_percent' in config.yaml.",
    "pdt_protection_enabled": "'pdt_protection_enabled' is no longer supported "
    "(PDT protection was removed). Delete it.",
}

_ACCOUNT_KEYS = {"account_id", "type", "enabled", "strategy_name", "replacement_set", "broker"}


class AccountConfig(BaseModel):
    """One trading account (ACC-1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    type: Literal["paper", "live"]
    enabled: bool = True
    strategy_name: str
    replacement_set: str | None = None
    broker: Literal["ibkr"] = "ibkr"

    @field_validator("account_id")
    @classmethod
    def _validate_account_id(cls, v: str) -> str:
        if not _ACCOUNT_ID_RE.fullmatch(v):
            raise ValueError("account_id must match ^[A-Z0-9]+$")
        if not any(ch.isdigit() for ch in v):
            raise ValueError("account_id must contain at least one digit")
        return v

    @field_validator("strategy_name")
    @classmethod
    def _validate_strategy(cls, v: str) -> str:
        if not _STRATEGY_RE.fullmatch(v):
            raise ValueError("strategy_name must match ^[a-z0-9]+(-[a-z0-9]+)*$")
        return v

    @property
    def client_id(self) -> int:
        """The IBKR client ID: int of the digits in the account ID (existing rule)."""
        digits = "".join(ch for ch in self.account_id if ch.isdigit())
        return int(digits)


class AccountsFile(BaseModel):
    """The `accounts:` list (ACC-1)."""

    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountConfig] = Field(default_factory=list)


class AccountRegistry:
    """Holds only the active accounts (enabled and type == trading_mode)."""

    def __init__(self, active: list[AccountConfig]):
        self._active = active
        self._by_id = {a.account_id: a for a in active}

    def get(self, account_id: str) -> AccountConfig | None:
        return self._by_id.get(account_id)

    def for_strategy(self, name: str) -> list[AccountConfig]:
        return [a for a in self._active if a.strategy_name == name]

    def strategies(self) -> list[str]:
        return sorted({a.strategy_name for a in self._active})

    @property
    def accounts(self) -> list[AccountConfig]:
        return list(self._active)


def _account_id_of(entry: dict) -> str:
    aid = entry.get("account_id")
    return aid if isinstance(aid, str) else "?"


def load_accounts(path: Path, trading_mode: str) -> AccountRegistry:
    """Validate the whole file (including disabled/other-mode accounts) and fail once.

    ACC-2: collects every problem and raises ValueError with one line per problem.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
        raise ValueError("accounts.yaml: expected a top-level 'accounts' list.")

    problems: list[str] = []
    entries = raw["accounts"]
    seen_ids: dict[str, list[int]] = {}
    configs: list[tuple[int, AccountConfig | None, dict]] = []

    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"accounts.yaml: account #{n} (?) is not a mapping.")
            continue
        aid = _account_id_of(entry)
        label = f"account #{n} ({aid})"

        for key in entry:
            if key in _REMOVED_ACCOUNT_KEYS:
                problems.append(
                    f"accounts.yaml: {label}: {_REMOVED_ACCOUNT_KEYS[key]}"
                )
            elif key not in _ACCOUNT_KEYS:
                problems.append(
                    f"accounts.yaml: {label}: unknown setting '{key}'."
                )

        rs = entry.get("replacement_set")
        if isinstance(rs, str) and rs not in REPLACEMENT_SETS:
            valid = ", ".join(sorted(REPLACEMENT_SETS))
            problems.append(
                f"accounts.yaml: {label}: replacement_set '{rs}' does not exist. "
                f"Valid: {valid}."
            )

        if aid != "?":
            seen_ids.setdefault(aid, []).append(n)

        cfg = None
        try:
            cfg = AccountConfig(**entry)
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            problems.append(
                f"accounts.yaml: {label}: {_format_pydantic_error(exc)}"
            )
        configs.append((n, cfg, entry))

    # duplicate account IDs across the file
    for aid, positions in seen_ids.items():
        if len(positions) > 1:
            pos = ", ".join(f"#{p}" for p in positions)
            problems.append(f"accounts.yaml: account '{aid}' is listed more than once ({pos}).")

    if problems:
        raise ValueError("\n".join(problems))

    # client ID range + duplicates among accounts active for this trading mode
    active: list[AccountConfig] = []
    for _n, cfg, _entry in configs:
        if cfg is None:
            continue
        if cfg.type == trading_mode and cfg.enabled:
            active.append(cfg)
        if cfg.client_id > 2_147_483_647:
            problems.append(
                f"accounts.yaml: account '{cfg.account_id}': client ID {cfg.client_id} is too large."
            )

    if problems:
        raise ValueError("\n".join(problems))

    by_client: dict[int, str] = {}
    for cfg in active:
        other = by_client.get(cfg.client_id)
        if other is not None and other != cfg.account_id:
            problems.append(
                f"accounts.yaml: accounts '{other}' and '{cfg.account_id}' produce the same "
                f"IBKR client ID {cfg.client_id}; they cannot both be active."
            )
            break
        by_client[cfg.client_id] = cfg.account_id

    if problems:
        raise ValueError("\n".join(problems))

    return AccountRegistry(active)


def _format_pydantic_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if isinstance(exc, Exception) and msg:
        return msg
    return "invalid account definition"