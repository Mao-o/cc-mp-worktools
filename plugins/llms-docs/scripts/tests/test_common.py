"""Fixture-based tests for scripts/_common.py shared helpers.

Covers the fixtures identified during the 2026-08 plugin audit: index
bullet-marker variants, heading-level skips, code-fence / table protection
in content extraction, keyword scoring, AND-to-partial search fallback,
overflow reporting, and the format-change detection helpers
(assert_parsed / check_join_rate / full_corpus_body_search).
"""

import unittest

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

import _common


class ParseLlmsIndexTest(unittest.TestCase):
    def test_colon_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks): Configure hook events\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries, [{
            "title": "Hooks",
            "url": "https://example.com/hooks",
            "description": "Configure hook events",
        }])

    def test_dash_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks) - Configure hook events\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries[0]["description"], "Configure hook events")

    def test_no_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks)\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries, [{
            "title": "Hooks", "url": "https://example.com/hooks", "description": "",
        }])

    def test_asterisk_bullet_accepted(self):
        lines = ["* [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Hooks")

    def test_plus_bullet_accepted(self):
        lines = ["+ [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)

    def test_indented_bullet_accepted(self):
        lines = ["  - [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)

    def test_non_ascii_title_and_description(self):
        lines = ["- [フック](https://example.com/hooks): フックイベントの設定\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries[0]["title"], "フック")
        self.assertEqual(entries[0]["description"], "フックイベントの設定")

    def test_asterisk_prose_line_without_link_produces_no_entry(self):
        # A plain '* some text' bullet (no markdown link) must not become a
        # spurious entry now that '*' is accepted as a bullet marker.
        lines = ["* just some text, no link here\n"]
        self.assertEqual(_common.parse_llms_index(lines), [])

    def test_ignores_non_bullet_lines(self):
        lines = ["# Heading\n", "\n", "Some prose.\n"]
        self.assertEqual(_common.parse_llms_index(lines), [])


class ExtractSectionsTest(unittest.TestCase):
    def test_level_skip_h2_to_h4(self):
        body = [
            "## Top\n",
            "text\n",
            "#### Deep\n",
            "deep text\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["heading_path"] for s in sections], ["Top", "Top/Deep"])
        self.assertEqual(sections[1]["level"], 4)

    def test_tilde_fence_is_not_recognized_known_limitation(self):
        """Characterization test, not a spec assertion.

        ``FenceTracker`` only recognizes backtick fences (```` ``` ````); a
        CommonMark tilde fence (``~~~``) is not tracked, so a heading-shaped
        line inside one is still collected as a real section. Discovered
        while building this fixture suite — not covered by any existing
        backlog item, and deliberately not fixed here: widening
        ``FenceTracker`` changes document-boundary detection for all three
        sources (affects ``split_documents`` in claude-docs / ai-sdk too,
        not just this function) and needs a real-corpus before/after diff
        per this repo's regression discipline, not just a synthetic
        fixture. This test pins the current (imperfect) behavior so a
        future deliberate fix changes it on purpose.
        """
        body = [
            "## Real\n",
            "~~~\n",
            "## Not a heading\n",
            "~~~\n",
            "## Also Real\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual(
            [s["title"] for s in sections], ["Real", "Not a heading", "Also Real"]
        )


class ExtractContentTest(unittest.TestCase):
    def test_extends_to_close_unclosed_code_fence(self):
        body = [
            "## Section A\n",
            "```python\n",
            "x = 1\n",
            "## Section B\n",  # inside the fence: not a real heading
            "```\n",
            "## Section C\n",
        ]
        content = _common.extract_content(body, "Section A")
        self.assertIn("x = 1", content)
        self.assertIn("## Section B", content)  # still inside the fenced block
        self.assertNotIn("Section C", content)

    def test_extends_to_include_straddling_table(self):
        body = [
            "## Section A\n",
            "text\n",
            "## Section B\n",
            "| a | b |\n",
            "|---|---|\n",
            "| 1 | 2 |\n",
            "## Section C\n",
            "more\n",
        ]
        content = _common.extract_content(body, "Section B")
        self.assertIn("| 1 | 2 |", content)
        self.assertNotIn("Section C", content)

    def test_full_document_when_heading_path_none(self):
        body = ["## A\n", "x\n"]
        self.assertEqual(_common.extract_content(body, None), "".join(body))


class ScoreEntryTest(unittest.TestCase):
    def test_title_exact_match_scores_highest(self):
        self.assertEqual(_common.score_entry("Hooks", "", ["hooks"]), 10)

    def test_plural_mismatch_scores_zero_known_limitation(self):
        # Characterization test: score_entry does no stemming, so a plural
        # query does not match a singular title (tracked separately as a
        # search-quality improvement).
        self.assertEqual(_common.score_entry("Hook events", "", ["hooks"]), 0)

    def test_title_substring_match(self):
        self.assertGreater(_common.score_entry("Hooks reference", "", ["hooks"]), 0)

    def test_all_keyword_bonus(self):
        multi = _common.score_entry("Hook events", "matcher config", ["hook", "matcher"])
        single = _common.score_entry("Hook events", "matcher config", ["hook"])
        self.assertGreater(multi, single)


class SearchContentInBodyTest(unittest.TestCase):
    def _body(self):
        return [
            "## Section A\n",
            "alpha bravo\n",
            "## Section B\n",
            "alpha only\n",
        ]

    def test_and_match_mode_when_all_keywords_in_one_section(self):
        result = _common.search_content_in_body(self._body(), "alpha bravo")
        self.assertEqual(result["match_mode"], "and")
        self.assertEqual(result["results"][0]["heading_path"], "Section A")

    def test_falls_back_to_partial_when_no_section_has_all_keywords(self):
        result = _common.search_content_in_body(self._body(), "bravo charlie")
        # "charlie" appears nowhere, so strict AND finds 0 sections; partial
        # (>= ceil(2/2)=1 of 2 keywords) should surface "Section A" (has
        # "bravo").
        self.assertEqual(result["match_mode"], "partial")
        self.assertTrue(any(r["heading_path"] == "Section A" for r in result["results"]))

    def test_overflow_sections_reported_when_over_limit(self):
        body = []
        for i in range(5):
            body.append(f"## Section {i}\n")
            body.append("keyword hit\n")
        result = _common.search_content_in_body(body, "keyword", max_matches_per_doc=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(result["overflow_sections"]), 3)


class AssertParsedTest(unittest.TestCase):
    def test_zero_count_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            _common.assert_parsed("test-source", 0, "/tmp/fixture.txt")
        self.assertEqual(cm.exception.code, 2)

    def test_nonzero_count_does_not_exit(self):
        _common.assert_parsed("test-source", 5, "/tmp/fixture.txt")  # must not raise


class CheckJoinRateTest(unittest.TestCase):
    def test_below_fail_threshold_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            _common.check_join_rate("test", 1, 10)  # 10%
        self.assertEqual(cm.exception.code, 2)

    def test_between_warn_and_fail_thresholds_warns_but_continues(self):
        # 70%: below the 80% warn threshold, above the 50% fail threshold.
        _common.check_join_rate("test", 7, 10)  # must not raise

    def test_above_warn_threshold_is_silent(self):
        _common.check_join_rate("test", 9, 10)  # must not raise

    def test_zero_total_is_noop(self):
        _common.check_join_rate("test", 0, 0)  # must not raise


class FullCorpusBodySearchTest(unittest.TestCase):
    def test_ranks_by_total_matches_and_skips_docs_with_no_hits(self):
        docs_body_lines = [
            ["## A\n", "nothing here\n"],
            ["## B\n", "target target target\n"],
            ["## C\n", "target once\n"],
        ]
        results = _common.full_corpus_body_search(docs_body_lines, "target", limit=5)
        self.assertEqual([idx for idx, _ in results], [1, 2])

    def test_limit_truncates_results(self):
        docs_body_lines = [["## D%d\n" % i, "target\n"] for i in range(5)]
        results = _common.full_corpus_body_search(docs_body_lines, "target", limit=2)
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
