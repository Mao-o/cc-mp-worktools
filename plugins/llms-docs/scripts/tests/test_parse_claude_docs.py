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
    """'search' can report a body hit above a page's first heading as
    'Section: (top)', and the tool's own documented flow (see SKILL.md
    Quick Start) is to copy a search/content result straight into the next
    'content' call. This must actually work end-to-end — not just at the
    extract_content unit level (see test_common.py) — and the display's
    clarifying suffix ("(top)  [before first heading]", a separate
    bracketed annotation chosen so the raw value stays copy-paste-safe)
    must not corrupt what gets copied."""

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
    """--max-age must gate the re-fetch decision through the
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
    """--file must be read-only — no fetch, no overwrite — and a
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
    """Usage errors (argparse's own exit 2) must not be confused
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
    """Pin one representative command's FULL stdout verbatim so
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


class SectionsHeadingPathTest(unittest.TestCase):
    """'sections' printed the bare title, not the heading_path it is
    documented (SKILL.md) as safe to copy straight into content's
    heading_path argument. Two sibling subsections sharing a title under
    different parents (e.g. "Examples" under two different H2s) were
    indistinguishable in the listing even though only the full path
    identifies which one 'content' would actually resolve."""

    def test_nested_sections_show_full_path_not_ambiguous_bare_title(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "- [Guide](https://example.com/guide): Guide\n",
            "# Guide\n"
            "Source: https://example.com/guide\n"
            "\n"
            "## Client\n"
            "text\n"
            "### Examples\n"
            "client examples\n"
            "\n"
            "## Server\n"
            "text\n"
            "### Examples\n"
            "server examples\n",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "sections", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[L3] Client/Examples", out)
        self.assertIn("[L3] Server/Examples", out)
        # The old bug printed just "Examples" for both — no longer present
        # as a standalone (non-prefixed) heading label.
        self.assertNotIn("[L3] Examples", out)


class FetchIndexDocIdxJoinTest(unittest.TestCase):
    """fetch-index showed the llms.txt list POSITION as `[N]`,
    but 'sections'/'content' resolve integer refs against the llms-full.txt
    DOC_IDX — a different numbering whenever the two files aren't in the
    same order. Passing the displayed [N] through silently opened the
    wrong page. fetch-index must never eagerly fetch llms-full.txt just to
    compute this label (that would make the documented *lightweight*
    fallback command the heaviest one in the tool for 'platform')."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_shows_slug_and_does_not_fetch_full_text_when_uncached(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks]", out)
        self.assertNotIn("[0]", out)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "claude-code-llms-full.txt"))
        )

    def test_shows_joined_full_text_doc_idx_when_full_text_already_cached(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        # llms-full.txt order deliberately differs from llms.txt order —
        # this is exactly the case that made the old raw-position label
        # wrong (Skills is llms.txt entry 0 but llms-full.txt doc 1, and
        # vice versa for Hooks).
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
            "# Skills\nSource: https://example.com/en/skills\n\nbody\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Skills", out)
        self.assertIn("[0] Hooks", out)

    def test_grouped_variants_show_per_variant_ref_not_misleading_range(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Batches (Python)](https://example.com/en/batches-py): Python batches\n"
            "- [Batches (Go)](https://example.com/en/batches-go): Go batches\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Batches", out)
        self.assertIn("Python [batches-py]", out)
        self.assertIn("Go [batches-go]", out)
        # No misleading numeric range bracket like "[0-1]" (an llms.txt
        # list position, not a valid sections/content doc_idx).
        self.assertNotIn("[0-1]", out)

    def test_falls_back_to_slug_when_full_text_cache_is_stale(self):
        # llms-full.txt exists but is older than --max-age: a later
        # sections/content call (passed this same --max-age) will actually
        # refetch it. Joining against this about-to-be-replaced numbering
        # would silently reproduce the exact drift this feature exists to
        # prevent, so a stale cache must be treated the same as "uncached".
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        full_path = Path(self.tmp, "claude-code-llms-full.txt")
        full_path.write_text(
            "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
            "# Skills\nSource: https://example.com/en/skills\n\nbody\n",
            encoding="utf-8",
        )
        stale = time.time() - 1000
        os.utime(full_path, (stale, stale))
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
            "--max-age", "100",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[skills] Skills", out)
        self.assertIn("[hooks] Hooks", out)
        self.assertNotIn("[0]", out)
        self.assertNotIn("[1]", out)

    def test_fresh_full_text_cache_within_max_age_still_joins(self):
        # The negative case for the test above: a cache younger than
        # --max-age is exactly what fetch_url would also keep using, so
        # the join must still happen (this isn't "never join with
        # --max-age set", only "not with a cache old enough to refetch").
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
            "# Skills\nSource: https://example.com/en/skills\n\nbody\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "fetch-index", "--cache-dir", self.tmp,
            "--max-age", "1000",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Skills", out)
        self.assertIn("[0] Hooks", out)


class SearchIndexDocIdxJoinTest(unittest.TestCase):
    """search-index had the same raw-list-position bug as
    fetch-index, papered over with an unconditional disclaimer note
    instead of a fix. Now it joins when llms-full.txt is already cached
    (dropping the note, since the numbering is then actually correct) and
    falls back to a slug + a conditional note otherwise."""

    def test_uses_slug_and_shows_note_when_full_text_uncached(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "claude-code-llms.txt").write_text(
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index", "hooks", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertIn("Note:", out)

    def test_uses_joined_doc_idx_and_omits_note_when_full_text_cached(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "claude-code-llms.txt").write_text(
            "- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        Path(tmp, "claude-code-llms-full.txt").write_text(
            "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
            "# Skills\nSource: https://example.com/en/skills\n\nbody\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index", "hooks", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Hooks", out)
        self.assertNotIn("Note:", out)

    def test_falls_back_to_slug_and_shows_note_when_full_text_cache_is_stale(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "claude-code-llms.txt").write_text(
            "- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
            "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n",
            encoding="utf-8",
        )
        full_path = Path(tmp, "claude-code-llms-full.txt")
        full_path.write_text(
            "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
            "# Skills\nSource: https://example.com/en/skills\n\nbody\n",
            encoding="utf-8",
        )
        stale = time.time() - 1000
        os.utime(full_path, (stale, stale))
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "search-index", "hooks", "--cache-dir", tmp,
            "--max-age", "100",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertIn("Note:", out)


class ContentMaxCharsTruncationTest(unittest.TestCase):
    """content had no output-size cap, so a large page (Platform
    pages average ~38KB) silently overflowed the Bash tool's ~30KB inline-
    output threshold, hiding the subsection hint / Next line at the end."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            "# Hooks\n"
            "Source: https://example.com/hooks\n"
            "\n"
            "0123456789 0123456789 0123456789 0123456789 0123456789\n",
        )

    def test_content_longer_than_max_chars_is_truncated_with_narrow_hint(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "content", "0",
            "--cache-dir", self.tmp, "--max-chars", "20",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("chars truncated", out)
        self.assertIn('narrow with parse-claude-docs.py content 0 "<heading_path>"', out)
        self.assertNotIn("0123456789 0123456789 0123456789 0123456789 0123456789", out)

    def test_max_chars_zero_disables_truncation(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "content", "0",
            "--cache-dir", self.tmp, "--max-chars", "0",
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("chars truncated", out)
        self.assertIn("0123456789 0123456789 0123456789 0123456789 0123456789", out)


class ContentSubsectionHintBeforeAndAfterTest(unittest.TestCase):
    """The subsection hint / Next line, previously printed only
    AFTER the body, is now ALSO printed right after the metadata header
    (before the body) so it survives truncation regardless of where the
    cut lands — not just when --max-chars happens to catch it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "- [Hooks](https://example.com/hooks): Configure hook matchers\n",
            "# Hooks\n"
            "Source: https://example.com/hooks\n"
            "\n"
            "## Configuration\n"
            "short body text\n",
        )

    def test_hint_and_next_line_appear_both_before_and_after_body(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "content", "0", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("--- Top-level sections (1) ---"), 2)
        self.assertEqual(
            out.count('Next: parse-claude-docs.py content 0 "<heading_path from above>"'),
            2,
        )
        # The first hint occurrence must come before the body text, and the
        # second after it — not both on the same side.
        hint_pos = out.index("--- Top-level sections (1) ---")
        body_pos = out.index("short body text")
        second_hint_pos = out.index("--- Top-level sections (1) ---", hint_pos + 1)
        self.assertLess(hint_pos, body_pos)
        self.assertGreater(second_hint_pos, body_pos)

    def test_no_subsection_hints_suppresses_both_occurrences(self):
        code, out, err = _loader.run_cli(parse_claude_docs, [
            "parse-claude-docs.py", "content", "0",
            "--cache-dir", self.tmp, "--no-subsection-hints",
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Top-level sections", out)
        self.assertNotIn("Next:", out)


if __name__ == "__main__":
    unittest.main()
