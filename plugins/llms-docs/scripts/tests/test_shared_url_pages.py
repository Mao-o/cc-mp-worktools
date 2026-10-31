"""One URL heading different pages.

learn.chatgpt.com lists "Command line options" and "Slash commands in Codex
CLI" under the same ``developer-commands.md?surface=cli`` (the index_url join
gives both pages that URL). A URL page_ref, an anchor URL passed back as one,
an index entry and a link annotation must not reach the first (or last) of
them silently: the URL page_ref lists the candidates, the joins name none.
Copies of one page under one URL (Claude Code's llms-full.txt heads a few
pages twice, with the same body) still open the first.
"""

import contextlib
import io
import json
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
URL_FORMS = ("int", "url", "title", "url_substring")

# Cut from learn.chatgpt.com's llms.txt / llms-full.txt (2026-10-15 cache).
CODEX_INDEX = (
    "# Codex\n\n"
    "- [Command line options](https://learn.chatgpt.com/docs/developer-commands.md?surface=cli): "
    "Options and flags for the Codex terminal client\n"
    "- [Slash commands in Codex CLI](https://learn.chatgpt.com/docs/developer-commands.md?surface=cli): "
    "Control Codex during interactive sessions\n"
    "- [Config basics](https://learn.chatgpt.com/docs/config-file/config-basic.md): Configure Codex\n"
)
CODEX_FULL = (
    "# Command line options\n\n"
    "## How to read this reference\n\n"
    "This page catalogs every documented Codex CLI command and flag.\n\n"
    "## Global flags\n\n"
    "Choose a model available to your signed-in account or workspace.\n\n"
    "---\n\n"
    "# Slash commands in Codex CLI\n\n"
    "Slash commands give you fast, keyboard-first control over Codex.\n\n"
    "## Built-in slash commands\n\n"
    "Codex ships with the following commands. Open the slash popup and start typing.\n\n"
    "---\n\n"
    "# Config basics\n\n"
    "## Configuration precedence\n\n"
    "Overrides you pass at the command line take precedence.\n"
)
CLI_URL = "https://learn.chatgpt.com/docs/developer-commands.md?surface=cli"


def _docs(rows):
    return [{"title": t, "url": u, "body_lines": [b]} for t, u, b in rows]


