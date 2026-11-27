"""Account configuration and migration-only YAML compatibility."""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ibkr_connector import DISCOVERY_CLIENT_ID
from rebalance_calculator import REPLACEMENT_SETS

_ACCOUNT_ID_RE = re.compile(r"^[A-Z0-9]+$")
_STRATEGY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MAX_CLIENT_ID = DISCOVERY_CLIENT_ID

_REMOVED_ACCOUNT_KEYS = {
    "cash_reserve_percent": "'cash_reserve_percent' is no longer supported. Delete it; "
    "the cash floor now uses the application default.",
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
        client_id = int("".join(ch for ch in v if ch.isdigit()))
        if client_id > _MAX_CLIENT_ID:
            raise ValueError(f"derived IBKR client ID {client_id} is too large")
        if client_id == DISCOVERY_CLIENT_ID:
            raise ValueError(
                f"derived IBKR client ID {DISCOVERY_CLIENT_ID} is reserved for discovery"
            )
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

    @property
    def account_type(self) -> Literal["paper", "live"]:
        """Storage-facing name for the legacy import field ``type``."""
        return self.type


class AccountsFile(BaseModel):
    """The `accounts:` list (ACC-1)."""

    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountConfig] = Field(default_factory=list)


class AccountRegistry:
    """Mutable in-memory snapshot of active Accounts for the current mode."""

    def __init__(self, active: list[AccountConfig] | None = None):
        self.replace_active(active or [])

    @classmethod
    def from_accounts(
        cls, accounts: list[AccountConfig], trading_mode: str
    ) -> "AccountRegistry":
        return cls([a for a in accounts if a.type == trading_mode and a.enabled])

    def replace_active(self, active: list[AccountConfig]) -> None:
        self._active = list(active)
        self._by_id = {a.account_id: a for a in self._active}

    def refresh(self, accounts: list[AccountConfig], trading_mode: str) -> None:
        self.replace_active(
            [a for a in accounts if a.type == trading_mode and a.enabled]
        )

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


def parse_accounts_yaml(document: str) -> list[AccountConfig]:
    """Validate the complete legacy ``accounts.yaml`` document.

    The runtime never reads this format.  It is retained solely for the
    dashboard's one-time import and export operations.
    """
    try:
        raw = yaml.safe_load(document) or {}
    except yaml.YAMLError:
        raise ValueError("Account import: invalid YAML document.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
        raise ValueError("Account import: expected a top-level 'accounts' list.")

    problems: list[str] = []
    entries = raw["accounts"]
    seen_ids: dict[str, list[int]] = {}
    configs: list[tuple[int, AccountConfig | None, dict]] = []

    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"Account import: account #{n} (?) is not a mapping.")
            continue
        aid = _account_id_of(entry)
        label = f"account #{n} ({aid})"

        for key in entry:
            if key in _REMOVED_ACCOUNT_KEYS:
                problems.append(
                    f"Account import: {label}: {_REMOVED_ACCOUNT_KEYS[key]}"
                )
            elif key not in _ACCOUNT_KEYS:
                problems.append(
                    f"Account import: {label}: unknown setting '{key}'."
                )

        rs = entry.get("replacement_set")
        if isinstance(rs, str) and rs not in REPLACEMENT_SETS:
            valid = ", ".join(sorted(REPLACEMENT_SETS))
            problems.append(
                f"Account import: {label}: replacement_set '{rs}' does not exist. "
                f"Valid: {valid}."
            )

        if aid != "?":
            seen_ids.setdefault(aid, []).append(n)

        cfg = None
        try:
            cfg = AccountConfig(**entry)
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            problems.append(
                f"Account import: {label}: {_format_pydantic_error(exc)}"
            )
        configs.append((n, cfg, entry))

    # duplicate account IDs across the file
    for aid, positions in seen_ids.items():
        if len(positions) > 1:
            pos = ", ".join(f"#{p}" for p in positions)
            problems.append(f"Account import: account '{aid}' is listed more than once ({pos}).")

    if problems:
        raise ValueError("\n".join(problems))

    parsed = [cfg for _n, cfg, _entry in configs if cfg is not None]
    validate_account_set(parsed)
    return parsed


def validate_account_set(
    accounts: list[AccountConfig], *, expected_mode: str | None = None
) -> None:
    """Validate one complete submitted Account set before persistence."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    clients: dict[tuple[str, int], str] = {}
    for account in accounts:
        if expected_mode is not None and account.type != expected_mode:
            problems.append(
                f"Account '{account.account_id}' does not belong to {expected_mode} mode."
            )
        if account.account_id in seen_ids:
            problems.append(f"Account '{account.account_id}' is listed more than once.")
        seen_ids.add(account.account_id)
        key = (account.type, account.client_id)
        other = clients.get(key)
        if other is not None and other != account.account_id:
            problems.append(
                f"Accounts '{other}' and '{account.account_id}' produce the same "
                f"IBKR client ID {account.client_id}."
            )
        clients[key] = account.account_id
        if account.replacement_set not in (None, *REPLACEMENT_SETS.keys()):
            problems.append(
                f"Account '{account.account_id}' has an unknown Replacement Set."
            )
    if problems:
        raise ValueError("\n".join(problems))


def export_accounts_yaml(accounts: list[AccountConfig]) -> str:
    """Serialize all stored paper/live Accounts in the legacy interchange shape."""
    ordered = sorted(accounts, key=lambda account: (account.type, account.account_id))
    payload = {
        "accounts": [
            account.model_dump(mode="json", exclude_none=True) for account in ordered
        ]
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _format_pydantic_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if isinstance(exc, Exception) and msg:
        return msg
    return "invalid account definition"
