"""WP7 acceptance: StrategyExecutor (PRD §10.2, §10.5 rows, §16.2)."""

import json
import logging
import sys
import tempfile
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ / "event-broker"))

from app_config import AppConfig  # noqa: E402

import app.services.strategy_executor as SEM  # noqa: E402
from app.accounts import AccountConfig  # noqa: E402
from app.results import (  # noqa: E402
    AccountRunResult,
    FailureReason,
    NoteOut,
    RunOutcome,
)
from app.services.notification_service import (  # noqa: E402
    Notifier,
    NotificationContext,
    NotificationKind,
)
from app.services.run_records import RunRecords  # noqa: E402
from fakes import FakeClock, RecordingNotifier  # noqa: E402

ACCOUNT = AccountConfig(account_id="U123456", type="paper", enabled=True,
                        strategy_name="strategy-one")


class FakePool:
    """In-process executor stand-in: never spawns a subprocess."""

    def __init__(self, *args, **kwargs):
        self.shutdown_args = None
        self.breaks = False

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            if self.breaks:
                raise BrokenProcessPool()
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


def _run_records(tmp: str) -> RunRecords:
    return RunRecords(str(Path(tmp) / "runs"))


def _config(tmp: str, enabled: bool = True) -> AppConfig:
    return AppConfig(scheduler={
        "enabled": enabled,
        "scheduled_file_path": str(Path(tmp) / "scheduled.json"),
    })


def make_executor(tmp: str, notifier=None, clock=None, config=None, registry=None):
    with patch("app.services.strategy_executor.get_context", return_value="fork-dummy"), \
         patch("app.services.strategy_executor.ProcessPoolExecutor", return_value=FakePool()):
        ex = SEM.StrategyExecutor(
            config=config or _config(tmp),
            settings=SimpleNamespace(trading_mode="paper", ib_port=None,
                                     allocations_base_url="http://x",
                                     allocations_api_key="zehnlabs-key"),
            registry=registry or SimpleNamespace(
                get=lambda i: ACCOUNT if i == "U123456" else None,
                for_strategy=lambda s: [ACCOUNT], strategies=lambda: ["strategy-one"],
            ),
            clock=clock or FakeClock(datetime(2026, 11, 20, 10, 0)),
            notifier=notifier or RecordingNotifier(),
            records=_run_records(tmp),
            logger=logging.getLogger("test_executor"),
        )
    return ex


def make_result(outcome=RunOutcome.SUCCEEDED, mode="live", source="signal", **kw) -> AccountRunResult:
    data = dict(
        run_id="r1", account_id=ACCOUNT.account_id, strategy_name=ACCOUNT.strategy_name,
        mode=mode, source=source, outcome=outcome, record={}, error_message=None,
    )
    data.update(kw)
    return AccountRunResult(**data)


