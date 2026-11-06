import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from media_manager.web.services import transcode_scheduler as sched


class TestTranscodeScheduler(unittest.TestCase):
    def test_housekeeping_sweeps_purges_and_notifies(self):
        from media_manager.web.services import integration_health, transcode_notify
        with patch.object(sched.transcode_jobs, "sweep_expired_leases", AsyncMock(return_value={})) as sweep, \
             patch.object(integration_health, "run_health_checks", AsyncMock()) as health, \
             patch.object(transcode_notify, "purge_expired_backups", AsyncMock(side_effect=RuntimeError("disk"))), \
             patch.object(transcode_notify, "run_notifications", AsyncMock()) as notify, \
             patch.object(sched, "_maybe_auto_queue", AsyncMock()) as auto:
            asyncio.run(sched.run_transcode_housekeeping())
        auto.assert_awaited_once()
        sweep.assert_awaited_once()
        notify.assert_awaited_once()  # still runs after the purge failed
        health.assert_awaited_once()

    def test_auto_queue_runs_first_sweep_then_hourly(self):
        sched._last_auto_queue = None
        with patch.object(sched.transcode_jobs, "enqueue_new", AsyncMock(return_value={})) as enqueue, \
             patch.object(sched, "_clock", side_effect=[1000.0, 1100.0, 1000.0 + sched.AUTO_QUEUE_INTERVAL_SECONDS]):
            for _ in range(3):  # first sweep, 100 s later (skipped), an hour later
                asyncio.run(sched._maybe_auto_queue(None))
        self.assertEqual(enqueue.await_count, 2)
        sched._last_auto_queue = None

    def test_loop_survives_errors_until_cancelled(self):
        sleeps = {"n": 0}

        async def fake_sleep(_):
            sleeps["n"] += 1
            if sleeps["n"] > 2:
                raise asyncio.CancelledError

        work = AsyncMock(side_effect=[RuntimeError("db locked"), None])
        with patch.object(sched.asyncio, "sleep", fake_sleep), \
             patch.object(sched, "run_transcode_housekeeping", work):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sched.transcode_scheduler())
        self.assertEqual(work.await_count, 2)


if __name__ == "__main__":
    unittest.main()
