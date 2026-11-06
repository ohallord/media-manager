import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import delete, select

from media_manager.database import AsyncSessionLocal
from media_manager.integrations.radarr import RadarrClient
from media_manager.models import MediaItem, PlaybackStat
from media_manager.web.services import empty_file_redownload as svc


class TestPureHelpers(unittest.TestCase):
    def test_parse_movie_title_year_from_filename_or_folder(self):
        self.assertEqual(svc.parse_movie_title_year("/m/Bad Moms/Bad Moms (2016).mkv"), ("Bad Moms", 2016))
        self.assertEqual(svc.parse_movie_title_year("/m/Gladiator (2000)/movie.mkv"), ("Gladiator", 2000))

    def test_parse_movie_title_year_guessit_fallback(self):
        title, year = svc.parse_movie_title_year("/m/x/The.Pest.1997.720p.mkv")
        self.assertEqual((title, year), ("The Pest", 1997))
        self.assertEqual(svc.parse_movie_title_year("/m/x/Some Thing.mkv")[1], None)

    def test_match_movie_by_title_clean_title_or_folder(self):
        movies = [
            {"id": 1, "title": "Sharknado 3: Oh Hell No!", "year": 2015, "path": "/x/Sharknado 3 Oh Hell No! (2015)"},
            {"id": 2, "title": "Other", "year": 2015, "cleanTitle": "badmoms", "path": "/x/Other"},
            {"id": 3, "title": "Renamed Upstream", "year": 1999, "path": "/x/Folder Title (1999)"},
        ]
        self.assertEqual(svc.match_movie("Sharknado 3 Oh Hell No!", 2015, movies)["id"], 1)
        self.assertEqual(svc.match_movie("Bad Moms", 2015, movies)["id"], 2)
        self.assertEqual(svc.match_movie("Folder Title", 1999, movies)["id"], 3)
        self.assertIsNone(svc.match_movie("Sharknado 3 Oh Hell No!", 2016, movies))  # year must match
        self.assertEqual(svc.match_movie("Other", None, movies)["id"], 2)  # no year: title only

    def test_match_series_and_episode(self):
        series = [{"id": 5, "title": "Ghosts", "path": "/tv/Ghosts (US)"}, {"id": 6, "title": "Scrubs", "path": "/tv/Scrubs"}]
        self.assertEqual(svc.match_series("Ghosts (US)", series)["id"], 5)
        self.assertEqual(svc.match_series("scrubs", series)["id"], 6)
        self.assertIsNone(svc.match_series("Lost", series))
        episodes = [{"id": 10, "seasonNumber": 1, "episodeNumber": 6}]
        self.assertEqual(svc.match_episode(1, 6, episodes)["id"], 10)
        self.assertIsNone(svc.match_episode(1, 7, episodes))
        self.assertIsNone(svc.match_episode(None, 6, episodes))

    def test_pick_series_lookup(self):
        results = [{"title": "Ghosts", "tvdbId": 1, "year": 2019}, {"title": "Ghosts (US)", "tvdbId": 2, "year": 2021},
                   {"title": "Doctor Who", "tvdbId": 3, "year": 2005}, {"title": "No Id"}]
        self.assertEqual(svc.pick_series_lookup("Ghosts (US)", results)["tvdbId"], 2)
        self.assertEqual(svc.pick_series_lookup("Ghosts", results)["tvdbId"], 1)
        self.assertEqual(svc.pick_series_lookup("Doctor Who (2005)", results)["tvdbId"], 3)
        self.assertIsNone(svc.pick_series_lookup("Doctor Who (1963)", results))
        self.assertIsNone(svc.pick_series_lookup("No Id", results))
        self.assertEqual(svc._show_search_term("Ghosts (US)"), "Ghosts")
        self.assertEqual(svc._show_search_term("Scrubs"), "Scrubs")

    def test_pick_lookup_result_requires_exact_title_year_and_tmdb(self):
        results = [
            {"title": "The 355", "year": 2022},  # no tmdbId
            {"title": "The 355", "year": 2021, "tmdbId": 1},
            {"title": "The 355", "year": 2022, "tmdbId": 2},
        ]
        self.assertEqual(svc.pick_lookup_result("The 355", 2022, results)["tmdbId"], 2)
        self.assertIsNone(svc.pick_lookup_result("The 356", 2022, results))

    def test_disk_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty, full = os.path.join(tmp, "e.mkv"), os.path.join(tmp, "f.mkv")
            open(empty, "wb").close()
            with open(full, "wb") as fh:
                fh.write(b"x")
            self.assertEqual(svc._disk_state(empty), "empty")
            self.assertEqual(svc._disk_state(full), "has_data")
            self.assertEqual(svc._disk_state(os.path.join(tmp, "nope.mkv")), "missing")
        with patch("os.path.getsize", side_effect=PermissionError("denied")):
            self.assertEqual(svc._disk_state("/x"), "has_data")


