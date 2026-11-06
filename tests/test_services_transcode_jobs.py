import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import delete, select, update

from media_manager.database import AsyncSessionLocal, SyncSessionLocal
from media_manager.models import (
    FileMoveLog, MediaItem, PlaybackStat, ServiceHeartbeat, TranscodeControl, TranscodeJob, TranscodeSettings,
)
from media_manager.transcode.errors import classify
from media_manager.transcode.protocol import PROTOCOL_VERSION, is_compatible
from media_manager.web.services import transcode_jobs as jobs

W = "worker-1"


def _db(fn):
    async def run():
        async with AsyncSessionLocal() as db:
            return await fn(db)
    return asyncio.run(run())


class JobTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mm_jobs_")
        self.item_ids = []
        _db(self._reset)

    def tearDown(self):
        _db(self._reset)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _reset(self, db):
        await db.execute(delete(TranscodeJob))
        await db.execute(delete(TranscodeControl))
        await db.execute(delete(TranscodeSettings))
        await db.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.service == jobs.WORKER_SERVICE))
        await db.execute(delete(FileMoveLog).where(FileMoveLog.status == "TRANSCODED"))
        await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(self.item_ids)))
        await db.execute(delete(MediaItem).where(MediaItem.path.like(self.tmp + "%")))
        await db.commit()

    def _seed(self, name, **kw):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 10)
        fields = dict(path=path, storage_volume=self.tmp, title=name, media_type="movie", resolution="1080p",
                      height=1080, width=1920, video_codec="h264", audio_codec="aac", audio_channels=2,
                      bitrate=11_000_000, video_bitrate=10_000_000, size_bytes=8_000_000_000, quality_score=600,
                      file_created_at=datetime.utcnow(), hdr_format="sdr", bit_depth=8, pix_fmt="yuv420p",
                      field_order="progressive", duration_seconds=6000, probe_version=1)
        fields.update(kw)

        async def add(db):
            item = MediaItem(**fields)
            db.add(item)
            await db.commit()
            return item.id
        item_id = _db(add)
        self.item_ids.append(item_id)
        return item_id

    def _control(self, run_state="running", mode="full"):
        async def set_(db):
            from media_manager.web.services.transcode_planner import get_or_create_control
            c = await get_or_create_control(db)
            c.run_state, c.mode = run_state, mode
            await db.commit()
        _db(set_)

    def _job(self, job_id):
        return _db(lambda db: jobs.get_job(db, job_id))

    def _claim(self):
        return _db(lambda db: jobs.claim(db, W))


