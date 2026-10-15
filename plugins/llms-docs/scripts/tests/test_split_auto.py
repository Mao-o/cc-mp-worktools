"""``split: "auto"`` (with ``split_candidates``) and ``join_rate_guard`` of
parse-llms-txt.py, both off by default.

``split: "auto"`` splits the file with each candidate and keeps the one
whose pages carry the most URLs (a tie goes to the earlier candidate).
``join_rate_guard`` compares the pages' own URLs with the ``llms.txt``
entries before the title join, and exits 2 below 50% (WARNING below 80%):
a profile that splits a file the wrong way (a frontmatter file read with
``split: h1``) has no page URLs and is stopped there. No network: the files
are passed with ``--file`` / ``--index-file``.
"""

import json
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _common

generic = _loader.load_script("parse-llms-txt.py")

URL = "https://example.com/llms-full.txt"
INDEX_URL = "https://example.com/llms.txt"
PARITY = Path(__file__).resolve().parent / "fixtures" / "parity" / "claude-docs"

H1 = {"split": "h1", "page_url": "line:Source: "}
FM = {"split": "frontmatter", "page_url": "frontmatter:url"}

# frontmatter pages with ``# …`` lines outside fences: split by H1 it has
# more pages (3) than split by frontmatter (2), but none with a URL
FM_WITH_H1_LINES = """\
---
title: Alpha
url: https://example.com/docs/alpha
---

# shell comment one

Alpha.

# shell comment two

---
title: Beta
url: https://example.com/docs/beta
---

# another comment

Beta.
"""

# one page with a URL either way: by H1 it is "Beta" (its ``Source:`` line),
# by frontmatter it is "Alpha" (its ``url:``, the H1 is in its body)
TIE = """\
---
title: Alpha
url: https://example.com/docs/alpha
---

Alpha.

# Beta
Source: https://example.com/docs/beta

Beta body.
"""

# H1 pages with no ``Source:`` line whose titles are in the index: the title
# join gives them URLs, the pages themselves have none
H1_NO_SOURCE = "# Alpha\n\nAlpha body.\n\n# Beta\n\nBeta body.\n"
H1_NO_SOURCE_INDEX = ("# Docs\n\n- [Alpha](https://example.com/docs/alpha.md)\n"
                      "- [Beta](https://example.com/docs/beta.md)\n")


def _validate(raw: dict) -> dict:
    """``_validate_profile`` for source ``site``; a rejected profile (``die``
    exits) fails the test instead of erroring it."""
    try:
        return generic._validate_profile("sources.json", "site", {"url": URL, **raw})
    except SystemExit as e:
        raise AssertionError(f"profile rejected (exit {e.code}): {raw}") from None


def _auto(*cands, **shared) -> dict:
    return _validate({"split": "auto", "split_candidates": list(cands), **shared})


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


class _Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_cli(self, profile: dict, argv: list, *, full: str, index: str | None = None):
        sources = self.write("sources.json", json.dumps({"sources": {"site": {"url": URL, **profile}}}))
        extra = ["--index-file", index] if index else []
        return _loader.run_cli(generic, ["parse-llms-txt.py", *argv, "--source", "site",
                                         "--sources-file", sources, "--file", full, *extra])

    def sources_error(self, raw: dict) -> tuple[int, str]:
        sources = self.write("sources.json", json.dumps({"sources": {"site": {"url": URL, **raw}}}))
        code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", sources])
        return code, err


