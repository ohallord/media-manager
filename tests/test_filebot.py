import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from media_manager import filebot


class TestRunFilebotArtwork(unittest.TestCase):
    def test_missing_file_returns_empty_list(self):
        result = filebot.run_filebot_artwork("/tmp/_filebot_test_/does_not_exist.mkv")
        self.assertEqual(result, [])

    @patch("media_manager.filebot.subprocess.run")
    def test_successful_move_parsed_and_permissions_applied(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Source.File.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            dest = os.path.join(tmpdir, "Movies", "Some Movie (2020)", "Some Movie (2020).mkv")
            os.makedirs(os.path.dirname(dest))
            with open(dest, "wb") as f:
                f.write(b"0")

            mock_run.return_value = MagicMock(
                stdout=f"[MOVE] from [{src}] to [{dest}]\n",
                stderr="",
            )

            with patch("media_manager.filebot.os.chown") as mock_chown, \
                 patch("media_manager.filebot.os.chmod") as mock_chmod:
                result = filebot.run_filebot_artwork(src)

            self.assertEqual(result, [dest])
            self.assertTrue(mock_chown.called)
            self.assertTrue(mock_chmod.called)

    @patch("media_manager.filebot.subprocess.run")
    def test_formats_match_library_layout(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Show.S01E05.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            mock_run.return_value = MagicMock(stdout="", stderr="")
            filebot.run_filebot_artwork(src)
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[cmd.index("--output") + 1], tmpdir)
            self.assertIn(f"seriesFormat={filebot.series_format(tmpdir)}", cmd)
            self.assertIn("movieFormat={n}/{ny}", cmd)

    @patch("media_manager.filebot.subprocess.run")
    def test_ambiguous_series_queried_by_sonarr_title(self, mock_run):
        # "American Hostage" vs two "Hostage" shows, or two "Tracker" shows:
        # strict mode refuses to pick, leaving the file loose in the library
        # root where Plex never sees it. Sonarr's title in the filename pins
        # the show and the library folder.
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Tracker (2024) - 4x02 - Switchback HDTV-720p.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            mock_run.return_value = MagicMock(stdout="", stderr="")
            filebot.run_filebot_artwork(src)
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[cmd.index("--q") + 1], "Tracker (2024)")
            self.assertIn("seriesFormat=Tracker (2024)/Season {s}/{sxe} - {t}", cmd)
            self.assertNotIn("-non-strict", cmd)

    def test_sonarr_series_title(self):
        self.assertEqual(
            filebot.sonarr_series_title("/x/American Hostage - 1x04 - A Taste of Indianapolis WEBDL-1080p.mkv"),
            "American Hostage",
        )
        self.assertEqual(filebot.sonarr_series_title("/x/Ghosts (US) - 5x20 - Polar Opposites.mkv"), "Ghosts (US)")
        self.assertIsNone(filebot.sonarr_series_title("/x/Beauty.and.the.Billionaire.S01E01.AMZN.1080p.mkv"))
        self.assertIsNone(filebot.sonarr_series_title("/x/Some Movie (2020).mkv"))
        self.assertIsNone(filebot.sonarr_series_title("/x/{bad} - 1x01 - Pilot.mkv"))

    def test_series_format_reuses_existing_year_folder(self):
        # FileBot names TMDb's 2024 "Tracker" plain "Tracker", but the library
        # already has "Tracker (2024)"; the format must prefer that folder.
        fmt = filebot.series_format("/media/TvShows")
        self.assertTrue(fmt.endswith("/Season {s}/{sxe} - {t}"))
        self.assertIn('new File("/media/TvShows", d).isDirectory() ? d : n', fmt)
        self.assertIn('n + " (" + y + ")"', fmt)

    @patch("media_manager.filebot.subprocess.run")
    def test_no_matches_returns_empty_list(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Source.File.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            mock_run.return_value = MagicMock(stdout="No moves performed.\n", stderr="")
            result = filebot.run_filebot_artwork(src)
            self.assertEqual(result, [])

    @patch("media_manager.filebot.subprocess.run")
    def test_stderr_logged_but_does_not_raise(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Source.File.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            mock_run.return_value = MagicMock(stdout="", stderr="some warning from filebot")
            result = filebot.run_filebot_artwork(src)
            self.assertEqual(result, [])

    @patch("media_manager.filebot.subprocess.run", side_effect=OSError("filebot binary not found"))
    def test_subprocess_exception_caught_and_returns_empty_list(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Source.File.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            result = filebot.run_filebot_artwork(src)
            self.assertEqual(result, [])

    @patch("media_manager.filebot.subprocess.run")
    def test_permission_error_on_matched_path_is_caught(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "Source.File.mkv")
            with open(src, "wb") as f:
                f.write(b"0")
            dest = os.path.join(tmpdir, "Movies", "Some Movie (2020)", "Some Movie (2020).mkv")
            os.makedirs(os.path.dirname(dest))
            with open(dest, "wb") as f:
                f.write(b"0")

            mock_run.return_value = MagicMock(
                stdout=f"[MOVE] from [{src}] to [{dest}]\n",
                stderr="",
            )
            with patch("media_manager.filebot.os.chown", side_effect=PermissionError("nope")):
                result = filebot.run_filebot_artwork(src)
            # Even though chown failed, the move was still detected and returned.
            self.assertEqual(result, [dest])


if __name__ == "__main__":
    unittest.main()


class TestFileSonarrEpisode(unittest.TestCase):
    """Fallback when FileBot can't identify a Sonarr-named episode (e.g. it
    groups "Doc (US) - 3x02 - ..." as Series=null): file it from the name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"0")
        return path

    @patch("media_manager.filebot.os.chown")
    def test_files_into_show_season_folder(self, _chown):
        src = self._touch("Doc (US) - 3x02 - Ready or Not HDTV-720p.mkv")
        dest = os.path.join(self.root, "Doc (US)", "Season 3", "3x02 - Ready or Not.mkv")
        self.assertEqual(filebot.file_sonarr_episode(src), [dest])
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(src))

    @patch("media_manager.filebot.os.chown")
    def test_strips_proper_suffix_and_keeps_title_parens(self, _chown):
        src = self._touch("Tracker (2024) - 3x21 - Chrono Stasis (1) WEBDL-1080p Proper.mkv")
        dest = os.path.join(self.root, "Tracker (2024)", "Season 3", "3x21 - Chrono Stasis (1).mkv")
        self.assertEqual(filebot.file_sonarr_episode(src), [dest])

    def test_existing_episode_is_left_alone(self):
        src = self._touch("Modern Family - 7x02 - The Day Alex Left for College WEBDL-1080p.mkv")
        existing = self._touch("Modern Family/Season 7/7x02 - The Day Alex Left for College.mkv")
        self.assertEqual(filebot.file_sonarr_episode(src), [])
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(existing))

    def test_same_episode_under_other_title_is_left_alone(self):
        src = self._touch("Tulsa King - 4x01 - W.W.D.D WEBRip-1080p.mkv")
        self._touch("Tulsa King/Season 4/4x01 - W.W.D.D..mkv")
        self._touch("Tulsa King/Season 4/4x02 - Other.mkv")
        self.assertEqual(filebot.file_sonarr_episode(src), [])
        self.assertTrue(os.path.exists(src))

    def test_non_sonarr_names_are_left_alone(self):
        for name in ("Beauty.and.the.Billionaire.S01E01.AMZN.1080p.mkv", "Some Movie (2020).mkv"):
            src = self._touch(name)
            self.assertEqual(filebot.file_sonarr_episode(src), [])
            self.assertTrue(os.path.exists(src))

    def test_missing_file_returns_empty(self):
        self.assertEqual(filebot.file_sonarr_episode(os.path.join(self.root, "X - 1x01 - Y.mkv")), [])

    @patch("media_manager.filebot.os.rename", side_effect=OSError("cross-device"))
    def test_move_error_returns_empty(self, _rename):
        src = self._touch("Doc (US) - 3x02 - Ready or Not HDTV-720p.mkv")
        self.assertEqual(filebot.file_sonarr_episode(src), [])
        self.assertTrue(os.path.exists(src))
