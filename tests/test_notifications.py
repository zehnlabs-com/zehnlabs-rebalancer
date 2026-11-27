"""WP7 acceptance: notifications (PRD §11, §16.2)."""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ / "rebalancer"))

from app.results import (  # noqa: E402
    AccountRunResult,
    ExecutedOrder,
    FailureReason,
    NoteOut,
    PlannedOrderOut,
    RunOutcome,
    RunRecord,
    WorkAction,
)
from app.services.notification_service import (  # noqa: E402
    NotificationContext,
    NotificationKind,
    NtfyNotifier,
    render,
)
from app.state.models import (  # noqa: E402
    RetryDecision,
    RetryDisposition,
    WorkItem,
    WorkSource,
    WorkStatus,
)

ACCOUNT_ID = "U123456"


def base_result(**kw) -> AccountRunResult:
    data = dict(
        run_id="20261120-155210-U123456-a3f9",
        account_id=ACCOUNT_ID,
        strategy_name="etf-blend-200-35",
        mode="live",
        source="signal",
        outcome=RunOutcome.SUCCEEDED,
    )
    data.update(kw)
    data["record"] = RunRecord(
        run_id=data["run_id"],
        account_id=data["account_id"],
        strategy_name=data["strategy_name"],
        mode=data["mode"],
        source=data["source"],
        started_at="2026-11-20T20:52:10Z",
        finished_at="2026-11-20T20:52:10Z",
        outcome=data["outcome"],
        failure_reason=data.get("failure_reason"),
        error_message=data.get("error_message"),
    )
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
        self.assertIn("Source: Signal", msg.body)
        self.assertIn(
            "Time: 2026-11-20 15:52 EST (America/New_York)", msg.body
        )
        self.assertNotIn(result.run_id, msg.body)
        self.assertIn("Orders: 1 sells, 0 buys", msg.body)
        self.assertIn("Portfolio value ≈ $104,031.45", msg.body)
        self.assertIn("Cash ≈ $335.00", msg.body)
        self.assertIn("Priced XLE from last trade (no live quote).", msg.body)

    def test_each_kind_title_and_key_line(self):
        cases = [
            (NotificationKind.SUCCESS, "✅ Rebalance complete", "Orders:"),
            (
                NotificationKind.FAILED_WILL_RETRY,
                "⚠️ Rebalance failed: will retry",
                "Next-open retry:",
            ),
            (
                NotificationKind.QUEUED_MARKET_CLOSED,
                "🕒 Market closed: rebalance queued",
                "Next-open retry will run:",
            ),
            (NotificationKind.FAILED_FINAL, "❌ Rebalance failed: not retried", "not be retried automatically"),
            (NotificationKind.SKIPPED_FINAL, "⏭️ Rebalance request skipped", "already running"),
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
                ctx = NotificationContext(kind=kind)
                if kind in (NotificationKind.FAILED_WILL_RETRY, NotificationKind.QUEUED_MARKET_CLOSED):
                    ctx = _retry_context(kind)
                msg = render(result, ctx)
                self.assertEqual(msg.title, title)
                self.assertIn(key, msg.body)
                self.assertNotIn(result.run_id, msg.body)
                self.assertNotIn(ACCOUNT_ID, msg.body)

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

    def test_long_body_is_truncated_without_run_id(self):
        long_text = "x" * 120
        result = base_result(
            notes=[NoteOut(level="info", text=f"{long_text} {i}") for i in range(200)]
        )
        msg = render(result, NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertLessEqual(len(msg.body.encode("utf-8")), 3500)
        self.assertIn("…and", msg.body)
        self.assertIn("see logs", msg.body)
        self.assertNotIn(result.run_id, msg.body)

    def test_sensitive_identifiers_are_redacted_at_render_boundary(self):
        result = base_result(
            outcome=RunOutcome.FAILED,
            error_message=f"Account {ACCOUNT_ID} is unavailable for run "
                          "20261120-155210-U123456-a3f9.",
            notes=[NoteOut(level="warning", text=f"Check account {ACCOUNT_ID}.")],
        )
        for kind in (
            NotificationKind.SUCCESS,
            NotificationKind.FAILED_WILL_RETRY,
            NotificationKind.FAILED_FINAL,
            NotificationKind.PREVIEW,
            NotificationKind.PREVIEW_FAILED,
        ):
            with self.subTest(kind=kind):
                msg = render(result, NotificationContext(kind=kind))
                self.assertNotIn(ACCOUNT_ID, msg.body)
                self.assertNotIn(result.run_id, msg.body)

    def test_masks_valid_account_id_without_three_consecutive_digits(self):
        result = base_result(account_id="A1B2C3", run_id="safe-run")
        msg = render(result, NotificationContext(kind=NotificationKind.SUCCESS))
        self.assertIn("Account: A*B*C*", msg.body)
        self.assertNotIn("A1B2C3", msg.body)

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
        body = render(partial, _retry_context(NotificationKind.FAILED_WILL_RETRY)).body
        self.assertIn("SOLD 30 of 40 XLE", body)
        self.assertIn("BUY orders did not complete.", body)

    def test_retry_message_describes_durable_created_or_reused_work(self):
        failed = base_result(outcome=RunOutcome.FAILED, error_message="e")
        created = render(
            failed,
            _retry_context(NotificationKind.FAILED_WILL_RETRY),
        ).body
        self.assertIn("2026-11-21 09:35 EST (America/New_York)", created)
        self.assertIn("Next-open retry", created)
        self.assertIn("next market open", created)

        reused_scheduled = render(
            failed,
            _retry_context(
                NotificationKind.FAILED_WILL_RETRY,
                disposition=RetryDisposition.REUSED,
                source=WorkSource.SCHEDULED,
            ),
        ).body
        self.assertIn("already scheduled", reused_scheduled)
        self.assertIn("next market open", reused_scheduled)

        reused_immediate = render(
            failed,
            _retry_context(
                NotificationKind.FAILED_WILL_RETRY,
                disposition=RetryDisposition.REUSED,
                source=WorkSource.DASHBOARD,
                execute_at="2026-11-20T20:50:00.000000Z",
            ),
        ).body
        self.assertIn("2026-11-20 15:50 EST (America/New_York)", reused_immediate)
        self.assertNotIn("next market open", reused_immediate.lower())

    def test_source_labels_and_strategy_busy_message(self):
        for source, label in [
            ("signal", "Signal"),
            ("dashboard", "Dashboard"),
            ("initial", "Initial setup"),
            ("scheduled", "Scheduled"),
            ("retry", "Next-open retry"),
        ]:
            with self.subTest(source=source):
                result = base_result(source=source)
                body = render(
                    result, NotificationContext(kind=NotificationKind.SUCCESS)
                ).body
                self.assertIn(f"Source: {label}", body)

        busy = base_result(
            source="scheduled",
            outcome=RunOutcome.FAILED,
            failure_reason=FailureReason.STRATEGY_BUSY,
        )
        body = render(
            busy, NotificationContext(kind=NotificationKind.FAILED_FINAL)
        ).body
        self.assertIn(
            "The Strategy was already running; this request did not reach the broker.",
            body,
        )


def _retry_context(
    kind,
    *,
    disposition=RetryDisposition.CREATED,
    source=WorkSource.RETRY,
    execute_at="2026-11-21T14:35:00.000000Z",
):
    item = WorkItem(
        work_id="retry-work",
        submission_id="retry-submission",
        account_id=ACCOUNT_ID,
        action=WorkAction.REBALANCE,
        source=source,
        status=WorkStatus.PENDING,
        execute_at=execute_at,
        request_token_hash="0" * 64,
        created_at="2026-11-20T20:52:10.000000Z",
    )
    return NotificationContext(
        kind=kind,
        retry_decision=RetryDecision(disposition=disposition, item=item),
    )


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

    async def test_notification_failure_does_not_log_exception_or_topic(self):
        notifier = NtfyNotifier(
            enabled=True,
            channel="synthetic-secret-topic",
            logger=logging.getLogger("test_ntfy_exception"),
        )
        with patch.object(
            notifier,
            "_post",
            side_effect=RuntimeError("synthetic-secret-topic"),
        ), self.assertLogs("test_ntfy_exception", level="ERROR") as captured:
            await notifier.send(
                base_result(), NotificationContext(kind=NotificationKind.SUCCESS)
            )

        self.assertNotIn("synthetic-secret-topic", "\n".join(captured.output))

    async def test_non_200_response_does_not_log_topic_url(self):
        class FakeResponse:
            status = 503

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, **kwargs):
                return FakeResponse()

        notifier = NtfyNotifier(
            enabled=True,
            channel="synthetic-secret-topic",
            logger=logging.getLogger("test_ntfy_status"),
        )
        with patch(
            "app.services.notification_service.aiohttp.ClientSession",
            FakeSession,
        ), self.assertLogs("test_ntfy_status", level="ERROR") as captured:
            await notifier.send(
                base_result(), NotificationContext(kind=NotificationKind.SUCCESS)
            )

        self.assertNotIn("synthetic-secret-topic", "\n".join(captured.output))
