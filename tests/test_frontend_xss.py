"""
Regression guards for the stored-XSS findings in the 2026-11-07 security review
(HIGH 2, MEDIUM 3). The SPA has no JS test runner, so these are static checks on
the shipped files, plus a behavioural check of escapeHtml when node is available.
"""
import os
import re
import shutil
import subprocess
import unittest

JS_DIR = os.path.join(os.path.dirname(__file__), "..", "media_manager", "web", "static", "js")


def _read(name):
    with open(os.path.join(JS_DIR, name), encoding="utf-8") as f:
        return f.read()


# (file, raw interpolation that must no longer appear unescaped)
RAW_SINKS = [
    ("admin_users.js", "${u.email}"),
    ("admin_users.js", "${u.phone}"),
    ("admin_users.js", "${u.email || ''}"),
    ("admin_users.js", "${u.phone || ''}"),
    ("admin_users.js", "${u.first_name || ''}"),
    ("admin_users.js", "${u.last_name || ''}"),
    ("admin_users.js", "${u.two_factor_method.toUpperCase()}"),
    ("admin_users.js", "'${u.username}'"),
    ("admin_users.js", "<b>${username}</b>"),
    ("duplicates.js", "${best.path}"),
    ("duplicates.js", "${c.path}"),
    ("duplicates.js", "                    ${path}\n"),
    ("inventory.js", 'title="${ep.title}"'),
    ("inventory.js", "${ep.title || "),
    ("inventory.js", "${ep.path}"),
    ("inventory.js", '"${meta.title}"'),
    ("inventory.js", "${meta.overview}"),
    ("downloads.js", "${s.name}"),
    ("downloads.js", "${item.filename}"),
    ("downloads.js", "${f.path}"),
    ("downloads.js", "${f.label}"),
    ("downloads.js", "${file.name}"),
    ("media_calendar_grid.js", ">${it.title}<"),
    ("admin_integrations.js", "${ds.error}"),
    ("admin_integrations.js", "${d.path}"),
]


class TestFrontendEscaping(unittest.TestCase):
    def test_reported_sinks_are_escaped(self):
        leftovers = [f"{f}: {pat}" for f, pat in RAW_SINKS if pat in _read(f)]
        self.assertEqual(leftovers, [], "unescaped interpolations remain")

    def test_no_inline_onclick_with_file_path(self):
        # Escaping a path for an HTML attribute does not make it safe inside a JS
        # string in onclick; paths must go through data-* attributes instead.
        offenders = []
        for name in sorted(os.listdir(JS_DIR)):
            if name.endswith(".js") and re.search(r"deleteSingleDuplicateItem\('\$\{", _read(name)):
                offenders.append(name)
        self.assertEqual(offenders, [])

    def test_error_messages_not_raw_in_innerhtml(self):
        offenders = []
        pattern = re.compile(r"innerHTML\s*=.*\$\{((err|e|error)\.message|data\.detail)")
        for name in sorted(os.listdir(JS_DIR)):
            if not name.endswith(".js"):
                continue
            for i, line in enumerate(_read(name).splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{name}:{i}")
        self.assertEqual(offenders, [])

    def test_escape_html_handles_single_quote_statically(self):
        src = _read("utils.js")
        body = src[src.index("function escapeHtml"):src.index("function formatGB")]
        self.assertIn("&#39;", body)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_escape_html_behaviour(self):
        src = _read("utils.js")
        fn = src[src.index("function escapeHtml"):src.index("function formatGB")]
        script = fn + "\nprocess.stdout.write(escapeHtml(`<a href=\"x\" onclick='y'>&</a>`));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;")


if __name__ == "__main__":
    unittest.main()
