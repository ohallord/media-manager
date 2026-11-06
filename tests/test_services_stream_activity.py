import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from media_manager.web.services import stream_activity as sa
from media_manager.web.services import transcode_post_swap as post


def _cfg(name, extra=None):
    return SimpleNamespace(service_name=name, api_url=f"http://{name}", api_key_encrypted="k", enabled=True,
                           extra_json=extra)


class TestStreamActivity(unittest.TestCase):
    def setUp(self):
        sa.clear_cache()

    def tearDown(self):
        sa.clear_cache()

    def _run(self, configs, sessions_by_source, force=False):
        def factory(name):
            client = MagicMock()
            value = sessions_by_source[name]
            client.get_sessions = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
            return MagicMock(return_value=client)
        sources = {name: factory(name) for name in sessions_by_source}
        with patch.object(sa, "_configs", AsyncMock(return_value=configs)), patch.dict(sa.SOURCES, sources):
            return asyncio.run(sa.get_activity(None, force=force))

    def test_aggregates_maps_paths_and_survives_a_down_server(self):
        mapping = '{"path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}]}'
        activity = self._run(
            [_cfg("plex"), _cfg("jellyfin", mapping), _cfg("emby")],
            {"plex": [{"path": "/media/Movies/A/A.mkv", "video_transcode": True, "state": "playing"}],
             "jellyfin": [{"path": "/data/TvShows/x.mkv", "video_transcode": False, "state": "paused"},
                          {"path": None, "video_transcode": False}],
             "emby": RuntimeError("emby down")})
        self.assertEqual((activity["streams"], activity["video_transcodes"], activity["errors"]), (3, 1, ["emby"]))
        self.assertEqual(activity["sessions"][1]["path"], "/mnt/TvShows/x.mkv")
        self.assertTrue(sa.is_playing(activity, "/media/Movies/A/../A/A.mkv"))
        self.assertFalse(sa.is_playing(activity, "/mnt/Other.mkv"))

    def test_cached_until_forced(self):
        first = self._run([_cfg("plex")], {"plex": []})
        cached = self._run([_cfg("plex")], {"plex": [{"path": "/a", "video_transcode": True}]})
        self.assertIs(first, cached)
        fresh = self._run([_cfg("plex")], {"plex": [{"path": "/a", "video_transcode": True}]}, force=True)
        self.assertEqual(fresh["video_transcodes"], 1)

    def test_pause_reason_policies(self):
        idle = {"streams": 0, "video_transcodes": 0}
        direct = {"streams": 2, "video_transcodes": 0}
        transcoding = {"streams": 1, "video_transcodes": 1}
        self.assertIsNone(sa.pause_reason(idle, "transcode_only"))
        self.assertIsNone(sa.pause_reason(direct, "transcode_only"))
        self.assertIn("transcode", sa.pause_reason(transcoding, "transcode_only"))
        self.assertIn("2 stream", sa.pause_reason(direct, "any_stream"))
        self.assertIsNotNone(sa.pause_reason(transcoding, "any_stream"))
        self.assertIsNone(sa.pause_reason(transcoding, "never"))

    def test_configs_filter(self):
        rows = [SimpleNamespace(service_name="plex", enabled=True, api_url="u", api_key_encrypted="k"),
                SimpleNamespace(service_name="emby", enabled=False, api_url="u", api_key_encrypted="k"),
                SimpleNamespace(service_name="jellyfin", enabled=True, api_url="", api_key_encrypted="k")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db = MagicMock(execute=AsyncMock(return_value=result))
        self.assertEqual([c.service_name for c in asyncio.run(sa._configs(db))], ["plex"])


class TestPostSwap(unittest.TestCase):
    def test_updates_for(self):
        self.assertEqual(post.updates_for("/a.mkv", "/a.mkv"), [{"Path": "/a.mkv", "UpdateType": "Modified"}])
        self.assertEqual(post.updates_for("/a.mp4", "/a.mkv"), [{"Path": "/a.mp4", "UpdateType": "Deleted"},
                                                                {"Path": "/a.mkv", "UpdateType": "Created"}])

    def test_notifies_each_server_with_mapped_paths(self):
        plex, jelly, emby = MagicMock(), MagicMock(), MagicMock()
        plex.refresh_path = AsyncMock(return_value=True)
        jelly.notify_media_updated = AsyncMock(return_value=True)
        emby.notify_media_updated = AsyncMock(side_effect=RuntimeError("down"))
        mapping = '{"path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}]}'
        configs = [_cfg("plex"), _cfg("jellyfin", mapping), _cfg("emby")]
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=MagicMock())
        session.__aexit__ = AsyncMock(return_value=False)
        with patch.object(post, "AsyncSessionLocal", return_value=session), \
             patch.object(post, "_configs", AsyncMock(return_value=configs)), \
             patch.object(post, "PlexClient", return_value=plex), \
             patch.object(post, "JellyfinClient", return_value=jelly), \
             patch.object(post, "EmbyClient", return_value=emby):
            results = asyncio.run(post.notify_media_servers("/mnt/A/a.mp4", "/mnt/A/a.mkv"))
        self.assertEqual(results, {"plex": True, "jellyfin": True, "emby": False})
        plex.refresh_path.assert_awaited_once_with("/mnt/A/a.mkv")
        self.assertEqual(jelly.notify_media_updated.await_args.args[0][1], {"Path": "/data/A/a.mkv", "UpdateType": "Created"})

    def test_config_load_failure_is_swallowed(self):
        with patch.object(post, "AsyncSessionLocal", side_effect=RuntimeError("db gone")):
            self.assertEqual(asyncio.run(post.notify_media_servers("/a", "/b")), {})


if __name__ == "__main__":
    unittest.main()
