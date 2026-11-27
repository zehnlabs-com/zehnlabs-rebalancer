"""Read-only managed-Account discovery for dashboard onboarding."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ib_async import IB, StartupFetch
from pydantic import BaseModel, ConfigDict


DISCOVERY_CLIENT_ID = 2_147_483_647
_ACCOUNT_ID_RE = re.compile(r"^[A-Z0-9]+$")


class AccountDiscoveryResult(BaseModel):
    """Normalized managed Accounts visible on one Gateway login."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accounts: tuple[str, ...]


class AccountDiscoveryError(RuntimeError):
    """The Gateway could not be reached or authenticated for discovery."""


class AccountDiscoveryResponseError(AccountDiscoveryError):
    """The Gateway returned an invalid managed-Account response."""


def _normalize_account(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    account_id = value.strip().upper()
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        return None
    digits = "".join(character for character in account_id if character.isdigit())
    if not digits:
        return None
    client_id = int(digits)
    if client_id >= DISCOVERY_CLIENT_ID:
        return None
    return account_id


async def discover_managed_accounts(
    *,
    host: str,
    port: int,
    timeout_seconds: float,
    ib_factory: Callable[[], Any] | None = None,
) -> AccountDiscoveryResult:
    """Connect without startup fetches and return managed Accounts.

    The reserved client ID is never available to trading connections.  This
    function performs no Account filter, position/order fetch, bind, cancel,
    or placement operation.
    """

    ib = (ib_factory or IB)()
    try:
        await ib.connectAsync(
            host=host,
            port=port,
            clientId=DISCOVERY_CLIENT_ID,
            timeout=timeout_seconds,
            readonly=True,
            account="",
            raiseSyncErrors=True,
            fetchFields=StartupFetch(0),
        )
        raw_accounts = ib.managedAccounts()
        if not isinstance(raw_accounts, (list, tuple)):
            raise AccountDiscoveryResponseError(
                "IBKR Gateway returned an invalid managed-Account response."
            )
        accounts = tuple(
            sorted(
                {
                    normalized
                    for value in raw_accounts
                    if (normalized := _normalize_account(value)) is not None
                }
            )
        )
        return AccountDiscoveryResult(accounts=accounts)
    except AccountDiscoveryError:
        raise
    except Exception as exc:
        raise AccountDiscoveryError(
            f"Could not discover Accounts through IBKR Gateway at {host}:{port}."
        ) from exc
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
