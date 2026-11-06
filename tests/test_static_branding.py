"""The logo is the favicon, the header mark, and the sign-in logo.

Favicon assets must be served without a login (the browser fetches them
before anyone signs in), and every icon index.html links to must exist.
"""
import os
import re
import unittest
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from media_manager.web.app import app

STATIC = os.path.join(os.path.dirname(__file__), "..", "media_manager", "web", "static")


class TestBranding(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_logo_svg_is_valid_and_labelled(self):
        root = ET.parse(os.path.join(STATIC, "img", "logo.svg")).getroot()
        self.assertTrue(root.tag.endswith("svg"))
        self.assertEqual(root.get("viewBox"), "0 0 64 64")
        self.assertEqual(root.get("aria-label"), "Media Manager")

    def test_icon_links_point_at_existing_files(self):
        hrefs = re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="/static/([^"]+)"', self.html)
        self.assertEqual(sorted(hrefs), ["img/apple-touch-icon.png", "img/logo.svg"])
        for href in hrefs:
            self.assertTrue(os.path.isfile(os.path.join(STATIC, href)), href)

    def test_icons_served_without_login(self):
        r = self.client.get("/static/img/logo.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/svg+xml", r.headers["content-type"])
        r = self.client.get("/static/img/apple-touch-icon.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:8], b"\x89PNG\r\n\x1a\n")

    def test_header_and_login_use_the_mark(self):
        self.assertIn('class="brand-mark"', self.html)
        self.assertIn('class="auth-logo" src="/static/img/logo.svg"', self.html)


if __name__ == "__main__":
    unittest.main()
