from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from app.services.scheduler_service import SchedulerService
from app.state.sqlite import SQLiteState


class BlockingWorkManager:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.persisted = False

    def recover_overdue(self):
        pass

    async def dispatch_due(self):
        self.started.set()
        await self.release.wait()
        self.persisted = True


class FakeScheduler:
    def __init__(self):
        self.paused = False
        self.shutdown_calls = []

    def pause(self):
        self.paused = True

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


class SchedulerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_failure_does_not_log_exception_text(self):
        secret = "raw-dispatch-secret"

        class FailingWorkManager:
            async def dispatch_due(self):
                raise RuntimeError(secret)

        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.db")
            state.initialize()
            logger = logging.getLogger("test_scheduler_safe_logging")
            service = SchedulerService(
                state=state,
                work_manager=FailingWorkManager(),
                logger=logger,
            )

            with self.assertLogs(logger.name, level="ERROR") as captured:
                await service._dispatch()

        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertIn("RuntimeError", output)

    async def test_stop_waits_for_active_dispatch_before_scheduler_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteState(Path(directory) / "state.db")
            state.initialize()
            work = BlockingWorkManager()
            service = SchedulerService(state=state, work_manager=work)
            scheduler = FakeScheduler()
            service.scheduler = scheduler
            service.running = True

            dispatch = asyncio.create_task(service._dispatch())
            await work.started.wait()
            stop = asyncio.create_task(service.stop())
            await asyncio.sleep(0)

            self.assertTrue(scheduler.paused)
            self.assertFalse(stop.done())
            self.assertEqual(scheduler.shutdown_calls, [])

            work.release.set()
            await stop
            await dispatch

            self.assertTrue(work.persisted)
            self.assertEqual(scheduler.shutdown_calls, [False])
            self.assertIsNone(service._active_dispatch)


if __name__ == "__main__":
    unittest.main()
