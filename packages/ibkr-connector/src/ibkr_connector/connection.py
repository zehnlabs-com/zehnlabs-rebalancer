"""IBKR connection implementing `broker_connector_base.BrokerConnection` (PRD §9)."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Collection, Optional, Sequence

from ib_async import IB, LimitOrder, MarketOrder, StartupFetch, Stock

from broker_connector_base import (
    AccountState,
    BrokerConnection,
    OrderRequest,
    OrderState,
    OrderStatus,
    Position,
    Quote,
    Side,
    WorkingOrder,
    AccountDataError,
    CancelNotConfirmedError,
    ConnectionFailedError,
    NoPriceError,
    OrderPlacementError,
    PricingError,
)

logger = logging.getLogger(__name__)


def _price(value: Any) -> Optional[float]:
    """A finite number > 0, or None. NaN, 0, negatives and non-numbers become None."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value) or value <= 0:
        return None
    return value


def _parse_money(value: Any, tag: str) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = float("nan")
    if not math.isfinite(amount):
        raise AccountDataError(f"IBKR reported a non-finite {tag} value")
    return amount


class IBKRConnection(BrokerConnection):
    """One IBKR Gateway connection serving one account.

    `ibkr_config` is the app-config `IBKRConfig`; `settings` carries `ib_host`,
    `ib_port` (optional) and `trading_mode` ("paper"/"live"). The account ID is
    given to the constructor; the client ID is derived by the caller (digits of
    the account ID, §0.9 / ACC-1).
    """

    supports_fractional = False

    def __init__(
        self,
        account_id: str,
        client_id: int,
        ibkr_config: Any,
        settings: Any,
        logger: Optional[logging.Logger] = None,
    ):
        self.account_id = account_id
        self.client_id = client_id
        self._cfg = ibkr_config
        self._settings = settings
        self._log = logger or logging.getLogger(__name__)

        self._ib: Optional[IB] = None
        self._contracts: dict[str, Any] = {}   # symbol -> Contract
        self._trades: dict[str, Any] = {}      # handle -> Trade

    def _require(self) -> IB:
        if self._ib is None or not self._ib.isConnected():
            raise ConnectionFailedError("Not connected to IB Gateway")
        return self._ib

    async def _bounded(self, awaitable: Any, exc_type: type, tag: str) -> Any:
        """Run an IBKR await under `request_timeout_seconds`; map a stall (CFG-2, M3).

        A silently stalled Gateway (e.g. mid nightly restart with a half-open socket)
        would otherwise let a worker hang forever, keeping the strategy `running` and
        dropping every later signal with no notification.
        """
        try:
            return await asyncio.wait_for(awaitable, self._cfg.request_timeout_seconds)
        except asyncio.TimeoutError:
            raise exc_type(f"{tag} on IBKR gateway timed out")

    def _resolve_port(self) -> int:
        if self._settings.ib_port:
            return self._settings.ib_port
        ports = self._cfg.ports
        if self._settings.trading_mode == "live":
            return ports.live_internal
        return ports.paper_internal

    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """IB-1."""
        cfg = self._cfg
        host = getattr(self._settings, "ib_host", None) or "ibkr-gateway"
        port = self._resolve_port()
        ib = IB()
        try:
            await ib.connectAsync(
                host=host,
                port=port,
                clientId=self.client_id,
                timeout=cfg.connection_timeout_seconds,
                account=self.account_id,
                raiseSyncErrors=True,
                fetchFields=(
                    StartupFetch.POSITIONS
                    | StartupFetch.ORDERS_OPEN
                    | StartupFetch.ACCOUNT_UPDATES
                ),
            )
        except Exception as exc:  # noqa: BLE001 - wrap any connect failure
            raise ConnectionFailedError(
                f"Could not connect to IB Gateway at {host}:{port}."
            ) from exc
        await asyncio.sleep(cfg.connection_stabilization_delay_seconds)
        if not ib.isConnected():
            ib.disconnect()
            raise ConnectionFailedError(
                f"Connection to IB Gateway at {host}:{port} was lost during startup."
            )
        if self.account_id not in ib.managedAccounts():
            ib.disconnect()
            raise ConnectionFailedError(
                f"Account {self.account_id} is not available on this IB Gateway login."
            )
        ib.reqMarketDataType(cfg.market_data_type)
        self._ib = ib

    async def close(self) -> None:
        """IB-14. Never raises; idempotent."""
        ib, self._ib = self._ib, None
        if ib is not None:
            try:
                ib.disconnect()
            except Exception as exc:  # noqa: BLE001 - close never raises outward
                self._log.warning("Error disconnecting from IBKR Gateway: %s", exc)

    # ------------------------------------------------------------------ #
    async def account_state(self) -> AccountState:
        """IB-2..IB-6."""
        ib = self._require()
        values = ib.accountValues(self.account_id)

        # IB-2: cash. $LEDGER-CashBalance preferred, else CashBalance.
        cash = None
        for av in values:
            if av.currency == "USD" and av.tag == "$LEDGER-CashBalance":
                cash = av.value
                break
        if cash is None:
            for av in values:
                if av.currency == "USD" and av.tag == "CashBalance":
                    cash = av.value
                    break
        if cash is None:
            raise AccountDataError("USD cash balance not reported by IBKR")
        cash = _parse_money(cash, "cash")

        # IB-3: NetLiquidation, USD, missing is an error (never default to 0).
        nlv = None
        for av in values:
            if av.currency == "USD" and av.tag == "NetLiquidation":
                nlv = _parse_money(av.value, "NetLiquidation")
                break
        if nlv is None:
            raise AccountDataError("NetLiquidation not reported by IBKR")

        positions: list[Position] = []
        for pos in ib.positions(self.account_id):
            if pos.position == 0:
                continue
            sec_type = pos.contract.secType
            symbol = getattr(pos.contract, "symbol", "") or ""
            if sec_type != "STK" or pos.position < 0:
                raise AccountDataError(
                    f"Unsupported position: {sec_type} {symbol} {pos.position}. "
                    "Close it manually or remove it from this account."
                )
            positions.append(Position(symbol=symbol, quantity=float(pos.position)))

        return AccountState(
            account_id=self.account_id,
            cash=cash,
            net_liquidation=nlv,
            positions=tuple(positions),
        )

    # ------------------------------------------------------------------ #
    async def _qualify(self, symbols: Collection[str]) -> list[Any]:
        """IB-7. Qualify all needed symbols in one call; cache contracts."""
        ib = self._require()
        missing = [s for s in symbols if s not in self._contracts]
        if missing:
            contracts = [Stock(s, "SMART", "USD") for s in missing]
            qualified = await self._bounded(
                ib.qualifyContractsAsync(*contracts), PricingError, "Symbol qualification"
            )
            for symbol, contract in zip(missing, qualified):
                if contract is None:
                    raise PricingError(f"Symbol {symbol} could not be qualified on IBKR")
                self._contracts[symbol] = contract
        return [self._contracts[s] for s in symbols]

    async def quotes(self, symbols: Collection[str]) -> dict[str, Quote]:
        """IB-8. `reqTickersAsync`, retry missing bid/ask, bar fallback, NoPriceError."""
        ib = self._require()
        contracts = await self._qualify(symbols)
        quote_map: dict[str, Quote] = {}

        # IB-8: retry only the still-missing symbols on later attempts.
        pending = list(contracts)
        for attempt in range(self._cfg.market_data_max_retries + 1):
            if pending:
                tickers = await self._bounded(
                    ib.reqTickersAsync(*pending), PricingError, "Market data request"
                )
                for contract, ticker in zip(pending, tickers):
                    symbol = contract.symbol
                    quote_map[symbol] = Quote(
                        symbol=symbol,
                        bid=_price(ticker.bid),
                        ask=_price(ticker.ask),
                        last=_price(ticker.last),
                        close=_price(ticker.close),
                    )
            missing = [c for c in pending
                       if quote_map[c.symbol].bid is None or quote_map[c.symbol].ask is None]
            if not missing or attempt >= self._cfg.market_data_max_retries:
                break
            pending = missing
            await asyncio.sleep(self._cfg.market_data_retry_delay_seconds)

        # Bar fallback + final NoPriceError.
        for symbol in symbols:
            q = quote_map.get(symbol)
            if q is None:
                raise NoPriceError(f"No price available for {symbol}")
            cleaned = q._clean()
            if cleaned["bid"] is None and cleaned["ask"] is None \
                    and cleaned["last"] is None and cleaned["close"] is None:
                bars = await self._bounded(
                    ib.reqHistoricalDataAsync(
                        self._contracts[symbol], endDateTime="",
                        durationStr="1800 S", barSizeSetting="1 min",
                        whatToShow="TRADES", useRTH=False,
                    ),
                    NoPriceError, "Historical bar request",
                )
                if bars:
                    quote_map[symbol] = Quote(
                        symbol=symbol, bid=q.bid, ask=q.ask, last=q.last,
                        close=q.close, bar_close=float(bars[-1].close),
                    )
            final = quote_map[symbol]._clean()
            if final["bid"] is None and final["ask"] is None and final["last"] is None \
                    and final["close"] is None and final["bar_close"] is None:
                raise NoPriceError(f"No price available for {symbol}")

        return quote_map

    # ------------------------------------------------------------------ #
    async def place(self, order: OrderRequest) -> str:
        """IB-9."""
        ib = self._require()
        contract = self._contracts.get(order.symbol)
        if contract is None:
            raise OrderPlacementError(f"Unknown contract for {order.symbol} (qualify first)")
        quantity = float(order.quantity)
        if not quantity.is_integer():
            raise OrderPlacementError(
                "IBKR does not accept fractional API orders."
            )
        quantity = int(quantity)

        if order.side == Side.SELL:
            ib_order = MarketOrder("SELL", quantity)
        else:
            if order.limit_price is None:
                raise OrderPlacementError(
                    f"Buy order for {order.symbol} requires a limit price"
                )
            ib_order = LimitOrder("BUY", quantity, order.limit_price)

        ib_order.tif = "DAY"
        ib_order.account = self.account_id
        ib_order.orderRef = f"zl-rebalancer:{order.run_id}"
        ib_order.outsideRth = False

        trade = ib.placeOrder(contract, ib_order)
        handle = str(trade.order.orderId)
        self._trades[handle] = trade
        await asyncio.sleep(self._cfg.order_placement_delay_seconds)
        return handle

    async def order_states(self, handles: Sequence[str]) -> list[OrderState]:
        """IB-10. Read status only from the stored Trade objects."""
        result: list[OrderState] = []
        for handle in handles:
            trade = self._trades.get(handle)
            if trade is None:
                continue
            raw = trade.orderStatus.status
            if raw == "Filled":
                status = OrderStatus.FILLED
            elif raw in ("Cancelled", "ApiCancelled"):
                status = OrderStatus.CANCELLED
            elif raw == "Inactive":
                status = OrderStatus.REJECTED
            else:
                status = OrderStatus.WORKING  # empty/unknown counts as WORKING
            message = None
            for entry in reversed(trade.log):
                if getattr(entry, "message", None):
                    message = entry.message
                    break
            result.append(OrderState(
                handle=handle,
                symbol=trade.contract.symbol,
                side=Side(trade.order.action),
                quantity=float(trade.order.totalQuantity),
                status=status,
                filled_quantity=float(trade.orderStatus.filled),
                average_fill_price=_price(trade.orderStatus.avgFillPrice),
                message=message,
            ))
        return result

    # ------------------------------------------------------------------ #
    async def working_orders(self) -> list[WorkingOrder]:
        """IB-12."""
        ib = self._require()
        trades = await self._bounded(
            ib.reqAllOpenOrdersAsync(), ConnectionFailedError, "Open-orders request"
        )
        result: list[WorkingOrder] = []
        for trade in trades:
            if trade.order.account == self.account_id and not trade.isDone():
                result.append(WorkingOrder(
                    symbol=trade.contract.symbol,
                    side=Side(trade.order.action),
                    quantity=float(trade.order.totalQuantity),
                    own=(trade.order.clientId == self.client_id),
                ))
        return result

    async def cancel_own_orders(self, timeout_seconds: float) -> int:
        """IB-11."""
        ib = self._require()
        own = [
            t for t in ib.openTrades()
            if t.order.account == self.account_id
            and t.order.clientId == self.client_id
            and not t.isDone()
        ]
        for trade in own:
            ib.cancelOrder(trade.order)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while any(not t.isDone() for t in own):
            if loop.time() >= deadline:
                still = [t for t in own if not t.isDone()]
                listing = ", ".join(
                    f"{t.order.action} {int(t.order.totalQuantity)} {t.contract.symbol}"
                    for t in still
                )
                raise CancelNotConfirmedError(
                    f"Orders not confirmed cancelled: {listing}"
                )
            await asyncio.sleep(0.5)
        return len(own)