class AutoValidationTest(_Files):
    def test_split_candidates_are_validated_as_profiles(self):
        p = _auto(H1, {**FM, "strip_trailing_headings": True}, min_heading_level=2, label="Docs")
        self.assertEqual(p["split"], "auto")
        self.assertEqual([c["split"] for c in p["split_candidates"]], ["h1", "frontmatter"])
        # shared keys reach every candidate; per-candidate keys stay in theirs
        self.assertEqual([c["min_heading_level"] for c in p["split_candidates"]], [2, 2])
        self.assertEqual([c["label"] for c in p["split_candidates"]], ["Docs", "Docs"])
        self.assertEqual([c["strip_trailing_headings"] for c in p["split_candidates"]], [False, True])
        self.assertEqual([c["page_url"] for c in p["split_candidates"]],
                         [("line", "Source: "), ("frontmatter", "url")])

    def test_a_line_candidate_takes_its_url_from_the_delimiter(self):
        p = _auto(H1, {"split": "line", "line_prefix": "Source: "})
        self.assertEqual(p["split_candidates"][1]["page_url"], ("line", "Source: "))

    def test_rejected(self):
        cases = [
            ("no candidates", {"split": "auto"}, "split_candidates must be a list of 2 or more"),
            ("one candidate", {"split": "auto", "split_candidates": [H1]}, "split_candidates must be a list of 2 or more"),
            ("not objects", {"split": "auto", "split_candidates": [H1, "frontmatter"]},
             "split_candidates must be a list of 2 or more"),
            ("candidates without auto", {**H1, "split_candidates": [H1, FM]},
             'split_candidates is only valid with split "auto"'),
            ("per-candidate key next to auto", {"split": "auto", "page_url": "frontmatter:url",
                                                "split_candidates": [H1, FM]},
             'sources.site.page_url goes in each of split_candidates with split "auto"'),
            ("shared key in a candidate", {"split": "auto", "split_candidates": [H1, {**FM, "label": "x"}]},
             "sources.site.split_candidates[1] has keys shared by the candidates: label"),
            ("unknown key in a candidate", {"split": "auto", "split_candidates": [H1, {**FM, "nope": 1}]},
             "sources.site.split_candidates[1] has unknown keys: nope"),
            ("auto inside auto", {"split": "auto", "split_candidates": [H1, {"split": "auto"}]},
             "sources.site.split_candidates[1].split must be one of: h1, frontmatter, line ("),
            ("candidate without page_url", {"split": "auto", "split_candidates": [H1, {"split": "frontmatter"}]},
             "sources.site.split_candidates[1] needs page_url"),
            ("bad value reported at its candidate", {"split": "auto",
                                                     "split_candidates": [{**H1, "tags_key": "tags"}, FM]},
             'sources.site.split_candidates[0].tags_key is only valid with split "frontmatter"'),
            ("bad page_url reported at its candidate", {"split": "auto",
                                                        "split_candidates": [H1, {**FM, "page_url": "bogus"}]},
             "sources.site.split_candidates[1].page_url must be"),
            ("bad shared key reported next to auto", {"split": "auto", "drop_lines": 5,
                                                      "split_candidates": [H1, FM]},
             "sources.site.drop_lines must be a list"),
            ("unknown split", {"split": "pages"}, "sources.site.split must be one of: h1, frontmatter, line, auto ("),
        ]
        for label, raw, needle in cases:
            with self.subTest(label):
                code, err = self.sources_error(raw)
                self.assertNotEqual(code, 0)
                self.assertIn(needle, err)


class ResolveSplitTest(unittest.TestCase):
    def test_the_candidate_with_more_url_pages_wins_in_either_order(self):
        lines = _lines(FM_WITH_H1_LINES)
        self.assertEqual(len(generic.split_documents(lines, _auto(H1, FM)["split_candidates"][0])), 3)
        for order, want in (((H1, FM), 1), ((FM, H1), 0)):
            with self.subTest(first=order[0]["split"]):
                choice, used, docs = generic.resolve_split(lines, _auto(*order))
                self.assertEqual(choice, want)
                self.assertEqual(used["split"], "frontmatter")
                self.assertEqual([d["title"] for d in docs], ["Alpha", "Beta"])

    def test_a_tie_goes_to_the_earlier_candidate(self):
        lines = _lines(TIE)
        for order, title in (((H1, FM), "Beta"), ((FM, H1), "Alpha")):
            with self.subTest(first=order[0]["split"]):
                choice, used, docs = generic.resolve_split(lines, _auto(*order))
                self.assertEqual((choice, used["split"]), (0, order[0]["split"]))
                self.assertEqual([d["title"] for d in docs], [title])

    def test_split_documents_returns_the_chosen_pages(self):
        try:
            docs = generic.split_documents(_lines(FM_WITH_H1_LINES), _auto(H1, FM))
        except Exception as e:  # noqa: BLE001  (a missing "auto" branch raises KeyError)
            self.fail(f"split_documents raised {e!r}")
        self.assertEqual([d["title"] for d in docs], ["Alpha", "Beta"])

    def test_a_fixed_split_is_used_as_is(self):
        p = _validate(FM)
        choice, used, docs = generic.resolve_split(_lines(FM_WITH_H1_LINES), p)
        self.assertIsNone(choice)
        self.assertIs(used, p)
        self.assertEqual(len(docs), 2)

    def test_claude_docs_fixtures_pick_their_shape(self):
        for name, want in (("claude-code", "h1"), ("claude-platform", "frontmatter")):
            with self.subTest(name):
                lines = _common.load_lines(str(PARITY / f"{name}-llms-full.txt"))
                _choice, used, _docs = generic.resolve_split(lines, _auto(H1, FM))
                self.assertEqual(used["split"], want)


