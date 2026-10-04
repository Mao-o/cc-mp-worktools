"""How sections and pages are ranked against a query.

A huge reference section (one environment-variable table, one command list)
contains almost every word somewhere and used to win on its number of hit
lines alone, even against a section whose heading is the query typed word for
word. The rank now looks first at whether the page title and the section's
headings (its own and its ancestors') name every keyword as a word, then the
hit count; function words ("in", "the", "when") are not matched at all.
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
# heading naming them.
HUGE_TABLE = "## Variables\n" + "".join(
    f"| `VAR_{i}` | {word} value {i} |\n"
    for i, word in enumerate(["when", "edits", "take", "effect"] * 20))
SETTINGS_BODY = (
    "## Change a setting\n"
    "Open the file.\n"
    "### When edits take effect\n"
    "Saved files are reloaded.\n"
)
# "sandbox" is only in the parent heading of the section that is about the
# query; another section has both words on more lines.
SANDBOX_BODY = (
    "## Settings\n"
    "sandbox network a\nsandbox network b\nsandbox network c\n"
    "## Sandbox\n"
    "### Network access\n"
    "Allow sandbox network hosts.\n"
)


def _hits(body: str, query: str, **kw) -> dict:
    return _common.search_content_in_body(body.splitlines(), query, **kw)


class QueryTermsTest(unittest.TestCase):

    def test_function_words_are_dropped(self):
        self.assertEqual(_common.query_terms("Run hooks in the background"),
                         ["Run", "hooks", "background"])
        self.assertEqual(_common.dropped_query_terms("When edits take effect"), ["when"])

    def test_a_query_of_only_function_words_is_kept(self):
        self.assertEqual(_common.query_terms("how to"), ["how", "to"])
        self.assertEqual(_common.dropped_query_terms("how to"), [])

    def test_an_abbreviation_in_capitals_is_kept(self):
        # "DO" is Durable Objects, not the function word "do"
        self.assertEqual(_common.query_terms("DO alarm"), ["DO", "alarm"])
        self.assertEqual(_common.query_terms("do alarm"), ["alarm"])
        # one capital letter is still the article
        self.assertEqual(_common.query_terms("A alarm"), ["alarm"])

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


class HeadingWordTest(unittest.TestCase):
    """A heading names a keyword only as a word (a plural allowed)."""

    CASES = [
        # (label, keyword, heading, names it)
        ("plural s", "hook", "Hooks reference", True),
        ("plural es", "match", "Matches", True),
        ("code span", "streamtext", "`streamText` changes", True),
        ("digit", "2", "Exit code 2 behavior", True),
        ("markdown escape", "max_tokens", "max\\_tokens", True),
        ("dotted name", "worktree.baseref", "`worktree.baseRef`", True),
        ("inside a word (start)", "env", "Environment variables", False),
        ("inside a word (end)", "hook", "Webhooks", False),
        ("prefix of a word", "add", "Additional options", False),
        ("inside a word (middle)", "struct", "Constructor", False),
        ("other suffix", "hook", "Hooked", False),
    ]

    def test_cases(self):
        for label, kw, heading, expected in self.CASES:
            with self.subTest(label):
                self.assertIs(_common.names_every_keyword(
                    _common._heading_text(heading), [kw]), expected)

    def test_a_section_whose_heading_holds_the_word_inside_another_does_not_match(self):
        body = "## Webhooks\nhook payload\n"
        self.assertFalse(_hits(body, "hook payload")["results"][0]["heading_match"])


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

    def test_an_ancestor_heading_counts(self):
        hits = _hits(SANDBOX_BODY, "sandbox network")
        self.assertEqual(hits["results"][0]["heading_path"], "Sandbox/Network access")
        self.assertEqual(hits["best_fit"], 0)

    def test_the_page_title_counts_as_a_heading(self):
        body = "## Usage\nstream with options here.\n## Other\nstream options\nstream options\n"
        self.assertEqual(_hits(body, "stream options")["best_fit"], 1)
        hits = _hits(body, "stream options", page_title="Stream options")
        self.assertEqual(hits["best_fit"], 0)
        # every section is named by the title: the hit count decides
        self.assertEqual(hits["results"][0]["heading_path"], "Other")

    def test_without_a_heading_match_more_hits_win(self):
        body = ("## Wrapper scripts\n"
                + "A hook process inherits the environment.\n" * 3
                + "## Common input fields\n"
                "A hook process inherits the parent environment.\n")
        hits = _hits(body, "hook process inherits environment")
        self.assertEqual(hits["results"][0]["heading_path"], "Wrapper scripts")
        self.assertEqual(hits["best_fit"], 1)

    def test_one_keyword_orders_sections_but_not_pages(self):
        body = "## Intro\nuseChat a\nuseChat b\n## `useChat` changes\nuseChat c\n"
        hits = _hits(body, "useChat")
        self.assertEqual(hits["results"][0]["heading_path"], "`useChat` changes")
        self.assertEqual(hits["best_fit"], 1)


class PageRankTest(unittest.TestCase):

    def test_page_with_the_heading_outranks_the_page_with_more_hits(self):
        pages = [HUGE_TABLE.splitlines(), SETTINGS_BODY.splitlines()]
        found = _common.full_corpus_body_search(pages, "When edits take effect")
        self.assertEqual([idx for idx, _h in found], [1, 0])

    def test_page_title_passed_to_the_full_corpus_search(self):
        pages = [["## Usage", "stream options"] * 2, ["## Usage", "stream options"]]
        found = _common.full_corpus_body_search(pages, "stream options",
                                                titles=["Guide", "Stream options"])
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

    def test_candidate_whose_title_names_the_query_needs_no_extra_page(self):
        cand = ["## Usage", "stream options"]
        other = ["## Stream options", "stream options"]
        results = [{"doc_idx": 0, "title": "Stream options",
                    "body_hits": _common.search_content_in_body(
                        cand, "stream options", page_title="Stream options")}]
        extra = _common.full_corpus_extra_hits(
            results, [cand, other], "stream options", titles=["Stream options", "Guide"])
        self.assertEqual(extra, [])

    def test_one_keyword_adds_no_page_named_by_a_heading(self):
        # The reference page (title only in the H1, outside the body) must
        # not be outranked by a migration guide whose H2 names the API.
        ref = ["# streamText", "", "## Parameters", "Call streamText with a model.",
               "## Returns", "streamText returns a result."]
        mig = ["# Migrate", "", "## `streamText` changes", "streamText now ..."]
        q = "streamText"
        results = [{"doc_idx": 0, "title": "streamText",
                    "body_hits": _common.search_content_in_body(ref, q)}]
        got = _common.full_corpus_extra_hits(
            results, [ref, mig], q, titles=["streamText", "Migrate AI SDK 4.x to 5.0"])
        self.assertEqual(got, [])


class PageTitleWiringTest(unittest.TestCase):
    """Each script hands the page title to the body search: the page titled
    with the query outranks a page with more hits, though no heading of its
    body names the query."""

    # (title, H1 in the body, body)
    PAGES = [
        ("Guide", "Guide", "## Usage\nstream options a\nstream options b\nstream options c\n"),
        ("Stream options", "Reference", "## Usage\nstream options here\n"),
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _first_next(self, module, argv):
        code, out, err = _loader.run_cli(module, argv)
        self.assertEqual(code, 0, err)
        nexts = [ln for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        return shlex.split(nexts[0][len("Next: "):], comments=True)[2]

    def test_claude_docs(self):
        Path(self.tmp, "claude-code-llms.txt").write_text("".join(
            f"- [{t}](https://example.com/docs/en/p{i}): about stream options\n"
            for i, (t, _h, _b) in enumerate(self.PAGES)), encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text("".join(
            f"# {t}\nSource: https://example.com/docs/en/p{i}\n\n{b}\n"
            for i, (t, _h, b) in enumerate(self.PAGES)), encoding="utf-8")
        for cmd in ("search", "search-content"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._first_next(claude, [
                    "parse-claude-docs.py", cmd, "stream options", "--cache-dir", self.tmp]), "1")

    def test_ai_sdk(self):
        ai_sdk = _loader.load_script("parse-ai-sdk.py")
        Path(self.tmp, "ai-sdk-llms-full.txt").write_text("".join(
            f"---\ntitle: {t}\ndescription: about stream options\n---\n\n# {h}\n\n{b}\n"
            for t, h, b in self.PAGES), encoding="utf-8")
        for cmd in ("search", "search-content"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._first_next(ai_sdk, [
                    "parse-ai-sdk.py", cmd, "stream options", "--cache-dir", self.tmp]), "1")


class SearchRoundTripTest(unittest.TestCase):
    """``search`` puts the section with the heading first, and the ``Next:``
    line it prints, run exactly as printed, reads that section."""

    PAGES = [
        ("Environment variables", "https://example.com/docs/en/env-vars", HUGE_TABLE),
        ("Settings", "https://example.com/docs/en/settings", SETTINGS_BODY),
        ("Sandboxing", "https://example.com/docs/en/sandboxing", SANDBOX_BODY),
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "claude-code-llms-full.txt")
        self.full.write_text("".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in self.PAGES),
                             encoding="utf-8")

    def _index(self, pages):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")

    def _round_trip(self, corpus_args, query="When edits take effect", page="1",
                    heading="Change a setting/When edits take effect",
                    text="Saved files are reloaded."):
        code, out, err = _loader.run_cli(
            claude, ["parse-claude-docs.py", "search", query, *corpus_args])
        self.assertEqual(code, 0, err)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        argv = shlex.split(nexts[0], comments=True)
        self.assertEqual(argv[1:4], ["content", page, heading], nexts[0])
        code, out, err = _loader.run_cli(claude, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(f"# heading_path: {heading}\n", out)
        self.assertIn(text, out)

    CORPUS_ARGS = staticmethod(lambda tmp, full: (
        ["--cache-dir", tmp], ["--file", str(full), "--cache-dir", tmp]))

    def test_all_pages_in_the_index(self):
        self._index(self.PAGES)
        for args in self.CORPUS_ARGS(self.tmp, self.full):
            with self.subTest(args=args):
                self._round_trip(args)

    def test_page_missing_from_the_index_is_added_from_the_bodies(self):
        # the index only knows the huge page; it matches every keyword, but
        # no heading names them, so the settings page is still added
        self._index(self.PAGES[:1])
        for args in self.CORPUS_ARGS(self.tmp, self.full):
            with self.subTest(args=args):
                self._round_trip(args)

    def test_section_named_through_its_parent_heading(self):
        self._index(self.PAGES)
        for args in self.CORPUS_ARGS(self.tmp, self.full):
            with self.subTest(args=args):
                self._round_trip(args, query="sandbox network", page="2",
                                 heading="Sandbox/Network access",
                                 text="Allow sandbox network hosts.")

    def test_words_left_out_are_named_once_on_a_hit(self):
        self._index(self.PAGES)
        for cmd in ("search", "search-content"):
            with self.subTest(cmd=cmd):
                code, out, err = _loader.run_cli(claude, [
                    "parse-claude-docs.py", cmd, "When edits take effect",
                    "--cache-dir", self.tmp])
                self.assertEqual(code, 0, err)
                self.assertEqual(out.count("(not searched, too common: when)"), 1, out)

    def test_no_note_when_no_word_is_left_out(self):
        self._index(self.PAGES)
        code, out, err = _loader.run_cli(claude, [
            "parse-claude-docs.py", "search", "edits effect", "--cache-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertNotIn("not searched", out)

    def test_zero_hits_names_the_words_left_out(self):
        self._index(self.PAGES)
        code, out, err = _loader.run_cli(claude, [
            "parse-claude-docs.py", "search-content", "the zzzunknown", "--cache-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzunknown": 0 of 3', out)
        self.assertEqual(out.count("(not searched, too common: the)"), 1, out)
        self.assertNotIn('"the":', out)


if __name__ == "__main__":
    unittest.main()