def read_schedule_file(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


class ScheduleAndNotificationRowsTests(unittest.IsolatedAsyncioTestCase):
    """Each row of the §10.5 table: notification kind + scheduled-file change."""

    async def test_each_10_5_row(self):
        # (mode, source, outcome, scheduler_enabled, expected_kind, seed, expected_file)
        cases = [
            # signal / manual live
            ("live", "signal", RunOutcome.SUCCEEDED, True, NotificationKind.SUCCESS, [ACCOUNT.account_id], []),
            ("live", "signal", RunOutcome.FAILED, True, NotificationKind.FAILED_WILL_RETRY, [], [ACCOUNT.account_id]),
            ("live", "signal", RunOutcome.MARKET_CLOSED, True, NotificationKind.QUEUED_MARKET_CLOSED, [], [ACCOUNT.account_id]),
            # scheduled live
            ("live", "scheduled", RunOutcome.SUCCEEDED, True, NotificationKind.SUCCESS, [ACCOUNT.account_id], [ACCOUNT.account_id]),
            ("live", "scheduled", RunOutcome.FAILED, True, NotificationKind.FAILED_FINAL, [ACCOUNT.account_id], [ACCOUNT.account_id]),
            ("live", "scheduled", RunOutcome.MARKET_CLOSED, True, NotificationKind.FAILED_FINAL, [ACCOUNT.account_id], [ACCOUNT.account_id]),
            # preview
            ("preview", "signal", RunOutcome.PREVIEWED, True, NotificationKind.PREVIEW, None, None),
            ("preview", "signal", RunOutcome.FAILED, True, NotificationKind.PREVIEW_FAILED, None, None),
        ]
        for mode, source, outcome, enabled, expected_kind, seed, expected_file in cases:
            with self.subTest(mode=mode, source=source, outcome=outcome.value):
                with tempfile.TemporaryDirectory() as tmp:
                    sched_path = str(Path(tmp) / "scheduled.json")
                    if seed is not None:
                        Path(sched_path).write_text(json.dumps(seed), encoding="utf-8")
                    notifier = RecordingNotifier()
                    ex = make_executor(tmp, notifier=notifier,
                                       config=_config(tmp, enabled=enabled),
                                       clock=FakeClock(datetime(2026, 11, 20, 10, 0)))
                    result = make_result(mode=mode, source=source, outcome=outcome)
                    with patch.object(SEM, "execute_strategy_batch",
                                      return_value=[result.model_dump()]):
                        await ex.execute(ACCOUNT.strategy_name, [ACCOUNT], mode, source)
                    self.assertEqual(notifier.sends[0][1].kind, expected_kind)
                    if expected_file is None:
                        self.assertFalse(Path(sched_path).exists())  # no schedule change
                    else:
                        self.assertEqual(read_schedule_file(sched_path), expected_file)

    async def test_scheduler_disabled_gives_failed_final_with_retry_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched_path = str(Path(tmp) / "scheduled.json")
            Path(sched_path).write_text(json.dumps([]), encoding="utf-8")
            notifier = RecordingNotifier()
            ex = make_executor(tmp, notifier=notifier, config=_config(tmp, enabled=False))
            result = make_result(outcome=RunOutcome.FAILED, source="signal")
            with patch.object(SEM, "execute_strategy_batch", return_value=[result.model_dump()]):
                await ex.execute(ACCOUNT.strategy_name, [ACCOUNT], "live", "signal")
            self.assertEqual(notifier.sends[0][1].kind, NotificationKind.FAILED_FINAL)
            sent_result = notifier.sends[0][0]
            self.assertTrue(any(
                n.text == "Automatic retry is disabled (scheduler.enabled: false)."
                for n in sent_result.notes
            ))
            self.assertEqual(read_schedule_file(sched_path), [])


class DedupAndMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_dropped_trigger_while_running_no_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            notifier = RecordingNotifier()
            ex = make_executor(tmp, notifier=notifier)
            ex.running.add("strategy-one")
            result = await ex.execute(ACCOUNT.strategy_name, [ACCOUNT], "live", "signal")
            self.assertIsNone(result)
            self.assertEqual(notifier.sends, [])

    async def test_running_mark_held_during_pool_and_released_before_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks_on_submit = []
            in_notifier = []

            class AssertingNotifier(Notifier):
                async def send(self, result, context):
                    in_notifier.append(set(ex.running))
                    self.sends = getattr(self, "sends", 0) + 1

            notifier = AssertingNotifier()
            ex = make_executor(tmp, notifier=notifier)

            def fake_batch(*args):
                marks_on_submit.append("strategy-one" in ex.running)
                return [make_result().model_dump()]

            with patch.object(SEM, "execute_strategy_batch", side_effect=fake_batch):
                await ex.execute(ACCOUNT.strategy_name, [ACCOUNT], "live", "signal")
            self.assertEqual(marks_on_submit, [True])   # mark held while in the pool
            self.assertTrue(all("strategy-one" not in s for s in in_notifier))  # released first


class PoolFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_broken_pool_produces_worker_crashed_and_replaces_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            never = MakeNeverPool()
            with patch("app.services.strategy_executor.get_context", return_value="x"), \
                 patch("app.services.strategy_executor.ProcessPoolExecutor",
                       side_effect=lambda *a, **k: never.next_pool()):
                ex = SEM.StrategyExecutor(
                    config=_config(tmp), settings=SimpleNamespace(allocations_api_key="k",
                                                                  trading_mode="paper"),
                    registry=SimpleNamespace(get=lambda i: None), clock=FakeClock(datetime(2026, 11, 20, 10, 0)),
                    notifier=RecordingNotifier(), records=_run_records(tmp),
                    logger=logging.getLogger("test_executor"),
                )
                original = ex._pool
                with patch.object(SEM, "execute_strategy_batch", side_effect=BrokenProcessPool()):
                    results = await ex.execute(ACCOUNT.strategy_name, [ACCOUNT], "live", "signal")
            self.assertTrue(all(r.failure_reason == FailureReason.WORKER_CRASHED for r in results))
            self.assertEqual(results[0].error_message,
                             "The worker process stopped unexpectedly. Orders may have been placed; check the account.")
            self.assertIsNot(ex._pool, original)


class MakeNeverPool:
    """Yields a fresh FakePool on every construction (so replacement is a new object)."""

    def __init__(self):
        self.count = 0

    def next_pool(self):
        self.count += 1
        pool = FakePool()
        setattr(pool, "index", self.count)
        return pool


class ForkContextTests(unittest.TestCase):
    def test_pool_uses_fork_context(self):
        captured = {}

        def factory(*args, **kwargs):
            captured.update(kwargs)
            return FakePool()

        with patch("app.services.strategy_executor.get_context", return_value="FORK_CTX"), \
             patch("app.services.strategy_executor.ProcessPoolExecutor", side_effect=factory):
            with tempfile.TemporaryDirectory() as tmp:
                SEM.StrategyExecutor(
                    config=_config(tmp), settings=SimpleNamespace(allocations_api_key="k"),
                    registry=SimpleNamespace(), clock=FakeClock(datetime(2026, 11, 20, 10, 0)),
                    notifier=RecordingNotifier(), records=_run_records(tmp),
                    logger=logging.getLogger("test_executor"),
                )
        self.assertEqual(captured["mp_context"], "FORK_CTX")

    def test_cleanup_shuts_down_with_wait_and_cancel_futures(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = make_executor(tmp)
            ex.cleanup()
            self.assertEqual(ex._pool.shutdown_args, (True, True))