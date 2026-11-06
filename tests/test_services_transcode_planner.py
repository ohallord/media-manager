import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import delete

from media_manager.database import AsyncSessionLocal
from media_manager.models import MediaItem, TranscodeControl, TranscodeSettings
from media_manager.transcode.profiles import DEFAULTS
from media_manager.web.services import transcode_planner as planner

PREFIX = "/_ut_planner_/"


async def _reset():
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MediaItem).where(MediaItem.path.like(PREFIX + "%")))
        await db.execute(delete(TranscodeSettings))
        await db.execute(delete(TranscodeControl))
        await db.commit()


def _seed(name, **kw):
    fields = dict(path=PREFIX + name, storage_volume="/_ut_planner_", title=name, media_type="movie",
                  resolution="1080p", height=1080, width=1920, video_codec="h264", audio_codec="aac",
                  audio_channels=2, bitrate=11_000_000, video_bitrate=10_000_000, size_bytes=8_000_000_000,
                  quality_score=1, file_created_at=datetime.utcnow(), hdr_format="sdr", bit_depth=8,
                  pix_fmt="yuv420p", field_order="progressive", duration_seconds=6000, probe_version=1)
    fields.update(kw)

    async def run():
        async with AsyncSessionLocal() as db:
            item = MediaItem(**fields)
            db.add(item)
            await db.commit()
            return item.id
    return asyncio.run(run())


def _db(fn):
    async def run():
        async with AsyncSessionLocal() as db:
            return await fn(db)
    return asyncio.run(run())


class TestSettings(unittest.TestCase):
    def setUp(self):
        asyncio.run(_reset())

    def tearDown(self):
        asyncio.run(_reset())

    def test_defaults_then_partial_update_persists(self):
        self.assertEqual(_db(lambda db: planner.get_settings(db, "tv")), DEFAULTS["tv"])
        updated = _db(lambda db: planner.update_settings(db, "tv", {"cq_1080": 27, "skip_hdr": False}))
        self.assertEqual(updated["cq_1080"], 27)
        self.assertEqual(updated["preset"], DEFAULTS["tv"]["preset"])
        again = _db(lambda db: planner.update_settings(db, "tv", {"preset": "p4"}))
        self.assertEqual((again["cq_1080"], again["preset"], again["skip_hdr"]), (27, "p4", False))

    def test_merge_ignores_unknown_stored_keys(self):
        self.assertNotIn("bogus", planner.merge_settings("movie", {"bogus": 1}))

    def test_validation(self):
        bad = [
            {"nope": 1}, {"skip_hdr": "yes"}, {"cq_1080": -1}, {"cq_1080": True}, {"cq_1080": 60},
            {"preset": 7}, {"preset": "p9"}, {"multipass": "full"},
        ]
        for update in bad:
            with self.subTest(update=update), self.assertRaises(ValueError):
                planner.validate_settings_update("movie", update)
        self.assertEqual(planner.validate_settings_update("movie", {"multipass": "disabled"}), {"multipass": "disabled"})


class TestControlAndReport(unittest.TestCase):
    def setUp(self):
        asyncio.run(_reset())
        self.ok = _seed("ok.mkv")
        self.hevc = _seed("hevc.mkv", video_codec="hevc")
        self.tv4k = _seed("tv4k.mkv", media_type="tv", height=2160, width=3840, video_bitrate=15_000_000)
        self.excluded = _seed("excl/x.mkv")

    def tearDown(self):
        asyncio.run(_reset())

    def test_control_defaults(self):
        control = _db(lambda db: planner.get_or_create_control(db))
        d = planner.control_to_dict(control)
        self.assertEqual(d["run_state"], "paused")
        self.assertEqual(d["mode"], "off")
        self.assertIn("h264", d["source_codecs"])
        self.assertEqual(d["excluded_prefixes"], [])
        self.assertIsInstance(d["always_excluded_prefixes"], list)

    def test_report_and_candidates(self):
        async def exclude(db):
            control = await planner.get_or_create_control(db)
            control.excluded_prefixes_json = '["%sexcl/"]' % PREFIX
            await db.commit()
        _db(exclude)
        report = _db(planner.compute_dry_run_report)
        self.assertGreaterEqual(report["totals"]["eligible"], 2)
        self.assertGreater(report["totals"]["estimated_savings_bytes"], 0)
        self.assertGreater(report["totals"]["estimated_encode_hours"], 0)
        reasons = {r["reason"]: r for r in report["by_reason"]}
        self.assertGreaterEqual(reasons["already_hevc"]["count"], 1)
        self.assertGreaterEqual(reasons["excluded_path"]["count"], 1)
        self.assertEqual(reasons["excluded_path"]["label"], "In an excluded folder")
        self.assertTrue(any(b["bucket"] == "4k_down" and b["media_type"] == "tv" for b in report["by_bucket"]))

        cands = _db(lambda db: planner.list_candidates(db, "tv", 10, 0))
        mine = [c for c in cands["items"] if c["path"].startswith(PREFIX)]
        self.assertEqual([c["id"] for c in mine], [self.tv4k])
        self.assertEqual(mine[0]["plan"]["scale_width"], 1920)
        all_c = _db(lambda db: planner.list_candidates(db, None, 500, 0))["items"]
        savings = [c["estimated_savings_bytes"] for c in all_c]
        self.assertEqual(savings, sorted(savings, reverse=True))

    def test_item_decision(self):
        self.assertTrue(_db(lambda db: planner.get_item_decision(db, self.ok))["decision"].eligible)
        self.assertEqual(_db(lambda db: planner.get_item_decision(db, self.hevc))["decision"].reason, "already_hevc")
        self.assertIsNone(_db(lambda db: planner.get_item_decision(db, "nope")))

    def test_watcher_source_dirs_always_excluded(self):
        with patch.object(planner, "SOURCE_DIRECTORIES", [{"from_dir": "/dl/b"}, {"from_dir": "/dl/a"}, {"to_dir": "/x"}]):
            self.assertEqual(planner.watcher_source_prefixes(), ["/dl/a", "/dl/b"])


if __name__ == "__main__":
    unittest.main()
