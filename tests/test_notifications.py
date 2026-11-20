"""WP7 acceptance: notifications (PRD §11, §16.2)."""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ / "event-broker"))

from app.results import (  # noqa: E402
    AccountRunResult,
    ExecutedOrder,
    NoteOut,
    PlannedOrderOut,
    RunOutcome,
)
from app.services.notification_service import (  # noqa: E402
    NotificationContext,
    NotificationKind,
    NtfyNotifier,
    render,
)

ACCOUNT_ID = "U123456"


def base_result(**kw) -> AccountRunResult:
    data = dict(
        run_id="20261120-155210-1234567-a3f9",
        account_id=ACCOUNT_ID,
        strategy_name="etf-blend-200-35",
        mode="live",
        source="signal",
        outcome=RunOutcome.SUCCEEDED,
        record={"started_at": "2026-11-20T15:52:10-05:00"},
    )
    data.update(kw)
    return AccountRunResult(**data)


class RenderTests(unittest.TestCase):
    def test_success_title_and_key_lines(self):
        result = base_result(
            executed=[ExecutedOrder(symbol="XLE", side="SELL", quantity=100,
                                    filled_quantity=100, average_fill_price=84.95,
                                    status="FILLED")],
            value_end=104031.45, cash_end=335.0,
            notes=[NoteOut(level="info", text="Priced XLE from last trade (no live quote).")],
        )
        msg = render(result, NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertEqual(msg.title, "✅ Rebalance complete")
        self.assertIn("Account: U***456", msg.body)
        self.assertIn("Strategy: ETF Blend 200-35", msg.body)
        self.assertIn("Time: 2026-11-20 15:52:10 ET", msg.body)
        self.assertIn("Run: 20261120-155210-1234567-a3f9", msg.body)
        self.assertIn("Orders: 1 sells, 0 buys", msg.body)
        self.assertIn("Portfolio value ≈ $104,031.45", msg.body)
        self.assertIn("Cash ≈ $335.00", msg.body)
        self.assertIn("Priced XLE from last trade (no live quote).", msg.body)

    def test_each_kind_title_and_key_line(self):
        cases = [
            (NotificationKind.SUCCESS, "✅ Rebalance complete", "Orders:"),
            (NotificationKind.FAILED_WILL_RETRY, "⚠️ Rebalance failed: will retry", "Retry:"),
            (NotificationKind.QUEUED_MARKET_CLOSED, "🕒 Market closed: rebalance queued", "Will run:"),
            (NotificationKind.FAILED_FINAL, "❌ Rebalance failed: not retried", "not be retried automatically"),
            (NotificationKind.SKIPPED_FINAL, "❌ Scheduled rebalance skipped", "already running"),
            (NotificationKind.PREVIEW, "🔎 Rebalance preview", "Portfolio value ≈"),
            (NotificationKind.PREVIEW_FAILED, "❌ Rebalance preview failed", "Error:"),
        ]
        for kind, title, key in cases:
            with self.subTest(kind=kind):
                kw = {"outcome": RunOutcome.FAILED, "error_message": "boom"}
                if kind == NotificationKind.SUCCESS:
                    kw = {"value_end": 1.0, "cash_end": 2.0}
                if kind == NotificationKind.PREVIEW:
                    kw = {"planned_sells": [PlannedOrderOut(symbol="XLE", side="SELL",
                                                            quantity=55, limit_price=None)],
                          "planned_buys": [PlannedOrderOut(symbol="TLT", side="BUY",
                                                           quantity=62, limit_price=92.11)],
                          "value_end": 1000.0}
                result = base_result(**kw)
                ctx = NotificationContext(kind=kind, next_run=None)
                if kind in (NotificationKind.FAILED_WILL_RETRY, NotificationKind.QUEUED_MARKET_CLOSED):
                    ctx = NotificationContext(kind=kind, next_run=_next_run())
                msg = render(result, ctx)
                self.assertEqual(msg.title, title)
                self.assertIn(key, msg.body)

    def test_preview_order_lines_and_cap(self):
        result = base_result(
            planned_sells=[PlannedOrderOut(symbol="XLE", side="SELL", quantity=55, limit_price=None)],
            planned_buys=[PlannedOrderOut(symbol="TLT", side="BUY", quantity=62, limit_price=92.11)],
            value_end=104031.45,
        )
        msg = render(result, NotificationContext(kind=NotificationKind.PREVIEW))
        self.assertIn("Planned: 1 sells, 1 buys", msg.body)
        self.assertIn("SELL 55 XLE (market)", msg.body)
        self.assertIn("BUY 62 TLT ≤ $92.11", msg.body)
        self.assertIn("Portfolio value ≈ $104,031.45", msg.body)

    def test_body_capped_at_3500_bytes_with_200_notes(self):
        result = base_result(
            notes=[NoteOut(level="info", text=f"note {i}") for i in range(200)]
        )
        msg = render(result, NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertLessEqual(len(msg.body.encode("utf-8")), 3500)

    def test_long_body_is_truncated_with_run_id(self):
        long_text = "x" * 120
        result = base_result(
            notes=[NoteOut(level="info", text=f"{long_text} {i}") for i in range(200)]
        )
        msg = render(result, NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertLessEqual(len(msg.body.encode("utf-8")), 3500)
        self.assertIn("…and", msg.body)
        self.assertIn("see logs, run 20261120-155210-1234567-a3f9", msg.body)

    def test_what_happened_lines(self):
        none = base_result(outcome=RunOutcome.FAILED, error_message="e")
        body = render(none, NotificationContext(kind=NotificationKind.FAILED_FINAL)).body
        self.assertIn("Nothing was traded.", body)

        partial = base_result(
            outcome=RunOutcome.FAILED, error_message="e",
            executed=[ExecutedOrder(symbol="XLE", side="SELL", quantity=40,
                                    filled_quantity=30, average_fill_price=84.97,
                                    status="WORKING")],
            planned_sells=[PlannedOrderOut(symbol="XLE", side="SELL", quantity=40, limit_price=None)],
            planned_buys=[PlannedOrderOut(symbol="GLD", side="BUY", quantity=12,
                                          limit_price=250.0)],
        )
        body = render(partial, NotificationContext(kind=NotificationKind.FAILED_WILL_RETRY,
                                                   next_run=_next_run())).body
        self.assertIn("SOLD 30 of 40 XLE", body)
        self.assertIn("BUY orders did not complete.", body)


def _next_run():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 11, 21, 9, 35, tzinfo=ZoneInfo("America/New_York"))


class NtfyNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_or_no_channel_skips(self):
        notifier = NtfyNotifier(enabled=False, channel="c", logger=logging.getLogger("t"))
        await notifier.send(base_result(), NotificationContext(kind=NotificationKind.SUCCESS))
        notifier = NtfyNotifier(enabled=True, channel="", logger=logging.getLogger("t"))
        await notifier.send(base_result(), NotificationContext(kind=NotificationKind.SUCCESS))
        # no exception; nothing posted (would have needed a session)

    async def test_one_post_per_send_with_timeout_and_headers(self):
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            last = None

            def __init__(self, *args, **kwargs):
                FakeSession.last = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, **kw):
                self.posted = (url, kw)
                return FakeResponse()

        notifier = NtfyNotifier(enabled=True, channel="my-chan", logger=logging.getLogger("t"))
        with patch("app.services.notification_service.aiohttp.ClientSession", FakeSession):
            await notifier.send(base_result(), NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertEqual(FakeSession.last["timeout"].total, 10)
        self.assertGreater(len(FakeSession.last), 0)