"""``body: "fetch"``: a source with no ``llms-full.txt`` (Firebase).

The ``llms.txt`` entries are the pages, numbered in ``llms.txt`` order, and a
page's body is fetched from its entry's URL only when a command needs it,
into a page cache directory whose file names are those of
``parse-firebase.py`` (``_common.page_cache_filename``).

The checks here:

* the tests of ``test_parse_firebase.py``, run on the generic loader with a
  ``body: "fetch"`` profile (``FirebaseTestsOnGenericLoader``);
* validation of the keys (``body``, the keys it refuses, ``page_cache_dir``
  for the bundled presets only, the integer keys) and the page cache names;
* what is fetched, counted on a mocked network: ``fetch-index`` /
  ``search-index`` fetch no page, ``sections`` / ``content`` one, ``search``
  only its candidates (none when nothing ranks, and no full-corpus fallback)
  at most ``page_fetch_workers`` at a time, ``search-content`` one page at a
  time with each page's hits printed before the next page is fetched;
* a page that cannot be fetched is skipped and counted, its neighbours keep
  their own bodies, and a later run fetches only what is still missing;
* the round trip over the cache states (index and pages cached, only the
  pages cached, nothing cached, a local ``--index-file``): every page number
  and ``Next:`` line printed, run as printed, opens the page it names, and
  the ``Next page:`` options of a paged ``fetch-index`` list the next entries.
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _loader
import test_migration_parity as parity

g = _loader.load_script("parse-llms-txt.py")
fb = _loader.load_script("parse-firebase.py")
SCRIPT = "parse-llms-txt.py"

INDEX_URL = "https://firebase.google.com/docs/llms.txt"
PROFILE = {"body": "fetch", "rank_from": "index", "index_url": INDEX_URL, "label": "Firebase",
           "page_ref_forms": ["int", "url", "slug"], "page_url_strip_suffix": ".md.txt",
           "anchor_style": "devsite", "min_heading_level": 2}
QL = "https://firebase.google.com/docs/firestore/query-limit.md.txt"
AUTH = "https://firebase.google.com/docs/auth/overview.md.txt"


class _Resp:
    def __init__(self, data: bytes, url: str):
        self._data, self.headers, self.url = data, {}, url

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Net:
    """A mocked network: *served* maps URL -> text; anything else fails.
    Records every requested URL and the most requests in flight at once;
    *delay* keeps each request open that long (seconds); *on_fetch* is
    called with the URL before answering."""

    def __init__(self, served=None, delay: float = 0.0, on_fetch=None):
        self.served = dict(served or {})
        self.delay, self.on_fetch = delay, on_fetch
        self.calls: list = []
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, req, *a, **kw):
        url = getattr(req, "full_url", req)
        with self._lock:
            self.calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.on_fetch:
                self.on_fetch(url)
            if self.delay:
                time.sleep(self.delay)
            if url not in self.served:
                raise OSError(f"mock network: no {url}")
            return _Resp(self.served[url].encode("utf-8"), url)
        finally:
            with self._lock:
                self.active -= 1

    def pages(self) -> list:
        return [u for u in self.calls if u != INDEX_URL]


class _FetchSource(unittest.TestCase):
    """A temp cache dir and a sources file with the profile ``firebase``."""

    profile = PROFILE

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"firebase": self.profile}}, f)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.p = g._validate_profile(self.sources, "firebase", self.profile)
        except SystemExit:
            self.fail(f"profile refused: {err.getvalue()}")
        self.net = _Net()

    def seed(self, index: str | None = None, pages: dict | None = None) -> None:
        """Put *index* (llms.txt text) and *pages* (URL -> body) in the cache."""
        if index is not None:
            Path(g._index_cache_path(self.tmp, self.p)).write_text(index, encoding="utf-8")
        for url, body in (pages or {}).items():
            path = Path(g._page_cache_path(self.tmp, self.p, url))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def argv(self, *argv: str) -> list:
        return [SCRIPT, *argv, "--source", "firebase", "--sources-file", self.sources,
                "--cache-dir", self.tmp]

    def run_g(self, *argv: str, raw: bool = False):
        return self._run_words([SCRIPT, *argv] if raw else self.argv(*argv))

    def _run_words(self, words: list):
        """Run the generic loader with argv *words* on the mocked network."""
        with mock.patch("urllib.request.urlopen", side_effect=self.net):
            try:
                return _loader.run_cli(g, words)
            except Exception as e:  # a crash is a failed check, not an error of the run
                self.fail(f"{words[1:3]} crashed: {e!r}")


def _valid(path: str, name: str, raw: dict) -> dict:
    """``_validate_profile`` that must accept *raw* (a refusal fails the test)."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return g._validate_profile(path, name, raw)
    except SystemExit:
        raise AssertionError(f"profile refused: {err.getvalue()}") from None


def _index(*rows) -> str:
    return "".join(f"- [{t}]({u}): {d}\n" for t, u, d in rows)


# ---------------------------------------------------------------------------
# test_parse_firebase.py on the generic loader
# ---------------------------------------------------------------------------