class TestEnqueueAndClaim(JobTestBase):
    def test_savings_split_by_type_with_queued_estimate(self):
        async def add(db):
            for state, media_type, source, output, est in [
                    ("done", "movie", 30, 10, 0), ("done", "movie", 20, 5, 0), ("done", "tv", 8, 3, 0),
                    ("done", None, 4, 2, 0), ("pending", "tv", 9, None, 6), ("running", "movie", 7, None, 4),
                    ("failed", "movie", 50, None, 25), ("skipped", "tv", 40, None, 20)]:
                db.add(TranscodeJob(state=state, media_type=media_type, source_path=f"/x/{state}{source}",
                                    source_size=source, output_size=output, est_savings_bytes=est))
            await db.commit()
        _db(add)
        savings = _db(jobs.get_savings)
        self.assertEqual((savings["files"], savings["source_bytes"], savings["output_bytes"], savings["saved_bytes"]),
                         (4, 62, 20, 42))
        self.assertEqual(savings["by_type"]["movie"],
                         {"files": 3, "source_bytes": 54, "output_bytes": 17, "saved_bytes": 37})  # untyped counts as movie
        self.assertEqual(savings["by_type"]["tv"]["saved_bytes"], 5)
        self.assertEqual((savings["remaining_files"], savings["remaining_estimated_bytes"]), (2, 10))
        _db(self._reset)
        empty = _db(jobs.get_savings)
        self.assertEqual((empty["saved_bytes"], empty["by_type"], empty["remaining_estimated_bytes"]), (0, {}, 0))

    def test_enqueue_skips_ineligible_duplicates_and_missing(self):
        ok = self._seed("a.mkv")
        hevc = self._seed("b.mkv", video_codec="hevc")
        out = _db(lambda db: jobs.enqueue(db, [ok, ok, hevc, "nope"], pilot=True))
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual({s["reason"] for s in out["skipped"]}, {"already_hevc", "not_found"})
        again = _db(lambda db: jobs.enqueue(db, [ok], pilot=True))
        self.assertEqual(again["skipped"], [{"media_item_id": ok, "reason": "already_queued"}])
        job = self._job(out["created"][0])
        self.assertTrue(job.is_pilot)
        self.assertGreater(job.est_savings_bytes, 0)
        self.assertEqual(json.loads(job.plan_json)["bucket"], "1080")
        self.assertIsNotNone(job.source_mtime)

    def test_enqueue_all_is_idempotent(self):
        self._seed("a.mkv")
        self._seed("b.mkv")
        first = _db(jobs.enqueue_all)["created_count"]
        self.assertGreaterEqual(first, 2)
        self.assertEqual(_db(jobs.enqueue_all)["created_count"], 0)

    def test_enqueue_new_only_full_mode_never_seen_and_settled(self):
        old = self._seed("old.mkv")
        skipped_before = self._seed("skipped.mkv")
        fresh = self._seed("fresh.mkv")
        self._seed("gone.mkv", path=os.path.join(self.tmp, "missing.mkv"))
        _db(lambda db: jobs.enqueue(db, [skipped_before], pilot=False))

        async def mark_skipped(db):
            await db.execute(update(TranscodeJob).values(state="skipped", error_code="predicted_not_smaller"))
            await db.commit()
        _db(mark_skipped)

        def queued_ids():
            async def q(db):
                rows = await db.execute(select(TranscodeJob.media_item_id).where(TranscodeJob.state == "pending"))
                return {r[0] for r in rows.all()}
            return _db(q)

        self._control(mode="pilot")
        self.assertEqual(_db(jobs.enqueue_new)["created_count"], 0)
        self._control(mode="full", run_state="paused")
        hour_ago = time.time() - jobs.AUTO_QUEUE_MIN_AGE_SECONDS - 1
        real_stat = os.stat

        def fake_stat(path, *a, **kw):  # only "fresh.mkv" was touched within the last hour
            st = real_stat(path, *a, **kw)
            if path.endswith("fresh.mkv"):
                return st
            return os.stat_result(st[:9] + (hour_ago,))
        with patch.object(jobs.os, "stat", side_effect=fake_stat), self.assertLogs(level="INFO"):
            self.assertGreaterEqual(_db(jobs.enqueue_new)["created_count"], 1)
        mine = queued_ids() & {old, skipped_before, fresh}
        self.assertEqual(mine, {old})  # skipped one not retried, fresh one waits, missing file ignored
        with patch.object(jobs.os, "stat", side_effect=fake_stat):
            self.assertEqual(_db(jobs.enqueue_new)["created_count"], 0)

    def test_claim_respects_run_state_mode_and_priority(self):
        small = self._seed("small.mkv", size_bytes=2_000_000_000, video_bitrate=5_000_000, bitrate=6_000_000)
        big = self._seed("big.mkv", size_bytes=20_000_000_000)
        pilot = self._seed("pilot.mkv", size_bytes=1_000_000_000, video_bitrate=5_000_000, bitrate=6_000_000)
        _db(lambda db: jobs.enqueue(db, [small, big], pilot=False))
        _db(lambda db: jobs.enqueue(db, [pilot], pilot=True))

        self._control(run_state="paused")
        self.assertIsNone(self._claim())
        self._control(mode="off")
        self.assertIsNone(self._claim())

        self._control(mode="pilot")
        first = self._claim()
        self.assertEqual(first.media_item_id, pilot)
        self.assertEqual((first.state, first.phase, first.lease_owner, first.attempts), ("claimed", "preparing", W, 1))
        self.assertIsNone(self._claim())  # pilot mode: only pilot jobs

        self._control(mode="full")
        self.assertEqual(self._claim().media_item_id, big)  # biggest savings first
        self.assertEqual(self._claim().media_item_id, small)
        self.assertIsNone(self._claim())

    def test_claim_skips_jobs_that_became_ineligible(self):
        item = self._seed("a.mkv")
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))

        async def make_hevc(db):
            row = (await db.execute(select(MediaItem).where(MediaItem.id == item))).scalar_one()
            row.video_codec = "hevc"
            await db.commit()
        _db(make_hevc)
        self._control()
        self.assertIsNone(self._claim())
        job = _db(lambda db: jobs.list_jobs(db))["items"][0]
        self.assertEqual((job["state"], job["error_code"], job["error_message"]), ("skipped", "not_eligible", "already_hevc"))

    def test_claim_race_loses_cleanly(self):
        item = self._seed("a.mkv")
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))
        self._control()
        real_get = jobs.planner.get_item_decision

        async def steal_then_decide(db, media_item_id):
            # Another worker claims the row between our SELECT and UPDATE.
            async with AsyncSessionLocal() as other:
                await other.execute(TranscodeJob.__table__.update().values(state="claimed", lease_owner="other"))
                await other.commit()
            return await real_get(db, media_item_id)

        with patch.object(jobs.planner, "get_item_decision", steal_then_decide):
            self.assertIsNone(self._claim())
        self.assertEqual(_db(lambda db: jobs.list_jobs(db))["items"][0]["lease_owner"], "other")


