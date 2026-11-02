"""What the loader refuses to read or fetch: an HTML page answered for a text
file (``_common.looks_like_html``), and with ``respect_robots`` (Zed / Amp)
anything ``robots.txt`` disallows, a redirect's target included.

The robots rules are the real files' shape: ampcode.com and zed.dev open with
``Allow: /``, which ``urllib.robotparser`` would take as the answer for every
path (first match); RFC 9309 takes the longest match.
"""

import contextlib
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _loader
from test_page_fetch import _Net, _Resp  # noqa: F401  (the mocked network of the page tests)

import _common

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"

AMP_ROBOTS = """\
User-agent: *
Allow: /
Allow: /api/external
Disallow: /admin
Disallow: /api/
Disallow: /auth/
Disallow: /feed

Host: https://ampcode.com
Sitemap: https://ampcode.com/sitemap.xml
"""
# the shape that made Kiro out of scope: the index and the pages are disallowed
KIRO_LIKE_ROBOTS = "User-agent: *\nDisallow: /llms.txt\nDisallow: /docs/*.md\n"
HTML = "<!doctype html>\n<!-- Appearance placeholders -->\n<html><body>Sign in</body></html>\n"


class LooksLikeHtmlTest(unittest.TestCase):
    def test_table(self):
        cases = {
            b"<!doctype html>\n<html>": True,
            b"<!DOCTYPE HTML PUBLIC>": True,
            b"  \n<html lang=\"en\">": True,
            b"\xef\xbb\xbf<!doctype html>": True,
            b"<html>": True,
            # Markdown that holds HTML, as Amp's pages do
            b"---\nlastModified: 2026-11-02\n---\n\n<script>\n</script>\n# Prompting\n": False,
            b"<h2 id=\"x\">X</h2>\n": False,
            b"# Title\n\n<!doctype html> in prose\n": False,
            b"<htmlish>": False,
            b"": False,
        }
        for data, want in cases.items():
            with self.subTest(data=data):
                self.assertEqual(_common.looks_like_html(data), want)


class SoftNotFoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "x.txt")
        self.url = "https://example.com/llms-full.txt"

    def fetch(self, body: str, **kw):
        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=_Resp(body.encode(), self.url)), \
                contextlib.redirect_stderr(err):
            return _common.fetch_url(self.url, self.path, user_agent="t", max_age=0, **kw), err.getvalue()

    def test_html_is_a_failed_fetch_and_is_not_cached(self):
        with self.assertRaises(_common.FetchError) as cm:
            self.fetch(HTML, raise_on_error=True)
        self.assertIsInstance(cm.exception.cause, _common.NotTextError)
        self.assertFalse(os.path.exists(self.path))

    def test_html_exits_1_without_raise_on_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.fetch(HTML)
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_html_keeps_the_good_copy_in_the_cache(self):
        Path(self.path).write_text("# Good\n", encoding="utf-8")
        path, err = self.fetch(HTML)
        self.assertEqual(path, self.path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "# Good\n")
        self.assertIn("using cached copy", err)

    def test_markdown_that_holds_html_is_kept(self):
        path, _err = self.fetch('<script>\n</script>\n\n<h2 id="a">A</h2>\n')
        self.assertTrue(os.path.exists(path))


