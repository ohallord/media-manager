import os
import re
import unittest
from urllib.parse import unquote

from media_manager.web.app import STATIC_DIR


def _index_html():
    path = os.path.join(STATIC_DIR, "index.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _help_js():
    path = os.path.join(STATIC_DIR, "js", "help.js")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_app_hash(hash_value):
    """Keep in lockstep with window.parseAppHash in help.js."""
    raw = str(hash_value or "").lstrip("#").strip() or "inventory"
    slash = raw.find("/")
    if slash == -1:
        return {"viewId": raw, "topic": None}
    topic = unquote(raw[slash + 1 :])
    return {"viewId": raw[:slash], "topic": topic or None}


class TestHelpPageMarkup(unittest.TestCase):
    def test_help_nav_and_view_exist(self):
        html = _index_html()
        self.assertIn('id="nav-help"', html)
        self.assertIn('id="view-help"', html)
        self.assertIn("help: 'Help'", html)
        self.assertIn("/static/js/help.js", html)

    def test_shareable_topic_anchors(self):
        html = _index_html()
        for topic in ("layout", "search", "redownload", "requests", "discover", "library", "account"):
            self.assertIn(f'id="help-{topic}"', html)
            self.assertIn(f'href="#help/{topic}"', html)

    def test_redownload_instructions_present(self):
        html = _index_html()
        self.assertIn("Search &amp; Redownload", html)
        self.assertIn("#help/redownload", html)


class TestParseAppHash(unittest.TestCase):
    def test_bare_help(self):
        self.assertEqual(parse_app_hash("#help"), {"viewId": "help", "topic": None})

    def test_help_topic(self):
        self.assertEqual(
            parse_app_hash("#help/redownload"),
            {"viewId": "help", "topic": "redownload"},
        )

    def test_empty_defaults_to_inventory(self):
        self.assertEqual(parse_app_hash(""), {"viewId": "inventory", "topic": None})

    def test_help_js_defines_parser(self):
        js = _help_js()
        self.assertIn("window.parseAppHash", js)
        self.assertIn("window.scrollHelpTopic", js)
        self.assertTrue(re.search(r"indexOf\s*\(\s*[\"']/[\"']\s*\)", js))
