"""Fixture-based tests for scripts/parse-ai-sdk.py.

Covers ``split_documents`` fixtures named in the 2026-08 audit (leading
non-frontmatter content skipped; ``---`` inside a code fence must not
split) plus CLI-level tests for the upstream format-change detection, the
untitled-ratio warning, and the search-fallback fix — using pre-seeded
cache-dir fixtures so no network access is needed.
"""

import os
import shlex
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

    def test_unclosed_fence_recovers_before_the_next_frontmatter(self):
        # ```ts inside the stray block is content, not a closer, so the
        # block ends at the bare ``` and the next frontmatter still splits
        lines = [
            "---\n", "title: A\n", "---\n", "# A\n",
            "```\n",
            "```ts\n",
            "const x = 1;\n",
            "```\n",
            "---\n", "title: B\n", "---\n", "# B\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual([d["frontmatter_lines"] for d in docs], [["title: A\n"], ["title: B\n"]])

    def test_hr_followed_by_kv_like_prose_is_not_a_boundary(self):
        """A body ``---`` horizontal rule followed by prose that happens to
        start with ``Note:`` must not open a new document: every line
        between the delimiters has to be YAML-shaped and one must be
        ``title:`` (internal backlog). The live ai-sdk corpus has 0
        untitled docs before and after, and the sections dump is
        identical."""
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
        self.assertEqual(len(docs), 1)
        self.assertIn("Options continued", "".join(docs[0]["body_lines"]))

    def test_hr_around_yaml_shaped_lines_without_title_is_not_a_boundary(self):
        lines = [
            "---\n", "title: A\n", "---\n", "body\n",
            "---\n", "key: value\n", "other: thing\n", "---\n", "more\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual(len(docs), 1)
        self.assertIn("more", "".join(docs[0]["body_lines"]))

    def test_real_frontmatter_with_list_and_continuation_lines_splits(self):
        lines = [
            "---\n", "title: A\n", "---\n", "body A\n",
            "---\n",
            "title: B\n",
            "description: >\n",
            "  a folded\n",
            "  description\n",
            "tags:\n",
            "  - one\n",
            "  - two\n",
            "\n",
            "---\n",
            "body B\n",
        ]
        docs = parse_ai_sdk.split_documents(lines)
        self.assertEqual(len(docs), 2)
        self.assertEqual(parse_ai_sdk.parse_frontmatter(docs[1]["frontmatter_lines"])["title"], "B")

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

    def test_block_form_tags_are_a_list(self):
        # the form the published corpus uses
        fm = parse_ai_sdk.parse_frontmatter([
            "title: Server\n", "docs_index: /llms.txt\n",
            "tags:\n", "  - api servers\n", "  - streaming\n",
        ])
        self.assertEqual(fm["tags"], ["api servers", "streaming"])

    def test_block_form_tags_followed_by_another_key(self):
        fm = parse_ai_sdk.parse_frontmatter([
            "tags:\n", "  - 'one'\n", '  - "two words"\n', "description: after\n",
        ])
        self.assertEqual(fm["tags"], ["one", "two words"])
        self.assertEqual(fm["description"], "after")

    def test_children_of_another_key_are_not_read_as_a_continuation(self):
        fm = parse_ai_sdk.parse_frontmatter([
            "title: Server\n", "description: Short\n",
            "sidebar:\n", "  order: 3\n", "  label: hidden\n",
            "tags:\n", "  - api\n",
        ])
        self.assertEqual(fm["description"], "Short")
        self.assertEqual(fm["title"], "Server")
        self.assertEqual(fm["tags"], ["api"])

    def test_inline_and_bare_tags_still_work(self):
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["tags: [a, 'b c']\n"])["tags"], ["a", "b c"])
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["tags: a, b\n"])["tags"], ["a", "b"])

    def test_escaped_quotes_in_a_double_quoted_title(self):
        fm = parse_ai_sdk.parse_frontmatter(['title: "useChat \\"An error occurred\\""\n'])
        self.assertEqual(fm["title"], 'useChat "An error occurred"')

    def test_escaped_backslash_and_single_quoted_forms(self):
        self.assertEqual(parse_ai_sdk.parse_frontmatter(['title: "a\\\\b"\n'])["title"], "a\\b")
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["title: 'it''s'\n"])["title"], "it's")

    def test_plain_titles_are_unchanged(self):
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["title: streamText\n"])["title"], "streamText")
        self.assertEqual(parse_ai_sdk.parse_frontmatter(['title: "Quoted: yes"\n'])["title"], "Quoted: yes")
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["title: Don't stop\n"])["title"], "Don't stop")


