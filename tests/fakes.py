"""Test fakes (PRD §16.1).

Skeletons for WP1: later work packages build them out against the contracts
that land with them.

- `FakeBrokerConnection` implements the §6.2 broker contract now. WP2 adds the
  `BrokerConnection` ABC as its base (the module does not exist yet); the fake's
  methods already match its signatures so the subclass change is mechanical.
- `FakeAllocations` serves raw allocations per strategy. WP6 types it to
  `rebalance_calculator.models.RawAllocation`.
- `FakeClock` injects a controllable clock for `MarketClock` (WP6).
- `RecordingNotifier` records every `send()`. WP7 makes it subclass `Notifier`.

All fakes use only the standard library so the module imports before the later
packages exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, Collection, Sequence


class BrokerError(Exception):
    """Stand-in for broker_connector_base.errors.BrokerError.

    WP2 replaces this with the real hierarchy; tests that assert a specific
    subclass will import from the package and the fake will raise those.
    """


# --------------------------------------------------------------------------- #
# FakeClock
# --------------------------------------------------------------------------- #
class FakeClock:
    """A controllable clock standing in for `MarketClock` (PRD §10.1).

    Construct with a session window (open, close, ET) and an initial `now`.
    WP6 may reconcile it to ExchangeCalendar-driven semantics; for tests, a
    hand-set window is all the planner/run fakes need.
    """

    def __init__(
        self,
        now: datetime,
        open_time: time = time(9, 30),
        close_time: time = time(16, 0),
    ):
        self._now = now
        self.open_time = open_time
        self.close_time = close_time

    def set_now(self, value: datetime) -> None:
        self._now = value

    def now(self) -> datetime:
        return self._now

    def is_open(self, now: datetime | None = None) -> bool:
        t = (now or self._now).time()
        return self.open_time <= t < self.close_time

    def is_trading_day(self, d: datetime) -> bool:
        return d.weekday() < 5

    def next_scheduled_run(self, now: datetime | None = None) -> datetime:
        n = now or self._now
        candidate = n.replace(
            hour=self.open_time.hour, minute=self.open_time.minute, second=0, microsecond=0
        )
        while candidate <= n or candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


# --------------------------------------------------------------------------- #
# FakeAllocations
# --------------------------------------------------------------------------- #
class FakeAllocations:
    """Serves raw allocations per strategy (PRD §8.2 / ALC-1).

    Seed with `{strategy: {symbol: weight}}`. WP6 types the return value to the
    `RawAllocation` model; the fake today returns the plain mapping so it is
    decoupled from a model that does not yet exist.
    """

    def __init__(self, data: dict[str, dict[str, float]] | None = None):
        self.data: dict[str, dict[str, float]] = data or {}
        self.fetched: list[str] = []

    async def __aenter__(self) -> "FakeAllocations":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetch(self, strategy: str) -> dict[str, float]:
        self.fetched.append(strategy)
        if strategy not in self.data:
            raise LookupError(f"no allocations for {strategy}")
        return dict(self.data[strategy])


# --------------------------------------------------------------------------- #
# FakeBrokerConnection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _FakeOrder:
    """Minimal scripted order descriptor (WP2 will use the package's models)."""

    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: float
    limit_price: float | None = None


class FakeBrokerConnection:
    """A scripted, stateful broker connection (PRD §16.1).

    Behaviours are driven per-symbol by `fill`:

        fill = {
          "SPY": ("fill", 600.0),          # fills at 600.00
          "TLT": ("partial", (3, 90.0)),   # fills 3 of the requested qty at 90.00
          "XLE": ("reject", "why not"),    # rejected, never fills
          "GLD": ("never", None),          # stays WORKING
        }

    Also scriptable: `cancel_confirmed` (if False, `cancel_own_orders` raises
    and cancels nothing) and an initial set of foreign/own working orders.
    """

    def __init__(
        self,
        account_id: str = "U123456",
        *,
        state: SimpleNamespace | None = None,
        quotes: dict[str, Any] | None = None,
        fill: dict[str, tuple[Any, ...]] | None = None,
        working: Sequence[SimpleNamespace] = (),
        cancel_confirmed: bool = True,
    ):
        self.account_id = account_id
        self._state = state or SimpleNamespace(
            cash=2500.0, net_liquidation=104000.0, positions=()
        )
        self._quotes: dict[str, Any] = dict(quotes or {})
        self._fill: dict[str, tuple[Any, ...]] = dict(fill or {})
        self._working = list(working)  # own + foreign working orders
        self._cancel_confirmed = cancel_confirmed

        self._orders: dict[str, SimpleNamespace] = {}  # handle -> order state
        self._next_handle = 0

        # call-recording for assertions
        self.connects = 0
        self.closes = 0
        self.placed: list[_FakeOrder] = []
        self.cancel_calls = 0
        self.account_state_calls = 0

    # ---- lifecycle ------------------------------------------------------ #
    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        self.closes += 1

    async def __aenter__(self) -> "FakeBrokerConnection":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ---- reads ---------------------------------------------------------- #
    async def account_state(self) -> SimpleNamespace:
        self.account_state_calls += 1
        return self._state

    async def quotes(self, symbols: Collection[str]) -> dict[str, Any]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    async def working_orders(self) -> list[SimpleNamespace]:
        return list(self._working)

    async def cancel_own_orders(self, timeout_seconds: float) -> int:
        self.cancel_calls += 1
        if not self._cancel_confirmed:
            raise BrokerError("cancel not confirmed")
        own = [o for o in self._working if getattr(o, "own", False)]
        self._working = [o for o in self._working if not getattr(o, "own", False)]
        return len(own)

    # ---- orders --------------------------------------------------------- #
    async def place(self, order: _FakeOrder) -> str:
        self.placed.append(order)
        handle = str(self._next_handle)
        self._next_handle += 1
        f = self._fill.get(order.symbol, ("fill", None))
        kind, payload = f[0], f[1] if len(f) > 1 else None
        if kind == "fill":
            price = payload if isinstance(payload, (int, float)) else 100.0
            status, filled, avg = "FILLED", order.quantity, price
        elif kind == "partial":
            filled, avg = payload
            status = "FILLED" if filled >= order.quantity else "WORKING"
        elif kind == "never":
            status, filled, avg = "WORKING", 0.0, None
        elif kind == "reject":
            status, filled, avg = "REJECTED", 0.0, None
        else:
            status, filled, avg = "WORKING", 0.0, None
        self._orders[handle] = SimpleNamespace(
            handle=handle,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            limit_price=order.limit_price,
            status=status,
            filled_quantity=filled,
            average_fill_price=avg,
            message=None if status != "REJECTED" else payload,
        )
        return handle

    async def order_states(self, handles: Sequence[str]) -> list[SimpleNamespace]:
        return [self._orders[h] for h in handles if h in self._orders]

    supports_fractional = False


# --------------------------------------------------------------------------- #
# RecordingNotifier
# --------------------------------------------------------------------------- #
class RecordingNotifier:
    """Records every `send()` call (PRD §16.1).

    WP7 makes this subclass the `Notifier` ABC; today it is a functional stub
    that records `(result, context)` pairs and never performs I/O, so the run
    path can assert the number/kind of notifications without HTTP.
    """

    def __init__(self) -> None:
        self.sends: list[tuple[Any, Any]] = []

    async def send(self, result: Any, context: Any) -> None:
        self.sends.append((result, context))

    @property
    def count(self) -> int:
        return len(self.sends)

    def results(self) -> list[Any]:
        return [r for r, _ in self.sends]