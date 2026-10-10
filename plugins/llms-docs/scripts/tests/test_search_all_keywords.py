"""A page that has every keyword in one section must not be left out.

``search`` drills into the top-N pages of the index. When all of them match
only part of the query ("[partial match]"), a page of the corpus that has every
keyword together used to stay invisible, because the full-corpus search only ran
when no candidate had any body hit. ``search-content`` (ai-sdk and the generic
loader) cut its list by page number before ordering, so low-numbered partial pages pushed it out.

A changelog page with every keyword does not count as the answer (it is ranked
last), and at most two pages are appended, so the top index candidate keeps a
``Next:`` line.

Each script is driven through its CLI on a small corpus, and the ``Next:``
lines it prints are run as printed (split the way a shell would).
"""

import json
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _common

claude = _loader.load_script("parse-claude-docs.py")
ai_sdk = _loader.load_script("parse-ai-sdk.py")
generic = _loader.load_script("parse-llms-txt.py")

QUERY = "alpha beta"
# page 3 is the answer: both keywords in one section, nothing in title/description.
# Page 2 also has both, but is a changelog (ranked last; the index ranks it first).
# Pages 5 and 6 also have both: with them, more pages could be appended than
# the top index candidate leaves room for.
ANSWER_LINE = "alpha was renamed to beta in this release"
PAGES = [
    ("Alpha guide", "## Setup\nalpha is set up here\n\n## Other\nbeta is somewhere else\n"),
    ("Beta guide", "## Setup\nbeta is set up here\n\n## Other\nalpha is somewhere else\n"),
    ("Alpha beta changelog", "## v1\nalpha and beta changed\n"),
    ("Migration notes", f"## Rename\n{ANSWER_LINE}\n"),
    ("Unrelated", "## Overview\nNothing relevant.\n"),
    ("Upgrade steps", "## Steps\nmove alpha settings to beta\n"),
    ("Porting FAQ", "## Questions\nwhy alpha and beta differ\n"),
]
ANSWER_TITLE = "Migration notes"
CHANGELOG_TITLE = "Alpha beta changelog"


def _hits(text: str) -> dict:
    return _common.search_content_in_body(text.splitlines(keepends=True), QUERY, min_level=2)


