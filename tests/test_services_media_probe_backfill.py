import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.metadata import PROBE_VERSION
from media_manager.models import MediaItem
from media_manager.web.services import media_probe_backfill as backfill

PREFIX = "/_ut_probe_backfill_/"


async def _cleanup():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MediaItem).where(MediaItem.path.like(PREFIX + "%")))
        await db.commit()


def _seed(name, probe_version):
    async def run():
        async with AsyncSessionLocal() as db:
            db.add(MediaItem(
                path=PREFIX + name, storage_volume="/_ut_probe_backfill_", title=name,
                media_type="movie", resolution="1080p", height=1080, width=1920,
                video_codec="h264", audio_codec="aac", audio_channels=2, bitrate=1,
                size_bytes=1, quality_score=1, file_created_at=datetime.utcnow(),
                probe_version=probe_version,
            ))
            await db.commit()
    asyncio.run(run())


class TestTargetSelection(unittest.TestCase):
    def setUp(self):
        asyncio.run(_cleanup())
        _seed("never.mkv", None)
        _seed("current.mkv", PROBE_VERSION)
        _seed("old.mkv", PROBE_VERSION - 1)

    def tearDown(self):
        asyncio.run(_cleanup())

    def test_selects_unprobed_and_outdated_rows_only(self):
        targets = asyncio.run(backfill._select_targets())
        mine = {path for path, _ in targets if path.startswith(PREFIX)}
        self.assertEqual(mine, {PREFIX + "never.mkv", PREFIX + "old.mkv"})

    def test_coverage_counts_are_consistent(self):
        cov = asyncio.run(backfill.get_coverage())
        self.assertEqual(cov["probed_items"] + cov["pending_items"], cov["total_items"])
        self.assertGreaterEqual(cov["pending_items"], 2)
        self.assertGreaterEqual(cov["probed_items"], 1)


class TestRescoreAll(unittest.TestCase):
    def setUp(self):
        asyncio.run(_cleanup())

    def tearDown(self):
        asyncio.run(_cleanup())

    def test_updates_only_changed_scores(self):
        async def seed_and_rescore():
            async with AsyncSessionLocal() as db:
                db.add(MediaItem(
                    path=PREFIX + "hevc.mkv", storage_volume="/_ut_probe_backfill_", title="hevc",
                    media_type="movie", resolution="1080p", height=1080, width=1920,
                    video_codec="hevc", audio_codec="aac", audio_channels=2, bitrate=6_000_000,
                    size_bytes=1, quality_score=1, file_created_at=datetime.utcnow(),
                ))
                await db.commit()
            with patch.object(backfill, "RESCORE_BATCH_SIZE", 2):  # exercise batching
                first = await backfill.rescore_all()
                second = await backfill.rescore_all()
            async with AsyncSessionLocal() as db:
                row = (await db.execute(select(MediaItem).where(MediaItem.path == PREFIX + "hevc.mkv"))).scalar_one()
            return first, second, row

        first, second, row = asyncio.run(seed_and_rescore())
        self.assertGreaterEqual(first["changed"], 1)
        self.assertEqual(second["changed"], 0)  # idempotent
        from media_manager.quality import calculate_quality_score
        h264_equivalent = calculate_quality_score("1080p", 1080, "aac", 2, 12_000_000, path=row.path)
        self.assertEqual(row.quality_score, h264_equivalent)  # 6 Mbps HEVC scores as 12 Mbps h264
        self.assertGreaterEqual(first["total"], 1)


class TestRunProbeBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mm_probe_backfill_")
        self.paths = {}
        for name, content in [("ok.mkv", b"x"), ("bad.mkv", b"x"), ("boom.mkv", b"x"), ("empty.mkv", b"")]:
            path = os.path.join(self.tmp, name)
            with open(path, "wb") as fh:
                fh.write(content)
            self.paths[name] = path
        self.paths["missing.mkv"] = os.path.join(self.tmp, "missing.mkv")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_counts_every_outcome(self):
        targets = [(self.paths[n], "/vol_ok") for n in ("ok.mkv", "bad.mkv", "boom.mkv", "empty.mkv", "missing.mkv")]
        targets.append((self.paths["ok.mkv"], "/vol_down"))

        def fake_process(path):
            if path.endswith("boom.mkv"):
                raise RuntimeError("ffprobe exploded")
            return path.endswith("ok.mkv")

        with patch.object(backfill, "_select_targets", AsyncMock(return_value=targets)), \
             patch.object(backfill, "rescore_all", AsyncMock(return_value={"total": 6, "changed": 2})), \
             patch.object(backfill, "volume_is_healthy", side_effect=lambda v: v == "/vol_ok"), \
             patch.object(backfill, "process_file", side_effect=fake_process) as mock_process:
            status = asyncio.run(backfill.run_probe_backfill())

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["total"], 6)
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["failed"], 2)
        self.assertEqual(status["skipped_missing"], 1)
        self.assertEqual(status["skipped_empty"], 1)
        self.assertEqual(status["skipped_unhealthy_volume"], 1)
        self.assertEqual(status["rescored"], 2)
        self.assertIsNotNone(status["finished_at"])
        probed = {call.args[0] for call in mock_process.call_args_list}
        self.assertNotIn(self.paths["empty.mkv"], probed)
        self.assertNotIn(self.paths["missing.mkv"], probed)

    def test_aborts_cleanly_when_target_query_fails(self):
        with patch.object(backfill, "_select_targets", AsyncMock(side_effect=RuntimeError("db locked"))):
            status = asyncio.run(backfill.run_probe_backfill())
        self.assertEqual(status["state"], "failed")
        self.assertIn("db locked", status["error"])
        self.assertIsNotNone(status["finished_at"])

    def test_get_status_is_a_copy(self):
        status = backfill.get_status()
        status["state"] = "tampered"
        self.assertNotEqual(backfill.get_status()["state"], "tampered")


class TestStartProbeBackfill(unittest.TestCase):
    def test_only_one_backfill_runs_at_a_time(self):
        async def scenario():
            release = asyncio.Event()

            async def fake_run():
                await release.wait()

            with patch.object(backfill, "run_probe_backfill", fake_run):
                self.assertTrue(backfill.start_probe_backfill())
                self.assertTrue(backfill.is_running())
                self.assertFalse(backfill.start_probe_backfill())
                release.set()
                await backfill._task
            self.assertFalse(backfill.is_running())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
