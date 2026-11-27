"""Notifications (PRD §11).

`Notifier` is the ABC; `NtfyNotifier` sends to ntfy.sh. Rendering is the pure
`render(result, context) -> Message` so it is testable without HTTP.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import aiohttp

from ..presentation import format_eastern_time
from ..results import AccountRunResult, FailureReason, RunSource
from ..state.models import RetryDecision, RetryDisposition, WorkSource

logger = logging.getLogger(__name__)

_NTFY_URL = "https://ntfy.sh"
_MAX_BODY_BYTES = 3500
_SOURCE_LABELS = {
    RunSource.SIGNAL: "Signal",
    RunSource.DASHBOARD: "Dashboard",
    RunSource.INITIAL: "Initial setup",
    RunSource.SCHEDULED: "Scheduled",
    RunSource.RETRY: "Next-open retry",
}


class NotificationKind(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED_WILL_RETRY = "FAILED_WILL_RETRY"
    QUEUED_MARKET_CLOSED = "QUEUED_MARKET_CLOSED"
    FAILED_FINAL = "FAILED_FINAL"
    SKIPPED_FINAL = "SKIPPED_FINAL"
    PREVIEW = "PREVIEW"
    PREVIEW_FAILED = "PREVIEW_FAILED"


@dataclass
class NotificationContext:
    """Extra context for a notification (PRD §11.1)."""

    kind: NotificationKind
    retry_decision: Optional[RetryDecision] = None


@dataclass
class Message:
    title: str
    body: str
    tags: list[str]


def _mask_account_id(account_id: str) -> str:
    """Mask account digits, retaining only the final three when available."""
    digit_indexes = [index for index, char in enumerate(account_id) if char.isdigit()]
    if not digit_indexes:
        return "***"
    visible = set(digit_indexes[-3:]) if len(digit_indexes) > 3 else set()
    return "".join(
        char if not char.isdigit() or index in visible else "*"
        for index, char in enumerate(account_id)
    )


def _format_strategy_name(strategy_slug: str) -> str:
    """Convert a strategy slug to its display name (e.g., etf-blend-301-20 -> ETF Blend 301-20)."""
    parts = []
    for part in strategy_slug.split("-"):
        if part.isdigit() and parts and parts[-1].isdigit():
            parts.append(f"{parts.pop()}-{part}")
        elif part.isdigit():
            parts.append(part)
        elif len(part) <= 4:  # ponytail: acronym heuristic (etf -> ETF)
            parts.append(part.upper())
        else:
            parts.append(part.title())
    return " ".join(parts)


def _time_str(result: AccountRunResult) -> str:
    started = result.record.started_at
    if started:
        try:
            return format_eastern_time(started)
        except (TypeError, ValueError):
            pass
    return ""


def _money(value: Optional[float]) -> str:
    return "$unknown" if value is None else f"${value:,.2f}"


def _retry_timing(context: NotificationContext, *, queued: bool) -> str:
    decision = context.retry_decision
    if decision is None or decision.item is None:
        return "Will run after dispatch." if queued else "Retry is awaiting dispatch."
    item = decision.item
    timestamp = format_eastern_time(item.execute_at)
    next_open = item.source in (WorkSource.SCHEDULED, WorkSource.RETRY)
    if decision.disposition == RetryDisposition.CREATED:
        prefix = "Next-open retry will run" if queued else "Next-open retry"
        return f"{prefix}: {timestamp} (next market open)."
    prefix = "Already scheduled" if queued else "Retry: already scheduled"
    suffix = " (next market open)" if next_open else ""
    return f"{prefix}: {timestamp}{suffix}."


def _header(result: AccountRunResult) -> list[str]:
    return [
        f"Account: {_mask_account_id(result.account_id)}",
        f"Strategy: {_format_strategy_name(result.strategy_name)}",
        f"Source: {_SOURCE_LABELS[result.source]}",
        f"Time: {_time_str(result)}",
        "",
    ]


def _note_lines(result: AccountRunResult) -> list[str]:
    lines = []
    for note in result.notes:
        text = note.text.strip()
        if text:
            lines.append(text)
    return lines


def _what_happened(result: AccountRunResult) -> str:
    """PRD §11.3 'what happened' line (RUN-1)."""
    filled = [e for e in result.executed if e.filled_quantity and e.filled_quantity > 0]
    if not filled:
        return "Nothing was traded."
    parts = []
    for e in filled:
        verb = "SOLD" if e.side == "SELL" else "BOUGHT"
        if abs(float(e.filled_quantity) - float(e.quantity)) < 1e-9:
            avg = e.average_fill_price
            avg_str = f" @ ${avg:,.2f}" if avg is not None else ""
            parts.append(f"{verb} {int(e.filled_quantity)} {e.symbol}{avg_str}")
        else:
            parts.append(f"{verb} {int(e.filled_quantity)} of {int(e.quantity)} {e.symbol}")
    body = "Already done: " + ", ".join(parts)
    if result.planned_buys:
        body += "; BUY orders did not complete."
    return body


def _count(result: AccountRunResult) -> tuple[int, int]:
    sells = sum(1 for e in result.executed if e.side == "SELL")
    buys = sum(1 for e in result.executed if e.side == "BUY")
    return sells, buys


def _order_line(symbol: str, side: str, quantity: float, limit: Optional[float]) -> str:
    if side == "SELL":
        return f"SELL {int(quantity)} {symbol} (market)"
    if limit is None:
        return f"BUY {int(quantity)} {symbol}"
    return f"BUY {int(quantity)} {symbol} ≤ ${limit:,.2f}"


def _content(result: AccountRunResult, context: NotificationContext) -> list[str]:
    kind = context.kind

    if result.failure_reason == FailureReason.STRATEGY_BUSY:
        return ["The Strategy was already running; this request did not reach the broker."]

    if kind == NotificationKind.SUCCESS:
        sells, buys = _count(result)
        return [
            f"Orders: {sells} sells, {buys} buys",
            f"Portfolio value ≈ {_money(result.value_end)}",
            f"Cash ≈ {_money(result.cash_end)}",
        ] + _note_lines(result)

    if kind == NotificationKind.FAILED_WILL_RETRY:
        return [
            f"Error: {result.error_message or 'unknown error'}",
            _what_happened(result),
            _retry_timing(context, queued=False),
        ] + _note_lines(result)

    if kind == NotificationKind.QUEUED_MARKET_CLOSED:
        return ["No orders were placed.", _retry_timing(context, queued=True)]

    if kind == NotificationKind.FAILED_FINAL:
        return [
            f"Error: {result.error_message or 'unknown error'}",
            _what_happened(result),
            "This account will not be retried automatically. Fix the issue, then request "
            "another rebalance from the dashboard.",
        ] + _note_lines(result)

    if kind == NotificationKind.SKIPPED_FINAL:
        return ["The Strategy was already running; this request did not reach the broker."]

    if kind == NotificationKind.PREVIEW:
        all_orders = list(result.planned_sells) + list(result.planned_buys)
        shown = 0
        order_lines = []
        for o in all_orders:
            if shown >= 20:
                order_lines.append(f"…and {len(all_orders) - shown} more")
                break
            order_lines.append(_order_line(o.symbol, o.side, o.quantity, o.limit_price))
            shown += 1
        return [f"Planned: {len(result.planned_sells)} sells, {len(result.planned_buys)} buys"] \
            + order_lines + [
            f"Portfolio value ≈ {_money(result.value_end)}",
        ] + _note_lines(result)

    if kind == NotificationKind.PREVIEW_FAILED:
        return [f"Error: {result.error_message or 'unknown error'}"]

    return []


def _cap_body(lines: list[str]) -> str:
    """NTF-4: the body is at most 3,500 UTF-8 bytes."""
    body = "\n".join(lines)
    if len(body.encode("utf-8")) <= _MAX_BODY_BYTES:
        return body
    kept: list[str] = []
    size = 0
    for line in lines:
        add = len(line.encode("utf-8")) + (1 if kept else 0)
        if size + add > _MAX_BODY_BYTES:
            break
        size += add
        kept.append(line)
    dropped = len(lines) - len(kept)
    ending = f"…and {dropped} more notes (see logs)"
    while kept and len(("\n".join(kept) + "\n" + ending).encode("utf-8")) > _MAX_BODY_BYTES:
        kept.pop()
        dropped += 1
    return "\n".join(kept) + "\n" + ending


_TITLE_TAGS = {
    NotificationKind.SUCCESS: ("✅ Rebalance complete", ["white_check_mark"]),
    NotificationKind.FAILED_WILL_RETRY: ("⚠️ Rebalance failed: will retry", ["warning"]),
    NotificationKind.QUEUED_MARKET_CLOSED: ("🕒 Market closed: rebalance queued", ["hourglass"]),
    NotificationKind.FAILED_FINAL: ("❌ Rebalance failed: not retried", ["x"]),
    NotificationKind.SKIPPED_FINAL: ("⏭️ Rebalance request skipped", ["next_track_button"]),
    NotificationKind.PREVIEW: ("🔎 Rebalance preview", ["mag"]),
    NotificationKind.PREVIEW_FAILED: ("❌ Rebalance preview failed", ["x"]),
}


def render(result: AccountRunResult, context: NotificationContext) -> Message:
    """Pure rendering (NTF-1)."""
    title, tags = _TITLE_TAGS.get(context.kind, ("Rebalance notification", []))
    body_lines = _header(result) + _content(result, context)
    masked_account = _mask_account_id(result.account_id)
    safe_lines = []
    for line in body_lines:
        if result.run_id:
            line = line.replace(result.run_id, "[redacted]")
        if result.account_id:
            line = line.replace(result.account_id, masked_account)
        safe_lines.append(line)
    return Message(title=title, body=_cap_body(safe_lines), tags=tags)


class Notifier(ABC):
    """NTF-1."""

    @abstractmethod
    async def send(self, result: AccountRunResult, context: NotificationContext) -> None:
        raise NotImplementedError


class NtfyNotifier(Notifier):
    """NTF-1..5: one message per result, sent to `ntfy.sh/<channel>`."""

    def __init__(
        self,
        enabled: bool,
        channel: str,
        logger: Optional[logging.Logger] = None,
    ):
        self._enabled = enabled
        self._channel = channel
        self._log = logger or logging.getLogger(__name__)

    async def send(self, result: AccountRunResult, context: NotificationContext) -> None:
        if not self._enabled:
            self._log.info("Notifications disabled; skipping")
            return
        if not self._channel:
            self._log.info("USER_NOTIFICATIONS_CHANNEL not set; skipping")
            return
        message = render(result, context)
        try:
            await self._post(message)
        except Exception as exc:  # noqa: BLE001 - NTF-3: failures are logged, never raised
            self._log.error("Failed to send notification for %s: %s",
                            result.account_id, exc)

    async def _post(self, message: Message) -> None:
        url = f"{_NTFY_URL}/{self._channel}"
        headers = {
            "Title": message.title,
            "Priority": "default",
            "Tags": ",".join(message.tags),
        }
        timeout = aiohttp.ClientTimeout(total=10)  # NTF-3
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, data=message.body.encode("utf-8"), headers=headers
            ) as response:
                if response.status != 200:
                    self._log.error("ntfy returned HTTP %s for %s",
                                    response.status, url)