class ParseRobotsTest(unittest.TestCase):
    AGENT = "claude-code-llms-docs-generic/1.0"

    def allows(self, text: str, path: str) -> bool:
        return _common.robots_allows(_common.parse_robots(text, self.AGENT), path)

    def test_longest_match_wins_over_a_first_allow(self):
        for path, want in (("/docs/markdown/prompting", True), ("/llms.txt", True), ("/auth/sign-in?returnTo=x", False),
                           ("/api/x", False), ("/api/external", True), ("/admin", False), ("/feed/a", False)):
            with self.subTest(path=path):
                self.assertEqual(self.allows(AMP_ROBOTS, path), want)

    def test_wildcards(self):
        cases = [
            ("/docs/*.md", "/docs/a/b.md", False), ("/docs/*.md", "/docs/a/b.html", True),
            ("/docs/*.md$", "/docs/b.md", False), ("/docs/*.md$", "/docs/b.md?x=1", True),
            ("/llms.txt$", "/llms.txt", False), ("/llms.txt$", "/llms.txtx", True),
            ("/a*b*c", "/axbxc", False), ("/a*b*c", "/axcxb", True),
            ("/a*ab$", "/ab", True), ("/*b$", "/bxb", False),
            ("/_next/static/chunks/**/[*", "/_next/static/chunks/a/[id].js", False),
            ("*", "/x", False), ("/x", "/y", True),
        ]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.allows(f"User-agent: *\nDisallow: {pattern}\n", path), want)

    def test_tie_goes_to_allow(self):
        self.assertTrue(self.allows("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a"))
        self.assertTrue(self.allows("User-agent: *\nAllow: /a\nDisallow: /a\n", "/a"))

    def test_groups(self):
        named = "User-agent: *\nDisallow: /\n\nUser-agent: Claude-Code-LLMS-Docs-Generic\nAllow: /\n"
        self.assertTrue(self.allows(named, "/x"))
        other = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n"
        self.assertTrue(self.allows(other, "/x"))
        self.assertFalse(self.allows(other, "/private"))
        # consecutive User-agent lines share one group; a rule ends the group's names
        shared = "User-agent: GPTBot\nUser-agent: *\nDisallow: /a\nUser-agent: GPTBot\nDisallow: /b\n"
        self.assertFalse(self.allows(shared, "/a"))
        self.assertTrue(self.allows(shared, "/b"))

    def test_empty_disallow_comments_and_junk(self):
        self.assertTrue(self.allows("User-agent: *\nDisallow:\n", "/x"))
        self.assertFalse(self.allows("# c\nUser-Agent: * # c\nDISALLOW: /x # c\njunk line\n", "/x"))
        self.assertTrue(self.allows("", "/x"))

    def test_many_stars_stay_fast(self):
        rules = _common.parse_robots("User-agent: *\nDisallow: /" + "*a" * 2000 + "b\n", self.AGENT)
        self.assertTrue(_common.robots_allows(rules, "/" + "a" * 4000))


ORIGIN = "https://docs.example.com"
INDEX = ORIGIN + "/llms.txt"
ROBOTS = ORIGIN + "/robots.txt"
GOOD = ORIGIN + "/docs/markdown/good"
HIDDEN = ORIGIN + "/secret/page"
PROFILE = {"body": "fetch", "rank_from": "index", "index_url": INDEX, "label": "Polite",
           "page_ref_forms": ["int", "url", "slug"], "respect_robots": True, "page_fetch_workers": 1}
LLMS = f"- [Good page]({GOOD}): widgets\n- [Hidden page]({HIDDEN}): widgets\n"


def _http_404(url):
    import urllib.error
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class _RobotsNet(_Net):
    """``_Net`` that answers some URLs with an exception (*raises*)."""

    def __init__(self, served=None, raises=None):
        super().__init__(served)
        self.raises = dict(raises or {})

    def __call__(self, req, *a, **kw):
        url = getattr(req, "full_url", req)
        if url in self.raises:
            self.calls.append(url)
            raise self.raises[url]
        return super().__call__(req, *a, **kw)