class TestClientFactory(unittest.TestCase):
    def _db(self, cfg):
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        return db

    def test_returns_none_when_missing_or_disabled(self):
        self.assertIsNone(asyncio.run(svc._client(self._db(None), "radarr", RadarrClient)))
        disabled = MagicMock(enabled=False)
        self.assertIsNone(asyncio.run(svc._client(self._db(disabled), "radarr", RadarrClient)))

    def test_builds_client_from_config(self):
        cfg = MagicMock(enabled=True, api_url="http://radarr/", api_key_encrypted="k")
        client = asyncio.run(svc._client(self._db(cfg), "radarr", RadarrClient))
        self.assertIsInstance(client, RadarrClient)
        self.assertEqual(client.api_url, "http://radarr")


def _fake_radarr(movies):
    r = MagicMock()
    r.get_movies = AsyncMock(return_value=movies)
    r.delete_movie_file = AsyncMock(return_value=True)
    r.trigger_command = AsyncMock(return_value={"success": True})
    r.set_movies_monitored = AsyncMock(return_value=True)
    r.get_status = AsyncMock(return_value=True)
    r.search_movies = AsyncMock(return_value=[])
    return r


def _fake_sonarr(series, episodes):
    s = MagicMock()
    s.get_series = AsyncMock(return_value=series)
    s.get_episodes = AsyncMock(return_value=episodes)
    s.delete_episode_file = AsyncMock(return_value=True)
    s.trigger_command = AsyncMock(return_value={"success": True})
    s.set_episodes_monitored = AsyncMock(return_value=True)
    s.get_status = AsyncMock(return_value=True)
    s.search_series = AsyncMock(return_value=[])
    s.add_series = AsyncMock(return_value={"success": False, "error": "unused"})
    return s


