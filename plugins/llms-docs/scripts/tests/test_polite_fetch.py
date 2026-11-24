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
import time
import unittest
from pathlib import Path
from unittest import mock

import _loader
from test_page_fetch import _Net, _Resp  # (the mocked network of the page tests)

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


SCAN = 64 * 1024  # _common._HTML_SCAN_BYTES, written out: the bound is part of the rule
# the real start of Hono's llms-full.txt (2026-11-03)
HONO_START = (b"<SYSTEM>This is the full developer documentation for Hono.</SYSTEM>\n\n"
              b"# Start of Hono documentation\n\n# Hono\n\n")
XHTML_DOCTYPE = b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'


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
            # what comes before the document is skipped: comments, an XML declaration, blank lines
            b"<!-- build 1234 -->\n<!doctype html><html>": True,
            b"<!-- a -->\n<!-- b -->\n<html lang=\"en\">": True,
            b"\xef\xbb\xbf<!-- build -->\n<!doctype html>": True,
            b"<!-- build -->\n<head><title>404</title></head>": True,
            b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + XHTML_DOCTYPE + b"\n<html>": True,
            b"<?XML version=\"1.0\"?>\n<html xmlns=\"http://www.w3.org/1999/xhtml\">": True,
            b"\n" * 2048 + b"<!doctype html>": True,
            b"\r\n" * 600 + b"<!doctype html>": True,
            b"<!-- " + b"x" * 3600 + b" -->\n<!doctype html>": True,
            b"<!DOCTYPEhtml>": True,
            # white space is ASCII's (form feed and vertical tab too): before the document, in the
            # doctype, after the tag's name
            b"\f\v<!doctype html>": True,
            b"<!DOCTYPE\n  html>": True,
            b"<html\n  lang=\"en\">": True,
            # a page that leaves <html> out, in either case
            b"<head>\n<title>404</title></head><body>x</body>": True,
            b"<HEAD><TITLE>404</TITLE></HEAD>": True,
            b"<body>Page not found</body>": True,
            b"<BODY>Page not found</BODY>": True,
            # text the file is: comments and tags a Markdown file starts with
            b"<!-- markdownlint-disable -->\n# Title\n": False,
            b"<!-- badge -->\n<p align=\"center\">\n  <img src=\"logo.png\">\n</p>\n": False,
            b"<!-- never closed\n# Title\n\n<!doctype html>\n": False,
            HONO_START: False,
            b"<Note>\nA note.\n</Note>\n": False,
            b"<script>\n</script>\n# Title\n": False,
            b"<style>\n.x {}\n</style>\n# Title\n": False,
            b"<header>Site</header>\n# Title\n": False,
            b"<bodytext>x</bodytext>": False,
            b"<div class=\"x\">\n# Title\n": False,
            # MDX / React components named like the tags (react-email's <Html lang="en">)
            b"<Head>\n  <title>Docs</title>\n</Head>\n\n# Title\n": False,
            b"<Html lang=\"en\">\n<Body>x</Body>\n</Html>\n": False,
            b"<Body>x</Body>": False,
            b"<?xml version=\"1.0\"?>\n<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>\n": False,
            # tags that do not start a document on their own
            b"<meta charset=\"utf-8\">\n<title>404</title>": False,
            b"<title>404</title>\n": False,
            # only the first SCAN bytes are read: a comment that runs past them reads as text
            b"<!--" + b"x" * SCAN + b"-->\n<!doctype html>": False,
        }
        for data, want in cases.items():
            with self.subTest(data=data[:80]):
                self.assertEqual(_common.looks_like_html(data), want)

    def test_the_scan_bound(self):
        """Both sides of the bound: the doctype just within the first SCAN
        bytes, and a byte past them."""
        for blank, want in ((SCAN - len(b"<!doctype html>"), True), (SCAN - len(b"<!doctype html>") + 1, False)):
            with self.subTest(blank=blank):
                data = b"\n" * blank + b"<!doctype html>"
                self.assertEqual(_common.looks_like_html(data), want)
        for pad, want in ((SCAN - len(b"<!---->") - len(b"<html>"), True), (SCAN - len(b"<!---->") - len(b"<html>") + 1, False)):
            with self.subTest(comment_pad=pad):
                data = b"<!--" + b"x" * pad + b"-->" + b"<html>"
                self.assertEqual(_common.looks_like_html(data), want)

    def assert_quick_and_not_html(self, data: bytes) -> None:
        started = time.monotonic()
        self.assertFalse(_common.looks_like_html(data))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_thirty_comments_take_no_time(self):
        """One regular expression with a repeated group of comments takes
        about 80 s here (four times as long for every two more)."""
        self.assert_quick_and_not_html(b"<!--a-->" * 30 + b"<p>")

    def test_a_scan_full_of_comments_takes_no_time(self):
        self.assert_quick_and_not_html(b"<!--a-->" * (SCAN // 8))


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

    # HTML that does not open with the doctype: a build comment above it, or
    # no <html> before the <head>
    PAGES = {
        "a comment first": "<!-- build 1234 -->\n<!doctype html>\n<html><head><title>Sign in</title></head>"
                           "<body>Sign in</body></html>\n",
        "<head> first": "<head><title>404</title></head>\n<body>Page not found</body>\n",
    }

    def test_html_after_a_comment_or_from_its_head_is_not_cached(self):
        for what, body in self.PAGES.items():
            with self.subTest(what):
                with self.assertRaises(_common.FetchError) as cm:
                    self.fetch(body, raise_on_error=True)
                self.assertIsInstance(cm.exception.cause, _common.NotTextError)
                self.assertFalse(os.path.exists(self.path))

    def test_html_after_a_comment_or_from_its_head_keeps_the_good_copy(self):
        for what, body in self.PAGES.items():
            with self.subTest(what):
                Path(self.path).write_text("# Good\n", encoding="utf-8")
                path, err = self.fetch(body)
                self.assertEqual(path, self.path)
                self.assertEqual(Path(path).read_text(encoding="utf-8"), "# Good\n")
                self.assertIn("WARNING: fetch failed (the server answered with an HTML page", err)
                self.assertIn("using cached copy", err)

    def test_markdown_after_a_comment_is_cached(self):
        body = "<!-- markdownlint-disable MD033 -->\n# Title\n\nText.\n"
        path, err = self.fetch(body)
        self.assertEqual((path, err), (self.path, ""))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), body)


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

    def test_a_byte_order_mark_does_not_hide_the_first_group(self):
        for text in ("User-agent: *\nDisallow: /auth/\n",
                     "User-agent: GPTBot\nDisallow: /a\nUser-agent: *\nDisallow: /auth/\n"):
            with self.subTest(text=text):
                self.assertEqual(_common.parse_robots("\ufeff" + text, self.AGENT),
                                 _common.parse_robots(text, self.AGENT))
                self.assertFalse(self.allows("\ufeff" + text, "/auth/sign-in"))

    def test_a_wildcard_does_not_split_a_kept_escape(self):
        for rules, path in (("Disallow: /a/\nAllow: /a*F", "/a%2F"),
                            ("Disallow: /private/\nAllow: /private*2Fsecret", "/private%2Fsecret")):
            with self.subTest(rules=rules):
                self.assertFalse(self.allows(f"User-agent: *\n{rules}\n", path))

    def test_a_named_group_decides_even_with_no_rule(self):
        me = "User-agent: Claude-Code-LLMS-Docs-Generic"
        for text in (f"User-agent: *\nDisallow: /\n\n{me}\nDisallow:\n",
                     f"{me}\nDisallow:\n\nUser-agent: *\nDisallow: /\n",
                     f"User-agent: *\nDisallow: /\n\n{me}\n"):
            with self.subTest(text=text):
                self.assertTrue(self.allows(text, "/x"))
        # another name's empty group leaves the "*" rules in force
        self.assertFalse(self.allows("User-agent: GPTBot\nDisallow:\n\nUser-agent: *\nDisallow: /\n", "/x"))
        # no rule between the two names: one group, and its rules are mine
        self.assertFalse(self.allows(f"{me}\n\nUser-agent: *\nDisallow: /x\n", "/x"))

    def test_encoding_does_not_decide(self):
        # (pattern, path, the pattern matches)
        cases = [
            ("/%7Euser", "/~user", True), ("/~user", "/%7Euser", True), ("/%7euser", "/%7Euser", True),
            ("/a%20b", "/a b", True), ("/a b", "/a%20b", True),
            ("/foo/%62%61%7A", "/foo/baz", True), ("/foo/baz", "/foo/%62%61%7A", True),
            ("/?u=https://x", "/?u=https%3A%2F%2Fx", True), ("/?u=https%3A%2F%2Fx", "/?u=https://x", True),
            ("/ツ", "/%E3%83%84", True), ("/%E3%83%84", "/ツ", True), ("/%e3%83%84", "/ツ", True),
            # one level of escape only: %25 is a percent sign, not the start of another escape
            ("/%2541", "/A", False), ("/%2541", "/%2541", True), ("/%41", "/%2541", False), ("/%25", "/%25", True),
            # a lone percent sign is a literal one
            ("/100%", "/100%25", True), ("/100%25", "/100%", True), ("/%zz", "/%25zz", True),
            # an escaped * or $ is the character, not the wildcard or the end
            ("/a%2Ab", "/axb", False), ("/a%2Ab", "/a*b", True), ("/a*b", "/a%2Ab", True), ("/a*b", "/axb", True),
            ("/a%24", "/a", False), ("/a%24", "/a$", True), ("/a$", "/a$", False), ("/a$", "/a", True),
            ("/a$b", "/a$b", True), ("/a$b", "/a", False),
            # the wildcard between encoded pieces still works
            ("/%7Eu*%20x$", "/~u/any x", True), ("/%7Eu*%20x$", "/~u/any xy", False),
        ]
        for pattern, path, matches in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.allows(f"User-agent: *\nDisallow: {pattern}\n", path), not matches)

    def test_a_reserved_escape_in_allow_does_not_lift_a_refusal(self):
        # (rules, path, allowed)
        cases = [
            ("Disallow: /private/\nAllow: /private%2Fpub", "/private/pub/x", False),
            ("Disallow: /a?\nAllow: /a%3F", "/a?x", False),
            ("Disallow: /a/b\nAllow: /a%2Fb", "/a/b", False),
            ("Disallow: /a?\nAllow: /a%3f", "/a?x", False),
            ("Disallow: /x/\nAllow: /x%2F*.md$", "/x/a.md", False),
            # the spelling the rule names is still allowed, whatever the hex case
            ("Disallow: /\nAllow: /a%2Fb", "/a%2Fb", True),
            ("Disallow: /\nAllow: /a%2fb", "/a%2Fb", True),
            ("Disallow: /\nAllow: /a%2Fb", "/a%2fb", True),
            ("Disallow: /x/\nAllow: /x%2F*.md$", "/x%2Fa.md", True),
            # a literal reserved character in Allow names only itself
            ("Disallow: /\nAllow: /a/b", "/a/b", True),
            ("Disallow: /\nAllow: /a/b", "/a%2Fb", False),
            # a character that is not reserved compares as before
            ("Disallow: /\nAllow: /%7Euser", "/~user", True),
            ("Disallow: /\nAllow: /~user", "/%7Euser", True),
            ("Disallow: /\nAllow: /a%20b", "/a b", True),
            ("Disallow: /\nAllow: /%E3%83%84", "/ツ", True),
            ("Disallow: /\nAllow: /%41", "/A", True),
            # Disallow still decodes: no encoding slips past it
            ("Allow: /\nDisallow: /private%2F", "/private/x", False),
            ("Allow: /\nDisallow: /private/", "/private%2Fx", False),
            ("Allow: /\nDisallow: /a%3F", "/a?x", False),
            ("Allow: /\nDisallow: /a?", "/a%3Fx", False),
            # the longest match is counted decoded: the kept escape adds nothing
            ("Disallow: /a/b/c\nAllow: /a%2Fb", "/a%2Fb/c", False),
            ("Disallow: /a/b\nAllow: /a%2Fb", "/a%2Fb", True),
        ]
        for rules, path, allowed in cases:
            with self.subTest(rules=rules, path=path):
                self.assertEqual(self.allows(f"User-agent: *\n{rules}\n", path), allowed)

    def test_every_reserved_character_stays_escaped_in_allow(self):
        for ch in ":/?[]@!&'()+,;=":
            escape = f"%{ord(ch):02X}"
            with self.subTest(ch=ch):
                self.assertFalse(self.allows(f"User-agent: *\nDisallow: /a{ch}\nAllow: /a{escape}\n", f"/a{ch}x"))
                self.assertTrue(self.allows(f"User-agent: *\nDisallow: /\nAllow: /a{escape}\n", f"/a{escape}x"))
        # "#" starts a comment in robots.txt, so only the escape can be written
        self.assertFalse(self.allows("User-agent: *\nDisallow: /a\nAllow: /a%23\n", "/a#x"))
        self.assertTrue(self.allows("User-agent: *\nDisallow: /a\nAllow: /a%23\n", "/a%23x"))

    def test_the_longest_match_is_counted_on_one_spelling(self):
        # the same path written twice ties, and a tie goes to Allow, whichever is the longer text
        self.assertTrue(self.allows("User-agent: *\nDisallow: /%7Euser\nAllow: /~user\n", "/~user"))
        self.assertTrue(self.allows("User-agent: *\nDisallow: /~user\nAllow: /%7Euser\n", "/~user"))

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

    def test_an_unreachable_robots_file_stops_the_fetch_and_says_so(self):
        del self.net.served[ROBOTS]
        code, _out, err = self.run_g("search-index", "widgets")
        self.assertEqual(code, 1)
        self.assertNotIn(INDEX, self.net.calls)
        self.assertIn(f"could not read {ROBOTS}", err)
        self.assertIn("mock network", err)  # why it could not be read
        self.assertNotIn("disallows", err)  # the site forbade nothing

    def test_a_server_error_on_robots_stops_the_fetch_and_says_so(self):
        import urllib.error
        del self.net.served[ROBOTS]
        self.net.raises[ROBOTS] = urllib.error.HTTPError(ROBOTS, 503, "Unavailable", {}, None)
        code, _out, err = self.run_g("search-index", "widgets")
        self.assertEqual(code, 1)
        self.assertNotIn(INDEX, self.net.calls)
        self.assertIn(f"could not read {ROBOTS}", err)
        self.assertIn("503", err)
        self.assertNotIn("disallows", err)

    def test_a_byte_order_mark_on_the_served_file_keeps_the_refusal(self):
        self.net.served[ROBOTS] = "\ufeffUser-agent: *\nDisallow: /secret/\n"
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 1, err)
        self.assertNotIn(HIDDEN, self.net.calls)

    def test_an_encoded_rule_stops_the_page(self):
        self.net.served[ROBOTS] = "User-agent: *\nDisallow: /%73ecret/\n"
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 1, err)
        self.assertNotIn(HIDDEN, self.net.calls)
        self.assertIn("robots.txt disallows", err)

    def _page_cache(self, url):
        return g._page_cache_path(self.tmp, g._validate_profile(self.sources, "polite", self.profile), url)

    def _age(self, path):
        os.utime(path, (1, 1))

    def test_a_refused_page_with_a_stale_copy_answers_with_the_copy_and_no_request(self):
        self.net.served[ROBOTS] = "User-agent: *\nAllow: /\n"
        code, out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("widgets", out)
        self.net.served[ROBOTS] = "User-agent: *\nDisallow: /secret/\n"
        self.net.served[HIDDEN] = "# Hidden\n\nnew body\n"
        for url in (ROBOTS, HIDDEN):
            self._age(self._page_cache(url))
        self.net.calls.clear()
        code, out, err = self.run_g("content", "1", "--max-age", "60")
        self.assertEqual(code, 0, err)
        self.assertNotIn(HIDDEN, self.net.calls)
        self.assertIn(ROBOTS, self.net.calls)
        self.assertIn("WARNING", err)
        self.assertIn("using cached copy", err)
        self.assertIn("robots.txt disallows", err)
        self.assertIn("widgets", out)
        self.assertNotIn("new body", out)

    def test_a_refused_page_does_not_answer_with_a_cache_of_another_url(self):
        self.net.served[ROBOTS] = "User-agent: *\nAllow: /\n"
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        path = self._page_cache(HIDDEN)
        Path(path).write_text("# Another\n\nanother corpus\n", encoding="utf-8")
        Path(path + ".meta.json").write_text(json.dumps({"url": ORIGIN + "/elsewhere"}), encoding="utf-8")
        self.net.served[ROBOTS] = "User-agent: *\nDisallow: /secret/\n"
        self._age(self._page_cache(ROBOTS))
        self.net.calls.clear()
        code, out, err = self.run_g("content", "1", "--max-age", "60")
        self.assertEqual(code, 1, out)
        self.assertNotIn(HIDDEN, self.net.calls)
        self.assertNotIn("another corpus", out)
        self.assertNotIn("using cached copy", err)
        self.assertIn("robots.txt disallows", err)

    def run_safely(self, *argv):
        try:
            return self.run_g(*argv)
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"{argv} crashed: {e!r}")

    def test_an_index_url_whose_host_cannot_be_read_is_a_failed_fetch(self):
        # (an unclosed "[" is rejected by urlsplit on every Python)
        bad = "https://[unreadable.example.com/llms.txt"
        Path(self.sources).write_text(json.dumps({"sources": {"polite": {**self.profile, "index_url": bad}}}),
                                      encoding="utf-8")
        code, out, err = self.run_safely("search-index", "widgets")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn(f"Error: Failed to fetch {bad}: the host of the URL cannot be read (", err)
        self.assertEqual(self.net.calls, [])

    def test_a_stale_copy_of_a_page_whose_host_cannot_be_read_answers_with_a_warning(self):
        bad = "https://unreadable].example.com/docs/page"
        self.net.served[INDEX] = f"- [Unreadable page]({bad}): widgets\n" + LLMS
        path = self._page_cache(bad)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("# Unreadable\n\nthe copy\n", encoding="utf-8")
        self._age(path)
        code, out, err = self.run_safely("content", "0", "--max-age", "60")
        self.assertEqual(code, 0, err)
        self.assertIn("the copy", out)
        self.assertIn("WARNING: fetch failed (the host of the URL cannot be read (", err)
        self.assertIn("using cached copy", err)
        self.assertNotIn(bad, self.net.calls)
        self.assertEqual([c for c in self.net.calls if "unreadable" in c], [])

    def test_without_the_key_robots_is_never_read(self):
        profile = {k: v for k, v in PROFILE.items() if k != "respect_robots"}
        Path(self.sources).write_text(json.dumps({"sources": {"polite": profile}}), encoding="utf-8")
        code, _out, err = self.run_g("content", "1")
        self.assertEqual(code, 0, err)
        self.assertNotIn(ROBOTS, self.net.calls)
        self.assertIn(HIDDEN, self.net.calls)


