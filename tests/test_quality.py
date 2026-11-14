import os
import tempfile
import unittest
from datetime import datetime

from media_manager.database import SyncSessionLocal
from media_manager.models import MediaItem, DuplicateGroup
from media_manager.quality import (
    CODEC_EFFICIENCY,
    effective_bitrate,
    parse_path_qualities,
    calculate_quality_score,
    tv_series_name,
    update_duplicate_groups,
    volume_is_healthy,
)


class TestVolumeIsHealthy(unittest.TestCase):
    def test_empty_or_missing_volume_is_unhealthy(self):
        self.assertFalse(volume_is_healthy(""))
        self.assertFalse(volume_is_healthy(None))
        self.assertFalse(volume_is_healthy("/no/such/media-manager-volume-xyz"))

    def test_non_empty_directory_is_healthy(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "marker"), "w") as f:
                f.write("x")
            self.assertTrue(volume_is_healthy(d))
        finally:
            os.remove(os.path.join(d, "marker"))
            os.rmdir(d)

    def test_empty_directory_is_unhealthy(self):
        d = tempfile.mkdtemp()
        try:
            self.assertFalse(volume_is_healthy(d))
        finally:
            os.rmdir(d)


class TestParsePathQualities(unittest.TestCase):
    def test_remux_detected(self):
        self.assertEqual(parse_path_qualities("Movie.2020.2160p.REMUX.mkv")["source"], "remux")

    def test_bluray_detected(self):
        self.assertEqual(parse_path_qualities("Movie.2020.BluRay.mkv")["source"], "bluray")
        self.assertEqual(parse_path_qualities("Movie.2020.BDRip.mkv")["source"], "bluray")
        self.assertEqual(parse_path_qualities("Movie.2020.BRRip.mkv")["source"], "bluray")

    def test_webdl_variants_detected(self):
        for token in ["web-dl", "webdl", "amazon", "netflix", "hmax", "dnp"]:
            self.assertEqual(parse_path_qualities(f"Movie.2020.{token}.mkv")["source"], "web-dl")

    def test_webrip_detected(self):
        self.assertEqual(parse_path_qualities("Movie.2020.WEBRip.mkv")["source"], "webrip")

    def test_hdtv_detected(self):
        self.assertEqual(parse_path_qualities("Show.S01E01.HDTV.mkv")["source"], "hdtv")

    def test_dvd_detected(self):
        self.assertEqual(parse_path_qualities("Movie.2020.DVDRip.mkv")["source"], "dvd")

    def test_cam_detected(self):
        self.assertEqual(parse_path_qualities("Movie.2020.CAM.mkv")["source"], "cam")
        self.assertEqual(parse_path_qualities("Movie.2020.TS.mkv")["source"], "cam")
        self.assertEqual(parse_path_qualities("Movie.2020.TELESYNC.mkv")["source"], "cam")

    def test_ts_substring_in_temp_path_is_not_cam(self):
        self.assertEqual(
            parse_path_qualities("/tmp/tmpa1tsb2/Example.Movie.2010.1080p.WEB-DL.mkv")["source"],
            "web-dl",
        )

    def test_default_source_fallback(self):
        self.assertEqual(parse_path_qualities("Movie.2020.mkv")["source"], "web-dl")

    def test_dolby_vision_with_hdr10_fallback(self):
        attrs = parse_path_qualities("Movie.2020.DV.HDR10.mkv")
        self.assertEqual(attrs["hdr"], "dv_hdr10")

    def test_dolby_vision_only(self):
        attrs = parse_path_qualities("Movie.2020.DV.mkv")
        self.assertEqual(attrs["hdr"], "dv")

    def test_dv_substring_is_not_dolby_vision(self):
        self.assertEqual(
            parse_path_qualities("/tmp/mm_proc_file_dv12/Example.Movie.2010.1080p.WEB-DL.mkv")["hdr"],
            "sdr",
        )
        self.assertEqual(parse_path_qualities("Movie.2020.DVDRip.mkv")["hdr"], "sdr")

    def test_hdr10plus(self):
        self.assertEqual(parse_path_qualities("Movie.2020.HDR10Plus.mkv")["hdr"], "hdr10plus")

    def test_hdr10(self):
        self.assertEqual(parse_path_qualities("Movie.2020.HDR.mkv")["hdr"], "hdr10")

    def test_sdr_default(self):
        self.assertEqual(parse_path_qualities("Movie.2020.mkv")["hdr"], "sdr")

    def test_repack_and_proper_flags(self):
        self.assertTrue(parse_path_qualities("Movie.2020.REPACK.mkv")["repack"])
        self.assertTrue(parse_path_qualities("Movie.2020.PROPER.mkv")["repack"])
        self.assertFalse(parse_path_qualities("Movie.2020.mkv")["repack"])


