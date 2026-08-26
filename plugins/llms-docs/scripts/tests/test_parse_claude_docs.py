"""Fixture-based tests for scripts/parse-claude-docs.py.

Covers ``split_documents`` fixtures named in the 2026-08 audit (H1 inside a
code fence must not split; Platform's duplicate-H1 pattern must merge;
``Source:``/``URL:`` extraction) plus CLI-level tests for the upstream
format-change detection and the search-fallback fix — using pre-seeded
cache-dir fixtures so no network access is needed (``fetch_url`` short-
circuits when the cache file already exists and is younger than
``--max-age``).
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

parse_claude_docs = _loader.load_script("parse-claude-docs.py")


def _write_fixture(cache_dir, index_text, full_text):
    Path(cache_dir, "claude-code-llms.txt").write_text(index_text, encoding="utf-8")
    Path(cache_dir, "claude-code-llms-full.txt").write_text(full_text, encoding="utf-8")


class _FakeResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` returns."""

    def __init__(self, data, headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


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


class SearchToContentRoundTripTest(unittest.TestCase):
    """bd 2wd.15: 'search' can report a body hit above a page's first
    heading as 'Section: (top)', and the tool's own documented flow (see
    SKILL.md Quick Start) is to copy a search/content result straight into
    the next 'content' call. This must actually work end-to-end — not just
    at the extract_content unit level (see test_common.py) — and the
    display's clarifying suffix (bd 2wd.15's "(top: before first heading)"
    request, implemented as a separate bracketed annotation so the raw
    value stays copy-paste-safe) must not corrupt what gets copied."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            "# Hooks\n"
            "Source: https://example.com/hooks\n"
            "\n"
            "Intro text mentioning zzzpreambleterm before any heading.\n"
            "\n"
            "## Configuration\n"
            "Configure hook matchers here.\n",
        )

    def test_top_hit_is_displayed_with_clarifying_suffix(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search", "zzzpreambleterm",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Section: (top)  [before first heading]", out)

    def test_top_value_copied_into_content_resolves_the_preamble(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "content", "0", "(top)",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("zzzpreambleterm", out)
        self.assertIn("heading_path: (top)", out)
        self.assertNotIn("Configure hook matchers here", out)


class MaxAgeBoundaryIntegrationTest(unittest.TestCase):
    """bd 2wd.12: --max-age must gate the re-fetch decision through the
    WHOLE CLI pipeline, exercised here through 'fetch-index' with a mocked
    urlopen so the age < max_age boundary is deterministic (no reliance on
    real elapsed time or network access)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.index_path = Path(self.tmp, "claude-code-llms.txt")
        self.index_path.write_text(
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )

    def _age_cache(self, seconds_old):
        t = time.time() - seconds_old
        os.utime(self.index_path, (t, t))

    def test_cache_younger_than_max_age_is_not_refetched(self):
        self._age_cache(100)
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            code, out, err = _loader.run_cli(parse_claude_docs, [
                "parse-claude-docs.py", "fetch-index",
                "--cache-dir", self.tmp, "--max-age", "200",
            ])
        self.assertEqual(code, 0, err)
        mock_urlopen.assert_not_called()

    def test_cache_older_than_max_age_is_refetched(self):
        self._age_cache(300)
        fresh = _FakeResponse(
            b"- [Hooks](https://example.com/hooks): Configure hook matchers\n"
        )
        with mock.patch("urllib.request.urlopen", return_value=fresh) as mock_urlopen:
            code, out, err = _loader.run_cli(parse_claude_docs, [
                "parse-claude-docs.py", "fetch-index",
                "--cache-dir", self.tmp, "--max-age", "200",
            ])
        self.assertEqual(code, 0, err)
        mock_urlopen.assert_called_once()


class FileReadOnlyModeTest(unittest.TestCase):
    """bd 2wd.12: --file must be read-only — no fetch, no overwrite — and a
    missing path or a source/path mismatch must fail with a clear message
    rather than a silent wrong-source read or an unhandled crash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_file_flag_is_used_verbatim_without_fetching(self):
        snapshot = Path(self.tmp, "my-snapshot.txt")
        snapshot.write_text(
            "# Hooks\nSource: https://example.com/hooks\n\nbody text\n",
            encoding="utf-8",
        )
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            code, out, err = _loader.run_cli(parse_claude_docs, [
                "parse-claude-docs.py", "sections", "0",
                "--file", str(snapshot), "--cache-dir", self.tmp,
            ])
        self.assertEqual(code, 0, err)
        mock_urlopen.assert_not_called()
        self.assertIn("Hooks", out)

    def test_missing_file_dies_with_clear_message(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "sections", "0",
            "--file", os.path.join(self.tmp, "does-not-exist.txt"),
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_file_looking_like_other_source_cache_is_rejected(self):
        mismatched = Path(self.tmp, "claude-platform-llms-full.txt")
        mismatched.write_text("# Page\nSource: https://x\n\nbody\n", encoding="utf-8")
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "sections", "0",
            "--file", str(mismatched), "--source", "code",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 1)
        self.assertIn("platform", err)


class ArgparseErrorExitCodeTest(unittest.TestCase):
    """bd 2wd.12: usage errors (argparse's own exit 2) must not be confused
    with this plugin's exit 1 ("normal" failure) or exit 2 ("upstream
    format may have changed") — argparse happens to also use 2, but for a
    different reason (bad invocation vs. bad data), so pin both here."""

    def test_missing_required_query_argument_exits_2(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index",
        ])
        self.assertEqual(code, 2)

    def test_unknown_subcommand_exits_2(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "not-a-real-subcommand",
        ])
        self.assertEqual(code, 2)


class GoldenOutputTest(unittest.TestCase):
    """bd 2wd.12: pin one representative command's FULL stdout verbatim so
    a future change to this independently-implemented output format shows
    up as an explicit, intentional diff here instead of silent drift
    (each of the 3 parse-*.py scripts formats its own output; see the
    ai-sdk/firebase counterparts of this test)."""

    def test_sections_full_output_is_pinned(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            "# Hooks\n"
            "Source: https://example.com/hooks\n"
            "\n"
            "## Configuration\n"
            "text\n"
            "\n"
            "## Advanced\n"
            "more text\n",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "sections", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        expected = (
            'Sections in [0] "Hooks"\n'
            "  URL: https://example.com/hooks\n"
            + "=" * 60 + "\n"
            + "[L2] Configuration\n"
            "[L2] Advanced\n"
            "\n"
            "(2 sections)\n"
            "\n"
            'Next: parse-claude-docs.py content 0 "<heading_path>"\n'
        )
        self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()
