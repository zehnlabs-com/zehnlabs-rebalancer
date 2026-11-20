"""WP7 acceptance: SchedulerService and schedule-file helpers (PRD §10.4, §16.2)."""

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ / "event-broker"))

import app.services.scheduler_service as SVC  # noqa: E402
from app_config import AppConfig  # noqa: E402
from app.accounts import AccountConfig  # noqa: E402
from app.results import AccountRunResult, RunOutcome  # noqa: E402
from app.services.scheduler_service import (  # noqa: E402
    add_account,
    remove_accounts,
    SchedulerService,
)
from fakes import FakeClock, RecordingNotifier  # noqa: E402

from apscheduler.triggers.cron import CronTrigger, DayOfWeekField  # noqa: E402

ACCOUNT = AccountConfig(account_id="U100001", type="paper", enabled=True,
                        strategy_name="strategy-one")


class StubExecutor:
    """Returns a single SUCCEEDED result; records calls (no subprocess)."""

    def __init__(self):
        self.calls = []

    async def execute(self, strategy, accounts, mode, source):
        self.calls.append((strategy, [a.account_id for a in accounts], mode, source))
        return [AccountRunResult(
            run_id="r", account_id=accounts[0].account_id,
            strategy_name=strategy, mode=mode, source=source,
            outcome=RunOutcome.SUCCEEDED, record={},
        )]


def make_scheduler(tmp, executor, clock=None):
    config = AppConfig(scheduler={"enabled": True,
                                  "scheduled_file_path": str(Path(tmp) / "scheduled.json")})
    registry = SimpleNamespace(get=lambda i: ACCOUNT if i == "U100001" else None)
    return SchedulerService(
        config=config, strategy_executor=executor, accounts_lookup=registry.get,
        clock=clock or FakeClock(datetime(2026, 11, 20, 9, 35)),
        notifier=RecordingNotifier(), logger=logging.getLogger("test_scheduler"),
    )


class ScheduleFileBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_scheduled_file_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduled.json")
            for content in ['{"not": "array"}', "{not json]"]:
                with self.subTest(content=content):
                    Path(path).write_text(content, encoding="utf-8")
                    executor = StubExecutor()
                    scheduler = make_scheduler(tmp, executor)
                    await scheduler._process_scheduled_accounts()
                    self.assertEqual(Path(path).read_text(encoding="utf-8"), content)
                    self.assertEqual(executor.calls, [])

    async def test_missing_scheduled_file_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = StubExecutor()
            scheduler = make_scheduler(tmp, executor)
            await scheduler._process_scheduled_accounts()
            self.assertEqual(executor.calls, [])
            self.assertFalse((Path(tmp) / "scheduled.json").exists())

    async def test_schedule_removes_only_processed_keeps_id_added_during_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduled.json")
            Path(path).write_text(json.dumps(["U100001", "missing"]), encoding="utf-8")

            class AddsDuringJob(StubExecutor):
                async def execute(self, strategy, accounts, mode, source):
                    await super().execute(strategy, accounts, mode, source)
                    add_account(path, "U99999", logging.getLogger("t"))  # added mid-run
                    return [AccountRunResult(
                        run_id="r", account_id=accounts[0].account_id,
                        strategy_name=strategy, mode=mode, source=source,
                        outcome=RunOutcome.SUCCEEDED, record={},
                    )]

            await make_scheduler(tmp, AddsDuringJob())._process_scheduled_accounts()
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), ["U99999"])

    async def test_duplicate_ids_run_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduled.json")
            Path(path).write_text(json.dumps(["U100001", "U100001", "missing"]), encoding="utf-8")
            executor = StubExecutor()
            await make_scheduler(tmp, executor)._process_scheduled_accounts()
            self.assertEqual(executor.calls, [("strategy-one", ["U100001"], "live", "scheduled")])

    async def test_skipped_when_strategy_already_running_sends_skipped_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduled.json")
            Path(path).write_text(json.dumps(["U100001"]), encoding="utf-8")
            notifier = RecordingNotifier()

            class AlreadyRunning(StubExecutor):
                async def execute(self, strategy, accounts, mode, source):
                    return None  # strategy already running

            registry = SimpleNamespace(get=lambda i: ACCOUNT if i == "U100001" else None)
            config = AppConfig(scheduler={"enabled": True,
                                          "scheduled_file_path": path})
            scheduler = SchedulerService(
                config=config, strategy_executor=AlreadyRunning(),
                accounts_lookup=registry.get,
                clock=FakeClock(datetime(2026, 11, 20, 9, 35)),
                notifier=notifier, logger=logging.getLogger("t"),
            )
            await scheduler._process_scheduled_accounts()
            self.assertEqual(notifier.sends[0][1].kind.value, "SKIPPED_FINAL")
            # the account was processed -> removed
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), [])


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_misfire_settings_passed_to_add_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            class FakeAPS:
                jobs = []

                @classmethod
                def reset(cls):
                    cls.jobs = []

                def __init__(self):
                    self.started = False

                def add_job(self, fn, trigger, **kwargs):
                    FakeAPS.jobs.append((fn, trigger, kwargs))

                def start(self):
                    self.started = True

                def shutdown(self, wait=False):
                    pass

                def get_job(self, id):
                    return None

            FakeAPS.reset()
            with patch.object(SVC, "AsyncIOScheduler", FakeAPS):
                await make_scheduler(tmp, StubExecutor()).start()
            self.assertEqual(len(FakeAPS.jobs), 1)
            _, trigger, kwargs = FakeAPS.jobs[0]
            self.assertIsInstance(trigger, CronTrigger)
            self.assertEqual(kwargs["misfire_grace_time"], 300)
            self.assertTrue(kwargs["coalesce"])
            self.assertEqual(kwargs["max_instances"], 1)
            dow = [f for f in trigger.fields if isinstance(f, DayOfWeekField)][0]
            self.assertTrue(any(str(e) == "mon-fri" for e in dow.expressions))

    async def test_disabled_scheduler_adds_no_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            instances = []

            class FakeAPS:
                def __init__(self):
                    instances.append(self)
                    self.jobs = []
                    self.started = False

                def add_job(self, *a, **k):
                    self.jobs.append((a, k))

                def start(self):
                    self.started = True

            with patch.object(SVC, "AsyncIOScheduler", FakeAPS):
                config = AppConfig(scheduler={"enabled": False,
                                              "scheduled_file_path": str(Path(tmp) / "s.json")})
                scheduler = SchedulerService(
                    config=config, strategy_executor=StubExecutor(),
                    accounts_lookup=lambda i: None, clock=FakeClock(datetime(2026, 11, 20, 9, 35)),
                    notifier=RecordingNotifier(), logger=logging.getLogger("t"),
                )
                await scheduler.start()
            self.assertEqual(instances, [])


class HelperTests(unittest.TestCase):
    def test_add_and_remove_are_atomic_and_do_not_clobber_invalid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduled.json")
            log = logging.getLogger("t")
            add_account(path, "A", log)
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), ["A"])
            add_account(path, "A", log)  # idempotent
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), ["A"])
            remove_accounts(path, ["A"], log)
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), [])

            Path(path).write_text(json.dumps({"bad": True}), encoding="utf-8")
            before = Path(path).read_text(encoding="utf-8")
            add_account(path, "B", log)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), before)  # untouched