"""Fixture-based tests for scripts/parse-claude-docs.py.

Covers ``split_documents`` fixtures named in the 2026-08 audit (H1 inside a
code fence must not split; Platform's duplicate-H1 pattern must merge;
``Source:``/``URL:`` extraction) plus CLI-level tests for the upstream
format-change detection and the search-fallback fix — using pre-seeded
cache-dir fixtures so no network access is needed (``fetch_url`` short-
circuits when the cache file already exists and is younger than
``--max-age``).
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

parse_claude_docs = _loader.load_script("parse-claude-docs.py")


def _write_fixture(cache_dir, index_text, full_text):
    Path(cache_dir, "claude-code-llms.txt").write_text(index_text, encoding="utf-8")
    Path(cache_dir, "claude-code-llms-full.txt").write_text(full_text, encoding="utf-8")


class SplitDocumentsTest(unittest.TestCase):
    def test_h1_inside_fence_is_not_a_boundary(self):
        lines = [
            "# Hooks\n",
            "Source: https://example.com/hooks\n",
            "\n",
            "```bash\n",
            "# not a real heading\n",
            "echo hi\n",
            "```\n",
            "\n",
            "# Skills\n",
            "Source: https://example.com/skills\n",
        ]
        docs = parse_claude_docs.split_documents(lines)
        self.assertEqual([d["title"] for d in docs], ["Hooks", "Skills"])
        self.assertIn("# not a real heading", "".join(docs[0]["body_lines"]))

    def test_platform_duplicate_h1_merges_short_url_only_doc(self):
        lines = [
            "# Overview\n",
            "URL: https://platform.example.com/overview\n",
            "# Overview\n",
            "Source: https://platform.example.com/overview\n",
            "\n",
            "## Introduction\n",
            "Real content.\n",
        ]
        docs = parse_claude_docs.split_documents(lines)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["title"], "Overview")
        self.assertEqual(docs[0]["source_url"], "https://platform.example.com/overview")
        self.assertIn("Real content.", "".join(docs[0]["body_lines"]))

    def test_source_url_extracted_from_source_line(self):
        lines = ["# Page\n", "Source: https://example.com/page\n", "body\n"]
        docs = parse_claude_docs.split_documents(lines)
        self.assertEqual(docs[0]["source_url"], "https://example.com/page")

    def test_source_url_extracted_from_url_line(self):
        lines = ["# Page\n", "URL: https://example.com/page\n", "body\n"]
        docs = parse_claude_docs.split_documents(lines)
        self.assertEqual(docs[0]["source_url"], "https://example.com/page")

    def test_no_documents_when_no_h1_present(self):
        lines = ["not a heading\n", "still not one\n"]
        self.assertEqual(parse_claude_docs.split_documents(lines), [])


class CmdSearchFallbackTest(unittest.TestCase):
    """A term that lives only in a page body must still be found by
    'search', not just 'search-content'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n"
            "- [Skills](https://example.com/skills): Package reusable capabilities\n",
            "# Hooks\n"
            "Source: https://example.com/hooks\n"
            "\n"
            "## Configuration\n"
            "Configure hook matchers here. Nothing special in this section.\n"
            "\n"
            "# Skills\n"
            "Source: https://example.com/skills\n"
            "\n"
            "## Overview\n"
            "Skills let you package zzzonlyinbody reusable capabilities.\n",
        )

    def test_body_only_term_falls_back_to_full_corpus_search(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "zzzonlyinbody",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[body-only]", out)
        self.assertIn("Skills", out)

    def test_title_match_does_not_trigger_fallback(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "hooks",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("body-only", out)


class CmdSearchFallbackDoesNotDropIndexMatchesTest(unittest.TestCase):
    """A page correctly ranked by title, but with zero literal body hits,
    must still appear (as "index match only") — the fallback must APPEND
    body-only hits from other pages, not replace the whole result set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "- [Hooks](https://example.com/h1): Configure hook matchers\n"
            "- [Skills](https://example.com/skills): Package reusable capabilities\n",
            "# Hooks\n"
            "Source: https://example.com/h1\n"
            "\n"
            "## Configuration\n"
            "This page body never mentions the h-o-o-k-s term at all.\n"
            "\n"
            "# Skills\n"
            "Source: https://example.com/skills\n"
            "\n"
            "## Overview\n"
            "Skills let you package hooks (the term lives only here) reusable capabilities.\n",
        )

    def test_index_matched_zero_body_hit_page_is_kept_alongside_fallback(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "hooks",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Hooks (index_score:", out)
        self.assertIn("index match only", out)
        self.assertIn("Skills [body-only]", out)
        # The summary note is only for the "nothing ranked at all" case —
        # it must not appear when a real index match is present.
        self.assertNotIn("no title/description match", out)


class AssertParsedIntegrationTest(unittest.TestCase):
    """A malformed/empty source must exit 2 (format changed), distinct
    from a legitimately-empty search result (exit 0)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_malformed_index_exits_2_not_0(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "not a valid llms.txt line at all\n", encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 2)
        self.assertIn("format may have changed", err)

    def test_legitimately_empty_search_result_still_exits_0(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index", "totallyabsentkeyword",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("No matching pages found", out)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index", "totallyabsentkeyword",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("search-content", out)


class JoinRateHardFailTest(unittest.TestCase):
    """A badly broken URL join (< 50%) is a format-change signal, not a
    handful of legitimately-missing pages — exit 2."""

    def test_low_join_rate_exits_2(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "- [A](https://example.com/a): desc a\n"
            "- [B](https://example.com/b): desc b\n"
            "- [C](https://example.com/c): desc c\n"
            "- [D](https://example.com/d): desc d\n",
            "# A\n"
            "Source: https://example.com/a\n"
            "\n"
            "body a\n",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "desc", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 2)
        self.assertIn("join rate", err)

    def test_high_join_rate_does_not_fail(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "- [A](https://example.com/a): desc a\n",
            "# A\n"
            "Source: https://example.com/a\n"
            "\n"
            "body a\n",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "desc", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