class TestLifecycle(JobTestBase):
    def _claimed(self, **kw):
        item = self._seed("a.mkv", **kw)
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))
        self._control()
        return item, self._claim()

    def test_progress_phase_and_ownership(self):
        _, job = self._claimed()
        self.assertEqual(_db(lambda db: jobs.report_progress(db, job.id, W, 150, 400.0, 30)), {"cancel": False})
        row = self._job(job.id)
        self.assertEqual((row.state, row.progress_pct, row.fps), ("running", 100.0, 400.0))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.report_progress(db, job.id, "intruder"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.report_progress(db, "missing", W))
        _db(lambda db: jobs.set_phase(db, job.id, W, "installing", {"temp_path": "/t", "output_path": "/o"}))
        row = self._job(job.id)
        self.assertEqual((row.phase, row.temp_path, row.output_path), ("installing", "/t", "/o"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.set_phase(db, job.id, W, "encoding"))  # backwards
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.set_phase(db, job.id, W, "dancing"))

    def test_phase_on_claimed_job_marks_running_and_finished_job_rejected(self):
        _, job = self._claimed()
        self.assertEqual(_db(lambda db: jobs.set_phase(db, job.id, W, "encoding")).state, "running")
        _db(lambda db: jobs.fail(db, job.id, W, "not_smaller"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.set_phase(db, job.id, W, "verifying"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.report_progress(db, job.id, W))

    def test_cancel_signals_worker_and_blocks_mid_swap(self):
        _, job = self._claimed()
        _db(lambda db: jobs.cancel(db, job.id))
        self.assertEqual(_db(lambda db: jobs.report_progress(db, job.id, W)), {"cancel": True})
        self.assertEqual(_db(lambda db: jobs.fail(db, job.id, W, "worker_shutdown")).state, "cancelled")
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.cancel(db, job.id))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.cancel(db, "missing"))
        _db(self._reset)
        _, job = self._claimed()
        _db(lambda db: jobs.set_phase(db, job.id, W, "swapping"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.cancel(db, job.id))

    def test_fail_policy(self):
        _, job = self._claimed()
        row = _db(lambda db: jobs.fail(db, job.id, W, "gpu_busy", "sessions full"))
        self.assertEqual((row.state, row.attempts), ("pending", 0))  # free requeue
        job = self._claim()
        row = _db(lambda db: jobs.fail(db, job.id, W, "copy_failed"))
        self.assertEqual((row.state, row.attempts), ("pending", 1))  # first retry
        job = self._claim()
        row = _db(lambda db: jobs.fail(db, job.id, W, "copy_failed"))
        self.assertEqual(row.state, "failed")  # out of attempts
        self.assertIsNotNone(row.finished_at)
        _db(lambda db: jobs.requeue(db, job.id))
        job = self._claim()
        row = _db(lambda db: jobs.fail(db, job.id, W, "not_smaller", "only 3% smaller"))
        self.assertEqual((row.state, row.error_message), ("skipped", "only 3% smaller"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.fail(db, job.id, W, "copy_failed"))

    def test_requeue_rules(self):
        item, job = self._claimed()
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.requeue(db, job.id))  # still active
        _db(lambda db: jobs.fail(db, job.id, W, "not_smaller"))
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))  # a new active job for the same file
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.requeue(db, job.id))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.requeue(db, "missing"))

    def test_lease_expiry(self):
        _, job = self._claimed()

        async def expire(db, phase, attempts=1):
            row = (await db.execute(select(TranscodeJob).where(TranscodeJob.id == job.id))).scalar_one()
            row.state, row.phase, row.attempts, row.lease_owner = "running", phase, attempts, W
            row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
            await db.commit()

        _db(lambda db: expire(db, "encoding"))
        self.assertEqual(_db(jobs.sweep_expired_leases), {"requeued": 1, "needs_recovery": 0, "skipped": 0})
        self.assertEqual((self._job(job.id).state, self._job(job.id).error_code), ("pending", "worker_lost"))

        _db(lambda db: expire(db, "encoding", attempts=2))
        self.assertEqual(_db(jobs.sweep_expired_leases)["skipped"], 1)

        _db(lambda db: expire(db, "swapping"))
        self.assertEqual(_db(jobs.sweep_expired_leases)["needs_recovery"], 1)
        self.assertEqual(self._job(job.id).state, "needs_recovery")
        self.assertEqual(_db(jobs.sweep_expired_leases), {"requeued": 0, "needs_recovery": 0, "skipped": 0})

        # The (restarted) worker takes over recovery jobs on startup.
        recovered = _db(lambda db: jobs.inflight(db, "worker-2"))
        self.assertEqual([j.id for j in recovered], [job.id])
        self.assertEqual(self._job(job.id).lease_owner, "worker-2")

    def test_swap_lock_sync(self):
        item, job = self._claimed()
        session = SyncSessionLocal()
        try:
            self.assertEqual(jobs.swap_locked_sync(session), {"item_ids": set(), "output_paths": set()})
            _db(lambda db: jobs.set_phase(db, job.id, W, "swapping", {"output_path": "/new.mkv"}))
            self.assertEqual(jobs.swap_locked_sync(session), {"item_ids": {item}, "output_paths": {"/new.mkv"}})
        finally:
            session.close()