class RespectRobotsTest(unittest.TestCase):
    profile = PROFILE

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {"polite": self.profile}}), encoding="utf-8")
        self.net = _RobotsNet({INDEX: LLMS, ROBOTS: "User-agent: *\nAllow: /\nDisallow: /secret/\n",
                               GOOD: "# Good\n\n## Widgets\nwidgets here\n", HIDDEN: "# Hidden\n\nwidgets\n"})

    def run_g(self, *argv):
        words = [SCRIPT, *argv, "--source", "polite", "--sources-file", self.sources, "--cache-dir", self.tmp]
        with mock.patch("urllib.request.urlopen", side_effect=self.net):
            return _loader.run_cli(g, words)

    def test_an_allowed_page_is_fetched_and_robots_is_read_once(self):
        code, out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("widgets here", out)
        self.assertEqual(self.net.calls.count(ROBOTS), 1)
        self.assertIn(GOOD, self.net.calls)

    def test_a_disallowed_page_is_not_requested(self):
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 1)
        self.assertIn("robots.txt disallows", err)
        self.assertNotIn(HIDDEN, self.net.calls)

    def test_search_skips_a_disallowed_candidate_and_reports_it(self):
        code, out, err = self.run_g("search", "widgets")
        self.assertEqual(code, 0, err)
        self.assertNotIn(HIDDEN, self.net.calls)
        self.assertIn("(skip: fetch failed", err)
        self.assertIn("robots.txt disallows", err)
        self.assertIn("Good page", out)
        self.assertIn("(1 documents skipped", out)

    def test_a_disallowed_index_is_not_requested(self):
        self.net.served[ROBOTS] = KIRO_LIKE_ROBOTS
        code, _out, err = self.run_g("search-index", "widgets")
        self.assertEqual(code, 1)
        self.assertNotIn(INDEX, self.net.calls)
        self.assertIn("robots.txt disallows", err)

    def test_a_cached_page_needs_no_robots_request(self):
        self.run_g("content", "0")
        self.net.calls.clear()
        code, out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.calls, [])
        self.assertIn("widgets here", out)

    def test_a_fresh_cached_page_does_not_read_robots_even_with_no_robots_cached(self):
        self.run_g("content", "0")
        robots = g._page_cache_path(self.tmp, g._validate_profile(self.sources, "polite", self.profile), ROBOTS)
        self.assertTrue(os.path.exists(robots), "the first run read robots.txt")
        os.unlink(robots)
        os.unlink(robots + ".meta.json")
        self.net.calls.clear()
        code, _out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.calls, [])

    def test_a_missing_robots_file_is_asked_for_once_in_a_run(self):
        # a 404 leaves nothing in the cache, so only the run's memory stops a
        # request per page
        del self.net.served[ROBOTS]
        self.net.raises[ROBOTS] = _http_404(ROBOTS)
        code, _out, err = self.run_g("search", "widgets")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.calls.count(ROBOTS), 1)

    def test_no_robots_file_means_no_restriction(self):
        del self.net.served[ROBOTS]
        self.net.raises[ROBOTS] = _http_404(ROBOTS)
        code, out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        self.assertIn(HIDDEN, self.net.calls)
        self.assertIn("Hidden", out)

    def test_an_html_answer_for_robots_is_a_missing_file(self):
        self.net.served[ROBOTS] = HTML
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        self.assertIn(HIDDEN, self.net.calls)

    def test_an_unreachable_robots_file_stops_the_fetch(self):
        del self.net.served[ROBOTS]
        code, _out, err = self.run_g("search-index", "widgets")
        self.assertEqual(code, 1)
        self.assertNotIn(INDEX, self.net.calls)
        self.assertIn("robots.txt disallows", err)

    def test_a_server_error_on_robots_stops_the_fetch(self):
        import urllib.error
        del self.net.served[ROBOTS]
        self.net.raises[ROBOTS] = urllib.error.HTTPError(ROBOTS, 503, "Unavailable", {}, None)
        code, _out, _err = self.run_g("search-index", "widgets")
        self.assertEqual(code, 1)
        self.assertNotIn(INDEX, self.net.calls)

    def test_without_the_key_robots_is_never_read(self):
        profile = {k: v for k, v in PROFILE.items() if k != "respect_robots"}
        Path(self.sources).write_text(json.dumps({"sources": {"polite": profile}}), encoding="utf-8")
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        self.assertNotIn(ROBOTS, self.net.calls)
        self.assertIn(HIDDEN, self.net.calls)


class _Server(http.server.BaseHTTPRequestHandler):
    """Amp's shape: an unknown path redirects to a sign-in page under a
    disallowed prefix, which answers 200 with HTML."""

    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.path)
        if self.path == "/robots.txt":
            self._send(200, AMP_ROBOTS)
        elif self.path == "/llms.txt":
            base = f"http://{self.headers['Host']}"
            self._send(200, f"- [Good]({base}/docs/good): a\n- [Gone]({base}/docs/gone): b\n- [Moved]({base}/docs/moved): c\n")
        elif self.path == "/docs/good":
            self._send(200, "# Good\n\nbody\n")
        elif self.path == "/docs/moved":
            self._redirect("/docs/good")
        elif self.path == "/docs/gone":
            self._redirect("/hop")
        elif self.path == "/hop":
            self._redirect("/auth/sign-in")
        else:
            self._send(200, HTML)

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.end_headers()

    def _send(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class RedirectTest(unittest.TestCase):
    """A real server: the redirect handler only runs on a real redirect."""

    def setUp(self):
        _Server.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")

    def run_g(self, profile, *argv):
        profile = {**PROFILE, "index_url": self.base + "/llms.txt", **profile}
        Path(self.sources).write_text(json.dumps({"sources": {"polite": profile}}), encoding="utf-8")
        return _loader.run_cli(g, [SCRIPT, *argv, "--source", "polite", "--sources-file", self.sources,
                                   "--cache-dir", self.tmp])

    def test_a_redirect_into_a_disallowed_place_is_not_followed(self):
        code, _out, err = self.run_g({}, "content", "1")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertIn("/hop", _Server.seen)  # the first hop is allowed
        self.assertNotIn("/auth/sign-in", _Server.seen)

    def test_an_allowed_redirect_is_followed(self):
        code, out, err = self.run_g({}, "content", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("body", out)

    def test_without_the_key_the_sign_in_page_is_reached_and_still_not_read_as_a_page(self):
        profile = {"respect_robots": False}
        code, _out, err = self.run_g(profile, "content", "1")
        self.assertEqual(code, 1)
        self.assertIn("/auth/sign-in", _Server.seen)
        self.assertIn("HTML page", err)


if __name__ == "__main__":
    unittest.main()
