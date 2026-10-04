"""How sections and pages are ranked against a query.

A huge reference section (one environment-variable table, one command list)
contains almost every word somewhere and used to win on its number of hit
lines alone, even against a section whose heading is the query typed word for
word. The rank now looks at the heading first, then whether one line holds
every keyword close together, then the hit count; function words ("in",
"the", "when") are not matched at all.
"""

import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _common

claude = _loader.load_script("parse-claude-docs.py")

# Every word of "When edits take effect" (the function word too) many times,
# one word per row, so the section is a strict AND with lots of hits but no
# line holding all of them.
HUGE_TABLE = "## Variables\n" + "".join(
    f"| `VAR_{i}` | {word} value {i} |\n"
    for i, word in enumerate(["when", "edits", "take", "effect"] * 20))
SETTINGS_BODY = (
    "## Change a setting\n"
    "Open the file.\n"
    "### When edits take effect\n"
    "Saved files are reloaded.\n"
)


def _hits(body: str, query: str) -> dict:
    return _common.search_content_in_body(body.splitlines(), query)


class QueryTermsTest(unittest.TestCase):

    def test_function_words_are_dropped(self):
        self.assertEqual(_common.query_terms("Run hooks in the background"),
                         ["Run", "hooks", "background"])
        self.assertEqual(_common.dropped_query_terms("When edits take effect"), ["when"])

    def test_a_query_of_only_function_words_is_kept(self):
        self.assertEqual(_common.query_terms("how to"), ["how", "to"])
        self.assertEqual(_common.dropped_query_terms("how to"), [])

    def test_repeated_words_count_once(self):
        self.assertEqual(_common.query_terms("hooks Hooks matcher"), ["hooks", "matcher"])

    def test_index_score_ignores_function_words(self):
        entries = [
            {"title": "Use Claude in the cloud", "description": "Sessions in the cloud"},
            {"title": "Hooks reference", "description": "Run hooks in the background"},
        ]
        scored = _common.search_index_entries(entries, "Run hooks in the background")
        self.assertEqual([e["title"] for _s, _i, e in scored], ["Hooks reference"])

    def test_body_and_needs_no_function_word(self):
        # no "in" / "the" anywhere: still a strict match on the content words
        hits = _hits("## Async\nRun hooks async, background jobs.\n",
                     "Run hooks in the background")
        self.assertEqual(hits["match_mode"], "and")


class SectionRankTest(unittest.TestCase):

    def test_heading_naming_every_keyword_beats_a_huge_section(self):
        hits = _hits(HUGE_TABLE + SETTINGS_BODY, "When edits take effect")
        self.assertEqual(hits["results"][0]["heading_path"],
                         "Change a setting/When edits take effect")
        self.assertEqual(hits["best_fit"], 0)

    def test_heading_match_ignores_a_link_url(self):
        body = ("## [Alarm Handler](https://example.com/durable-objects/alarms)\n"
                "durable objects alarm\n")
        self.assertFalse(_hits(body, "durable objects alarm")["results"][0]["heading_match"])

    def test_keywords_on_one_line_beat_more_hits_spread_out(self):
        body = (
            "## Wrapper scripts\n"
            + "The hook runs here.\nA process starts.\nIt inherits flags.\nThe environment is set.\n" * 3
            + "## Common input fields\n"
            "A hook process inherits the parent environment.\n"
        )
        hits = _hits(body, "hook process inherits environment")
        self.assertEqual(hits["results"][0]["heading_path"], "Common input fields")
        self.assertEqual(hits["best_fit"], 1)

    def test_keywords_far_apart_on_one_long_line_are_not_close(self):
        pad = "x" * (_common.CLOSE_CHARS + 10)
        body = (
            "## Long line\n"
            f"hook {pad} process inherits environment\n"
            "## Many hits\n"
            + "hook process\ninherits environment\n" * 3
        )
        hits = _hits(body, "hook process inherits environment")
        self.assertEqual(hits["best_fit"], 2)
        self.assertEqual(hits["results"][0]["heading_path"], "Many hits")

    def test_keywords_within_the_window_on_a_long_line_are_close(self):
        pad = "x" * (_common.CLOSE_CHARS + 10)
        body = f"## Long line\n{pad} hook process inherits environment {pad}\n"
        self.assertEqual(_hits(body, "hook process inherits environment")["best_fit"], 1)


