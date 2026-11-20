"""Allocations client (PRD §8.2)."""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import aiohttp

from rebalance_calculator import RawAllocation

logger = logging.getLogger(__name__)


class AllocationsUnavailable(Exception):
    """Raised when allocations cannot be fetched (ALC-3: status code only, no body)."""


class AllocationsClient:
    """Owns one aiohttp session shared by all of a worker's accounts."""

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AllocationsClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, strategy: str) -> List[RawAllocation]:
        session = self._session
        if session is None:
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (no session)"
            )
        url = f"{self._base_url}/{strategy}/allocations"
        try:
            async with session.get(url, headers={"x-api-key": self._api_key}) as resp:
                if resp.status != 200:
                    # ALC-3: status code only; never the body (it may echo credentials).
                    raise AllocationsUnavailable(
                        f"Allocations API unavailable for {strategy} (HTTP {resp.status})"
                    )
                payload = await resp.json()
        except asyncio.TimeoutError:
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (timeout)"
            ) from None
        except AllocationsUnavailable:
            raise
        except aiohttp.ClientError:
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (network error)"
            ) from None
        except ValueError:
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (invalid response)"
            ) from None

        # ALC-2: body must be {status: "success", data: {allocations: [...]}}.
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (invalid response)"
            )
        data = payload.get("data")
        allocations = data.get("allocations") if isinstance(data, dict) else None
        if not isinstance(allocations, list):
            raise AllocationsUnavailable(
                f"Allocations API unavailable for {strategy} (invalid response)"
            )
        result: List[RawAllocation] = []
        for item in allocations:
            if not isinstance(item, dict):
                continue
            symbol = item.get("ticker")
            if symbol is None:
                symbol = item.get("symbol")
            allocation = item.get("allocation")
            result.append(RawAllocation(
                symbol=symbol,
                weight=float(allocation) if allocation is not None else None,
            ))
        return result