class TestComplete(JobTestBase):
    PROBE = {"format": {"format_name": "matroska,webm", "duration": "6000", "bit_rate": "5000000", "size": "3750000000"},
             "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
                          "pix_fmt": "yuv420p10le", "profile": "Main 10", "tags": {"BPS": "4200000"}},
                         {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6}]}

    def test_complete_updates_row_in_place_merges_stray_and_is_idempotent(self):
        item = self._seed("movie.mp4")
        _db(lambda db: jobs.enqueue(db, [item], pilot=True))
        self._control()
        job = self._claim()
        new_path = os.path.join(self.tmp, "movie.mkv")
        with open(new_path, "wb") as fh:
            fh.write(b"y")
        stray = self._seed("movie.mkv")  # a scan indexed the new file mid-swap

        async def add_stat(db):
            db.add(PlaybackStat(media_item_id=stray, source="plex", play_count=1))
            await db.commit()
        _db(add_stat)

        payload = {"output_path": new_path, "output_size": 3_750_000_000, "output_hash": "abc",
                   "backup_path": "/b", "probe": self.PROBE, "verify": {"ok": True}}
        done = _db(lambda db: jobs.complete(db, job.id, W, payload))
        self.assertEqual((done.state, done.phase, done.output_hash, done.backup_path), ("done", "done", "abc", "/b"))

        async def rows(db):
            return (await db.execute(select(MediaItem).where(MediaItem.path.like(self.tmp + "%")))).scalars().all()
        remaining = _db(rows)
        self.assertEqual([r.id for r in remaining], [item])  # same row, stray merged away
        row = remaining[0]
        self.assertEqual((row.path, row.video_codec, row.size_bytes, row.audio_codec), (new_path, "hevc", 3_750_000_000, "ac3"))
        self.assertEqual((row.bit_depth, row.video_bitrate, row.container), (10, 4_200_000, "matroska,webm"))

        logs = _db(lambda db: db.execute(select(FileMoveLog).where(FileMoveLog.status == "TRANSCODED")))
        self.assertEqual(len(logs.scalars().all()), 1)
        again = _db(lambda db: jobs.complete(db, job.id, "anyone", payload))
        self.assertEqual(again.state, "done")

        status = _db(jobs.get_status)
        self.assertEqual(status["saved_bytes"], 8_000_000_000 - 3_750_000_000)
        savings = status["savings"]
        self.assertEqual((savings["files"], savings["source_bytes"], savings["output_bytes"], savings["saved_bytes"]),
                         (1, 8_000_000_000, 3_750_000_000, 4_250_000_000))
        self.assertEqual(list(savings["by_type"]), ["movie"])
        self.assertEqual(savings["by_type"]["movie"]["saved_bytes"], 4_250_000_000)
        self.assertEqual((savings["remaining_files"], savings["remaining_estimated_bytes"]), (0, 0))
        self.assertEqual(status["counts"]["done"], 1)
        listed = _db(lambda db: jobs.list_jobs(db, "done"))
        self.assertEqual(listed["items"][0]["saved_bytes"], 4_250_000_000)

    def test_complete_without_probe_and_rejections(self):
        item = self._seed("a.mkv")
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))
        self._control()
        job = self._claim()
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.complete(db, "missing", W, {"output_path": "/x"}))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.complete(db, job.id, "intruder", {"output_path": "/x"}))
        done = _db(lambda db: jobs.complete(db, job.id, W, {"output_path": os.path.join(self.tmp, "gone.mkv"),
                                                            "output_size": 5}))
        self.assertEqual(done.state, "done")
        _db(self._reset)
        item = self._seed("b.mkv")
        _db(lambda db: jobs.enqueue(db, [item], pilot=False))
        self._control()
        job = self._claim()
        _db(lambda db: jobs.fail(db, job.id, W, "not_smaller"))
        with self.assertRaises(jobs.JobError):
            _db(lambda db: jobs.complete(db, job.id, W, {"output_path": "/x"}))