class TestCalculateQualityScore(unittest.TestCase):
    def test_4k_by_height(self):
        score = calculate_quality_score("", 2160, "aac", 2, 0, path="movie.mkv")
        self.assertGreaterEqual(score, 1000)

    def test_4k_by_label_string(self):
        score = calculate_quality_score("2160p", 0, "aac", 2, 0, path="movie.mkv")
        self.assertGreaterEqual(score, 1000)

    def test_4k_label_lowercase_4k(self):
        score = calculate_quality_score("4k", 0, "aac", 2, 0, path="movie.mkv")
        self.assertGreaterEqual(score, 1000)

    def test_1080p_by_height(self):
        score = calculate_quality_score("", 1080, "aac", 2, 0, path="movie.mkv")
        self.assertGreaterEqual(score, 500)
        self.assertLess(score, 1000)

    def test_720p_by_height(self):
        score = calculate_quality_score("", 720, "aac", 2, 0, path="movie.mkv")
        self.assertGreaterEqual(score, 200)

    def test_sd_default(self):
        base_only = calculate_quality_score("", 0, "unknown", 2, 0, path="movie.mkv")
        self.assertEqual(base_only, 350)  # 50 base + 300 web-dl default source

    def test_remux_source_bonus(self):
        remux = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.remux.mkv")
        webdl = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.web-dl.mkv")
        self.assertGreater(remux, webdl)

    def test_cam_penalized(self):
        score = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.cam.mkv")
        self.assertLess(score, 0)

    def test_dolby_vision_without_hdr10_penalized(self):
        with_dv = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.dv.mkv")
        without_hdr = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.mkv")
        self.assertLess(with_dv, without_hdr)

    def test_audio_codec_scoring_ladder(self):
        atmos = calculate_quality_score("1080p", 1080, "Atmos", 8, 0, path="movie.mkv")
        truehd = calculate_quality_score("1080p", 1080, "TrueHD", 8, 0, path="movie.mkv")
        dtshd = calculate_quality_score("1080p", 1080, "DTS-HD", 6, 0, path="movie.mkv")
        dts = calculate_quality_score("1080p", 1080, "DTS", 6, 0, path="movie.mkv")
        eac3 = calculate_quality_score("1080p", 1080, "EAC3", 6, 0, path="movie.mkv")
        ac3 = calculate_quality_score("1080p", 1080, "AC3", 6, 0, path="movie.mkv")
        aac = calculate_quality_score("1080p", 1080, "AAC", 2, 0, path="movie.mkv")
        unknown = calculate_quality_score("1080p", 1080, "unknown", 2, 0, path="movie.mkv")
        self.assertGreater(atmos, truehd)
        self.assertGreater(truehd, dtshd)
        self.assertGreater(dtshd, dts)
        self.assertGreater(dts, eac3)
        self.assertGreater(eac3, ac3)
        self.assertGreater(ac3, aac)
        self.assertGreater(aac, unknown)

    def test_repack_bonus_applied(self):
        repack = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.repack.mkv")
        normal = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.mkv")
        self.assertEqual(repack - normal, 50)

    def test_bitrate_bonus_capped_at_150(self):
        huge_bitrate = calculate_quality_score("1080p", 1080, "aac", 2, 1_000_000_000, path="movie.mkv")
        no_bitrate = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.mkv")
        self.assertEqual(huge_bitrate - no_bitrate, 150)

    def test_zero_and_negative_bitrate_no_bonus(self):
        zero = calculate_quality_score("1080p", 1080, "aac", 2, 0, path="movie.mkv")
        negative = calculate_quality_score("1080p", 1080, "aac", 2, -100, path="movie.mkv")
        self.assertEqual(zero, negative)