class AutoCliTest(_Files):
    def test_the_commands_read_the_chosen_candidates_keys(self):
        # the frontmatter candidate (the second) warns about untitled pages;
        # the first candidate has no such key
        full = self.write("full.txt", "---\ntitle: Alpha\nurl: https://example.com/a\n---\n\nA.\n\n"
                                      "---\nurl: https://example.com/b\n---\n\n# B heading\n\nB.\n")
        profile = {"split": "auto", "split_candidates": [
            H1, {**FM, "frontmatter_key": "url", "untitled_warn_ratio": 0.1}]}
        code, out, err = self.run_cli(profile, ["fetch-index"], full=full)
        self.assertEqual(code, 0, err)
        self.assertIn("[1] B heading", out)
        self.assertIn("WARNING: 1/2 documents", err)

    def test_sources_lists_auto(self):
        sources = self.write("sources.json", json.dumps({"sources": {"site": {
            "url": URL, "split": "auto", "split_candidates": [H1, FM]}}}))
        code, out, _err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", sources])
        self.assertEqual(code, 0)
        self.assertIn(f"site  [user, auto]  {URL}", out)


class IndexJoinRateTest(unittest.TestCase):
    def test_counts_entries_naming_a_page_url(self):
        docs = [{"url": "https://e.com/docs/a"}, {"url": "https://e.com/docs/b/"}, {"url": ""}]
        index = _lines("# Docs\n\n"
                       "- [A](https://e.com/docs/a.md)\n"         # .md
                       "- [B](https://e.com/docs/b)\n"            # trailing slash on the page
                       "- [A section](https://e.com/docs/a#sec)\n"  # fragment
                       "- [More docs](https://e.com/docs/sub/llms.txt)\n"  # another index: not counted
                       "- [Gone](https://e.com/docs/gone)\n")
        self.assertEqual(generic.index_join_rate(docs, index), (3, 4))