class PageRankTest(unittest.TestCase):

    def test_page_with_the_heading_outranks_the_page_with_more_hits(self):
        pages = [HUGE_TABLE.splitlines(), SETTINGS_BODY.splitlines()]
        found = _common.full_corpus_body_search(pages, "When edits take effect")
        self.assertEqual([idx for idx, _h in found], [1, 0])

    def _candidate(self, body):
        return {"doc_idx": 0, "title": "Environment variables",
                "body_hits": _hits(body, "When edits take effect")}

    def test_weak_strict_candidate_lets_a_better_page_in(self):
        pages = [HUGE_TABLE.splitlines(), SETTINGS_BODY.splitlines()]
        extra = _common.full_corpus_extra_hits(
            [self._candidate(HUGE_TABLE)], pages, "When edits take effect")
        self.assertEqual([idx for idx, _h in extra], [1])

    def test_candidate_with_the_heading_needs_no_extra_page(self):
        pages = [SETTINGS_BODY.splitlines(), SETTINGS_BODY.splitlines()]
        extra = _common.full_corpus_extra_hits(
            [self._candidate(SETTINGS_BODY)], pages, "When edits take effect")
        self.assertEqual(extra, [])


class SearchRoundTripTest(unittest.TestCase):
    """``search`` puts the section with the heading first, and the ``Next:``
    line it prints, run exactly as printed, reads that section."""

    PAGES = [
        ("Environment variables", "https://example.com/docs/en/env-vars", HUGE_TABLE),
        ("Settings", "https://example.com/docs/en/settings", SETTINGS_BODY),
    ]
    HEADING = "Change a setting/When edits take effect"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "claude-code-llms-full.txt")
        self.full.write_text("".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in self.PAGES),
                             encoding="utf-8")

    def _index(self, pages):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")

    def _round_trip(self, corpus_args):
        code, out, err = _loader.run_cli(
            claude, ["parse-claude-docs.py", "search", "When edits take effect", *corpus_args])
        self.assertEqual(code, 0, err)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        argv = shlex.split(nexts[0], comments=True)
        self.assertEqual(argv[1:4], ["content", "1", self.HEADING], nexts[0])
        code, out, err = _loader.run_cli(claude, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(f"# heading_path: {self.HEADING}\n", out)
        self.assertIn("Saved files are reloaded.", out)

    def test_both_pages_in_the_index(self):
        self._index(self.PAGES)
        for args in (["--cache-dir", self.tmp],
                     ["--file", str(self.full), "--cache-dir", self.tmp]):
            with self.subTest(args=args):
                self._round_trip(args)

    def test_page_missing_from_the_index_is_added_from_the_bodies(self):
        # the index only knows the huge page; it matches every keyword, but
        # only spread over its table, so the settings page is still added
        self._index(self.PAGES[:1])
        for args in (["--cache-dir", self.tmp],
                     ["--file", str(self.full), "--cache-dir", self.tmp]):
            with self.subTest(args=args):
                self._round_trip(args)

    def test_zero_hits_names_the_words_left_out(self):
        self._index(self.PAGES)
        code, out, err = _loader.run_cli(claude, [
            "parse-claude-docs.py", "search-content", "the zzzunknown", "--cache-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzunknown": 0 of 2', out)
        self.assertIn("(not searched, too common: the)", out)
        self.assertNotIn('"the":', out)


if __name__ == "__main__":
    unittest.main()