class TestCodecEfficiency(unittest.TestCase):
    def _score(self, **kw):
        base = dict(resolution="1080p", height=1080, audio_codec="aac", audio_channels=2,
                    bitrate=10_000_000, path="/m/Movie (2020)/Movie (2020).mkv")
        base.update(kw)
        return calculate_quality_score(**base)

    def test_default_is_unchanged_h264_behaviour(self):
        self.assertEqual(self._score(), self._score(video_codec="h264"))
        self.assertEqual(self._score(), self._score(video_codec="something-new"))

    def test_hevc_at_half_bitrate_scores_like_its_h264_source(self):
        self.assertEqual(self._score(bitrate=12_000_000, video_codec="h264"),
                         self._score(bitrate=6_000_000, video_codec="hevc"))

    def test_older_codecs_score_lower_at_same_bitrate(self):
        h264 = self._score(bitrate=8_000_000, video_codec="h264")
        self.assertLess(self._score(bitrate=8_000_000, video_codec="mpeg4"), h264)
        self.assertLess(self._score(bitrate=8_000_000, video_codec="mpeg2video"), h264)

    def test_bonus_cap_still_applies(self):
        self.assertEqual(self._score(bitrate=40_000_000, video_codec="hevc"),
                         self._score(bitrate=30_000_000, video_codec="h264"))

    def test_effective_bitrate_scales_only_video_when_known(self):
        # 6 Mbps total, 5 Mbps of it HEVC video: audio's 1 Mbps isn't doubled.
        self.assertEqual(effective_bitrate(6_000_000, "hevc", 5_000_000), 11_000_000)
        self.assertEqual(effective_bitrate(6_000_000, "hevc"), 12_000_000)
        # video bitrate larger than the container total is bogus - fall back.
        self.assertEqual(effective_bitrate(6_000_000, "hevc", 9_000_000), 12_000_000)
        self.assertEqual(effective_bitrate(0, "hevc"), 0)
        self.assertEqual(effective_bitrate(None, None), 0)
        self.assertEqual(effective_bitrate(1_000, "HEVC"), 2_000)  # case-insensitive

    def test_efficiency_table_is_relative_to_h264(self):
        self.assertEqual(CODEC_EFFICIENCY["h264"], 1.0)
        self.assertGreater(CODEC_EFFICIENCY["hevc"], CODEC_EFFICIENCY["h264"])