class JoinRateGuardTest(_Files):
    GUARD_H1 = {**H1, "index_url": INDEX_URL, "join_rate_guard": True}

    def test_off_by_default(self):
        p = _validate({**H1, "index_url": INDEX_URL})
        self.assertFalse(p["join_rate_guard"])

    def test_rejected(self):
        for label, raw, needle in (
            ("no index_url", {**H1, "join_rate_guard": True}, "join_rate_guard needs index_url"),
            ("no page_url", {"split": "h1", "index_url": INDEX_URL, "join_rate_guard": True},
             "join_rate_guard needs page_url"),
            ("not a bool", {**H1, "index_url": INDEX_URL, "join_rate_guard": 1}, "join_rate_guard must be true or false"),
        ):
            with self.subTest(label):
                code, err = self.sources_error(raw)
                self.assertNotEqual(code, 0)
                self.assertIn(needle, err)

    def test_platform_read_by_h1_exits_2_in_every_command(self):
        full = str(PARITY / "claude-platform-llms-full.txt")
        index = str(PARITY / "claude-platform-llms.txt")
        for argv in (["fetch-index"], ["search-index", "token"], ["search", "token"],
                     ["search-content", "token"], ["sections", "0"], ["content", "0"]):
            with self.subTest(argv[0]):
                code, out, err = self.run_cli(self.GUARD_H1, argv, full=full, index=index)
                self.assertEqual(code, 2, out)
                self.assertEqual(out, "")
                self.assertIn("Error: site index↔full-text join rate is 0% (0/14), far below expected.", err)
                self.assertIn("check split / page_url", err)

    def test_without_the_guard_the_same_profile_runs(self):
        full = str(PARITY / "claude-platform-llms-full.txt")
        index = str(PARITY / "claude-platform-llms.txt")
        profile = {k: v for k, v in self.GUARD_H1.items() if k != "join_rate_guard"}
        code, _out, err = self.run_cli(profile, ["fetch-index"], full=full, index=index)
        self.assertEqual(code, 0, err)
        self.assertNotIn("join rate", err)

    def test_the_right_split_passes_and_a_partial_join_warns(self):
        # claude-code fixture: 11 of the 16 index entries have a page (69%)
        code, out, err = self.run_cli({**self.GUARD_H1, "label": "Quillon"}, ["fetch-index"],
                                      full=str(PARITY / "claude-code-llms-full.txt"),
                                      index=str(PARITY / "claude-code-llms.txt"))
        self.assertEqual(code, 0, err)
        self.assertIn("Document Index", out)
        self.assertEqual(err.strip(), "WARNING: Quillon index↔full-text join rate is 69% (11/16). "
                                      "Some index entries cannot be joined to full text.")

    def test_a_full_join_is_silent(self):
        full = self.write("full.txt", "# Alpha\nSource: https://example.com/docs/alpha\n\nA.\n\n"
                                      "# Beta\nSource: https://example.com/docs/beta\n\nB.\n")
        index = self.write("llms.txt", H1_NO_SOURCE_INDEX)
        code, _out, err = self.run_cli(self.GUARD_H1, ["fetch-index"], full=full, index=index)
        self.assertEqual((code, err), (0, ""))

    def test_urls_from_the_title_join_do_not_count(self):
        full = self.write("full.txt", H1_NO_SOURCE)
        index = self.write("llms.txt", H1_NO_SOURCE_INDEX)
        # without the guard the title join gives both pages a URL ...
        profile = {k: v for k, v in self.GUARD_H1.items() if k != "join_rate_guard"}
        code, out, _err = self.run_cli(profile, ["fetch-index"], full=full, index=index)
        self.assertEqual(code, 0)
        self.assertIn("URL: https://example.com/docs/alpha.md", out)
        # ... but the pages carry none of their own
        code, _out, err = self.run_cli(self.GUARD_H1, ["fetch-index"], full=full, index=index)
        self.assertEqual(code, 2)
        self.assertIn("join rate is 0% (0/2)", err)

    def test_no_index_no_check(self):
        # ``--file`` without ``--index-file`` reads no index, so there is
        # nothing to compare with
        code, _out, err = self.run_cli(self.GUARD_H1, ["fetch-index"],
                                       full=str(PARITY / "claude-platform-llms-full.txt"))
        self.assertEqual(code, 0, err)

    def test_auto_with_the_guard_picks_the_frontmatter_shape(self):
        profile = {"split": "auto", "split_candidates": [H1, FM], "index_url": INDEX_URL,
                   "join_rate_guard": True}
        code, out, err = self.run_cli(profile, ["fetch-index"],
                                      full=str(PARITY / "claude-platform-llms-full.txt"),
                                      index=str(PARITY / "claude-platform-llms.txt"))
        self.assertEqual(code, 0, err)
        self.assertIn("join rate is 79% (11/14)", err)  # a WARNING, not an exit
        self.assertIn("URL: https://platform.claude.com/docs/en/build/folding", out)


class UnclosedFenceCharacterizationTest(unittest.TestCase):
    """A frontmatter page whose body leaves a code fence open: the dedicated
    Claude docs loader ignores fences when it looks for frontmatter and
    still finds the pages after it; this loader tracks fences, so the open
    fence hides every later frontmatter block and those pages fold into the
    open one. No such page is in the current Claude docs corpora (the
    platform pages match one for one). Pinned as it is: whether the generic
    loader should find a frontmatter block inside an open fence (and with
    what guard against frontmatter examples in code samples) is not
    decided here."""

    TEXT = ("---\ntitle: First\nurl: https://example.com/docs/first\n---\n\nIntro.\n\n"
            "```bash\necho never closed\n\n"
            "---\ntitle: Second\nurl: https://example.com/docs/second\n---\n\nSecond.\n")

    def test_current_behaviour(self):
        lines = _lines(self.TEXT)
        old = _loader.load_script("parse-claude-docs.py").split_documents(lines)
        self.assertEqual([d["title"] for d in old], ["First", "Second"])
        p = _validate(FM)
        self.assertEqual([d["title"] for d in generic.split_documents(lines, p)], ["First"])


class CheckJoinRateAdviceTest(unittest.TestCase):
    def _error(self, **kw) -> str:
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            _common.check_join_rate("L", 0, 4, **kw)
        return err.getvalue()

    def test_default_text_is_unchanged(self):
        self.assertEqual(self._error(), "Error: L join rate is 0% (0/4), far below expected. "
                                        "The upstream format may have changed. "
                                        "Retry with --max-age 0; if it persists, report an issue.\n")

    def test_advice_replaces_the_default(self):
        self.assertEqual(self._error(advice="Fix it."),
                         "Error: L join rate is 0% (0/4), far below expected. Fix it.\n")


if __name__ == "__main__":
    unittest.main()
