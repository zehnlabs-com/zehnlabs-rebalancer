"""Test fakes (PRD §16.1).

- `FakeBrokerConnection(BrokerConnection)` — scripted state, quotes, working
  orders, fill behaviour (fill, partial, never, reject).
- `FakeAllocations` — serves raw allocations per strategy.
- `FakeClock` — injectable clock for `MarketClock` (WP6).
- `RecordingNotifier` — records every send (WP7 makes it subclass `Notifier`).

WP1 created these as skeletons; WP2 wired `FakeBrokerConnection` to the real
`BrokerConnection` ABC and domain models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Collection, Sequence

from broker_connector_base import (
    BrokerConnection,
    CancelNotConfirmedError,
    AccountState,
    Position,
    Quote,
    OrderRequest,
    OrderState,
    OrderStatus,
    Side,
    WorkingOrder,
)
from rebalance_calculator import RawAllocation


# --------------------------------------------------------------------------- #
# FakeClock
# --------------------------------------------------------------------------- #
class FakeClock:
    """A controllable clock standing in for `MarketClock` (PRD §10.1).

    Construct with a session window (open, close, ET) and an initial `now`.
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

    Seed with `{strategy: {symbol: weight}}`; `fetch` returns the same shape as
    `AllocationsClient.fetch` — a list of `RawAllocation`.
    """

    def __init__(self, data: dict[str, dict[str, float]] | None = None):
        self.data: dict[str, dict[str, float]] = data or {}
        self.fetched: list[str] = []

    async def __aenter__(self) -> "FakeAllocations":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def fetch(self, strategy: str) -> list[RawAllocation]:
        self.fetched.append(strategy)
        if strategy not in self.data:
            raise LookupError(f"no allocations for {strategy}")
        return [RawAllocation(symbol=s, weight=w) for s, w in self.data[strategy].items()]


# --------------------------------------------------------------------------- #
# FakeBrokerConnection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _FillRule:
    """Scripted fill behaviour for a symbol.

    kind in {"fill", "partial", "never", "reject"}. payload is a fill price
    (fill), a (filled_qty, price) tuple (partial), None (never), or an error
    message (reject).
    """

    kind: str
    payload: Any = None


class FakeBrokerConnection(BrokerConnection):
    """A scripted, stateful broker connection (PRD §16.1).

    Driven per-symbol by `fill: dict[str, _FillRule | tuple]`.
    """

    supports_fractional = False

    def __init__(
        self,
        account_id: str = "U123456",
        *,
        state: AccountState | None = None,
        quotes: dict[str, Quote] | None = None,
        fill: dict[str, _FillRule | tuple] | None = None,
        working: Sequence[WorkingOrder] = (),
        cancel_confirmed: bool = True,
        cash_factor: float = 1.0,
    ):
        self.account_id = account_id
        self._state = state or AccountState(
            account_id=account_id, cash=2500.0, net_liquidation=104000.0, positions=()
        )
        self._quotes: dict[str, Quote] = dict(quotes or {})
        self._fill: dict[str, _FillRule] = {}
        for symbol, rule in (fill or {}).items():
            self._fill[symbol] = rule if isinstance(rule, _FillRule) else _FillRule(*rule)
        self._working = list(working)  # own + foreign working orders
        self._cancel_confirmed = cancel_confirmed
        self._cash_factor = cash_factor  # fraction of sell proceeds visible in cash

        self._orders: dict[str, OrderState] = {}  # handle -> order state
        self._next_handle = 0

        # call-recording for assertions
        self.connects = 0
        self.closes = 0
        self.placed: list[OrderRequest] = []
        self.cancel_calls = 0
        self.account_state_calls = 0

    # ---- lifecycle ------------------------------------------------------ #
    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        self.closes += 1

    # ---- reads ---------------------------------------------------------- #
    async def account_state(self) -> AccountState:
        self.account_state_calls += 1
        cash = self._state.cash
        for st in self._orders.values():
            if st.side == Side.SELL and st.status == OrderStatus.FILLED \
                    and st.average_fill_price is not None:
                cash += st.filled_quantity * st.average_fill_price * self._cash_factor
        return AccountState(
            account_id=self.account_id,
            cash=cash,
            net_liquidation=self._state.net_liquidation,
            positions=self._state.positions,
        )

    async def quotes(self, symbols: Collection[str]) -> dict[str, Quote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    async def working_orders(self) -> list[WorkingOrder]:
        return list(self._working)

    async def cancel_own_orders(self, timeout_seconds: float) -> int:
        self.cancel_calls += 1
        if not self._cancel_confirmed:
            raise CancelNotConfirmedError("cancel not confirmed")
        own = [o for o in self._working if o.own]
        self._working = [o for o in self._working if not o.own]
        return len(own)

    # ---- orders --------------------------------------------------------- #
    async def place(self, order: OrderRequest) -> str:
        self.placed.append(order)
        handle = str(self._next_handle)
        self._next_handle += 1
        rule = self._fill.get(order.symbol, _FillRule("fill", 100.0))
        if rule.kind == "fill":
            status = OrderStatus.FILLED
            filled, avg, message = order.quantity, rule.payload or 100.0, None
        elif rule.kind == "partial":
            loaded = rule.payload or (0.0, 100.0)
            filled, avg = loaded
            status = OrderStatus.FILLED if filled >= order.quantity else OrderStatus.WORKING
            message = None
        elif rule.kind == "never":
            status, filled, avg, message = OrderStatus.WORKING, 0.0, None, None
        else:  # reject
            status, filled, avg = OrderStatus.REJECTED, 0.0, None
            message = rule.payload
        self._orders[handle] = OrderState(
            handle=handle,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            status=status,
            filled_quantity=filled,
            average_fill_price=avg,
            message=message,
        )
        return handle

    async def order_states(self, handles: Sequence[str]) -> list[OrderState]:
        return [self._orders[h] for h in handles if h in self._orders]


# --------------------------------------------------------------------------- #
# RecordingNotifier
# --------------------------------------------------------------------------- #
class RecordingNotifier:
    """Records every `send()` call (PRD §16.1). Never performs I/O.

    WP7 makes this subclass the `Notifier` ABC.
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