class TestUpdateDuplicateGroups(unittest.TestCase):
    def setUp(self):
        self.created_paths = []
        self.tmp_files = []

    def tearDown(self):
        session = SyncSessionLocal()
        try:
            for path in self.created_paths:
                session.query(MediaItem).filter_by(path=path).delete()
            session.query(DuplicateGroup).filter(
                DuplicateGroup.group_key.like("movie:_ut_duplicate_test_movie_%")
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()
        for f in self.tmp_files:
            try:
                os.remove(f)
            except OSError:
                pass

    def _make_real_file(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mkv")
        tmp.close()
        self.tmp_files.append(tmp.name)
        return tmp.name

    def _add_item(self, session, path, title, quality_score, media_type="movie", season=None, episode=None):
        item = MediaItem(
            path=path, storage_volume="/tmp", title=title, media_type=media_type,
            season=season, episode=episode, resolution="1080p", height=1080, width=1920,
            video_codec="h264", audio_codec="aac", audio_channels=2, bitrate=1000,
            size_bytes=1000, quality_score=quality_score, file_created_at=datetime.utcnow(),
        )
        session.add(item)
        self.created_paths.append(path)
        return item

    def test_creates_duplicate_group_for_multiple_items(self):
        title = "_ut_duplicate_test_movie_"
        path_a, path_b = self._make_real_file(), self._make_real_file()
        session = SyncSessionLocal()
        try:
            self._add_item(session, path_a, title, quality_score=100)
            self._add_item(session, path_b, title, quality_score=200)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            group = session.query(DuplicateGroup).filter_by(group_key=f"movie:{title.lower()}").first()
            self.assertIsNotNone(group)
            self.assertEqual(group.total_count, 2)
            best = session.query(MediaItem).filter_by(path=path_b).first()
            self.assertEqual(group.best_media_id, best.id)
        finally:
            session.close()

    def test_removes_stale_group_when_no_longer_duplicate(self):
        title = "_ut_duplicate_test_movie_solo"
        path_a = self._make_real_file()
        session = SyncSessionLocal()
        try:
            group = DuplicateGroup(
                group_key=f"movie:{title.lower()}", media_title=title,
                season_episode="Movie", best_media_id="nonexistent", total_count=2,
            )
            session.add(group)
            self._add_item(session, path_a, title, quality_score=100)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            group = session.query(DuplicateGroup).filter_by(group_key=f"movie:{title.lower()}").first()
            self.assertIsNone(group)
        finally:
            session.close()

    def test_purges_stale_media_item_missing_on_disk(self):
        title = "_ut_duplicate_test_movie_missing"
        missing_path = "/tmp/_ut_definitely_missing_file_media_manager_test.mkv"
        session = SyncSessionLocal()
        try:
            self._add_item(session, missing_path, title, quality_score=100)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            item = session.query(MediaItem).filter_by(path=missing_path).first()
            self.assertIsNone(item)
        finally:
            session.close()

    def test_items_mid_transcode_swap_are_never_purged(self):
        title = "_ut_duplicate_test_movie_swaplock"
        missing_path = "/tmp/_ut_swaplock_missing_file_media_manager_test.mkv"
        session = SyncSessionLocal()
        try:
            self._add_item(session, missing_path, title, quality_score=100)
            session.commit()
            item_id = session.query(MediaItem.id).filter_by(path=missing_path).scalar()
        finally:
            session.close()
        from unittest.mock import patch
        with patch("media_manager.web.services.transcode_jobs.swap_locked_sync",
                   return_value={"item_ids": {item_id}, "output_paths": set()}):
            update_duplicate_groups()
        session = SyncSessionLocal()
        try:
            self.assertIsNotNone(session.query(MediaItem).filter_by(path=missing_path).first())
        finally:
            session.close()
        with patch("media_manager.web.services.transcode_jobs.swap_locked_sync", side_effect=RuntimeError("no table")):
            update_duplicate_groups()  # lock lookup failure must not break the purge
        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(MediaItem).filter_by(path=missing_path).first())
        finally:
            session.close()

    def test_protected_ids_are_never_purged(self):
        title = "_ut_duplicate_test_movie_protected"
        missing_path = "/tmp/_ut_protected_missing_file_media_manager_test.mkv"
        session = SyncSessionLocal()
        try:
            item_id = self._add_item(session, missing_path, title, quality_score=100).id
            session.commit()
            item_id = session.query(MediaItem.id).filter_by(path=missing_path).scalar()
        finally:
            session.close()

        update_duplicate_groups(protected_ids=[item_id])

        session = SyncSessionLocal()
        try:
            self.assertIsNotNone(session.query(MediaItem).filter_by(path=missing_path).first())
        finally:
            session.close()

    def test_does_not_purge_items_when_storage_volume_looks_unmounted(self):
        """
        Regression test for a production incident: a MediaItem whose file is
        missing must NOT be purged if its storage_volume (the top-level mount
        directory) is itself empty or unreadable right now - that's a strong
        signal the mount dropped (NFS/CIFS remount race, automount hitting its
        systemd start-limit, etc.), not that the file was actually deleted.
        Purging on that signal previously deleted thousands of real rows
        during a mount outage. A missing file under a *healthy* (non-empty,
        reachable) volume should still be purged - see
        test_purges_stale_media_item_missing_on_disk above.
        """
        empty_volume = tempfile.mkdtemp()
        title = "_ut_duplicate_test_movie_unmounted_volume"
        missing_path = os.path.join(empty_volume, "some_movie.mkv")
        session = SyncSessionLocal()
        try:
            item = MediaItem(
                path=missing_path, storage_volume=empty_volume, title=title, media_type="movie",
                resolution="1080p", height=1080, width=1920, video_codec="h264", audio_codec="aac",
                audio_channels=2, bitrate=1000, size_bytes=1000, quality_score=100,
                file_created_at=datetime.utcnow(),
            )
            session.add(item)
            self.created_paths.append(missing_path)
            session.commit()
        finally:
            session.close()

        try:
            update_duplicate_groups()

            session = SyncSessionLocal()
            try:
                item = session.query(MediaItem).filter_by(path=missing_path).first()
                self.assertIsNotNone(item, "item under an unmounted/empty volume must not be purged")
            finally:
                session.close()
        finally:
            os.rmdir(empty_volume)

    def test_tv_show_group_key_includes_season_episode(self):
        title = "_ut_duplicate_test_tv_show"
        path_a, path_b = self._make_real_file(), self._make_real_file()
        session = SyncSessionLocal()
        try:
            self._add_item(session, path_a, title, quality_score=100, media_type="tv", season=1, episode=1)
            self._add_item(session, path_b, title, quality_score=150, media_type="tv", season=1, episode=1)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            group = session.query(DuplicateGroup).filter_by(group_key="tv:ut duplicate test tv show:S1E1").first()
            self.assertIsNotNone(group)
            self.assertEqual(group.season_episode, "S01E01")
        finally:
            session.close()


class TestTvSeriesName(unittest.TestCase):
    def test_season_folder_uses_show_folder(self):
        self.assertEqual(
            tv_series_name("/mnt/TV/Tulsa King/Season 4/4x02 - Blind Rooster.mkv", "Blind Rooster"),
            "Tulsa King",
        )
        self.assertEqual(tv_series_name("/mnt/TV/Show/Specials/0x01 - X.mkv", "X"), "Show")

    def test_loose_sonarr_style_file_uses_prefix(self):
        self.assertEqual(
            tv_series_name("/mnt/TV/Tulsa King - 4x02 - Blind Rooster WEBRip-1080p.mkv", "Tulsa King"),
            "Tulsa King",
        )
        self.assertEqual(
            tv_series_name("/mnt/TV/12 12 12 - 1x02 - I Want My Halo Back WEBRip-1080p.mkv", "I Want My Halo Back"),
            "12 12 12",
        )

    def test_loose_scene_file_falls_back_to_title(self):
        self.assertEqual(tv_series_name("/mnt/TV/Ted.Lasso.2020.S04E02.1080p.mkv", "Ted Lasso"), "Ted Lasso")


class TestTvDuplicateGrouping(unittest.TestCase):
    """TV duplicates must group by series + S/E, not by episode title."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.paths = []

    def tearDown(self):
        session = SyncSessionLocal()
        try:
            for path in self.paths:
                session.query(MediaItem).filter_by(path=path).delete()
            session.query(DuplicateGroup).filter(
                DuplicateGroup.group_key.like("tv:ut %")
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _add(self, session, rel, title, score, season, episode):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        self.paths.append(path)
        session.add(MediaItem(
            path=path, storage_volume=self.root, title=title, media_type="tv",
            season=season, episode=episode, resolution="1080p", height=1080, width=1920,
            video_codec="h264", audio_codec="aac", audio_channels=2, bitrate=1000,
            size_bytes=1000, quality_score=score, file_created_at=datetime.utcnow(),
        ))
        return path

    def _group_of(self, session, path):
        return session.query(MediaItem).filter_by(path=path).one().duplicate_group_id

    def test_season_folder_copy_and_loose_copy_are_one_group(self):
        session = SyncSessionLocal()
        try:
            a = self._add(session, "_ut_Tulsa/Season 4/4x01 - W.W.D.D..mkv", "W.W.D.D.", 917, 4, 1)
            b = self._add(session, "_ut_Tulsa - 4x01 - W.W.D.D WEBRip-1080p.mkv", "_ut_Tulsa", 523, 4, 1)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            gid = self._group_of(session, a)
            self.assertIsNotNone(gid)
            self.assertEqual(gid, self._group_of(session, b))
            group = session.get(DuplicateGroup, gid)
            self.assertEqual(group.media_title, "_ut_Tulsa")
            self.assertEqual(group.total_count, 2)
        finally:
            session.close()

    def test_different_shows_with_same_episode_title_are_not_grouped(self):
        session = SyncSessionLocal()
        try:
            a = self._add(session, "_ut_Hostage (2025)/Season 1/1x02 - Episode 2.mkv", "Episode 2", 900, 1, 2)
            b = self._add(session, "_ut_The Body (2026)/Season 1/1x02 - Episode 2.mkv", "Episode 2", 500, 1, 2)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            self.assertIsNone(self._group_of(session, a))
            self.assertIsNone(self._group_of(session, b))
        finally:
            session.close()

    def test_unnumbered_episodes_of_one_show_are_not_grouped(self):
        session = SyncSessionLocal()
        try:
            a = self._add(session, "_ut_DateShow/Season 2024/Pilot.mkv", "Pilot", 900, None, None)
            b = self._add(session, "_ut_DateShow/Season 2024/Finale.mkv", "Finale", 500, None, None)
            c = self._add(session, "_ut_DateShow/Season 1/E05 - X.mkv", "X", 500, None, 5)
            d = self._add(session, "_ut_DateShow/Season 2/E05 - Y.mkv", "Y", 400, None, 5)
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            for p in (a, b, c, d):
                self.assertIsNone(self._group_of(session, p))
        finally:
            session.close()

    def test_obsolete_groups_are_removed_and_items_unlinked(self):
        session = SyncSessionLocal()
        try:
            old = DuplicateGroup(group_key="tv:episode 9 _ut_:S1E9", media_title="Episode 9",
                                 season_episode="S01E09", best_media_id="x", total_count=2)
            session.add(old)
            session.flush()
            a = self._add(session, "_ut_ShowA/Season 1/1x09 - Episode 9.mkv", "Episode 9", 900, 1, 9)
            session.flush()
            session.query(MediaItem).filter_by(path=a).one().duplicate_group_id = old.id
            session.commit()
        finally:
            session.close()

        update_duplicate_groups()

        session = SyncSessionLocal()
        try:
            self.assertIsNone(session.query(DuplicateGroup).filter_by(group_key="tv:episode 9 _ut_:S1E9").first())
            self.assertIsNone(self._group_of(session, a))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