class _Base:
    """Shared tests; a subclass sets ``module`` / ``script`` / ``write_corpus``."""

    module = None
    script = None
    corpus_args: list

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.write_corpus()

    def run_cmd(self, *args):
        return _loader.run_cli(self.module, [self.script, *args, *self.corpus_args])

    def rows(self, out):
        """Titles of the printed result rows, in order."""
        return [m.group(1) for m in re.finditer(r"^(?:\[\w+\] )?\[\d+\] (.+?)(?: \(index_score|\s+\[body-only\]|$)",
                                                out, re.M)]

    def run_next_lines(self, out):
        """Run every ``Next:`` line as printed; return the outputs."""
        lines = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(lines, out)
        outputs = []
        for line in lines:
            with self.subTest(command=line):
                argv = shlex.split(line, comments=True)
                self.assertEqual(argv[0], self.script, line)
                code, got, err = _loader.run_cli(self.module, argv)
                self.assertEqual(code, 0, err)
                outputs.append(got)
        return outputs

    def next_refs(self, out):
        """Page refs of the ``Next:`` lines (argv[2] of ``content``)."""
        return [shlex.split(ln[len("Next: "):], comments=True)[2]
                for ln in out.splitlines() if ln.startswith("Next: ")]

    def test_search_appends_the_page_with_every_keyword(self):
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "3")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertIn(ANSWER_TITLE, rows, out)
        self.assertIn("[body-only]", out)
        # the rows the index ranked stay, and the answer comes before them
        self.assertIn("Alpha guide", rows)
        self.assertIn("Beta guide", rows)
        self.assertEqual(rows[0], ANSWER_TITLE, out)
        # a partial page the candidates did not include is not dragged in
        self.assertNotIn("Unrelated", rows)

    def test_a_changelog_with_every_keyword_is_not_the_answer(self):
        # the index ranks the changelog first; it has both keywords but is
        # ranked last, so the full-corpus search still runs
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "2")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertIn(ANSWER_TITLE, rows, out)
        self.assertEqual(rows[0], ANSWER_TITLE, out)
        self.assertEqual(rows[-1], CHANGELOG_TITLE, out)
        self.assertNotIn("Unrelated", rows)
        self.run_next_lines(out)

    def test_with_changelog_priority_the_changelog_is_the_answer(self):
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "2", "--include-changelog-priority")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertNotIn("[body-only]", out)
        self.assertEqual(rows[0], CHANGELOG_TITLE, out)
        self.assertNotIn(ANSWER_TITLE, rows)

    def test_at_most_two_pages_are_appended(self):
        # three pages have every keyword outside the index candidates
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "3")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(out.count("[body-only]"), 2, out)
        self.assertEqual(rows[:3], [ANSWER_TITLE, "Upgrade steps", "Alpha guide"], out)
        self.assertNotIn("Porting FAQ", rows)

    def test_the_index_top_keeps_a_next_line(self):
        # the top index candidate (Alpha guide, first row below the appended
        # ones) has a Next: line, and running it reaches that page
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "3")
        self.assertEqual(code, 0, err)
        self.assertIn("0", self.next_refs(out), out)  # page 0 = Alpha guide
        outputs = self.run_next_lines(out)
        self.assertTrue(any("alpha is set up here" in got for got in outputs), outputs)

    def test_search_next_line_reads_the_appended_page(self):
        code, out, err = self.run_cmd("search", QUERY, "--top-n", "2")
        self.assertEqual(code, 0, err)
        first = [ln for ln in out.splitlines() if ln.startswith("Next: ")][0]
        argv = shlex.split(first[len("Next: "):], comments=True)
        self.assertEqual(argv[1], "content")
        code, got, err = _loader.run_cli(self.module, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(ANSWER_LINE, got)
        for got in self.run_next_lines(out):
            self.assertTrue(got)

    def test_single_keyword_search_adds_nothing(self):
        code, out, err = self.run_cmd("search", "alpha", "--top-n", "1")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[body-only]", out)

    def test_search_without_a_page_that_has_every_keyword_adds_nothing(self):
        # "gamma" is nowhere: the partial rows of the index stay as they are
        code, out, err = self.run_cmd("search", "alpha gamma", "--top-n", "3")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[body-only]", out)
        self.assertNotIn(ANSWER_TITLE, self.rows(out))


class _SearchContentOrder:
    def test_search_content_orders_by_keyword_coverage_before_cutting(self):
        code, out, err = self.run_cmd("search-content", QUERY, "--limit", "2")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows[0], ANSWER_TITLE, out)
        self.assertEqual(len(rows), 2, out)
        # the summary still counts every matching page, not just the shown ones
        self.assertRegex(out, r"hits across 6 (documents|pages), showing top 2")

    def test_search_content_ranks_a_changelog_last(self):
        # the changelog comes before the answer in page order
        code, out, err = self.run_cmd("search-content", QUERY, "--limit", "3")
        self.assertEqual(code, 0, err)
        rows = self.rows(out)
        self.assertEqual(rows, [ANSWER_TITLE, "Upgrade steps", "Porting FAQ"], out)

    def test_search_content_with_changelog_priority_keeps_page_order(self):
        code, out, err = self.run_cmd("search-content", QUERY, "--limit", "2",
                                      "--include-changelog-priority")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out), [CHANGELOG_TITLE, ANSWER_TITLE], out)
        self.run_next_lines(out)

    def test_search_content_negative_limit_shows_nothing(self):
        code, out, err = self.run_cmd("search-content", QUERY, "--limit", "-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows(out), [], out)
        self.assertRegex(out, r"hits across 6 (documents|pages), showing top 0")

    def test_search_content_next_line_runs_and_names_the_answer(self):
        code, out, err = self.run_cmd("search-content", QUERY, "--limit", "2")
        self.assertEqual(code, 0, err)
        first = [ln for ln in out.splitlines() if ln.startswith("Next: ")][0]
        argv = shlex.split(first[len("Next: "):], comments=True)
        self.assertEqual(argv[1], "content")
        code, got, err = _loader.run_cli(self.module, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(ANSWER_LINE, got)
        self.run_next_lines(out)


class AiSdkTest(_Base, _SearchContentOrder, unittest.TestCase):
    module = ai_sdk
    script = "parse-ai-sdk.py"

    def write_corpus(self):
        Path(self.tmp, "ai-sdk-llms-full.txt").write_text(
            "".join(f"---\ntitle: {t}\ndescription: about {t}\n---\n\n# {t}\n\n{b}\n" for t, b in PAGES),
            encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]


class ClaudeDocsTest(_Base, _SearchContentOrder, unittest.TestCase):
    module = claude
    script = "parse-claude-docs.py"

    def write_corpus(self):
        urls = [f"https://example.com/en/cli/p{i}" for i in range(len(PAGES))]
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for (t, _b), u in zip(PAGES, urls)), encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "".join(f"# {t}\nSource: {u}\n\n{b}\n" for (t, b), u in zip(PAGES, urls)), encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp, "--source", "code"]

    def rows(self, out):
        return [t.replace(" [body-only]", "") for t in super().rows(out)]