class RobotsOnceWithWorkersTest(unittest.TestCase):
    """``search``'s workers meet the file together only when the pages live on
    another host than the index (the index read the index host's file first)."""

    OTHER = "https://pages.example.com"

    def test_eight_workers_read_robots_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sources = os.path.join(tmp, "sources.json")
        Path(sources).write_text(json.dumps({"sources": {"polite": {**PROFILE, "page_fetch_workers": 8}}}), encoding="utf-8")
        pages = [f"{self.OTHER}/p{i}" for i in range(8)]
        robots = self.OTHER + "/robots.txt"
        # Holds the first reader until a second one arrives. Under the lock no
        # second one can, so this waits out the timeout and the count is 1; with
        # no lock the other workers are at the same line, and the count is more.
        meet = threading.Barrier(2)

        def on_fetch(url):
            if url == robots:
                with contextlib.suppress(threading.BrokenBarrierError):
                    meet.wait(timeout=0.5)

        served = {INDEX: "".join(f"- [Page {i}]({u}): widgets\n" for i, u in enumerate(pages)),
                  ROBOTS: "User-agent: *\nAllow: /\n", robots: "User-agent: *\nAllow: /\n",
                  **{u: "# Page\n\nwidgets\n" for u in pages}}
        net = _Net(served, on_fetch=on_fetch)
        with mock.patch("urllib.request.urlopen", side_effect=net):
            code, _out, err = _loader.run_cli(g, [SCRIPT, "search", "widgets", "--top-n", "8", "--source", "polite",
                                                  "--sources-file", sources, "--cache-dir", tmp])
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(u in net.calls for u in pages), 8)
        self.assertEqual(net.calls.count(robots), 1)


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
            self._send(200, f"- [Good]({base}/docs/good): a\n- [Gone]({base}/docs/gone): b\n- [Moved]({base}/docs/moved): c\n"
                            f"- [Away]({base}/docs/away): d\n")
        elif self.path == "/docs/good":
            self._send(200, "# Good\n\nbody\n")
        elif self.path == "/docs/moved":
            self._redirect("/docs/good")
        elif self.path == "/docs/away":
            self._redirect(type(self).elsewhere)
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


