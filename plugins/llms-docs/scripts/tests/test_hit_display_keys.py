"""``anchor_style`` / ``show_overflow`` of parse-llms-txt.py.

``anchor_style`` (``github`` / ``devsite``) appends `` [<page URL>#<anchor>]``
to each ``Section:`` line of ``search`` / ``search-content``, as
``parse-claude-docs.py`` (github) and ``parse-firebase.py`` (devsite) do. The
page URL is the human-facing one: ``.md``, ``page_url_strip_suffix``
(``.md.txt``) and ``#…`` removed, the query kept. ``show_overflow`` lists the sections
with hits beyond ``--max-hits`` (``Other sections with hits (not shown):``),
as ``parse-claude-docs.py`` does. Both are off by default.

The anchor tests of the two dedicated parsers are ported here
(``SectionUrlAnchorIntegrationTest`` / ``SectionUrlAnchorSlashInTitleIntegrationTest``
of each). No network: the pages are passed with ``--file`` (or staged in
``--cache-dir``).
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")

SCRIPT = "parse-llms-txt.py"
URL = "https://example.com/llms-full.txt"
INDEX_URL = "https://example.com/llms.txt"
H1 = {"url": URL, "split": "h1", "page_url": "line:Source: "}

# parse-claude-docs.py's fixtures: a page whose ``Source:`` URL has no ``.md``
HOOKS = ("# Hooks\n"
         "Source: https://example.com/hooks\n"
         "\n"
         "## Hook events\n"
         "\n"
         "### PreToolUse\n"
         "Runs before a tool call. Configure hook matchers here.\n")
HOOKS_CICD = ("# Hooks\n"
              "Source: https://example.com/hooks\n"
              "\n"
              "## Reference\n"
              "\n"
              "### CI/CD\n"
              "Run hook matchers in a CI/CD pipeline.\n")
# parse-firebase.py's fixtures: the page URL is the raw ``.md.txt`` fetch URL
FB_URL = "https://firebase.google.com/docs/firestore/query-limit.md.txt"
FB_LIMITS = ("# Firestore Query Limits\n"
             f"Source: {FB_URL}\n"
             "\n"
             "## Limits\n"
             "Max 100 zzzonlyinbody results per query.\n")
FB_READ_WRITE = ("# Firestore Query Limits\n"
                 f"Source: {FB_URL}\n"
                 "\n"
                 "## Reference\n"
                 "\n"
                 "### Read / write data\n"
                 "Reads and writes per query limit.\n")
# one page, hits in four sections (``--max-hits 1`` leaves three over)
OVERFLOW = ("# Widgets\n"
            "Source: https://example.com/w\n"
            "\n"
            "## Alpha\nwidget one\nwidget two\n\n"
            "## Beta\nwidget three\n\n"
            "## Gamma\nwidget four\n\n"
            "## Delta\nwidget five\n")

SOURCES = {
    "plain": H1,
    "github": {**H1, "anchor_style": "github"},
    "devsite": {**H1, "anchor_style": "devsite", "page_url_strip_suffix": ".md.txt"},
    "devsite-nosuffix": {**H1, "anchor_style": "devsite"},
    "overflow": {**H1, "show_overflow": True},
    "both": {**H1, "anchor_style": "github", "show_overflow": True},
    # page URLs from the llms.txt by title (the raw ``.md`` index URL)
    "index": {"url": URL, "split": "h1", "index_url": INDEX_URL, "anchor_style": "github"},
    # page refs by URL, for the round trip of the printed anchors
    "refs": {**H1, "anchor_style": "github", "page_ref_forms": ["int", "url", "slug"]},
    "refs-devsite": {**H1, "anchor_style": "devsite", "page_url_strip_suffix": ".md.txt",
                     "page_ref_forms": ["int", "url", "slug"]},
}

_ANCHOR_RE = re.compile(r"  \[(https?://[^\]\s]+)\]$")


def validate(raw: dict) -> dict:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return generic._validate_profile("sources.json", "site", raw)
        except SystemExit:
            pass
    raise AssertionError(f"profile rejected: {err.getvalue()}")


def rejection(raw: dict) -> str:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            generic._validate_profile("sources.json", "site", raw)
            return ""
        except SystemExit:
            pass
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            raise AssertionError(f"validating {raw!r} crashed: {e!r}") from e
    return err.getvalue()


def section_lines(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.strip().startswith("Section: ")]


class _Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
        self.full = Path(self.tmp, "full.txt")
        self.index = Path(self.tmp, "llms.txt")

    def write(self, text: str, index: str | None = None) -> None:
        self.full.write_text(text, encoding="utf-8")
        if index is not None:
            self.index.write_text(index, encoding="utf-8")

    def run_cmd(self, source: str, *args):
        argv = [SCRIPT, *args, "--source", source, "--sources-file", str(self.sources), "--file", str(self.full)]
        if self.index.exists():
            argv += ["--index-file", str(self.index)]
        try:
            return _loader.run_cli(generic, argv)
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            self.fail(f"{argv!r} crashed: {e!r}")


class ClaudeDocsAnchorTest(_Cli):
    """parse-claude-docs.py's anchor tests, on the generic loader with
    ``anchor_style: "github"``."""

    def test_search_content_section_line_has_url_anchor(self):
        self.write(HOOKS)
        code, out, err = self.run_cmd("github", "search-content", "matchers", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Hook events/PreToolUse  (x1)  [https://example.com/hooks#pretooluse]\n", out)

    def test_search_section_line_has_url_anchor(self):
        self.write(HOOKS)
        code, out, err = self.run_cmd("github", "search", "matchers")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Hook events/PreToolUse  (x1)  [https://example.com/hooks#pretooluse]\n", out)

    def test_search_content_anchor_uses_full_leaf_title(self):
        self.write(HOOKS_CICD)
        code, out, err = self.run_cmd("github", "search-content", "matchers", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://example.com/hooks#ci-cd]", out)
        self.assertNotIn("#cd]", out)

    def test_search_anchor_uses_full_leaf_title(self):
        self.write(HOOKS_CICD)
        code, out, err = self.run_cmd("github", "search", "matchers")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://example.com/hooks#ci-cd]", out)
        self.assertNotIn("#cd]", out)

    def test_an_index_url_with_md_anchors_without_it(self):
        """The URL from the llms.txt is the raw ``.md`` fetch URL (shown on
        the ``URL:`` line as is); the anchor goes on the page URL without it."""
        page = HOOKS.replace("Source: https://example.com/hooks\n", "")
        self.write(page, "# Docs\n\n- [Hooks](https://example.com/hooks.md): Configure hook matchers\n")
        for cmd in (("search", "matchers"), ("search-content", "matchers")):
            with self.subTest(cmd=cmd[0]):
                code, out, err = self.run_cmd("index", *cmd)
                self.assertEqual(code, 0, err)
                self.assertIn("    URL: https://example.com/hooks.md\n", out)
                self.assertIn("  [https://example.com/hooks#pretooluse]\n", out)
                self.assertNotIn(".md#", out)


class FirebaseAnchorTest(_Cli):
    """parse-firebase.py's anchor tests, on the generic loader with
    ``anchor_style: "devsite"`` and ``page_url_strip_suffix: ".md.txt"``."""

    def test_search_content_section_line_has_url_anchor(self):
        self.write(FB_LIMITS)
        code, out, err = self.run_cmd("devsite", "search-content", "zzzonlyinbody", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Limits  (x1)  [https://firebase.google.com/docs/firestore/query-limit#limits]\n",
                      out)
        # the raw fetch URL stays on the URL: line and stays out of the anchor
        self.assertIn(f"    URL: {FB_URL}\n", out)
        self.assertNotIn("query-limit.md.txt#limits", out)

    def test_search_section_line_has_url_anchor(self):
        self.write(FB_LIMITS)
        code, out, err = self.run_cmd("devsite", "search", "query")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#limits]", out)
        self.assertNotIn("query-limit.md.txt#limits", out)

    def test_search_content_anchor_uses_full_leaf_title(self):
        self.write(FB_READ_WRITE)
        code, out, err = self.run_cmd("devsite", "search-content", "query", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        # "_" join, not "-": Google DevSite heading ids
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#read_write_data]", out)
        self.assertNotIn("#write_data]", out)

    def test_search_anchor_uses_full_leaf_title(self):
        self.write(FB_READ_WRITE)
        code, out, err = self.run_cmd("devsite", "search", "query")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit#read_write_data]", out)
        self.assertNotIn("#write_data]", out)

    def test_without_the_suffix_key_the_suffix_stays(self):
        # page_url_strip_suffix is what names the human-facing URL
        self.write(FB_LIMITS)
        code, out, err = self.run_cmd("devsite-nosuffix", "search-content", "zzzonlyinbody", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://firebase.google.com/docs/firestore/query-limit.md.txt#limits]", out)


class AnchorEdgeTest(_Cli):
    def test_default_profile_prints_no_anchor(self):
        self.write(HOOKS)
        for cmd in (("search", "matchers"), ("search-content", "matchers")):
            with self.subTest(cmd=cmd[0]):
                code, out, err = self.run_cmd("plain", *cmd)
                self.assertEqual(code, 0, err)
                lines = section_lines(out)
                self.assertTrue(lines, out)
                self.assertFalse([line for line in lines if "[http" in line], out)

    def test_hostile_headings(self):
        """Symbols only: no anchor. A leading ``-``, ``\\_`` escapes and
        function words: the slug of ``heading_anchor_slug`` (not changed here)."""
        page = ("# Page\nSource: https://example.com/p\n\n"
                "## ???\nkeyword a\n\n"
                "## --bg flag\nkeyword b\n\n"
                "## subagent\\_type is required\nkeyword c\n\n"
                "## the of and\nkeyword d\n")
        self.write(page)
        code, out, err = self.run_cmd("github", "search-content", "keyword", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        lines = section_lines(out)
        self.assertIn("Section: ???  (x1)", lines)
        self.assertIn("Section: --bg flag  (x1)  [https://example.com/p#bg-flag]", lines)
        self.assertIn("Section: subagent\\_type is required  (x1)  "
                      "[https://example.com/p#subagent_type-is-required]", lines)
        self.assertIn("Section: the of and  (x1)  [https://example.com/p#the-of-and]", lines)

    def test_top_of_page_and_pages_without_an_absolute_url_get_no_anchor(self):
        page = ("# A\nSource: https://example.com/a\n\nkeyword above any heading\n\n"
                "# B\nSource: /relative/b\n\n## Part\nkeyword in b\n\n"
                "# C\n\n## Part\nkeyword in c\n")
        self.write(page)
        code, out, err = self.run_cmd("github", "search-content", "keyword")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[http", out)
        self.assertNotIn("[/relative", out)
        self.assertEqual(len(section_lines(out)), 3, out)

    def test_fragment_of_the_page_url_is_dropped_and_its_query_kept(self):
        page = ("# Q\nSource: https://example.com/q/?surface=cli#top\n\n## Part one\nkeyword\n")
        self.write(page)
        code, out, err = self.run_cmd("github", "search-content", "keyword")
        self.assertEqual(code, 0, err)
        self.assertIn("  [https://example.com/q?surface=cli#part-one]\n", out)

    def test_each_page_is_anchored_on_its_own_url(self):
        page2 = ("# Matchers guide\nSource: https://example.com/guide\n\n"
                 "## Write matchers\nHook matchers by tool name.\n")
        # both titles name the query, so ``search`` ranks both pages
        self.write(HOOKS.replace("# Hooks\n", "# Hook matchers\n") + "\n" + page2)
        for cmd in (("search", "matchers"), ("search-content", "matchers")):
            with self.subTest(cmd=cmd[0]):
                code, out, err = self.run_cmd("github", *cmd)
                self.assertEqual(code, 0, err)
                seen, url = [], None
                for line in out.splitlines():
                    if line.startswith("    URL: "):
                        url = line[len("    URL: "):]
                    m = _ANCHOR_RE.search(line)
                    if m:
                        seen.append(m.group(1))
                        self.assertEqual(m.group(1).split("#")[0], url, line)
                self.assertEqual(sorted(seen), ["https://example.com/guide#write-matchers",
                                                "https://example.com/hooks#pretooluse"])

    def test_body_only_rows_of_search_are_anchored(self):
        # no title / description match: the full-body fallback rows get anchors too
        page = HOOKS.replace("Configure hook matchers here.", "zzzbodyonly here.")
        self.write(page)
        code, out, err = self.run_cmd("github", "search", "zzzbodyonly")
        self.assertEqual(code, 0, err)
        self.assertIn("[body-only]", out)
        self.assertIn("  [https://example.com/hooks#pretooluse]\n", out)


class AnchorRoundTripTest(_Cli):
    """Each printed anchor names its page and one of its headings: its URL,
    passed back as a ``url`` page ref, reaches the page the hit came from,
    and its fragment is the anchor of a heading ``sections`` lists there."""

    PAGES = (HOOKS + "\n" + OVERFLOW + "\n"
             + "# Hooks again\nSource: https://example.com/docs/en/hooks.md?x=1\n\n## Hook events\nmatchers too\n"
             # pages that differ only by the query (as on learn.chatgpt.com):
             # the anchor of each reaches that page, not the first of the two
             + "\n# Surface CLI\nSource: https://example.com/docs/a?surface=cli\n\n## Key bindings\nshortcuts for the cli\n"
             + "\n# Surface IDE\nSource: https://example.com/docs/a?surface=ide\n\n## Key bindings\nshortcuts for the ide\n")
    FB_PAGES = FB_LIMITS + "\n" + FB_READ_WRITE.replace("query-limit", "other-limit").replace(
        "# Firestore Query Limits", "# Other Limits")

    def _anchors(self, out: str) -> list[tuple[int, str]]:
        page, found = None, []
        for line in out.splitlines():
            m = re.match(r"^\[(\d+)\] ", line)
            if m:
                page = int(m.group(1))
            m = _ANCHOR_RE.search(line)
            if m:
                found.append((page, m.group(1)))
        return found

    def _check(self, source: str, style: str, query: str, opts: tuple) -> int:
        code, out, err = self.run_cmd(source, "search-content", query)
        self.assertEqual(code, 0, err)
        anchors = self._anchors(out)
        self.assertTrue(anchors, out)
        for page, anchor in anchors:
            url, frag = anchor.split("#", 1)
            line = " ".join([SCRIPT, "sections", shlex.quote(url), "--source", source,
                             "--sources-file", shlex.quote(str(self.sources)), *opts])
            with self.subTest(anchor=anchor, opts=opts[0]):
                code, out2, err2 = _loader.run_cli(generic, shlex.split(line, comments=True))
                self.assertEqual(code, 0, (line, err2))
                self.assertTrue(out2.startswith(f"Sections in [{page}] "), (line, out2[:80]))
                paths = [re.sub(r"^\s*\[L\d+\]\s*", "", s) for s in out2.splitlines() if re.match(r"^\s*\[L\d", s)]
                # ``sections`` lists heading paths; a leaf is any tail of one
                # (a title may itself hold a "/": "Read / write data")
                leaves = {"/".join(p.split("/")[i:]) for p in paths for i in range(len(p.split("/")))}
                slugs = {generic.section_url_anchor("u", t, style=style).rsplit("#", 1)[-1][:-1] for t in leaves}
                self.assertIn(frag, slugs, (anchor, paths))
        return len(anchors)

    def _cache_opts(self, source: str) -> tuple:
        cache = Path(self.tmp, "cache")
        cache.mkdir(exist_ok=True)
        profile = validate({**SOURCES[source]})
        profile["name"] = source
        shutil.copyfile(self.full, generic._cache_path(str(cache), profile))
        return ("--cache-dir", shlex.quote(str(cache)))

    def test_github_anchors_reach_their_page_and_heading(self):
        self.write(self.PAGES)
        for opts in (("--file", shlex.quote(str(self.full))), self._cache_opts("refs")):
            self.assertGreaterEqual(self._check("refs", "github", "matchers", opts), 2)
            self.assertGreaterEqual(self._check("refs", "github", "widget", opts), 1)
            self.assertEqual(self._check("refs", "github", "shortcuts", opts), 2)

    def test_query_only_pages_are_anchored_on_their_own_query(self):
        self.write(self.PAGES)
        code, out, err = self.run_cmd("refs", "search-content", "shortcuts")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(a for _, a in self._anchors(out)),
                         ["https://example.com/docs/a?surface=cli#key-bindings",
                          "https://example.com/docs/a?surface=ide#key-bindings"])

    def test_devsite_anchors_reach_their_page_and_heading(self):
        self.write(self.FB_PAGES)
        for opts in (("--file", shlex.quote(str(self.full))), self._cache_opts("refs-devsite")):
            self.assertGreaterEqual(self._check("refs-devsite", "devsite", "query", opts), 2)


class OverflowTest(_Cli):
    """``show_overflow``: the sections with hits past ``--max-hits``, listed
    under the shown ones (parse-claude-docs.py prints this in both commands)."""

    EXPECTED = ("    Other sections with hits (not shown):\n"
                "      - Beta  (x1)\n"
                "      - Gamma  (x1)\n"
                "      - Delta  (x1)\n")

    def test_search_content_lists_the_sections_not_shown(self):
        self.write(OVERFLOW)
        code, out, err = self.run_cmd("overflow", "search-content", "widget", "--max-hits", "1")
        self.assertEqual(code, 0, err)
        self.assertIn(self.EXPECTED, out)
        self.assertEqual(section_lines(out), ["Section: Alpha  (x2)"])

    def test_search_lists_the_sections_not_shown(self):
        self.write(OVERFLOW)
        code, out, err = self.run_cmd("overflow", "search", "widget", "--max-hits", "1")
        self.assertEqual(code, 0, err)
        self.assertIn(self.EXPECTED, out)

    def test_off_by_default_and_nothing_when_all_are_shown(self):
        self.write(OVERFLOW)
        for source, max_hits in (("plain", "1"), ("overflow", "5"), ("github", "1")):
            for cmd in (("search-content", "widget"), ("search", "widget")):
                with self.subTest(source=source, cmd=cmd[0], max_hits=max_hits):
                    code, out, err = self.run_cmd(source, *cmd, "--max-hits", max_hits)
                    self.assertEqual(code, 0, err)
                    self.assertNotIn("Other sections", out)

    def test_both_keys_together(self):
        self.write(OVERFLOW)
        code, out, err = self.run_cmd("both", "search-content", "widget", "--max-hits", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Alpha  (x2)  [https://example.com/w#alpha]\n", out)
        self.assertIn(self.EXPECTED, out)


class ProfileKeysTest(unittest.TestCase):
    def test_defaults_are_off(self):
        p = validate(H1)
        self.assertIsNone(p["anchor_style"])
        self.assertIs(p["show_overflow"], False)

    def test_values_are_read(self):
        p = validate({**H1, "anchor_style": "devsite", "show_overflow": True})
        self.assertEqual((p["anchor_style"], p["show_overflow"]), ("devsite", True))
        self.assertIsNone(validate({**H1, "anchor_style": None})["anchor_style"])

    def test_bad_values_are_rejected(self):
        for value in ("GitHub", "mintlify", "", 1, True, ["github"]):
            with self.subTest(anchor_style=value):
                self.assertIn("anchor_style must be one of: github, devsite", rejection({**H1, "anchor_style": value}))
        for value in ("true", 1, None):
            with self.subTest(show_overflow=value):
                self.assertIn("show_overflow must be true or false", rejection({**H1, "show_overflow": value}))

    def test_anchor_style_needs_page_urls(self):
        no_urls = {"url": URL, "split": "h1"}
        self.assertIn("anchor_style needs page_url or index_url", rejection({**no_urls, "anchor_style": "github"}))
        self.assertEqual(rejection({**no_urls, "anchor_style": "github", "index_url": INDEX_URL}), "")
        # show_overflow needs no URL
        self.assertEqual(rejection({**no_urls, "show_overflow": True}), "")

    def test_strip_suffix_is_accepted_with_anchor_style(self):
        self.assertEqual(rejection({**H1, "anchor_style": "devsite", "page_url_strip_suffix": ".md.txt"}), "")
        self.assertIn("only used by", rejection({**H1, "page_url_strip_suffix": ".md.txt"}))
        self.assertIn("only used by", rejection({**H1, "show_overflow": True, "page_url_strip_suffix": ".md.txt"}))

    def test_keys_are_shared_by_the_candidates_of_split_auto(self):
        auto = {"url": URL, "split": "auto", "anchor_style": "github", "show_overflow": True,
                "split_candidates": [{"split": "h1", "page_url": "line:Source: "},
                                     {"split": "frontmatter", "page_url": "frontmatter:url"}]}
        p = validate(auto)
        for cand in p["split_candidates"]:
            self.assertEqual((cand["anchor_style"], cand["show_overflow"]), ("github", True))
        for key, value in (("anchor_style", "github"), ("show_overflow", True)):
            with self.subTest(key=key):
                moved = {**auto, "split_candidates": [{**auto["split_candidates"][0], key: value},
                                                      auto["split_candidates"][1]]}
                self.assertIn("has keys shared by the candidates", rejection(moved))

    def test_bundled_presets_do_not_set_the_keys(self):
        """Off by default: the bundled presets print what they printed before."""
        presets = json.loads(Path(generic.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]
        self.assertTrue(presets)
        for name, raw in presets.items():
            with self.subTest(preset=name):
                self.assertFalse({"anchor_style", "show_overflow"} & set(raw))
                p = generic._validate_profile(generic.PRESETS_FILE, name, raw)
                self.assertIsNone(p["anchor_style"])
                self.assertIs(p["show_overflow"], False)


if __name__ == "__main__":
    unittest.main()