class TestRunRedownload(unittest.TestCase):
    """End-to-end over real temp files + DB rows, with fake Radarr/Sonarr."""

    def setUp(self):
        self.vol = tempfile.mkdtemp(prefix="mm_redownload_")
        open(os.path.join(self.vol, ".keep"), "wb").close()  # volume_is_healthy needs a non-empty dir
        self.ids = {}

    def tearDown(self):
        async def cleanup():
            async with AsyncSessionLocal() as db:
                ids = list(self.ids.values())
                await db.execute(delete(PlaybackStat).where(PlaybackStat.media_item_id.in_(ids)))
                await db.execute(delete(MediaItem).where(MediaItem.id.in_(ids)))
                await db.commit()
        asyncio.run(cleanup())
        shutil.rmtree(self.vol, ignore_errors=True)

    def _seed(self, key, rel_path, media_type="movie", season=None, episode=None, content=b"", on_disk=True):
        path = os.path.join(self.vol, rel_path)
        if on_disk:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(content)

        async def run():
            async with AsyncSessionLocal() as db:
                item = MediaItem(
                    path=path, storage_volume=self.vol, title=key, media_type=media_type,
                    season=season, episode=episode, resolution="Unknown", height=0, width=0,
                    video_codec="unknown", audio_codec="unknown", audio_channels=0, bitrate=0,
                    size_bytes=0, quality_score=0, file_created_at=datetime.utcnow(),
                )
                db.add(item)
                await db.commit()
                db.add(PlaybackStat(media_item_id=item.id, source="plex", play_count=1))
                await db.commit()
                return item.id
        self.ids[key] = asyncio.run(run())
        return path

    def _run(self, radarr, sonarr, ids=None, add_result=None):
        add_mock = AsyncMock(return_value=add_result or {"success": True})
        clients = {"radarr": radarr, "sonarr": sonarr}

        async def run():
            async with AsyncSessionLocal() as db:
                with patch.object(svc, "_client", AsyncMock(side_effect=lambda db_, name, cls: clients[name])), \
                     patch("media_manager.web.api.integrations._add_movie_via_radarr", add_mock):
                    return await svc.run_redownload(db, ids if ids is not None else list(self.ids.values()))
        return asyncio.run(run()), add_mock

    def _row_exists(self, key):
        async def run():
            async with AsyncSessionLocal() as db:
                return (await db.execute(select(MediaItem.id).where(MediaItem.id == self.ids[key]))).first() is not None
        return asyncio.run(run())

    def test_full_mix_of_outcomes(self):
        known = self._seed("known", "Movies/Bad Moms/Bad Moms (2016).mkv")
        added = self._seed("added", "Movies/The 355/The 355 (2022).mkv")
        self._seed("unmatched", "Movies/Nope/Nope Film (1990).mkv")
        replaced = self._seed("replaced", "Movies/Real/Real Movie (2001).mkv", content=b"data")
        gone = self._seed("gone", "Movies/Gone/Gone Movie (2003).mkv", on_disk=False)
        ep = self._seed("ep", "TV/Scrubs/Season 1/1x06 - My Bad.mkv", media_type="tv", season=1, episode=6)
        self._seed("noseries", "TV/Lost/Season 1/1x01 - Pilot.mkv", media_type="tv", season=1, episode=1)
        self._seed("noep", "TV/Scrubs/Season 9/9x99 - Nope.mkv", media_type="tv", season=9, episode=99)

        radarr = _fake_radarr([
            {"id": 346, "title": "Bad Moms", "year": 2016, "hasFile": True, "movieFile": {"id": 77}},
            {"id": 500, "title": "Real Movie", "year": 2001, "hasFile": True, "movieFile": {"id": 78}},
            {"id": 501, "title": "Gone Movie", "year": 2003, "hasFile": False},
        ])
        radarr.search_movies = AsyncMock(side_effect=lambda term: (
            [{"title": "The 355", "year": 2022, "tmdbId": 9}] if "355" in term else []))
        sonarr = _fake_sonarr(
            [{"id": 3, "title": "Scrubs", "path": "/tv/Scrubs"}],
            [{"id": 30, "seasonNumber": 1, "episodeNumber": 6, "hasFile": True, "episodeFileId": 300}],
        )

        out, add_mock = self._run(radarr, sonarr)
        by_title = {r["title"]: r for r in out["results"]}

        self.assertEqual(by_title["known"]["status"], "requested")
        radarr.delete_movie_file.assert_awaited_once_with(77)  # only the real 0-byte record
        radarr.trigger_command.assert_awaited_once_with("MoviesSearch", movieIds=[346, 501])
        radarr.set_movies_monitored.assert_awaited_once_with([346, 501])  # re-monitored before searching
        self.assertFalse(os.path.exists(known))
        self.assertFalse(self._row_exists("known"))

        self.assertEqual(by_title["added"]["status"], "requested")
        self.assertEqual(add_mock.await_args.args[1]["tmdbId"], 9)
        self.assertTrue(add_mock.await_args.kwargs["search_now"])
        self.assertFalse(os.path.exists(added))

        self.assertEqual(by_title["unmatched"]["status"], "not_found")
        self.assertTrue(self._row_exists("unmatched"))

        self.assertEqual(by_title["replaced"]["status"], "no_longer_empty")
        self.assertTrue(os.path.exists(replaced))
        self.assertTrue(self._row_exists("replaced"))

        self.assertEqual(by_title["gone"]["status"], "requested")  # missing file: row still cleaned up
        self.assertFalse(os.path.exists(gone))
        self.assertFalse(self._row_exists("gone"))

        self.assertEqual(by_title["ep"]["status"], "requested")
        sonarr.delete_episode_file.assert_awaited_once_with(300)
        sonarr.trigger_command.assert_awaited_once_with("EpisodeSearch", episodeIds=[30])
        sonarr.set_episodes_monitored.assert_awaited_once_with([30])
        sonarr.get_episodes.assert_awaited_once_with(3)  # cached across episodes of the same series
        self.assertFalse(os.path.exists(ep))

        self.assertEqual(by_title["noseries"]["status"], "not_found")  # not in Sonarr's lookup either
        self.assertEqual(by_title["noep"]["status"], "episode_not_found")
        self.assertEqual(out["counts"]["requested"], 4)

    def test_failures_keep_placeholders(self):
        a = self._seed("a", "Movies/A/A Movie (2010).mkv")
        self._seed("b", "Movies/B/B Movie (2011).mkv")
        self._seed("c", "Movies/C/C Movie (2012).mkv")
        self._seed("d", "TV/Show/Season 1/1x01 - X.mkv", media_type="tv", season=1, episode=1)
        radarr = _fake_radarr([
            {"id": 1, "title": "A Movie", "year": 2010, "hasFile": True, "movieFile": {"id": 11}},
            {"id": 2, "title": "B Movie", "year": 2011, "hasFile": True, "movieFile": {"id": 12}},
        ])
        radarr.delete_movie_file = AsyncMock(side_effect=lambda fid: fid != 11)
        radarr.trigger_command = AsyncMock(return_value={"success": False, "error": "boom"})
        radarr.set_movies_monitored = AsyncMock(return_value=False)
        radarr.search_movies = AsyncMock(return_value=[{"title": "C Movie", "year": 2012, "tmdbId": 3}])
        sonarr = _fake_sonarr([{"id": 4, "title": "Show", "path": "/tv/Show"}],
                              [{"id": 40, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 400}])
        sonarr.delete_episode_file = AsyncMock(return_value=False)

        out, _ = self._run(radarr, sonarr, add_result={"success": False, "error": "no profile"})
        by_title = {r["title"]: r for r in out["results"]}
        self.assertEqual(by_title["a"]["status"], "failed")
        self.assertIn("delete", by_title["a"]["detail"])
        self.assertEqual(by_title["b"]["status"], "failed")
        self.assertIn("MoviesSearch failed", by_title["b"]["detail"])
        self.assertEqual(by_title["c"]["status"], "failed")
        self.assertIn("no profile", by_title["c"]["detail"])
        self.assertEqual(by_title["d"]["status"], "failed")
        self.assertTrue(os.path.exists(a))
        self.assertTrue(self._row_exists("b"))

    def test_add_exception_and_failed_episode_search(self):
        self._seed("c", "Movies/C/C Movie (2012).mkv")
        self._seed("d", "TV/Show/Season 1/1x01 - X.mkv", media_type="tv", season=1, episode=1)
        radarr = _fake_radarr([{"id": 99, "title": "Unrelated", "year": 1900}])
        radarr.search_movies = AsyncMock(return_value=[{"title": "C Movie", "year": 2012, "tmdbId": 3}])
        sonarr = _fake_sonarr([{"id": 4, "title": "Show", "path": "/tv/Show"}],
                              [{"id": 40, "seasonNumber": 1, "episodeNumber": 1, "hasFile": False}])
        sonarr.trigger_command = AsyncMock(return_value={"success": False, "error": "sonarr down"})
        sonarr.set_episodes_monitored = AsyncMock(return_value=False)

        async def boom(*a, **k):
            raise RuntimeError("radarr exploded")

        clients = {"radarr": radarr, "sonarr": sonarr}

        async def run():
            async with AsyncSessionLocal() as db:
                with patch.object(svc, "_client", AsyncMock(side_effect=lambda db_, name, cls: clients[name])), \
                     patch("media_manager.web.api.integrations._add_movie_via_radarr", boom):
                    return await svc.run_redownload(db, list(self.ids.values()))
        out = asyncio.run(run())
        by_title = {r["title"]: r for r in out["results"]}
        self.assertIn("radarr exploded", by_title["c"]["detail"])
        self.assertEqual(by_title["d"]["status"], "failed")
        sonarr.delete_episode_file.assert_not_awaited()  # nothing on file -> nothing to delete

    def test_services_unavailable(self):
        self._seed("m", "Movies/M/M Movie (2010).mkv")
        self._seed("t", "TV/Show/Season 1/1x01 - X.mkv", media_type="tv", season=1, episode=1)
        out, _ = self._run(None, None)
        self.assertEqual(out["counts"], {"unavailable": 2})
        radarr, sonarr = _fake_radarr([]), _fake_sonarr([], [])
        radarr.get_status = AsyncMock(return_value=False)
        sonarr.get_status = AsyncMock(return_value=False)
        out, _ = self._run(radarr, sonarr)
        self.assertEqual(out["counts"], {"unavailable": 2})
        self.assertTrue(all("unreachable" in r["detail"] for r in out["results"]))

    def test_reachable_but_empty_libraries_still_add(self):
        self._seed("m", "Movies/M/M Movie (2010).mkv")
        radarr = _fake_radarr([])  # get_status True: Radarr is up, library just empty
        radarr.search_movies = AsyncMock(return_value=[{"title": "M Movie", "year": 2010, "tmdbId": 5}])
        out, add = self._run(radarr, None)
        self.assertEqual(out["results"][0]["status"], "requested")
        add.assert_awaited_once()

    def _sonarr_add_run(self, sonarr):
        async def run():
            async with AsyncSessionLocal() as db:
                with patch.object(svc, "_client", AsyncMock(side_effect=lambda db_, name, cls: {"sonarr": sonarr, "radarr": None}[name])), \
                     patch("media_manager.web.api.integrations._resolve_add_defaults", AsyncMock(return_value=(4, "/tv"))), \
                     patch("media_manager.web.api.integrations._resolve_language_profile_id", AsyncMock(return_value=1)), \
                     patch.object(svc.asyncio, "sleep", AsyncMock()):
                    return await svc.run_redownload(db, list(self.ids.values()))
        return asyncio.run(run())

    def test_missing_series_is_added_unmonitored_then_only_the_episode_searched(self):
        ep_path = self._seed("ep", "TV/Ghosts (US)/Season 5/5x20 - Polar.mkv", media_type="tv", season=5, episode=20)
        sonarr = _fake_sonarr([{"id": 1, "title": "Other Show", "path": "/tv/Other"}], [])
        sonarr.search_series = AsyncMock(return_value=[{"title": "Ghosts", "tvdbId": 7}, {"title": "Ghosts (US)", "tvdbId": 8, "year": 2021}])
        sonarr.add_series = AsyncMock(return_value={"success": True, "data": {"id": 42}})
        sonarr.get_episodes = AsyncMock(side_effect=[[], [{"id": 520, "seasonNumber": 5, "episodeNumber": 20}]])
        out = self._sonarr_add_run(sonarr)
        self.assertEqual(out["results"][0]["status"], "requested")
        sonarr.search_series.assert_awaited_once_with("Ghosts")
        add_kwargs = sonarr.add_series.await_args.kwargs
        self.assertEqual(sonarr.add_series.await_args.args[0]["tvdbId"], 8)
        self.assertEqual((add_kwargs["search_now"], add_kwargs["season_monitor"]), (False, "none"))
        sonarr.set_episodes_monitored.assert_awaited_once_with([520])
        sonarr.trigger_command.assert_awaited_once_with("EpisodeSearch", episodeIds=[520])
        self.assertFalse(os.path.exists(ep_path))

    def test_series_already_added_failure_and_pending_episodes(self):
        self._seed("a", "TV/Show A/Season 1/1x01 - X.mkv", media_type="tv", season=1, episode=1)
        sonarr = _fake_sonarr([{"id": 1, "title": "Unrelated", "path": "/tv/U"}], [])
        sonarr.search_series = AsyncMock(return_value=[{"title": "Show A", "tvdbId": 3}])
        sonarr.add_series = AsyncMock(return_value={"success": False, "error": "This series has already been added"})
        sonarr.get_series = AsyncMock(side_effect=[[{"id": 1, "title": "Unrelated", "path": "/tv/U"}],
                                                   [{"id": 77, "tvdbId": 3}]])
        with patch.object(svc, "EPISODE_WAIT_ATTEMPTS", 2):
            sonarr.get_episodes = AsyncMock(return_value=[])
            out = self._sonarr_add_run(sonarr)
        self.assertEqual(out["results"][0]["status"], "added_pending")
        sonarr.trigger_command.assert_not_awaited()  # never a whole-series search

        sonarr = _fake_sonarr([{"id": 1, "title": "Unrelated", "path": "/tv/U"}], [])
        sonarr.search_series = AsyncMock(return_value=[{"title": "Show A", "tvdbId": 3}])
        sonarr.add_series = AsyncMock(side_effect=RuntimeError("no root folder"))
        out = self._sonarr_add_run(sonarr)
        self.assertEqual(out["results"][0]["status"], "failed")
        self.assertIn("no root folder", out["results"][0]["detail"])

    def test_placeholder_kept_when_volume_unhealthy_or_file_refilled(self):
        path = self._seed("m", "Movies/M/M Movie (2010).mkv")

        async def run(item_state):
            async with AsyncSessionLocal() as db:
                item = (await db.execute(select(MediaItem).where(MediaItem.id == self.ids["m"]))).scalar_one()
                with patch.object(svc, "volume_is_healthy", return_value=item_state == "healthy"):
                    return await svc._remove_placeholder(db, item)

        self.assertFalse(asyncio.run(run("unhealthy")))
        with open(path, "wb") as fh:
            fh.write(b"refilled")
        self.assertFalse(asyncio.run(run("healthy")))
        self.assertTrue(os.path.exists(path))

    def test_remove_error_is_reported_not_raised(self):
        self._seed("m", "Movies/M/M Movie (2010).mkv")
        radarr = _fake_radarr([{"id": 1, "title": "M Movie", "year": 2010, "hasFile": False}])
        with patch.object(svc.os, "remove", side_effect=PermissionError("ro")):
            out, _ = self._run(radarr, None)
        self.assertEqual(out["results"][0]["status"], "requested")
        self.assertFalse(out["results"][0]["placeholder_removed"])

    def test_list_empty_items_skips_samples_and_filters_ids(self):
        self._seed("real", "Movies/R/R Movie (2010).mkv")
        self._seed("sample", "Movies/R/r.movie.2010-sample.mkv")

        async def run(ids):
            async with AsyncSessionLocal() as db:
                return [i.title for i in await svc.list_empty_items(db, ids)]
        self.assertEqual(asyncio.run(run(list(self.ids.values()))), ["real"])
        self.assertIn("real", asyncio.run(run(None)))


if __name__ == "__main__":
    unittest.main()
