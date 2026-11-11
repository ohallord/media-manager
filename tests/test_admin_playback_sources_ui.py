"""
Setup -> Media servers form for Plex/Emby/Jellyfin playback sources. The SPA has
no JS test runner, so these are static checks on the shipped files plus a
behavioural check of the path-mapping parser when node is available.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "media_manager", "web", "static")


def _read(rel):
    with open(os.path.join(STATIC_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _playback_section():
    src = _read("js/admin_integrations.js")
    return src[src.index("const PLAYBACK_SOURCE_SERVICES"):src.index("async function testIntegrationConnection")]


class TestPlaybackSourcesForm(unittest.TestCase):
    def test_setup_view_has_media_servers_card(self):
        html = _read("index.html")
        setup = html[html.index('<section id="view-admin"'):html.index('<section id="view-paths"')]
        self.assertIn('id="admin-playback-sources-list"', setup)
        self.assertIn("triggerPlaybackSync()", setup)

    def test_js_calls_every_playback_config_endpoint(self):
        js = _playback_section()
        for endpoint in (
            "'/api/v1/playback/config'",
            "`/api/v1/playback/config/${svc}`",
            "`/api/v1/playback/test/${svc}`",
            "'/api/v1/playback/sync'",
        ):
            self.assertIn(endpoint, js)
        for svc in ("'plex'", "'emby'", "'jellyfin'"):
            self.assertIn(svc, js)

    def test_loaded_with_the_rest_of_setup(self):
        src = _read("js/admin_integrations.js")
        loader = src[src.index("window.loadAdminIntegrations"):src.index("function renderAdminIntegrations")]
        self.assertIn("window.loadAdminPlaybackSources()", loader)

    def test_save_always_sends_path_mappings(self):
        # The PUT replaces stored mappings; a body without them would wipe them.
        js = _playback_section()
        form = js[js.index("function readPlaybackSourceForm"):js.index("async function savePlaybackSource")]
        self.assertIn("path_mappings: parsePathMappings(", form)

    def test_server_values_are_escaped(self):
        js = _playback_section()
        # pb.enabled only toggles a fixed 'checked' literal, so it is exempt.
        raw = re.findall(r"\$\{pb\.(?!enabled\b)[a-z_]+[^}]*\}", js)
        self.assertEqual(raw, [], "unescaped config values in the media servers form")
        self.assertIn("escapeHtml(pb.api_url", js)
        self.assertIn("escapeHtml(pb.api_key", js)
        self.assertIn("escapeHtml(formatPathMappings(pb.path_mappings))", js)
        self.assertIn("escapeHtml(pb.status", js)

    def test_uses_parse_api_json_not_res_json(self):
        self.assertNotIn("res.json()", _playback_section())

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_parse_and_format_path_mappings_behaviour(self):
        js = _playback_section()
        fns = js[js.index("function parsePathMappings"):js.index("window.loadAdminPlaybackSources")]
        script = fns + (
            "\nconst parsed = parsePathMappings('/data => /mnt\\n\\n  bad line\\n=> /x\\n/tv=>/mnt/tv  \\n');"
            "\nprocess.stdout.write(JSON.stringify({parsed, round: formatPathMappings(parsed),"
            " empty: parsePathMappings(undefined)}));"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(result["parsed"], [
            {"source_prefix": "/data", "local_prefix": "/mnt"},
            {"source_prefix": "/tv", "local_prefix": "/mnt/tv"},
        ])
        self.assertEqual(result["round"], "/data => /mnt\n/tv => /mnt/tv")
        self.assertEqual(result["empty"], [])


class TestSearchEmptyStateEscaping(unittest.TestCase):
    def test_empty_results_escape_the_search_term(self):
        src = _read("js/media_search.js")
        self.assertIn(
            "const displayTerm = escapeHtml(data.term || document.getElementById('arr-search-input')?.value.trim() || '');",
            src,
        )


if __name__ == "__main__":
    unittest.main()