def resolve(docs, ref):
    """``(index, stderr)``, or ``(None, stderr)`` when it exits 1."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return generic._resolve_page_ref(docs, ref, lambda i: f"{SCRIPT} sections {i}", URL_FORMS), err.getvalue()
        except SystemExit as e:
            if e.code != 1:
                raise AssertionError(f"exit {e.code}") from e
            return None, err.getvalue()


class UrlPageRefTest(unittest.TestCase):

    def test_different_pages_under_one_url_are_listed(self):
        for ref in (CLI_URL, "https://learn.chatgpt.com/docs/developer-commands?surface=cli#global-flags"):
            for rows in ([("A", CLI_URL, "a"), ("B", CLI_URL, "b")],
                         [("A", CLI_URL, "a"), ("C", "https://learn.chatgpt.com/docs/developer-commands.md?surface=ide", "c"),
                          ("B", CLI_URL, "b")]):
                with self.subTest(ref=ref, rows=len(rows)):
                    idx, err = resolve(_docs(rows), ref)
                    self.assertIsNone(idx, err)
                    self.assertIn(f"Ambiguous URL '{ref}'", err)
                    listed = re.findall(r"^  \[(\d+)\] (.+)$", err, re.M)
                    want = [str(i) for i, (_t, u, _b) in enumerate(rows) if u == CLI_URL]
                    self.assertEqual([n for n, _l in listed], want)
                    self.assertIn(f"A ({CLI_URL})", err)
                    for n in want:
                        self.assertIn(f"    {SCRIPT} sections {n}\n", err)

    def test_pages_with_no_query_under_one_url_are_listed_when_they_differ(self):
        rows = [("A", "https://example.com/docs/x", "a"), ("B", "https://example.com/docs/x/", "b"),
                ("Q", "https://example.com/docs/x?v=2", "q")]
        for ref in ("https://example.com/docs/x", "https://example.com/docs/x?id=anchor"):
            with self.subTest(ref=ref):
                idx, err = resolve(_docs(rows), ref)
                self.assertIsNone(idx)
                self.assertEqual(re.findall(r"^  \[(\d+)\]", err, re.M), ["0", "1"])
        # the query of one page still reaches that page
        self.assertEqual(resolve(_docs(rows), "https://example.com/docs/x?v=2")[0], 2)

    def test_copies_of_one_page_open_the_first(self):
        for tier, ref in (("exact", CLI_URL), ("bare", "https://example.com/docs/x?id=a")):
            with self.subTest(tier=tier):
                url = CLI_URL if tier == "exact" else "https://example.com/docs/x"
                rows = [("Other", "https://example.com/o", "o"), ("A", url, "same"), ("A again", url, "same")]
                self.assertEqual(resolve(_docs(rows), ref), (1, ""))

    def test_unfetched_pages_under_one_url_open_the_first(self):
        # body "fetch": the pages are llms.txt entries with no body yet; one
        # URL fetches one page, so they are copies
        docs = [{"title": t, "url": "https://e.com/p.md.txt", "body_lines": None} for t in ("A", "B")]
        self.assertEqual(resolve(docs, "https://e.com/p.md.txt"), (0, ""))

    def test_a_ref_with_no_query_still_opens_the_first_query_page(self):
        # unchanged: the URL without a query names the first page of the
        # URL (the order the listings and annotations use)
        rows = [("A", CLI_URL, "a"), ("B", CLI_URL, "b")]
        self.assertEqual(resolve(_docs(rows), "https://learn.chatgpt.com/docs/developer-commands")[0], 0)


class JoinUrlPageTest(unittest.TestCase):
    """``_join_url_page`` (index entries with ``index_join: "url"`` and link
    annotations) names no page where the URL page_ref lists candidates."""

    def test_different_pages_join_none_and_copies_the_last(self):
        cases = [
            ([("A", CLI_URL, "a"), ("B", CLI_URL, "b")], CLI_URL, None),
            ([("A", CLI_URL, "s"), ("B", CLI_URL, "s")], CLI_URL, 1),
            ([("A", "https://e.com/x", "a"), ("B", "https://e.com/x", "b")], "https://e.com/x?id=1", None),
            ([("A", "https://e.com/x", "s"), ("B", "https://e.com/x", "s")], "https://e.com/x?id=1", 1),
            ([("A", "https://e.com/x?v=1", "a"), ("B", "https://e.com/x?v=2", "b")], "https://e.com/x", 0),
        ]
        for rows, url, want in cases:
            with self.subTest(rows=rows, url=url):
                docs = _docs(rows)
                self.assertEqual(generic._join_url_page(list(range(len(docs))), docs, url, None), want)

    def test_link_to_a_shared_url_is_not_annotated(self):
        profile = generic._validate_profile("sources.json", "m", {
            "url": "https://e.com/llms-full.txt", "split": "h1", "page_url": "line:Source: ",
            "link_annotations": True})
        rows = [("Home", "https://e.com/home", "[a](https://e.com/x?surface=cli) [b](https://e.com/y)"),
                ("A", "https://e.com/x?surface=cli", "a"), ("B", "https://e.com/x?surface=cli", "b"),
                ("Y", "https://e.com/y", "y")]
        docs = _docs(rows)
        resolve_link = generic._link_resolver(profile, docs, 0)
        self.assertIsNone(resolve_link("https://e.com/x?surface=cli"))
        self.assertEqual(resolve_link("https://e.com/y"), 3)


class CodexAnchorRoundTripTest(unittest.TestCase):
    """On the codex shape (index_url title join) with the url page_ref form
    and anchors switched on: the anchor a search prints, passed back as
    printed, lists both pages, and each listed command opens its page."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "full.txt")
        self.full.write_text(CODEX_FULL, encoding="utf-8")
        self.index = Path(self.tmp, "index.txt")
        self.index.write_text(CODEX_INDEX, encoding="utf-8")
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": {"cx": {
            "url": "https://learn.chatgpt.com/docs/llms-full.txt", "split": "h1", "skip_empty": True,
            "index_url": "https://learn.chatgpt.com/docs/llms.txt",
            "page_ref_forms": ["int", "url", "title", "url_substring"], "anchor_style": "github"}}}),
            encoding="utf-8")
        self.corpus = ["--source", "cx", "--sources-file", str(self.sources),
                       "--file", str(self.full), "--index-file", str(self.index)]

    def run_cmd(self, *argv):
        return _loader.run_cli(generic, [SCRIPT, *argv])

    def test_anchor_of_either_page_lists_both(self):
        for query, page in (("catalogs", "Command line options"), ("popup", "Slash commands in Codex CLI")):
            with self.subTest(page=page):
                code, out, err = self.run_cmd("search", *self.corpus, query)
                self.assertEqual(code, 0, err)
                anchors = re.findall(r"\[(https://learn\.chatgpt\.com/\S+#\S+)\]", out)
                self.assertTrue(anchors, out)
                self.assertIn("?surface=cli#", anchors[0])
                code, out, err = self.run_cmd("sections", *self.corpus, anchors[0])
                self.assertEqual(code, 1, out)
                self.assertIn("Ambiguous URL", err)
                self.assertEqual(re.findall(r"^  \[(\d+)\] (.+?) \(", err, re.M),
                                 [("0", "Command line options"), ("1", "Slash commands in Codex CLI")])
                retries = [ln.strip() for ln in err.splitlines() if ln.strip().startswith(SCRIPT + " ")]
                self.assertEqual(len(retries), 2, err)
                for n, line in enumerate(retries):
                    code, out2, err2 = _loader.run_cli(generic, shlex.split(line, comments=True))
                    self.assertEqual(code, 0, (line, err2))
                    title = ["Command line options", "Slash commands in Codex CLI"][n]
                    self.assertTrue(out2.startswith(f'Sections in [{n}] "{title}"'), out2)

    def test_the_other_url_still_reaches_its_page(self):
        code, out, err = self.run_cmd(
            "sections", *self.corpus, "https://learn.chatgpt.com/docs/config-file/config-basic")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('Sections in [2] "Config basics"'), out)


if __name__ == "__main__":
    unittest.main()