class FirebaseTestsOnGenericLoader(_FetchSource):
    """The 20 tests of ``test_parse_firebase.py`` (same fixtures, same
    assertions), with ``parse-llms-txt.py --source <body fetch profile>``."""

    def _two_pages(self):
        self.seed(_index(("Firestore Query Limits", QL, "Limits on queries"),
                         ("Auth Overview", AUTH, "Overview of auth")),
                  {QL: "# Firestore Query Limits\n\n## Limits\nMax 100 zzzonlyinbody results per query.\n",
                   AUTH: "# Auth Overview\n\n## Introduction\nAuthenticate users.\n"})

    # FirebaseCliTest
    def test_content_retrieves_cached_page(self):
        self._two_pages()
        code, out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Firestore Query Limits", out)

    def test_search_content_finds_page_body_term(self):
        self._two_pages()
        code, out, err = self.run_g("search-content", "zzzonlyinbody", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("zzzonlyinbody", out)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        self._two_pages()
        code, out, _ = self.run_g("search-index", "totallyabsentkeyword")
        self.assertEqual(code, 0)
        self.assertIn("No matching documents found", out)
        self.assertIn("search-content", out)
        self.assertIn("omit --page-ref to scan every page", out)

    def test_search_zero_result_tip_mentions_search_content(self):
        self._two_pages()
        code, out, _ = self.run_g("search", "totallyabsentkeyword")
        self.assertEqual(code, 0)
        self.assertIn("No matching documents found", out)
        self.assertIn("search-content", out)
        self.assertIn("omit --page-ref to scan every page", out)

    # NextHintCorpusArgsRenderTest
    def test_fetch_index_hint_keeps_cache_dir(self):
        self.seed(_index(("Auth Overview", AUTH, "Overview of auth")))
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Next: {SCRIPT} sections <page_ref> --source firebase "
                      f"--sources-file {self.sources} --cache-dir {self.tmp}\n", out)

    # SectionUrlAnchorIntegrationTest
    def _one_page(self, body: str):
        self.seed(_index(("Firestore Query Limits", QL, "Limits on queries")), {QL: body})

    def test_search_content_section_line_has_url_anchor(self):
        self._one_page("# Firestore Query Limits\n\n## Limits\nMax 100 zzzonlyinbody results per query.\n")
        code, out, err = self.run_g("search-content", "zzzonlyinbody", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Limits  (x1)  "
                      "[https://firebase.google.com/docs/firestore/query-limit#limits]", out)
        self.assertNotIn("query-limit.md.txt#limits", out)

    def test_search_section_line_has_url_anchor(self):
        self._one_page("# Firestore Query Limits\n\n## Limits\nMax 100 zzzonlyinbody results per query.\n")
        code, out, err = self.run_g("search", "query")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#limits]", out)
        self.assertNotIn("query-limit.md.txt#limits", out)

    # SectionUrlAnchorSlashInTitleIntegrationTest
    _SLASH = ("# Firestore Query Limits\n\n## Reference\n\n### Read / write data\n"
              "Reads and writes per query limit.\n")

    def test_search_content_anchor_uses_full_leaf_title(self):
        self._one_page(self._SLASH)
        code, out, err = self.run_g("search-content", "query", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#read_write_data]", out)
        self.assertNotIn("#write_data]", out)

    def test_search_anchor_uses_full_leaf_title(self):
        self._one_page(self._SLASH)
        code, out, err = self.run_g("search", "query")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#read_write_data]", out)
        self.assertNotIn("#write_data]", out)

    # SearchContentMaxSnippetCharsTest
    _LONG = "# Firestore Query Limits\n\n## Limits\nkeywordhit " + "x" * 200 + " end of line\n"

    def test_default_does_not_truncate_a_short_enough_snippet(self):
        self._one_page(self._LONG)
        code, out, err = self.run_g("search-content", "keywordhit", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("…", out)
        self.assertIn("end of line", out)

    def test_max_snippet_chars_truncates_a_long_snippet(self):
        self._one_page(self._LONG)
        code, out, err = self.run_g("search-content", "keywordhit", "--page-ref", "0",
                                    "--max-snippet-chars", "20")
        self.assertEqual(code, 0, err)
        self.assertIn("→ keywordhit " + "x" * 67 + "…", out)
        self.assertNotIn("end of line", out)

    # SearchSkipsUnreachablePagesTest
    def _dead(self):
        self.seed(_index(("Firestore Query Limits", QL, "Reference docs on query limits"),
                         ("Dead Page", "https://firebase.google.com/docs/dead/page.md.txt",
                          "Reference docs that always fail to fetch")),
                  {QL: "# Firestore Query Limits\n\n## Limits\nMax 100 zzzonlyinbody results per query.\n"})

    def test_search_content_skips_dead_page_and_still_reports_live_hits(self):
        self._dead()
        code, out, err = self.run_g("search-content", "zzzonlyinbody")
        self.assertEqual(code, 0, err)
        self.assertIn("zzzonlyinbody", out)
        self.assertIn("1 documents skipped", out)
        self.assertIn("skip: fetch failed", err)
        self.assertIn("dead/page.md.txt", err)

    def test_search_skips_dead_page_and_still_reports_live_hits(self):
        self._dead()
        code, out, err = self.run_g("search", "reference")
        self.assertEqual(code, 0, err)
        self.assertIn("Firestore Query Limits", out)
        self.assertNotIn("Dead Page", out)
        self.assertIn("1 documents skipped", out)
        self.assertIn("skip: fetch failed", err)

    # AssertParsedIntegrationTest
    def test_malformed_index_exits_2_not_1(self):
        self.seed("not a valid index line\n")
        code, _out, err = self.run_g("fetch-index")
        self.assertEqual(code, 2)
        self.assertIn("format may have changed", err)

    # TopHeadingPathRoundTripTest
    def _preamble(self):
        self.seed(_index(("Auth Overview", AUTH, "Overview of auth")),
                  {AUTH: "# Auth Overview\n\nText mentioning zzzpreambleterm before any heading.\n\n"
                         "## Introduction\nAuthenticate users.\n"})

    def test_top_hit_is_displayed_with_clarifying_suffix(self):
        self._preamble()
        code, out, err = self.run_g("search-content", "zzzpreambleterm", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: (top)  [before first heading]", out)

    def test_top_value_copied_into_content_resolves_the_preamble(self):
        self._preamble()
        code, out, err = self.run_g("content", "0", "(top)")
        self.assertEqual(code, 0, err)
        self.assertIn("zzzpreambleterm", out)
        self.assertIn("heading_path: (top)", out)
        self.assertNotIn("Authenticate users", out)

    # GoldenOutputTest
    def test_content_full_output_is_pinned(self):
        self._one_page("# Firestore Query Limits\n\nIntro text before any heading.\n\n"
                       "## Limits\nMax 100 results per query.\n")
        code, out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        hint_block = (
            "\n--- Top-level sections (1) ---\n  - Limits\n\n"
            f'Next: {SCRIPT} content 0 "<heading_path from above>" --source firebase '
            f"--sources-file {self.sources} --cache-dir {self.tmp}\n"
        )
        expected = (
            "# doc_title: Firestore Query Limits\n"
            f"# source: {QL}\n"
            "---\n"
            + hint_block
            + "# Firestore Query Limits\n\nIntro text before any heading.\n\n"
            "## Limits\nMax 100 results per query.\n"
            + hint_block
        )
        self.assertEqual(out, expected)

    # SectionsHeadingPathTest
    def test_nested_sections_show_full_path_not_ambiguous_bare_title(self):
        url = "https://firebase.google.com/docs/guide.md.txt"
        self.seed(_index(("Guide", url, "Guide")),
                  {url: "# Guide\n\n## Client\ntext\n### Examples\nclient examples\n\n"
                        "## Server\ntext\n### Examples\nserver examples\n"})
        code, out, err = self.run_g("sections", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("[L3] Client/Examples", out)
        self.assertIn("[L3] Server/Examples", out)
        self.assertNotIn("[L3] Examples", out)

    def _doc(self, body: str):
        url = "https://firebase.google.com/docs/doc.md.txt"
        self.seed(_index(("Doc", url, "desc")), {url: body})

    def test_no_subsection_hints_suppresses_both_occurrences(self):
        self._doc("# Doc\n\n## Options\ntext\n")
        code, out, err = self.run_g("content", "0", "--no-subsection-hints")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Top-level sections", out)
        self.assertNotIn("Next:", out)

    def test_content_longer_than_max_chars_points_at_search_in_page(self):
        self._doc("# Doc\n\n0123456789 0123456789 0123456789 0123456789 0123456789\n")
        code, out, err = self.run_g("content", "0", "--max-chars", "20")
        self.assertEqual(code, 0, err)
        self.assertIn("chars truncated", out)
        self.assertIn(f"Next: {SCRIPT} search-content --page-ref 0 --context 0 ", out)
        self.assertIn(" -- doc\n", out)
        self.assertNotIn("0123456789 0123456789 0123456789 0123456789 0123456789", out)


# ---------------------------------------------------------------------------
# Profile keys and the page cache names
# ---------------------------------------------------------------------------

class FetchProfileValidationTest(unittest.TestCase):

    def _bad(self, raw: dict, needle: str, path: str = "sources.json") -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            g._validate_profile(path, "fb", raw)
        self.assertIn(needle, err.getvalue())

    def test_fetch_profile_needs_rank_from_index_and_index_url_but_no_url(self):
        p = _valid("sources.json", "fb", PROFILE)
        self.assertEqual((p["body"], p["url"], p["index_url"]), ("fetch", None, INDEX_URL))
        self.assertEqual((p["page_fetch_workers"], p["page_fetch_timeout"],
                          p["search_content_warn_pages"], p["page_cache_dir"]), (8, 30, 30, None))
        self._bad({k: v for k, v in PROFILE.items() if k != "rank_from"}, 'needs rank_from "index"')
        self._bad({**PROFILE, "rank_from": "pages"}, 'needs rank_from "index"')
        self._bad({k: v for k, v in PROFILE.items() if k != "index_url"}, "needs index_url")
        self._bad({**PROFILE, "index_url": "ftp://x/llms.txt"}, "needs index_url")

    def test_keys_that_read_an_llms_full_txt_are_refused(self):
        for key, value in (("url", "https://x/llms-full.txt"), ("split", "h1"),
                           ("page_url", "line:Source: "), ("index_join", "url"),
                           ("link_annotations", True), ("drop_lines", ["^x"]),
                           ("join_rate_guard", True), ("split_candidates", [])):
            with self.subTest(key):
                self._bad({**PROFILE, key: value}, f"so these keys do not apply: {key}")

    def test_body_values_and_fetch_only_keys(self):
        self._bad({**PROFILE, "body": "pages"}, "body must be one of: full, fetch")
        full = {"url": "https://x.dev/llms-full.txt", "split": "h1"}
        self.assertEqual(_valid("s", "x", {**full, "body": "full"})["body"], "full")
        for key, value in (("page_fetch_workers", 2), ("page_fetch_timeout", 5),
                           ("search_content_warn_pages", 3), ("page_cache_dir", "x-pages")):
            with self.subTest(key):
                self._bad({**full, key: value}, f'{key} is only valid with body "fetch"')

    def test_integer_keys(self):
        for key, bad, needle in (("page_fetch_workers", 0, "from 1 to 32"),
                                 ("page_fetch_workers", 33, "from 1 to 32"),
                                 ("page_fetch_workers", True, "from 1 to 32"),
                                 ("page_fetch_timeout", "30", "from 1 to 600"),
                                 ("search_content_warn_pages", -1, "of 0 or more"),
                                 ("index_page_size", -1, "of 0 or more"),
                                 ("index_page_size", 1.5, "of 0 or more")):
            with self.subTest(key=key, value=bad):
                self._bad({**PROFILE, key: bad}, f"{key} must be an integer {needle}")
        p = _valid("s", "fb", {**PROFILE, "page_fetch_workers": 32, "index_page_size": 0,
                                            "search_content_warn_pages": 0})
        self.assertEqual((p["page_fetch_workers"], p.get("index_page_size"), p["search_content_warn_pages"]),
                         (32, 0, 0))
        # index_page_size applies to an llms-full.txt profile too
        full = _valid("s", "x", {"url": "https://x.dev/llms-full.txt", "split": "h1",
                                               "index_page_size": 2})
        self.assertEqual(full.get("index_page_size"), 2)
        self._bad({**PROFILE, "description": 3}, "description must be a string")

    def test_page_cache_dir_is_for_the_bundled_presets_only_and_a_slug(self):
        self._bad({**PROFILE, "page_cache_dir": "firebase-docs"}, "only valid in the bundled presets")
        p = _valid(g.PRESETS_FILE, "fb", {**PROFILE, "page_cache_dir": "firebase-docs"})
        self.assertEqual(g._page_dir_name(p), "firebase-docs")
        for bad in ("../up", "a/b", ".hidden", "", "UPPER"):
            with self.subTest(bad):
                self._bad({**PROFILE, "page_cache_dir": bad}, "page_cache_dir must match", g.PRESETS_FILE)

    def test_two_fetch_profiles_cannot_share_a_page_cache(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "presets.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"sources": {"a": {**PROFILE, "cache_name": "x"},
                                   "b": {**PROFILE, "page_cache_dir": "x-pages"}}}, f)
        err = io.StringIO()
        with mock.patch.object(g, "PRESETS_FILE", path), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            g._read_sources_file(path)
        self.assertIn("caches its pages in 'x-pages', as sources.a does", err.getvalue())

    def test_page_dir_names(self):
        p = _valid("sources.json", "fb", PROFILE)
        self.assertRegex(g._page_dir_name(p), r"^generic-fb-[0-9a-f]{12}-pages$")
        other = _valid("sources.json", "fb", {**PROFILE, "index_url": "https://x.dev/llms.txt"})
        self.assertNotEqual(g._page_dir_name(other), g._page_dir_name(p))
        named = _valid(g.PRESETS_FILE, "fb", {**PROFILE, "cache_name": "fbx"})
        self.assertEqual(g._page_dir_name(named), "fbx-pages")

    def test_sources_lists_a_fetch_profile_by_its_index_url(self):
        src = _FetchSource("run")
        src.setUp()
        try:
            code, out, err = src.run_g("sources", "--sources-file", src.sources, raw=True)
        finally:
            src.doCleanups()
        self.assertEqual(code, 0, err)
        self.assertIn(f"firebase  [user, fetch]  {INDEX_URL}", out)


PLUGIN_DIR = Path(_loader.SCRIPTS_DIR).parent


def _readme_fetch_shape() -> tuple:
    """``(required, optional)`` keys of the ``body: fetch`` row of the README's
    shape table, as ``{key: value}`` (a key written without a value gets a
    placeholder URL): the part before `` (+ `` and the part inside it."""
    rows = [line for line in (PLUGIN_DIR / "README.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and line.split(" | ")[1:2]
            and line.split(" | ")[1].startswith("`body: fetch`")]
    assert len(rows) == 1, rows
    cell = rows[0].split(" | ")[1]
    required, _sep, optional = cell.partition(" (+ ")

    def keys(text: str) -> dict:
        out = {}
        for span in re.findall(r"`([^`]+)`", text):
            key, _colon, value = span.partition(": ")
            if not value:
                out[key] = "https://docs.example.com/llms.txt"
                continue
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = value
        return out
    return keys(required), keys(optional)


class ReadmeFetchShapeTest(unittest.TestCase):
    """The README's ``body: fetch`` shape row, as written, is a profile the
    validator accepts, and every key it calls required is."""

    def test_the_required_keys_are_a_valid_profile_and_each_is_needed(self):
        required, optional = _readme_fetch_shape()
        self.assertEqual(required.get("body"), "fetch")
        _valid("sources.json", "docs", required)
        _valid("sources.json", "docs", {**required, **optional})
        for key in required:
            with self.subTest(dropped=key):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    g._validate_profile("sources.json", "docs",
                                        {k: v for k, v in required.items() if k != key})


def _help_options(*argv: str) -> set:
    run = subprocess.run([sys.executable, os.path.join(_loader.SCRIPTS_DIR, SCRIPT), *argv, "--help"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", run.stdout))


class SkillCommandTableTest(unittest.TestCase):
    """The command table of the generic loader's SKILL.md names options the
    commands have; ``fetch-index``'s row names all of its own (the paging
    options included)."""

    def test_documented_options_exist_and_fetch_index_lists_its_own(self):
        text = (PLUGIN_DIR / "skills" / "researching-library-docs" / "SKILL.md").read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([a-z-]+)` \| ([^|]*) \|", text, re.M))
        common = _help_options("sections")
        for cmd in ("search", "search-index", "search-content", "sections", "content", "fetch-index"):
            with self.subTest(cmd=cmd):
                documented = set(re.findall(r"--[a-z][a-z-]*", rows[cmd]))
                self.assertLessEqual(documented, _help_options(cmd))
        self.assertEqual(set(re.findall(r"--[a-z][a-z-]*", rows["fetch-index"])),
                         _help_options("fetch-index") - common)


class PageCacheNameTest(unittest.TestCase):
    """The page cache file of a URL is the one ``parse-firebase.py`` uses, so
    an existing page cache is read as it is."""

    URLS = (
        QL, AUTH, "https://firebase.google.com/docs.md.txt",
        "https://firebase.google.com/docs/auth/user.md.txt",
        "https://firebase.google.com/docs/auth_user.md.txt",
        "https://firebase.google.com/docs/x.md.txt?hl=ja",
        "https://firebase.google.com/docs/x.md.txt?hl=en",
        "https://firebase.google.com/docs/" + "long-segment/" * 30 + "page.md.txt",
        "https://example.com/elsewhere.md.txt",
        "https://firebase.google.com/",
    )

    def test_names_match_the_old_script_and_never_contain_a_separator(self):
        p = _valid(g.PRESETS_FILE, "firebase", {**PROFILE, "page_cache_dir": "firebase-docs"})
        names = set()
        for url in self.URLS:
            with self.subTest(url):
                path = g._page_cache_path("/cache", p, url)
                self.assertEqual(path, os.path.join("/cache", "firebase-docs", fb._url_to_cache_filename(url)))
                name = os.path.basename(path)
                self.assertNotIn("/", name)
                self.assertLess(len(name), 256)
                names.add(name)
        self.assertEqual(len(names), len(self.URLS))  # every URL has its own file

    def test_names_are_the_ones_parse_firebase_wrote(self):
        # literal names written by parse-firebase.py before its rule moved to
        # _common (the two now share the function, so comparing them alone
        # would not catch a change to it)
        p = _valid(g.PRESETS_FILE, "firebase", {**PROFILE, "page_cache_dir": "firebase-docs"})
        long_url = "https://firebase.google.com/docs/" + "long-segment/" * 30 + "page.md.txt"
        for url, name in (
            (QL, "docs_firestore_query-limit-c526d78912364bbb.md.txt"),
            ("https://firebase.google.com/docs.md.txt", "docs-2af98de9003b2906.md.txt"),
            (long_url, "docs_" + "long-segment_" * 13 + "long-s-a5f693fb8f88e2de.md.txt"),
            ("https://example.com/elsewhere.md.txt", "7c3f68591ed42358.md.txt"),
            ("https://firebase.google.com/docs/x.md.txt?hl=ja", "docs_x.md.txt?hl=ja-0fdd56d2c1872386.md.txt"),
            ("https://firebase.google.com/", "139fef31661a0096.md.txt"),
        ):
            with self.subTest(url):
                self.assertEqual(os.path.basename(g._page_cache_path("/c", p, url)), name)

    def test_the_fixture_page_cache_files_are_found_by_their_urls(self):
        import _common
        lines = _common.load_lines(str(parity.FIXTURES / "firebase" / "firebase-llms.txt"))
        p = _valid(g.PRESETS_FILE, "firebase", {**PROFILE, "page_cache_dir": "firebase-docs"})
        cached = {f for f in os.listdir(parity.FIXTURES / "firebase" / "firebase-docs")
                  if not f.endswith(".meta.json")}
        found = {os.path.basename(g._page_cache_path("/c", p, e["url"]))
                 for e in _common.parse_llms_index(lines)} & cached
        self.assertEqual(found, cached)


# ---------------------------------------------------------------------------
# What is fetched
# ---------------------------------------------------------------------------

def _page(title: str, body: str = "") -> str:
    return f"# {title}\n\n## Overview\n{body or 'Plain text.'}\n"


ROWS = [(f"Topic {n} widget", f"https://firebase.google.com/docs/t{n}.md.txt", f"About widget {n}")
        for n in range(6)]


class FetchCostTest(_FetchSource):

    def _rows(self, *, cached_pages: bool = False, n: int = 6):
        rows = ROWS[:n]
        self.seed(_index(*rows), {u: _page(t) for t, u, _d in rows} if cached_pages else None)
        self.net.served.update({u: _page(t, "gizmo body text") for t, u, _d in rows})

    def test_fetch_index_and_search_index_fetch_no_page(self):
        self._rows()
        for argv in (("fetch-index",), ("fetch-index", "--compact"), ("search-index", "widget"),
                     ("search-index", "widget", "--show-sections")):
            with self.subTest(argv):
                code, _out, err = self.run_g(*argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.net.calls, [])
        self.assertIn("--show-sections is ignored", err)

    def test_sections_and_content_fetch_only_their_page(self):
        self._rows()
        for argv in (("sections", "3"), ("content", "https://firebase.google.com/docs/t4"), ("content", "t5")):
            with self.subTest(argv):
                self.net.calls.clear()
                code, out, err = self.run_g(*argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.net.calls), 1)
                self.assertIn(f"Topic {argv[1][-1]} widget", out)
        # cached now: no fetch
        self.net.calls.clear()
        self.assertEqual(self.run_g("sections", "3")[0], 0)
        self.assertEqual(self.net.calls, [])

    def test_search_with_no_candidate_fetches_no_page_and_has_no_fallback(self):
        self._rows()
        with mock.patch.object(g, "full_corpus_extra_hits", side_effect=AssertionError("fallback ran")):
            code, out, err = self.run_g("search", "gizmo")  # only in the bodies
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.calls, [])
        self.assertIn("No matching documents found", out)
        self.assertIn("6 index entries (title/description) checked", out)

    def test_search_fetches_only_its_candidates_even_without_body_hits(self):
        self._rows()
        with mock.patch.object(g, "full_corpus_extra_hits", side_effect=AssertionError("fallback ran")):
            code, out, err = self.run_g("search", "widget", "--top-n", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.net.pages()), 2)
        self.assertIn("(2 documents, ranked via index → body fetch)", out)

    def test_search_fetches_at_most_page_fetch_workers_at_once(self):
        self.profile = {**PROFILE, "page_fetch_workers": 2}
        self.setUp()
        self._rows()
        self.net.delay = 0.2
        code, _out, err = self.run_g("search", "widget", "--top-n", "6")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.net.pages()), 6)
        self.assertEqual(self.net.max_active, 2)

    def test_search_passes_page_fetch_timeout(self):
        self.profile = {**PROFILE, "page_fetch_timeout": 7}
        self.setUp()
        self._rows()
        seen = []
        real = g.fetch_url

        def spy(url, path, **kw):
            if url != INDEX_URL:
                seen.append(kw["timeout"])
            return real(url, path, **kw)
        with mock.patch.object(g, "fetch_url", side_effect=spy):
            self.assertEqual(self.run_g("search", "widget", "--top-n", "2")[0], 0)
            self.assertEqual(self.run_g("search-content", "widget", "--page-ref", "5")[0], 0)
        self.assertEqual(seen, [7, 7, 7])

    def test_search_content_fetches_one_page_at_a_time_and_prints_each_before_the_next(self):
        self._rows(n=4)
        buf = io.StringIO()
        seen_before: dict = {}
        self.net.on_fetch = lambda url: seen_before.setdefault(url, buf.getvalue())
        old_argv = g.sys.argv
        try:
            g.sys.argv = self.argv("search-content", "gizmo")
            with mock.patch("urllib.request.urlopen", side_effect=self.net), \
                    mock.patch.object(g.concurrent.futures, "ThreadPoolExecutor",
                                      side_effect=AssertionError("no pool in search-content")), \
                    contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                g.main()
        finally:
            g.sys.argv = old_argv
        self.assertEqual(self.net.max_active, 1)
        urls = [u for _t, u, _d in ROWS[:4]]
        self.assertEqual(self.net.pages(), urls)
        # page k's hits were printed before page k+1 was requested
        for k in range(1, 4):
            self.assertIn(f"[{k - 1}] Topic {k - 1} widget", seen_before[urls[k]])
            self.assertNotIn(f"[{k}] Topic {k} widget", seen_before[urls[k]])

    def test_search_content_warns_above_search_content_warn_pages(self):
        for warn_pages, warned in ((5, True), (6, False)):
            with self.subTest(warn_pages):
                self.profile = {**PROFILE, "search_content_warn_pages": warn_pages}
                self.setUp()
                self._rows(cached_pages=True)
                code, _out, err = self.run_g("search-content", "widget")
                self.assertEqual(code, 0, err)
                self.assertEqual("About to fetch and search all 6 pages" in err, warned)
                # with --page-ref, never
                self.assertNotIn("About to fetch", self.run_g("search-content", "widget", "--page-ref", "0")[2])

    def test_skipped_pages_are_reported_in_page_order(self):
        # the first candidate answers last; the report still follows the index
        self._rows(n=3)
        self.net.served.clear()
        self.net.on_fetch = lambda url: time.sleep(0.3) if url == ROWS[0][1] else None
        code, _out, err = self.run_g("search", "widget", "--top-n", "3")
        self.assertEqual(code, 0, err)
        skips = [line for line in err.splitlines() if line.startswith("(skip:")]
        self.assertEqual([s.split()[3].rstrip(":") for s in skips], [u for _t, u, _d in ROWS[:3]])

    def test_search_ranks_the_fetched_pages_by_their_body_hits(self):
        rows = [("Widget alpha", "https://firebase.google.com/docs/a.md.txt", "widget"),
                ("Widget beta", "https://firebase.google.com/docs/b.md.txt", "widget gizmo")]
        self.seed(_index(*rows), {rows[0][1]: _page("Widget alpha", "widget only"),
                                  rows[1][1]: _page("Widget beta", "widget gizmo here\n\nwidget gizmo again")})
        code, out, err = self.run_g("search", "widget gizmo")
        self.assertEqual(code, 0, err)
        # beta scores higher on the index too; alpha has no body hit for both words
        self.assertLess(out.index("[1] Widget beta"), out.index("[0] Widget alpha"))
        code, out, err = self.run_g("search", "widget alpha")
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("[0] Widget alpha"), out.index("[1] Widget beta"))

    def test_search_puts_a_changelog_page_last_as_the_other_sources_do(self):
        rows = [("Widget changelog", "https://firebase.google.com/docs/c.md.txt", "widget"),
                ("Widget guide", "https://firebase.google.com/docs/g.md.txt", "widget")]
        self.seed(_index(*rows), {u: _page(t, "widget text") for t, u, _d in rows})
        code, out, err = self.run_g("search", "widget")
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("[1] Widget guide"), out.index("[0] Widget changelog"))
        code, out, err = self.run_g("search", "widget", "--include-changelog-priority")
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("[0] Widget changelog"), out.index("[1] Widget guide"))

    def test_search_content_limit_cuts_the_pages_shown_not_the_pages_counted(self):
        self._rows(n=3, cached_pages=True)
        code, out, err = self.run_g("search-content", "widget", "--limit", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Topic 0 widget", out)
        self.assertNotIn("[1] Topic 1 widget", out)
        self.assertIn("across 3/3 documents, showing top 1)", out)

    def test_search_content_zero_hits_on_one_page_says_only_that_page_was_counted(self):
        self._rows(n=3, cached_pages=True)
        code, out, err = self.run_g("search-content", "zzznothing", "--page-ref", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("The counts cover only the --page-ref 1 page", out)
        code, out, err = self.run_g("search-content", "zzznothing")
        self.assertEqual(code, 0, err)
        self.assertIn("3 documents checked", out)
        self.assertNotIn("The counts cover only", out)

    def test_sections_header_names_the_page_and_its_cache_file(self):
        self._rows(n=2, cached_pages=True)
        code, out, err = self.run_g("sections", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1:3],
                         [f"  URL: {ROWS[1][1]}", f"  Cache: {g._page_cache_path(self.tmp, self.p, ROWS[1][1])}"])
        code, out, err = self.run_g("fetch-index")
        self.assertNotIn("llms-full.txt", out)

    def test_protect_tables_false_reaches_extract_content(self):
        import _commands
        for profile, want in (({**PROFILE, "protect_tables": False}, False), (PROFILE, None)):
            with self.subTest(want):
                self.profile = profile
                self.setUp()
                self._rows(n=1, cached_pages=True)
                seen = {}
                real = _commands.extract_content

                def spy(*a, **kw):
                    seen.update(kw)
                    return real(*a, **kw)
                with mock.patch.object(_commands, "extract_content", side_effect=spy):
                    code, _out, err = self.run_g("content", "0", "Overview")
                self.assertEqual(code, 0, err)
                self.assertEqual(seen.get("protect_tables"), want)

    def test_include_changelog_priority_is_reported_as_ignored(self):
        self._rows(cached_pages=True)
        code, _out, err = self.run_g("search-content", "widget", "--include-changelog-priority")
        self.assertEqual(code, 0, err)
        self.assertIn("--include-changelog-priority has no effect here", err)

    def test_file_is_refused_and_index_file_reads_a_local_index(self):
        self._rows(cached_pages=True)
        code, _out, err = self.run_g("fetch-index", "--file", "/nonexistent")
        self.assertEqual(code, 2)
        self.assertIn("--index-file", err)
        local = os.path.join(self.tmp, "local-llms.txt")
        Path(local).write_text(_index(*ROWS[:2]), encoding="utf-8")
        code, out, err = self.run_g("sections", "1", "--index-file", local)
        self.assertEqual(code, 0, err)
        self.assertIn('"Topic 1 widget"', out)
        self.assertEqual(self.net.calls, [])


class PartialFailureTest(_FetchSource):

    def setUp(self):
        super().setUp()
        self.urls = [u for _t, u, _d in ROWS[:3]]
        self.seed(_index(*ROWS[:3]))
        self.net.served = {u: _page(f"Body of {n}", "gizmo") for n, u in enumerate(self.urls)}
        del self.net.served[self.urls[1]]

    def test_a_failed_page_is_skipped_counted_and_never_answered_by_another(self):
        code, out, err = self.run_g("search-content", "gizmo")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Topic 0 widget", out)
        self.assertIn("[2] Topic 2 widget", out)
        self.assertNotIn("[1] Topic 1 widget", out)
        self.assertIn("(1 documents skipped — fetch failed, see stderr)", out)
        self.assertIn(f"(skip: fetch failed {self.urls[1]}", err)
        # no file (or leftover temp file) for the failed page
        page_dir = os.path.dirname(g._page_cache_path(self.tmp, self.p, self.urls[0]))
        self.assertEqual(sorted(f for f in os.listdir(page_dir) if not f.endswith(".meta.json")),
                         sorted(os.path.basename(g._page_cache_path(self.tmp, self.p, u))
                                for u in (self.urls[0], self.urls[2])))
        # content of the failed page: exit 1, nothing else shown in its place
        code, out, err = self.run_g("content", "1")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(f"Failed to fetch {self.urls[1]}", err)

    def test_the_next_run_fetches_only_the_page_still_missing(self):
        self.run_g("search-content", "gizmo")
        self.net.served[self.urls[1]] = _page("Body of 1", "gizmo")
        self.net.calls.clear()
        code, out, err = self.run_g("search-content", "gizmo")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.pages(), [self.urls[1]])
        self.assertIn("[1] Topic 1 widget", out)
        self.assertNotIn("skipped", out)

    def test_zero_hits_count_only_the_pages_fetched(self):
        code, out, err = self.run_g("search-content", "zzznothing")
        self.assertEqual(code, 0, err)
        self.assertIn("2 documents checked", out)
        self.assertIn("(1 documents skipped — fetch failed, see stderr)", out)

    def test_search_with_every_candidate_failing_says_so(self):
        self.net.served.clear()
        code, out, err = self.run_g("search", "widget")
        self.assertEqual(code, 0, err)
        self.assertIn("(0 documents, ranked via index → body fetch)", out)
        self.assertIn("(3 documents skipped — fetch failed, see stderr)", out)
        self.assertNotIn("showing full-body search results", out)
        # no page to name: only the placeholder line (as parse-firebase.py prints)
        self.assertEqual([line for line in out.splitlines() if line.startswith("Next:")],
                         [f'Next: {SCRIPT} content <page_ref> "<heading_path>" --source firebase '
                          f"--sources-file {self.sources} --cache-dir {self.tmp}"])

    def test_a_cached_page_fetched_from_another_url_is_fetched_again(self):
        # a page file whose sidecar names another URL (as if two URLs shared
        # a name) does not answer for this URL
        path = g._page_cache_path(self.tmp, self.p, self.urls[0])
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(_page("Wrong page", "gizmo"), encoding="utf-8")
        Path(path + ".meta.json").write_text(json.dumps({"url": "https://firebase.google.com/docs/other.md.txt"}),
                                             encoding="utf-8")
        code, out, err = self.run_g("content", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.pages(), [self.urls[0]])
        self.assertIn("Body of 0", out)
        self.assertNotIn("Wrong page", out)


# ---------------------------------------------------------------------------
# Round trip: every printed ref and Next: line opens the page it names
# ---------------------------------------------------------------------------

# Headings with awkward text (an option-like word, ``\_``, symbols) so the
# Next: lines quote them.
TRICKY = [
    ("Deploy targets", "https://firebase.google.com/docs/cli/targets.md.txt", "Deploy to targets",
     "# Deploy targets\n\n## Set up targets\nDeploy alpha targets here.\n\n"
     "## Use `--only` with \\_targets\nDeploy alpha with --only.\n"),
    ("Configure projects", "https://firebase.google.com/docs/projects/multiprojects.md.txt",
     "Several projects", "# Configure projects\n\n## Aliases (& more)\nDeploy alpha aliases.\n"),
    ("CLI reference", "https://firebase.google.com/docs/cli.md.txt", "The CLI",
     "# CLI reference\n\n## Commands\nAll commands.\n"),
]
STATES = ("all-cached", "pages-only", "none", "index-file")


class RoundTripTest(_FetchSource):

    def _state(self, state: str) -> list:
        """Set up *state*; return the extra argv it needs."""
        index = _index(*((t, u, d) for t, u, d, _b in TRICKY))
        pages = {u: b for _t, u, _d, b in TRICKY}
        self.net.served = {INDEX_URL: index, **pages}
        extra = []
        if state in ("all-cached", "index-file"):
            self.seed(None if state == "index-file" else index, pages)
        elif state == "pages-only":
            self.seed(None, pages)
        if state == "index-file":
            local = os.path.join(self.tmp, "llms copy.txt")
            Path(local).write_text(index, encoding="utf-8")
            extra = ["--index-file", local]
        return extra

    def _run_line(self, line: str, ref: str | None = None):
        """Run a printed ``Next:`` line as the shell would split it."""
        words = shlex.split(line, comments=True)
        self.assertEqual(words[:2], ["Next:", SCRIPT])
        if ref is not None:
            words = [ref if w == "<page_ref>" else w for w in words]
        return self._run_words(words[1:])

    def test_every_printed_ref_and_next_line_opens_its_page(self):
        titles = [t for t, _u, _d, _b in TRICKY]
        for state in STATES:
            with self.subTest(state=state):
                self.setUp()
                extra = self._state(state)
                # fetch-index: each [i] with the printed Next: sections line
                code, out, err = self.run_g("fetch-index", *extra)
                self.assertEqual(code, 0, err)
                listed = [line.split("]", 1)[0][1:] for line in out.splitlines() if line.startswith("[")]
                self.assertEqual(listed, ["0", "1", "2"])
                hint = next(line for line in out.splitlines() if line.startswith("Next:"))
                for ref in listed:
                    self.net.calls.clear()
                    code, sec, err = self._run_line(hint, ref)
                    self.assertEqual(code, 0, err)
                    self.assertIn(f'"{titles[int(ref)]}"', sec.splitlines()[0])
                    self.assertLessEqual(len(self.net.pages()), 1)
                    # each sections Next: line, with a heading put in
                    for line in sec.splitlines():
                        if line.startswith("Next:"):
                            c, o, e = self._run_line(line.replace('"<heading_path>"', "'(top)'"))
                            self.assertEqual(c, 0, e)
                            self.assertIn(f"# doc_title: {titles[int(ref)]}", o)
                # search and search-content: every Next: line opens the page of its hit
                for argv in (("search", "deploy alpha"), ("search-content", "deploy alpha"),
                             ("search-content", "deploy", "--page-ref", "targets")):
                    code, out, err = self.run_g(*argv, *extra)
                    self.assertEqual(code, 0, err)
                    nexts = [line for line in out.splitlines() if line.startswith("Next: ")]
                    self.assertTrue(nexts, out)
                    for line in nexts:
                        ref, heading = shlex.split(line)[3:5]
                        c, o, e = self._run_line(line)
                        self.assertEqual(c, 0, e)
                        self.assertIn(f"# doc_title: {titles[int(ref)]}", o)
                        self.assertIn(f"# heading_path: {heading}", o)
                # search-index refs open the listed entry
                code, out, err = self.run_g("search-index", "deploy", *extra)
                self.assertEqual(code, 0, err)
                for line in out.splitlines():
                    if line.startswith("[") and "(score:" in line:
                        ref, title = line[1:].split("] ", 1)
                        c, o, e = self.run_g("sections", ref, *extra)
                        self.assertEqual(c, 0, e)
                        self.assertIn(f'"{title.rsplit(" (score:", 1)[0]}"', o.splitlines()[0])

    def test_awkward_queries_do_not_crash_or_fetch_beyond_the_candidates(self):
        self._state("all-cached")
        for query in ("-x", "--only", "\\_targets", "the of and", "&& ||", "(& more)"):
            for cmd in ("search", "search-index", "search-content"):
                with self.subTest(query=query, cmd=cmd):
                    self.net.calls.clear()
                    code, out, err = self._run_words([*self.argv(cmd), "--", query])
                    self.assertIn(code, (0, 1), err)
                    self.assertEqual(self.net.calls, [])
                    for line in out.splitlines():
                        # the zero-hit "search-index <query>" line of the
                        # shared renderer puts a query that starts with "-"
                        # where argparse reads an option, for every source
                        # (not this mode's); that one is not run
                        if line.startswith("Next: ") and not shlex.split(line)[3].startswith("-"):
                            c, _o, e = self._run_line(line.replace("<page_ref>", "0"))
                            self.assertEqual(c, 0, f"{line}\n{e}")

    def test_paged_fetch_index_lists_the_next_entries_with_its_options(self):
        rows = [(f"Entry {n}", f"https://firebase.google.com/docs/e{n}.md.txt", f"d{n}") for n in range(7)]
        for page_size, argv in ((3, ()), (None, ("--limit", "3")), (5, ("--limit", "3"))):
            with self.subTest(page_size=page_size, argv=argv):
                self.profile = {**PROFILE, **({"index_page_size": page_size} if page_size else {})}
                self.setUp()
                self.seed(_index(*rows))
                seen = []
                words = self.argv("fetch-index", *argv)[1:]
                for _ in range(5):
                    code, out, err = self._run_words([SCRIPT, *words])
                    self.assertEqual(code, 0, err)
                    seen += [line.split("]", 1)[0][1:] for line in out.splitlines() if line.startswith("[")]
                    more = [line for line in out.splitlines() if line.startswith("  Next page: ")]
                    if not more:
                        break
                    words += shlex.split(more[0].split("Next page:", 1)[1])
                self.assertEqual(seen, [str(n) for n in range(7)])
                self.assertIn("(1 of 7 documents shown, offset=6)", out)
        # --limit 0 lists everything, as without a page size
        code, out, _ = self.run_g("fetch-index", "--limit", "0")
        self.assertIn("(7 documents total, 7 entries shown — 0 documents grouped)", out)
        self.assertNotIn("Next page", out)
        # a negative --offset is 0
        code, out, _ = self.run_g("fetch-index", "--offset", "-3", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertIn("[0] Entry 0", out)
        self.assertIn("(2 of 7 documents shown, offset=0)", out)

    def test_paged_grouped_index_counts_its_lines(self):
        self.profile = {**PROFILE, "index_group_variants": True}
        self.setUp()
        rows = [("Batches (Python)", "https://firebase.google.com/docs/b-py.md.txt", "d"),
                ("Batches (Go)", "https://firebase.google.com/docs/b-go.md.txt", "d"),
                ("Other", "https://firebase.google.com/docs/o.md.txt", "d")]
        self.seed(_index(*rows))
        code, out, err = self.run_g("fetch-index", "--limit", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("Variants: Python [0], Go [1]", out)
        self.assertIn("(1 of 2 entries shown, offset=0)", out)
        self.assertIn("  Next page: --offset 1 --limit 1", out)

    def test_an_llms_full_txt_profile_pages_its_index_too(self):
        corpus = os.path.join(self.tmp, "full.txt")
        Path(corpus).write_text("".join(f"# Page {n}\n\ntext {n}\n\n" for n in range(5)), encoding="utf-8")
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"firebase": {"url": "https://x.dev/llms-full.txt", "split": "h1",
                                                "index_page_size": 2}}}, f)
        words = self.argv("fetch-index", "--file", corpus)
        seen = []
        for compact in ((), ("--compact",)):
            with self.subTest(compact=compact):
                w = [*words, *compact]
                seen = []
                for _ in range(5):
                    code, out, err = self._run_words(w)
                    self.assertEqual(code, 0, err)
                    seen += [line.split("]", 1)[0][1:] for line in out.splitlines() if line.startswith("[")]
                    more = [line for line in out.splitlines() if line.startswith("  Next page: ")]
                    if not more:
                        break
                    w += shlex.split(more[0].split("Next page:", 1)[1])
                self.assertEqual(seen, ["0", "1", "2", "3", "4"])
                self.assertIn("(1 of 5 documents shown, offset=4)", out)
        # without the key the listing is as before
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"firebase": {"url": "https://x.dev/llms-full.txt", "split": "h1"}}}, f)
        code, out, err = self._run_words(words)
        self.assertEqual(code, 0, err)
        self.assertIn("(5 documents total)", out)
        self.assertNotIn("shown", out)


class FetchProfileKeyEffectTest(_FetchSource):
    """Each key of a ``body: "fetch"`` profile that the other classes do not
    already exercise changes the output it is documented to change."""

    def use(self, **keys) -> None:
        self.profile = {**PROFILE, **keys}
        self.setUp()

    def test_min_heading_level_1_makes_h1_sections(self):
        url = "https://firebase.google.com/docs/h1.md.txt"
        body = "# Page title\n\nintro text\n\n# Second part\nzzzword here\n"
        for level, h1 in ((1, True), (2, False)):
            with self.subTest(min_heading_level=level):
                self.use(min_heading_level=level)
                self.seed(_index(("Page title", url, "d")), {url: body})
                code, out, err = self.run_g("sections", "0")
                self.assertEqual(code, 0, err)
                self.assertEqual("[L1] Second part" in out, h1)
                code, out, err = self.run_g("search-content", "zzzword", "--page-ref", "0")
                self.assertEqual(code, 0, err)
                self.assertEqual("Section: Second part  (x1)" in out, h1)
                self.assertEqual("Section: (top)" in out, not h1)
                code, out, err = self.run_g("search", "page title zzzword")
                self.assertEqual(code, 0, err)
                self.assertEqual("Section: Page title  (x1)" in out, h1)
                if h1:
                    # the printed Next: line opens the H1 section
                    line = next(x for x in self.run_g("search-content", "zzzword", "--page-ref", "0")[1]
                                .splitlines() if x.startswith("Next: "))
                    c, o, e = self._run_words(shlex.split(line, comments=True)[1:])
                    self.assertEqual(c, 0, e)
                    self.assertIn("# heading_path: Second part", o)
                    self.assertIn("zzzword here", o)

    def test_show_overflow_lists_the_sections_past_max_hits(self):
        url = "https://firebase.google.com/docs/two.md.txt"
        body = "# Two\n\n## First\nwidget one\n\n## Second\nwidget two\n"
        for keys, shown in (({"show_overflow": True}, True), ({}, False)):
            with self.subTest(keys=keys):
                self.use(**keys)
                self.seed(_index(("Two widget", url, "d")), {url: body})
                for argv in (("search-content", "widget", "--max-hits", "1"),
                             ("search", "widget", "--max-hits", "1")):
                    code, out, err = self.run_g(*argv)
                    self.assertEqual(code, 0, err)
                    self.assertEqual("Other sections with hits (not shown):" in out, shown, argv)
                    self.assertEqual("      - Second  (x1)" in out, shown, argv)

    def test_compact_pack_packs_the_compact_index(self):
        for keys, packed in (({"compact_pack": True}, True), ({}, False)):
            with self.subTest(keys=keys):
                self.use(**keys)
                self.seed(_index(*ROWS))
                code, out, err = self.run_g("fetch-index", "--compact")
                self.assertEqual(code, 0, err)
                rows = [line for line in out.splitlines() if "Topic" in line]
                self.assertEqual(len(rows) < len(ROWS), packed, rows)
                self.assertEqual(sum(line.count("Topic") for line in rows), len(ROWS))

    def test_description_is_listed_by_sources(self):
        self.use(description="Pages fetched one by one")
        code, out, err = self.run_g("sources", "--sources-file", self.sources, raw=True)
        self.assertEqual(code, 0, err)
        self.assertIn(f"firebase  [user, fetch]  {INDEX_URL}\n    Pages fetched one by one\n", out)

    def test_page_ref_forms_url_reaches_the_page_a_substring_cannot_tell_apart(self):
        rows = [(t, u, d) for t, u, d, _b in TRICKY]
        self.seed(_index(*rows), {u: b for _t, u, _d, b in TRICKY})
        # both cli/targets.md.txt and cli.md.txt contain this prefix
        ref = "https://firebase.google.com/docs/cli"
        code, out, err = self.run_g("sections", ref)
        self.assertEqual(code, 0, err)
        self.assertIn('"CLI reference"', out.splitlines()[0])
        # the slug form: the URL's last part without page_url_strip_suffix
        code, out, err = self.run_g("sections", "multiprojects")
        self.assertEqual(code, 0, err)
        self.assertIn('"Configure projects"', out.splitlines()[0])


class FetchSearchFixesTest(_FetchSource):

    def test_a_url_listed_twice_is_fetched_once_for_both_candidates(self):
        url = "https://firebase.google.com/docs/twice.md.txt"
        self.seed(_index(("Widget guide", url, "widget"), ("Widget guide again", url, "widget")))
        self.net.served[url] = _page("Widget guide", "widget text")
        code, out, err = self.run_g("search", "widget")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.pages(), [url])
        self.assertIn("[0] Widget guide (", out)
        self.assertIn("[1] Widget guide again (", out)
        # a failure is counted for each of them
        self.setUp()
        self.seed(_index(("Widget guide", url, "widget"), ("Widget guide again", url, "widget")))
        code, out, err = self.run_g("search", "widget")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.net.pages(), [url])
        self.assertIn("(2 documents skipped — fetch failed, see stderr)", out)

    def test_top_n_must_not_be_negative(self):
        self.seed(_index(*ROWS), {u: _page(t) for t, u, _d in ROWS})
        for value in ("-1", "-5", "x"):
            with self.subTest(value):
                code, _out, err = self.run_g("search", "widget", "--top-n", value)
                self.assertEqual(code, 2, err)
                self.assertIn("argument --top-n", err)
        self.assertIn("argument --top-n: invalid int value: 'x'", self.run_g("search", "w", "--top-n", "x")[2])
        code, out, err = self.run_g("search", "widget", "--top-n", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[0] Topic", out)
        code, out, err = self.run_g("search", "widget", "--top-n", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("(2 documents, ranked via index → body fetch)", out)
        self.assertEqual(self.net.calls, [])

    def test_search_content_with_no_page_fetched_exits_1_without_a_zero_hit_report(self):
        self.seed(_index(*ROWS[:2]))
        for argv, what in ((("--page-ref", "1"), "the --page-ref 1 page"), ((), "any of the 2 pages")):
            with self.subTest(argv):
                code, out, err = self.run_g("search-content", "widget", *argv)
                self.assertEqual(code, 1, out)
                self.assertIn(f"Error: could not fetch {what}", err)
                self.assertIn("(skip: fetch failed", err)
                self.assertNotIn("not in this corpus", out)
                self.assertNotIn("No matching content found", out)
                self.assertNotIn("Why nothing matched", out)


class SearchContentPipeTest(unittest.TestCase):
    """``search-content`` on a pipe (as the Bash tool runs it): the first
    page's results can be read while the second page is still being
    fetched. A ``StringIO`` stdout cannot show this; a pipe is
    block-buffered unless the loader flushes."""

    def test_first_page_is_read_from_the_pipe_while_the_second_is_fetched(self):
        import http.server
        import queue
        release, asked = threading.Event(), threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/docs/p1"):
                    asked.set()
                    release.wait(60)
                data = f"# Page {self.path}\n\n## Overview\nzzzpipe word\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        base = f"http://127.0.0.1:{server.server_address[1]}/docs"
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sources = os.path.join(tmp, "sources.json")
        with open(sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"firebase": PROFILE}}, f)
        p = _valid(sources, "firebase", PROFILE)
        Path(g._index_cache_path(tmp, p)).write_text(
            _index(("Pipe zero", f"{base}/p0.md.txt", "d"), ("Pipe one", f"{base}/p1.md.txt", "d")),
            encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("pythonunbuffered", "http_proxy", "https_proxy", "all_proxy")}
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        with open(os.path.join(tmp, "stderr.txt"), "w", encoding="utf-8") as err_file:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(_loader.SCRIPTS_DIR, SCRIPT), "search-content", "zzzpipe",
                 "--source", "firebase", "--sources-file", sources, "--cache-dir", tmp],
                stdout=subprocess.PIPE, stderr=err_file, text=True, env=env)

        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
            proc.stdout.close()
        self.addCleanup(reap)
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [lines.put(x) for x in proc.stdout] and None, daemon=True).start()
        seen = []
        deadline = time.monotonic() + 15
        # the process cannot end before page 1 is released, so a line read
        # now was flushed to the pipe, not written out at exit
        while time.monotonic() < deadline and not ("[0] Pipe zero\n" in seen and asked.is_set()):
            try:
                seen.append(lines.get(timeout=0.2))
            except queue.Empty:
                pass
        self.assertTrue(asked.is_set(), f"page 1 was never requested: {seen}")
        self.assertFalse(release.is_set())
        self.assertIn("[0] Pipe zero\n", seen, "page 0's result was not readable while page 1 was pending")
        release.set()
        self.assertEqual(proc.wait(timeout=60), 0, Path(tmp, "stderr.txt").read_text(encoding="utf-8"))


class FetchMembersTest(_FetchSource):
    """A ``body: "fetch"`` source in a ``members`` alias next to a full-text one."""

    def test_each_group_and_next_line_opens_its_page(self):
        code_dir = parity.FIXTURES / "claude-docs"
        profiles = {"firebase": PROFILE, "claude-code": parity.GENERIC_PROFILES["claude-code"],
                    "both": {"members": ["claude-code", "firebase"]}}
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": profiles}, f)
        cc = {"name": "claude-code", **parity.GENERIC_PROFILES["claude-code"]}
        shutil.copyfile(code_dir / "claude-code-llms-full.txt", g._cache_path(self.tmp, cc))
        shutil.copyfile(code_dir / "claude-code-llms.txt", g._index_cache_path(self.tmp, cc))
        self.seed(_index(("Sample targets", QL, "sample")), {QL: "# Sample targets\n\n## Sample\nA sample.\n"})
        both = [SCRIPT, "search", "--source", "both", "--sources-file", self.sources, "--cache-dir", self.tmp]
        code, out, err = self._run_words([*both, "sample"])
        self.assertEqual(code, 0, err)
        self.assertIn("--- Firebase (--source firebase) ---", out)
        self.assertIn("[firebase] [0] Sample targets", out)
        lines = [line for line in out.splitlines() if line.startswith("Next: ")]
        self.assertTrue(any("--source firebase" in line for line in lines))
        for line in lines:
            c, o, e = self._run_words(shlex.split(line)[1:])
            self.assertEqual(c, 0, e)
        self.assertEqual(self.net.calls, [])
        # a candidate page that cannot be fetched is counted in its group
        dead = "https://firebase.google.com/docs/dead.md.txt"
        self.seed(_index(("Sample targets", QL, "sample"), ("Sample dead", dead, "sample")))
        code, out, err = self._run_words([*both, "sample"])
        self.assertEqual(code, 0, err)
        group = out.split("--- Firebase (--source firebase) ---", 1)[1]
        self.assertIn("(1 documents skipped — fetch failed, see stderr)", group)
        self.assertEqual(self.net.calls, [dead])
        self.net.calls.clear()
        # zero hits: the fetch member counts its index entries, fetching nothing
        code, out, err = self._run_words([*both, "zzznothing"])
        self.assertEqual(code, 0, err)
        self.assertIn("2 index entries (title/description) checked", out)
        self.assertEqual(self.net.calls, [])


@unittest.skipUnless(os.environ.get(parity.CORPUS_ENV) == "1",
                     f"local-only full-corpus gate ({parity.CORPUS_ENV}=1)")
class CachedFirebaseRoundTripTest(unittest.TestCase):
    """On the local Firebase cache: every page in the page cache, by its
    number in the index, opens with its own title and no fetch."""

    def test_every_cached_page_opens_by_its_number(self):
        cache = parity.corpus_cache_dir()
        if not (cache / "firebase-llms.txt").exists():
            self.skipTest("no firebase cache")
        import _common
        entries = _common.parse_llms_index(_common.load_lines(str(cache / "firebase-llms.txt")))
        cached = set(os.listdir(cache / "firebase-docs"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        parity.stage_corpus("firebase", tmp, source_dir=cache)
        sources = os.path.join(tmp, "sources.json")
        with open(sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"firebase": parity.GENERIC_PROFILES["firebase"]}}, f)
        argv = parity._generic_argv("firebase", [], tmp, sources)
        opened = 0
        for i, e in enumerate(entries):
            if fb._url_to_cache_filename(e["url"]) not in cached:
                continue
            run = parity.run_script(SCRIPT, ["sections", str(i), *argv], tmp)
            self.assertEqual(run.code, 0, run.stderr)
            self.assertEqual(run.fetches, 0)
            self.assertIn(f'"{e["title"]}"', run.stdout.splitlines()[0])
            opened += 1
        self.assertGreater(opened, 0)


if __name__ == "__main__":
    unittest.main()
