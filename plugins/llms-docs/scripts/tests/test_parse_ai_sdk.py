"""Fixture-based tests for scripts/parse-ai-sdk.py.

Covers ``split_documents`` fixtures named in the 2026-08 audit (leading
non-frontmatter content skipped; ``---`` inside a code fence must not
split) plus CLI-level tests for the upstream format-change detection, the
untitled-ratio warning, and the search-fallback fix — using pre-seeded
cache-dir fixtures so no network access is needed.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

parse_ai_sdk = _loader.load_script("parse-ai-sdk.py")


def _write_fixture(cache_dir, full_text):
    Path(cache_dir, "ai-sdk-llms-full.txt").write_text(full_text, encoding="utf-8")


class SplitDocumentsTest(unittest.TestCase):
    def test_leading_non_frontmatter_content_is_skipped(self):
        lines = [
            "some typescript contributing guide\n",
            "const x = 1;\n",
            "---\n",
            "title: Doc\n",
            "---\n",
            "# Doc\n",
            "body\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["frontmatter_lines"], ["title: Doc\n"])

    def test_hr_inside_code_fence_is_not_a_boundary(self):
        lines = [
            "---\n",
            "title: Doc\n",
            "---\n",
            "# Doc\n",
            "```\n",
            "---\n",
            "```\n",
            "## After\n",
            "more text\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual(len(docs), 1)
        self.assertIn("## After", "".join(docs[0]["body_lines"]))

    def test_kv_like_prose_around_hr_causes_false_split_known_limitation(self):
        """Characterization test, not a spec assertion.

        A body ``---`` horizontal rule followed by prose that happens to
        look like ``Key: value`` (e.g. ``Note: ...``) within 30 lines of a
        second ``---`` is misdetected by ``_looks_like_frontmatter_start``
        as a new frontmatter block, splitting one logical document into
        two. This is the known ai-sdk "false split" limitation (tracked
        separately in the internal backlog as a P3 parser fix, out of
        scope for this batch) — this test only pins the current behavior
        so a future fix changes it deliberately.
        """
        lines = [
            "---\n",
            "title: streamText\n",
            "---\n",
            "# streamText\n",
            "\n",
            "## Options\n",
            "before\n",
            "\n",
            "---\n",
            "Note: this is prose, not frontmatter\n",
            "---\n",
            "\n",
            "## Options continued\n",
            "after\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual(len(docs), 2)
        self.assertNotIn("Options continued", "".join(docs[0]["body_lines"]))

    def test_no_documents_when_no_frontmatter_delimiters_present(self):
        lines = ["no frontmatter delimiters anywhere in this file\n"]
        self.assertEqual(parse_ai_sdk.split_documents(lines), [])


class ParseFrontmatterTest(unittest.TestCase):
    def test_extracts_title_description_tags(self):
        fm_lines = [
            "title: streamText\n",
            "description: Stream text generation\n",
            "tags: [core, streaming]\n",
        ]
        fm = parse_ai_sdk.parse_frontmatter(fm_lines)
        self.assertEqual(fm["title"], "streamText")
        self.assertEqual(fm["description"], "Stream text generation")
        self.assertEqual(fm["tags"], ["core", "streaming"])

    def test_missing_title_yields_empty_string(self):
        fm = parse_ai_sdk.parse_frontmatter(["description: only desc\n"])
        self.assertEqual(fm["title"], "")


class CmdSearchFallbackTest(unittest.TestCase):
    """A term that lives only in a doc body must still be found by
    'search', not just 'search-content'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "---\n"
            "title: streamText\n"
            "description: Stream text generation\n"
            "---\n"
            "\n"
            "# streamText\n"
            "\n"
            "## onFinish\n"
            "This callback fires onFinishCallbackOnly in the body only.\n"
            "\n"
            "---\n"
            "title: generateText\n"
            "description: Generate text once\n"
            "---\n"
            "\n"
            "# generateText\n"
            "\n"
            "## Usage\n"
            "Basic usage.\n",
        )

    def test_body_only_term_falls_back(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search", "onFinishCallbackOnly",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[body-only]", out)
        self.assertIn("streamText", out)

    def test_title_match_does_not_fall_back(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search", "streamText",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("body-only", out)


class CmdSearchFallbackDoesNotDropIndexMatchesTest(unittest.TestCase):
    """A doc correctly ranked by description, but with zero literal body
    hits, must still appear (as "index match only") alongside body-only
    fallback hits from other docs — not be replaced by them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "---\n"
            "title: streamText\n"
            "description: Stream text generation\n"
            "---\n"
            "\n"
            "# streamText\n"
            "\n"
            "## Options\n"
            "Configure streaming options here.\n"
            "\n"
            "---\n"
            "title: generateText\n"
            "description: Generate text once\n"
            "---\n"
            "\n"
            "# generateText\n"
            "\n"
            "## Usage\n"
            "This section mentions generation eventOnlyInBody as an example term.\n",
        )

    def test_description_matched_zero_body_hit_doc_is_kept_alongside_fallback(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search", "generation", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("streamText (index_score:", out)
        self.assertIn("index match only", out)
        self.assertIn("generateText [body-only]", out)
        self.assertNotIn("no title/description/tags match", out)


class AssertParsedIntegrationTest(unittest.TestCase):
    """A malformed/empty source must exit 2 (format changed), distinct
    from a legitimately-empty search result (exit 0)."""

    def test_malformed_full_text_exits_2(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(tmp, "no frontmatter delimiters anywhere in this file\n")
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "fetch-index", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 2)
        self.assertIn("format may have changed", err)

    def test_legitimately_empty_search_result_still_exits_0(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(tmp, "---\ntitle: Doc\n---\n\n# Doc\n\nbody\n")
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search-index", "totallyabsentkeyword",
            "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("No matching documents found", out)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(tmp, "---\ntitle: Doc\n---\n\n# Doc\n\nbody\n")
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search-index", "totallyabsentkeyword",
            "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("search-content", out)


class UntitledRatioWarningTest(unittest.TestCase):
    """A high proportion of untitled docs usually means split_documents
    mis-split the file, not that upstream lacks titles."""

    def test_high_untitled_ratio_warns(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "---\ntitle: Doc1\n---\n\n# Doc1\n\nbody\n"
            "---\nno_title_field: true\n---\n\n# Doc2\n\nbody\n",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "fetch-index", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertIn("no title", err)

    def test_low_untitled_ratio_is_silent(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        docs_text = "".join(
            f"---\ntitle: Doc{i}\n---\n\n# Doc{i}\n\nbody\n" for i in range(9)
        )
        docs_text += "---\nno_title_field: true\n---\n\n# Doc9\n\nbody\n"
        _write_fixture(tmp, docs_text)
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "fetch-index", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", err)


class FileReadOnlyModeTest(unittest.TestCase):
    """--file must be read-only — no fetch, no overwrite — and a
    missing path must fail with a clear message. Unlike claude-docs, ai-sdk
    has a single source, so there is no source/path mismatch case here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_file_flag_is_used_verbatim_without_fetching(self):
        snapshot = Path(self.tmp, "my-snapshot.txt")
        snapshot.write_text(
            "---\ntitle: Doc\n---\n\n# Doc\n\nbody\n", encoding="utf-8",
        )
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            code, out, err = _loader.run_cli(parse_ai_sdk, [
                "parse-ai-sdk.py", "sections", "0",
                "--file", str(snapshot), "--cache-dir", self.tmp,
            ])
        self.assertEqual(code, 0, err)
        mock_urlopen.assert_not_called()
        self.assertIn("Doc", out)

    def test_missing_file_dies_with_clear_message(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "sections", "0",
            "--file", os.path.join(self.tmp, "does-not-exist.txt"),
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


class GoldenOutputTest(unittest.TestCase):
    """Pin one representative command's FULL stdout verbatim so
    a future change to this independently-implemented output format shows
    up as an explicit, intentional diff here instead of silent drift (see
    the claude-docs/firebase counterparts of this test)."""

    def test_content_full_output_is_pinned(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "---\n"
            "title: streamText\n"
            "description: Stream text generation\n"
            "tags: [core, streaming]\n"
            "---\n"
            "\n"
            "# streamText\n"
            "\n"
            "## Options\n"
            "Configure options here.\n",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "content", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        # ai-sdk's content now gets the same subsection-hint
        # block (before AND after the body) that claude-docs
        # already had. min_level=1 means the doc's own H1 counts as its
        # one top-level section here, matching what 'sections' already
        # shows for ai-sdk.
        hint_block = (
            "\n"
            "--- Top-level sections (1) ---\n"
            "  - streamText\n"
            "\n"
            'Next: parse-ai-sdk.py content 0 "<heading_path from above>"\n'
        )
        expected = (
            "# doc_title: streamText\n"
            "# doc_tags: core, streaming\n"
            "---\n"
            + hint_block
            + "\n"
            "# streamText\n"
            "\n"
            "## Options\n"
            "Configure options here.\n"
            + hint_block
        )
        self.assertEqual(out, expected)

    def test_no_subsection_hints_suppresses_both_occurrences(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "---\ntitle: Doc\n---\n\n# Doc\n\n## Options\ntext\n",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "content", "0",
            "--cache-dir", tmp, "--no-subsection-hints",
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Top-level sections", out)
        self.assertNotIn("Next:", out)

    def test_content_longer_than_max_chars_is_truncated_with_narrow_hint(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_fixture(
            tmp,
            "---\ntitle: Doc\n---\n\n# Doc\n\n"
            "0123456789 0123456789 0123456789 0123456789 0123456789\n",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "content", "0",
            "--cache-dir", tmp, "--max-chars", "20",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("chars truncated", out)
        self.assertIn('narrow with parse-ai-sdk.py content 0 "<heading_path>"', out)
        self.assertNotIn("0123456789 0123456789 0123456789 0123456789 0123456789", out)


if __name__ == "__main__":
    unittest.main()