class _BrokenRobots(http.server.BaseHTTPRequestHandler):
    """Another host, whose robots.txt answers 503."""

    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.path)
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def test_a_redirect_to_a_host_whose_robots_cannot_be_read_says_so(self):
        broken = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BrokenRobots)
        threading.Thread(target=broken.serve_forever, daemon=True).start()
        self.addCleanup(broken.server_close)
        self.addCleanup(broken.shutdown)
        _BrokenRobots.seen = []
        _Server.elsewhere = f"http://127.0.0.1:{broken.server_address[1]}/page"
        code, _out, err = self.run_g({}, "content", "3")
        self.assertEqual(code, 1, err)
        self.assertEqual(_BrokenRobots.seen, ["/robots.txt"])
        self.assertIn("refused: could not read", err)
        self.assertNotIn("disallows", err)

    def test_a_redirect_to_a_host_that_cannot_be_read_is_a_failed_fetch(self):
        # urllib reads the target before the robots check is asked: its
        # ValueError is a failed fetch too (an unclosed "[" fails everywhere)
        _Server.elsewhere = "https://[unreadable.example.com/page"
        try:
            code, out, err = self.run_g({}, "content", "3")
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"crashed: {e!r}")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertIn("Error: Failed to fetch ", err)
        self.assertIn("/docs/away: Invalid IPv6 URL", err)
        self.assertIn("/docs/away", _Server.seen)

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