class EscapedQuoteTitleCliTest(unittest.TestCase):
    """A page whose title has escaped quotes can be named by its real title."""

    TITLE = 'React error "Maximum update depth exceeded"'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "---\n"
            'title: "React error \\"Maximum update depth exceeded\\""\n'
            "description: Troubleshooting\n"
            "tags:\n"
            "  - api servers\n"
            "  - streaming\n"
            "---\n\n"
            "# React error\n\n"
            "## Cause\n"
            "A loop of state updates.\n",
        )

    def test_sections_finds_the_page_by_its_real_title(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "sections", self.TITLE, "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn(f'"{self.TITLE}"', out)

    def test_search_prints_the_title_and_tags_as_a_list(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search", "state updates", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn(f"[0] {self.TITLE}", out)
        self.assertIn("tags: api servers, streaming", out)
        self.assertNotIn("tags: - ", out)


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


class SearchContentMaxSnippetCharsTest(unittest.TestCase):
    """search-content used to have no --max-snippet-chars at all (unlike
    search, and unlike claude-docs' own search-content) — every snippet was
    printed in full, however long. Confirms the flag now truncates here too."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        long_line = "keywordhit " + ("x" * 200) + " end of line\n"
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
            + long_line,
        )

    def test_default_does_not_truncate_a_short_enough_snippet(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search-content", "keywordhit",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("…", out)
        self.assertIn("end of line", out)

    def test_max_snippet_chars_truncates_a_long_snippet(self):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search-content", "keywordhit",
            "--cache-dir", self.tmp, "--max-snippet-chars", "20",
        ])
        self.assertEqual(code, 0, err)
        # 一致行だけで予算を超えるときは、一致行を (80 字未満には切らずに) … 付きで切る
        self.assertIn("→ keywordhit " + "x" * 67 + "…", out)
        self.assertNotIn("end of line", out)


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
            "---\ntitle:\ndescription: no title value\n---\n\n# Doc2\n\nbody\n",
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
        docs_text += "---\ntitle:\ndescription: no title value\n---\n\n# Doc9\n\nbody\n"
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

    def test_fetch_index_file_flag_is_used_verbatim_without_fetching(self):
        # fetch-index used to be the one subcommand without --file (always
        # auto-fetched under --cache-dir) — now consistent with the rest.
        snapshot = Path(self.tmp, "my-snapshot.txt")
        snapshot.write_text(
            "---\ntitle: Doc\n---\n\n# Doc\n\nbody\n", encoding="utf-8",
        )
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            code, out, err = _loader.run_cli(parse_ai_sdk, [
                "parse-ai-sdk.py", "fetch-index",
                "--file", str(snapshot), "--cache-dir", self.tmp,
            ])
        self.assertEqual(code, 0, err)
        mock_urlopen.assert_not_called()
        self.assertIn("Doc", out)
        # Codex R1 P1: the printed index is only valid against *this*
        # snapshot — a follow-up 'sections' hint that dropped --file would
        # resolve the same integer against the default cached corpus
        # instead, silently returning a different document.
        self.assertIn(f"--file {snapshot}", out)

    def test_sections_file_flag_hint_includes_file(self):
        # sections already had --file before this round of changes; found
        # to have the identical gap while fixing the one Codex flagged on
        # the newly-added fetch-index --file support.
        snapshot = Path(self.tmp, "my-snapshot.txt")
        snapshot.write_text(
            "---\ntitle: Doc\n---\n\n# Doc\n\n## Section\nbody\n", encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "sections", "0",
            "--file", str(snapshot), "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn(f"--file {snapshot}", out)


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
        # shows for ai-sdk. The non-default --cache-dir (a tempdir, as
        # every test here uses) is echoed into the Next: hint so a reader
        # following it stays on the same corpus instead of falling back
        # to the default cache dir.
        hint_block = (
            "\n"
            "--- Top-level sections (1) ---\n"
            "  - streamText\n"
            "\n"
            f'Next: parse-ai-sdk.py content 0 "<heading_path from above>" --cache-dir {tmp}\n'
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
            "---\ntitle: Guide\n---\n\n"
            "# Guide\n\n"
            "## Client\n"
            "text\n"
            "### Examples\n"
            "client examples\n\n"
            "## Server\n"
            "text\n"
            "### Examples\n"
            "server examples\n",
        )
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "sections", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Client/Examples", out)
        self.assertIn("Server/Examples", out)
        # The old bug printed just "Examples" for both — assert the bare,
        # unqualified label no longer appears on its own heading line.
        self.assertNotIn("] Examples\n", out)

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

    def test_narrow_hint_and_subsection_hints_retain_file_flag(self):
        # A truncation/subsection-drilldown hint that drops --file would
        # send a follow-up command back to fetching/reading the default
        # corpus instead of this read-only snapshot — the SAME numeric
        # page index could then identify a completely different document.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        snapshot = Path(tmp, "my-snapshot.txt")
        snapshot.write_text(
            "---\ntitle: Doc\n---\n\n# Doc\n\n## Options\n"
            "0123456789 0123456789 0123456789 0123456789 0123456789\n",
            encoding="utf-8",
        )
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            code, out, err = _loader.run_cli(parse_ai_sdk, [
                "parse-ai-sdk.py", "content", "0",
                "--file", str(snapshot), "--cache-dir", tmp, "--max-chars", "20",
            ])
        mock_urlopen.assert_not_called()
        self.assertEqual(code, 0, err)
        self.assertIn(f"narrow with parse-ai-sdk.py content 0 \"<heading_path>\" --file {snapshot}", out)
        self.assertIn(f'Next: parse-ai-sdk.py content 0 "<heading_path from above>" --file {snapshot}', out)
        # --file makes --cache-dir irrelevant (read-only mode never
        # touches the cache), so it must NOT also be echoed redundantly.
        self.assertNotIn("--cache-dir", out)


class SearchRankingTest(unittest.TestCase):
    """ai-sdk ``search`` uses the shared ranking: changelog-style docs sink
    below a doc with fewer hits, and ``--include-changelog-priority`` lifts
    them back (the flag previously existed only in claude-docs)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "---\ntitle: Changelog\ndescription: streamText updates\n---\n\n# Changelog\n\n"
            "## 5.0\nstreamText a\nstreamText b\nstreamText c\nstreamText d\n\n"
            "---\ntitle: streamText\ndescription: streamText reference\n---\n\n# streamText\n\n"
            "## Usage\nstreamText once.\n",
        )

    def _titles(self, *extra):
        code, out, err = _loader.run_cli(parse_ai_sdk, [
            "parse-ai-sdk.py", "search", "streamText", "--cache-dir", self.tmp, *extra,
        ])
        self.assertEqual(code, 0, err)
        return [l for l in out.splitlines() if l.startswith("[")]

    def test_changelog_sinks_by_default(self):
        titles = self._titles()
        self.assertEqual(len(titles), 2)
        self.assertIn("streamText", titles[0])
        self.assertIn("Changelog", titles[1])

    def test_flag_restores_hit_order(self):
        titles = self._titles("--include-changelog-priority")
        self.assertIn("Changelog", titles[0])


class FrontmatterUrlTest(unittest.TestCase):
    """各ページの frontmatter の `url:` を、sections / content / search /
    search-content の出力に出す。url が無いページは従来どおり (行を足さない)。"""

    URL = "https://ai-sdk.example/docs/stream-text"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(
            self.tmp,
            "---\n"
            "title: streamText\n"
            "description: Stream text generation\n"
            f'url: "{self.URL}"\n'
            "docs_index: /llms.txt\n"
            "---\n\n"
            "# streamText\n\n"
            "## Options\n"
            "Configure the onFinishUnique callback here.\n"
            "\n"
            "---\n"
            "title: generateText\n"
            "description: Generate text once\n"
            "---\n\n"
            "# generateText\n\n"
            "## Usage\n"
            "Basic onFinishUnique usage.\n",
        )

    def _run(self, *argv):
        code, out, err = _loader.run_cli(
            parse_ai_sdk, ["parse-ai-sdk.py", *argv, "--cache-dir", self.tmp])
        self.assertEqual(code, 0, err)
        return out

    def test_parse_frontmatter_reads_the_quoted_url(self):
        fm = parse_ai_sdk.parse_frontmatter(
            ["title: A\n", f'url: "{self.URL}"\n', "docs_index: /llms.txt\n"])
        self.assertEqual(fm["url"], self.URL)
        self.assertEqual(parse_ai_sdk.parse_frontmatter(["title: A\n"])["url"], "")

    def test_sections_prints_the_url(self):
        out = self._run("sections", "0")
        self.assertIn(f"  URL: {self.URL}\n", out)

    def test_content_prints_the_url_as_source(self):
        out = self._run("content", "0")
        self.assertIn(f"# source: {self.URL}\n", out)

    def test_search_and_search_content_print_the_url(self):
        for sub in ("search", "search-content"):
            with self.subTest(sub=sub):
                out = self._run(sub, "onFinishUnique")
                self.assertEqual(out.count(f"    URL: {self.URL}\n"), 1, out)

    def test_page_without_url_gets_no_url_line(self):
        out = self._run("sections", "1")
        self.assertNotIn("URL:", out)
        out = self._run("content", "1")
        self.assertNotIn("# source:", out)
        out = self._run("search-content", "onFinishUnique")
        self.assertEqual(out.count("URL:"), 1)


class UrlPageRefTest(unittest.TestCase):
    """表示された URL をそのまま page_ref に貼った失敗が、次の一手 (その
    ページを読む実行できるコマンド) を出す。URL は解決しない (仕様) ので終了コードは 1 のまま。

    fixture の frontmatter は実 corpus (ai-sdk.dev の llms-full.txt) から切り出した形
    (`url:` は引用符つきで、`/docs/advanced` は他のページの URL の接頭辞)。"""

    SCRIPT = "parse-ai-sdk.py"
    PRUNE = "https://ai-sdk.dev/docs/reference/ai-sdk-ui/prune-messages"
    ADVANCED = "https://ai-sdk.dev/docs/advanced"
    CACHING = "https://ai-sdk.dev/docs/advanced/caching"

    FIXTURE = (
        "---\n"
        "title: pruneMessages\n"
        "description: API Reference for pruneMessages.\n"
        f'url: "{PRUNE}"\n'
        "docs_index: /llms.txt\n"
        "---\n\n"
        "# pruneMessages\n\n"
        "## Usage\n"
        "prunemarker body.\n"
        "\n"
        "---\n"
        "title: Advanced\n"
        "description: Advanced topics.\n"
        f'url: "{ADVANCED}"\n'
        "docs_index: /llms.txt\n"
        "---\n\n"
        "# Advanced\n\n"
        "## Overview\n"
        "advancedmarker body.\n"
        "\n"
        "---\n"
        "title: Caching\n"
        "description: Caching responses.\n"
        f'url: "{CACHING}"\n'
        "docs_index: /llms.txt\n"
        "---\n\n"
        "# Caching\n\n"
        "## Overview\n"
        "cachingmarker body.\n"
    )
    TITLES = {PRUNE: "pruneMessages", ADVANCED: "Advanced", CACHING: "Caching"}
    MARKERS = {PRUNE: "prunemarker", ADVANCED: "advancedmarker", CACHING: "cachingmarker"}
    # (page URL, a way to write it that is not the stored string)
    VARIANTS = [
        (PRUNE, PRUNE),
        (PRUNE, PRUNE + "/"),
        (PRUNE, PRUNE.replace("https://", "http://")),
        (PRUNE, PRUNE.replace("https://", "")),
        (PRUNE, PRUNE + ".md"),
        (PRUNE, PRUNE + "#usage"),
        (PRUNE, PRUNE + "?x=1"),
        (PRUNE, PRUNE.replace("ai-sdk.dev", "AI-SDK.dev")),
        (ADVANCED, ADVANCED),
        (CACHING, CACHING),
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _write_fixture(self.tmp, self.FIXTURE)
        self.full = str(Path(self.tmp, "ai-sdk-llms-full.txt"))

    def run_argv(self, argv):
        return _loader.run_cli(parse_ai_sdk, argv)

    def corpus_options(self):
        return ["--cache-dir", self.tmp]

    def failure(self, sub, ref, query="foo"):
        argv = [self.SCRIPT, sub]
        argv += [query, "--page-ref", ref] if sub == "search-content" else [ref]
        code, out, err = self.run_argv(argv + self.corpus_options())
        self.assertEqual(code, 1, (sub, ref, out, err))
        self.assertIn(f"No document found for: {ref}", err)
        return err

    def offered(self, err):
        return [ln.strip() for ln in err.splitlines()
                if ln.strip().startswith(self.SCRIPT + " ")]

    def one_offered(self, err):
        lines = self.offered(err)
        self.assertEqual(len(lines), 1, err)
        return lines[0]

    def run_printed(self, line):
        """The line as a shell splits it, unmodified. It must name the corpus the
        failing command used (checked first: without it, it reads the default corpus)."""
        self.assertTrue(line.endswith(" ".join(self.corpus_options())), line)
        return self.run_argv(shlex.split(line, comments=True))

    def test_url_failure_offers_the_command_for_that_page(self):
        for sub in ("content", "sections", "search-content"):
            for page_url, ref in self.VARIANTS:
                with self.subTest(sub=sub, ref=ref):
                    err = self.failure(sub, ref, self.MARKERS[page_url])
                    self.assertIn("integer index", err)
                    lines = self.offered(err)
                    self.assertEqual(len(lines), 1, err)
                    code, out, run_err = self.run_printed(lines[0])
                    self.assertEqual(code, 0, run_err)
                    self.assertIn(self.TITLES[page_url], out)
                    if sub == "search-content":
                        self.assertIn(self.MARKERS[page_url], out)
                    # the page it reads is the one that owns the URL (not the
                    # page whose URL merely starts with it, nor the reverse)
                    if sub != "search-content":
                        self.assertIn(page_url, out)

    def test_printed_command_keeps_the_corpus_and_other_options(self):
        err = self.failure_with(
            [self.SCRIPT, "content", self.CACHING, "Overview", "--max-chars", "99",
             *self.corpus_options()])
        line = self.one_offered(err)
        self.assertIn("content 2 Overview --max-chars 99 ", line)
        self.assertTrue(line.endswith(" ".join(self.corpus_options())), line)
        code, out, run_err = self.run_printed(line)
        self.assertEqual(code, 0, run_err)
        self.assertIn("cachingmarker", out)

    def failure_with(self, argv):
        code, out, err = self.run_argv(argv)
        self.assertEqual(code, 1, (out, err))
        return err

    def test_unknown_url_offers_a_search_on_its_slug(self):
        for ref, query in [
            ("https://ai-sdk.dev/docs/reference/ai-sdk-ui/prune-message", "prune message"),
            ("https://ai-sdk.dev/docs/nope-x/y-z/", "y z"),
            ("https://ai-sdk.dev/docs/caching.md", "caching"),
        ]:
            with self.subTest(ref=ref):
                err = self.failure("content", ref)
                self.assertIn("No page has this url", err)
                line = self.one_offered(err)
                self.assertEqual(shlex.split(line)[1:3], ["search", query])
                code, out, run_err = self.run_printed(line)
                self.assertEqual(code, 0, run_err)

    def test_two_pages_with_the_url_each_get_a_command(self):
        dup = self.FIXTURE + (
            "\n---\n"
            "title: Caching v2\n"
            "description: Caching again.\n"
            f'url: "{self.CACHING}/"\n'
            "docs_index: /llms.txt\n"
            "---\n\n"
            "# Caching v2\n\n"
            "## Overview\n"
            "dupmarker body.\n"
        )
        _write_fixture(self.tmp, dup)
        err = self.failure("content", self.CACHING)
        self.assertIn("Pages have this url", err)
        lines = self.offered(err)
        self.assertEqual(len(lines), 2, err)
        seen = []
        for line in lines:
            code, out, run_err = self.run_printed(line)
            self.assertEqual(code, 0, run_err)
            seen.append("dupmarker" in out)
        self.assertEqual(sorted(seen), [False, True])

    def test_hostile_urls_end_in_the_same_plain_failure(self):
        # words that are all symbols / escapes / dashes give no search command
        # and must not crash; a slug starting with "-" never becomes an option
        for ref in ["https://ai-sdk.dev/docs/---", "https://ai-sdk.dev/docs/\\_",
                    "https://ai-sdk.dev/docs/-x", "https://ai-sdk.dev/docs/'", "https://",
                    "https://ai-sdk.dev/docs/the"]:
            with self.subTest(ref=ref):
                code, out, err = self.run_argv(
                    [self.SCRIPT, "content", *self.corpus_options(), "--", ref])
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith(f"Error: No document found for: {ref}"), err)
                for line in self.offered(err):
                    code, out, run_err = self.run_printed(line)
                    self.assertEqual(code, 0, run_err)

    def test_non_url_failure_is_unchanged(self):
        code, out, err = self.run_argv(
            [self.SCRIPT, "content", "zzz-no-such-page", *self.corpus_options()])
        self.assertEqual((code, err), (1, "Error: No document found for: zzz-no-such-page\n"))

    def test_a_url_is_still_not_resolved(self):
        # the spec (a URL is for citing): no output from the page is printed
        code, out, err = self.run_argv(
            [self.SCRIPT, "content", self.CACHING, *self.corpus_options()])
        self.assertEqual((code, out), (1, ""))

    def test_a_title_that_is_not_a_url_still_resolves(self):
        code, out, err = self.run_argv(
            [self.SCRIPT, "sections", "cach", *self.corpus_options()])
        self.assertEqual(code, 0, err)
        self.assertIn("Caching", out)


class UrlPageRefFileTest(UrlPageRefTest):
    """The same with ``--file`` (the printed command keeps ``--file``)."""

    def corpus_options(self):
        return ["--file", self.full]


if __name__ == "__main__":
    unittest.main()