class TestHeartbeatAndStatus(JobTestBase):
    def test_heartbeat_and_worker_status(self):
        self.assertIsNone(_db(jobs.get_status)["worker"])
        _db(lambda db: jobs.record_heartbeat(db, "host-a", "9.9.9", {"pid": "123", "gpu": "ok"}))
        _db(lambda db: jobs.record_heartbeat(db, "host-a", "9.9.9", {"pid": "not-a-number"}))
        worker = _db(jobs.get_status)["worker"]
        self.assertEqual((worker["worker_id"], worker["version"], worker["online"]), ("host-a", "9.9.9", True))
        self.assertFalse(worker["version_matches"])
        self.assertEqual(worker["detail"], {"pid": "not-a-number"})


class TestHeartbeatDuringJobs(JobTestBase):
    """A long job never calls /heartbeat: lease renewals must keep the worker 'online'."""

    _claimed = TestLifecycle._claimed

    def _age_heartbeat(self, seconds):
        async def age(db):
            row = (await db.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.service == jobs.WORKER_SERVICE))).scalar_one()
            row.last_beat_at = datetime.utcnow() - timedelta(seconds=seconds)
            await db.commit()
        _db(age)

    def test_progress_and_phase_refresh_the_worker_heartbeat(self):
        _, job = self._claimed()
        _db(lambda db: jobs.record_heartbeat(db, W, "1.0", {}))
        self._age_heartbeat(900)
        self.assertFalse(_db(jobs.get_status)["worker"]["online"])
        _db(lambda db: jobs.report_progress(db, job.id, W))
        self.assertTrue(_db(jobs.get_status)["worker"]["online"])
        self._age_heartbeat(900)
        _db(lambda db: jobs.set_phase(db, job.id, W, "verifying"))
        worker = _db(jobs.get_status)["worker"]
        self.assertTrue(worker["online"])
        self.assertEqual(worker["version"], "1.0")  # renewals don't clobber what /heartbeat reported

    def test_other_worker_or_no_heartbeat_row_is_left_alone(self):
        _, job = self._claimed()
        _db(lambda db: jobs.report_progress(db, job.id, W))  # no heartbeat row yet: nothing to touch
        self.assertIsNone(_db(jobs.get_status)["worker"])
        _db(lambda db: jobs.record_heartbeat(db, "other-host", "1.0", {}))
        self._age_heartbeat(900)
        _db(lambda db: jobs.report_progress(db, job.id, W))
        self.assertFalse(_db(jobs.get_status)["worker"]["online"])


class TestErrorsAndProtocol(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("gpu_busy"), "requeue")
        self.assertEqual(classify("not_smaller"), "skip")
        self.assertEqual(classify("copy_failed"), "retry")
        self.assertEqual(classify("never-heard-of-it"), "retry")

    def test_protocol(self):
        self.assertTrue(is_compatible(PROTOCOL_VERSION))
        self.assertTrue(is_compatible(str(PROTOCOL_VERSION)))
        self.assertFalse(is_compatible(PROTOCOL_VERSION + 1))
        self.assertFalse(is_compatible(0))
        self.assertFalse(is_compatible("x"))
        self.assertFalse(is_compatible(None))


if __name__ == "__main__":
    unittest.main()