class GenericLoaderTest(_Base, _SearchContentOrder, unittest.TestCase):
    module = generic
    script = "parse-llms-txt.py"

    def write_corpus(self):
        sources = Path(self.tmp, "sources.json")
        sources.write_text(json.dumps({"sources": {
            "demo": {"url": "https://example.com/llms-full.txt", "split": "h1"},
        }}), encoding="utf-8")
        full = Path(self.tmp, "demo-full.txt")
        full.write_text("".join(f"# {t}\n\n{b}\n" for t, b in PAGES), encoding="utf-8")
        self.corpus_args = ["--source", "demo", "--file", str(full), "--sources-file", str(sources)]


class FullCorpusExtraHitsTest(unittest.TestCase):
    """The shared rule, without a CLI."""

    BODIES = [
        "## A\nalpha\n\n## B\nbeta\n",           # 0 partial
        "## A\nalpha alpha\n\n## B\nbeta\n",     # 1 partial
        "## A\nalpha beta together\n",           # 2 strict
        "## A\nnothing\n",                       # 3 no hit
        "## A\nalpha only\n",                    # 4 partial
    ]

    def rows(self, *idxs):
        return [{"doc_idx": i, "body_hits": _hits(self.BODIES[i])} for i in idxs]

    def extra(self, results):
        return _common.full_corpus_extra_hits(
            results, [b.splitlines(keepends=True) for b in self.BODIES], QUERY, limit=5)

    def test_a_strict_candidate_means_nothing_is_added(self):
        self.assertEqual(self.extra(self.rows(2, 0)), [])

    def test_only_partial_candidates_add_only_strict_pages(self):
        got = self.extra(self.rows(0, 1))
        self.assertEqual([i for i, _h in got], [2])

    def test_a_strict_page_already_listed_is_not_added_twice(self):
        # an index-only row (no body hit) for page 2 has rank 2, so the search runs
        results = self.rows(0) + [{"doc_idx": 2, "body_hits": {"total_matches": 0, "results": []}}]
        self.assertEqual(self.extra(results), [])

    def test_no_body_hit_at_all_adds_every_page_with_hits(self):
        results = self.rows(3)
        got = self.extra(results)
        self.assertEqual([i for i, _h in got], [2, 0, 1, 4])

    def test_at_most_two_strict_pages_are_added_to_partial_candidates(self):
        bodies = self.BODIES + ["## A\nalpha beta\n", "## A\nbeta alpha\n"]  # 5, 6 strict
        got = _common.full_corpus_extra_hits(
            self.rows(0), [b.splitlines(keepends=True) for b in bodies], QUERY, limit=5)
        self.assertEqual([i for i, _h in got], [2, 5])

    def test_a_changelog_page_does_not_take_an_appended_slot(self):
        # page 5 has the most strict hits but is a changelog: with titles it
        # ranks last, so the two slots go to the other strict pages
        bodies = self.BODIES + ["## A\nalpha beta\n\n## B\nalpha beta\n\n## C\nalpha beta\n",
                                "## A\nbeta alpha\n"]
        titles = ["p0", "p1", "p2", "p3", "p4", "Changelog", "p6"]
        lines = [b.splitlines(keepends=True) for b in bodies]
        got = _common.full_corpus_extra_hits(self.rows(0), lines, QUERY, limit=5, titles=titles)
        self.assertEqual(sorted(i for i, _h in got), [2, 6])
        got = _common.full_corpus_extra_hits(self.rows(0), lines, QUERY, limit=5, titles=titles,
                                             include_changelog_priority=True)
        self.assertIn(5, [i for i, _h in got])

    def test_top_n_below_two_still_caps_the_strict_pages(self):
        got = _common.full_corpus_extra_hits(
            self.rows(0), [b.splitlines(keepends=True) for b in self.BODIES], QUERY, limit=1)
        self.assertEqual([i for i, _h in got], [2])

    def test_a_strict_changelog_candidate_does_not_stop_the_search(self):
        results = self.rows(0, 2)
        results[1]["title"] = "Release notes"
        self.assertEqual(self.extra(results), [])  # page 2 is listed: nothing new
        bodies = self.BODIES + ["## A\nalpha beta\n"]  # 5 strict, not listed
        got = _common.full_corpus_extra_hits(
            results, [b.splitlines(keepends=True) for b in bodies], QUERY, limit=1)
        # the listed changelog does not use the one slot
        self.assertEqual([i for i, _h in got], [5])
        got = _common.full_corpus_extra_hits(
            results, [b.splitlines(keepends=True) for b in bodies], QUERY, limit=1,
            include_changelog_priority=True)
        self.assertEqual(got, [])

    def test_partial_candidates_and_no_strict_page_in_the_corpus(self):
        bodies = [b for i, b in enumerate(self.BODIES) if i != 2]
        results = [{"doc_idx": 0, "body_hits": _hits(bodies[0])}]
        got = _common.full_corpus_extra_hits(
            results, [b.splitlines(keepends=True) for b in bodies], QUERY, limit=5)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
