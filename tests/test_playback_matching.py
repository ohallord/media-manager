import unittest

from media_manager.playback_matching import (
    parse_path_mappings,
    normalize_path,
    match_media_item,
)


class TestParsePathMappings(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(parse_path_mappings(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(parse_path_mappings(""), [])

    def test_invalid_json_returns_empty(self):
        self.assertEqual(parse_path_mappings("{not json"), [])

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(parse_path_mappings("[1, 2, 3]"), [])

    def test_missing_path_mappings_key_returns_empty(self):
        self.assertEqual(parse_path_mappings("{}"), [])

    def test_path_mappings_not_a_list_returns_empty(self):
        self.assertEqual(parse_path_mappings('{"path_mappings": "x"}'), [])

    def test_valid_mappings_parsed(self):
        raw = '{"path_mappings": [{"source_prefix": "/data", "local_prefix": "/mnt"}]}'
        self.assertEqual(
            parse_path_mappings(raw),
            [{"source_prefix": "/data", "local_prefix": "/mnt"}],
        )

    def test_entries_missing_required_keys_are_dropped(self):
        raw = '{"path_mappings": [{"source_prefix": "/data"}, "not-a-dict", {"source_prefix": "/a", "local_prefix": "/b"}]}'
        self.assertEqual(
            parse_path_mappings(raw),
            [{"source_prefix": "/a", "local_prefix": "/b"}],
        )


class TestNormalizePath(unittest.TestCase):
    def test_empty_path_returned_as_is(self):
        self.assertEqual(normalize_path("", [{"source_prefix": "/a", "local_prefix": "/b"}]), "")

    def test_no_mappings_returns_unchanged(self):
        self.assertEqual(normalize_path("/data/Movies/a.mkv", []), "/data/Movies/a.mkv")

    def test_matching_prefix_rewritten(self):
        mappings = [{"source_prefix": "/data", "local_prefix": "/mnt"}]
        self.assertEqual(normalize_path("/data/Movies/a.mkv", mappings), "/mnt/Movies/a.mkv")

    def test_no_matching_prefix_returns_unchanged(self):
        mappings = [{"source_prefix": "/other", "local_prefix": "/mnt"}]
        self.assertEqual(normalize_path("/data/Movies/a.mkv", mappings), "/data/Movies/a.mkv")

    def test_first_matching_rule_wins(self):
        mappings = [
            {"source_prefix": "/data", "local_prefix": "/first"},
            {"source_prefix": "/data/Movies", "local_prefix": "/second"},
        ]
        self.assertEqual(normalize_path("/data/Movies/a.mkv", mappings), "/first/Movies/a.mkv")


class TestMatchMediaItem(unittest.TestCase):
    def test_exact_path_match(self):
        items_by_path = {"/mnt/Movies/a.mkv": "item-a"}
        items_by_basename = {}
        self.assertEqual(match_media_item("/mnt/Movies/a.mkv", items_by_path, items_by_basename), "item-a")

    def test_falls_back_to_unique_basename(self):
        items_by_path = {}
        items_by_basename = {"a.mkv": ["item-a"]}
        self.assertEqual(match_media_item("/some/other/path/a.mkv", items_by_path, items_by_basename), "item-a")

    def test_ambiguous_basename_returns_none(self):
        items_by_path = {}
        items_by_basename = {"episode1.mkv": ["item-a", "item-b"]}
        self.assertIsNone(match_media_item("/x/episode1.mkv", items_by_path, items_by_basename))

    def test_no_match_returns_none(self):
        self.assertIsNone(match_media_item("/x/y.mkv", {}, {}))

    def test_empty_path_returns_none(self):
        self.assertIsNone(match_media_item("", {"a": "item"}, {}))


if __name__ == "__main__":
    unittest.main()


class TestToRemotePath(unittest.TestCase):
    def test_inverse_of_normalize(self):
        from media_manager.playback_matching import normalize_path, to_remote_path
        mappings = [{"source_prefix": "/data/movies", "local_prefix": "/mnt/Movies"}]
        self.assertEqual(to_remote_path("/mnt/Movies/A.mkv", mappings), "/data/movies/A.mkv")
        self.assertEqual(normalize_path(to_remote_path("/mnt/Movies/A.mkv", mappings), mappings), "/mnt/Movies/A.mkv")
        self.assertEqual(to_remote_path("/other/A.mkv", mappings), "/other/A.mkv")
        self.assertEqual(to_remote_path("", mappings), "")
