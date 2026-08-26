"""Fixture-based tests for scripts/parse-firebase.py.

Firebase has no llms-full.txt — each page is a separate on-demand fetch
cached under ``<cache-dir>/firebase-docs/<hashed filename>``. Fixtures here
pre-seed both the index and per-page cache files (naming pages via the
module's own ``_url_to_cache_filename`` so the fixture matches exactly what
``_fetch_page`` will look up) so no network access is needed.

Firebase does not get the full-corpus search fallback that claude-docs /
ai-sdk got (that would mean fetching every page — explicitly out of scope,
see internal backlog); it only gets updated 0-result Tip wording pointing
at 'search-content'. It does get the upstream format-change detection
(assert_parsed), migrated from a plain ``die()`` (exit 1) to
``assert_parsed`` (exit 2) for consistency with claude-docs / ai-sdk.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

parse_firebase = _loader.load_script("parse-firebase.py")


class FirebaseCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        Path(self.tmp, "firebase-llms.txt").write_text(
            "- [Firestore Query Limits](https://firebase.google.com/docs/firestore/query-limit.md.txt): Limits on queries\n"
            "- [Auth Overview](https://firebase.google.com/docs/auth/overview.md.txt): Overview of auth\n",
            encoding="utf-8",
        )
        pages_dir = Path(self.tmp) / "firebase-docs"
        pages_dir.mkdir()
        pages = {
            "https://firebase.google.com/docs/firestore/query-limit.md.txt":
                "# Firestore Query Limits\n\n## Limits\nMax 100 zzzonlyinbody results per query.\n",
            "https://firebase.google.com/docs/auth/overview.md.txt":
                "# Auth Overview\n\n## Introduction\nAuthenticate users.\n",
        }
        for url, body in pages.items():
            # Use the module's own filename hashing so the fixture matches
            # what _fetch_page will look up (hand-computing the sha1 here
            # would silently drift if the hashing scheme ever changes).
            filename = parse_firebase._url_to_cache_filename(url)
            (pages_dir / filename).write_text(body, encoding="utf-8")

    def test_content_retrieves_cached_page(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "content", "0", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Firestore Query Limits", out)

    def test_search_content_finds_page_body_term(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "search-content", "zzzonlyinbody",
            "--page-ref", "0", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("zzzonlyinbody", out)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "search-index", "totallyabsentkeyword",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("No matching pages found", out)
        self.assertIn("search-content", out)

    def test_search_zero_result_tip_mentions_search_content(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "search", "totallyabsentkeyword",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0)
        self.assertIn("No matching pages found", out)
        self.assertIn("search-content", out)


class AssertParsedIntegrationTest(unittest.TestCase):
    """Firebase already refused to run on 0 parsed entries, but via a
    plain die() (exit 1). Migrated to assert_parsed (exit 2) so all three
    scripts distinguish "format changed" from other failures the same
    way."""

    def test_malformed_index_exits_2_not_1(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "firebase-llms.txt").write_text(
            "not a valid index line\n", encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "fetch-index", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 2)
        self.assertIn("format may have changed", err)


class TopHeadingPathRoundTripTest(unittest.TestCase):
    """A firebase page's own H1 is part of the "(top)" preamble
    here (unlike claude-docs, firebase hands the raw page — H1 included —
    straight through to extract_sections/extract_content, since it has no
    split_documents step to strip the H1 first). The (top) sentinel must
    still round-trip end-to-end through search-content -> content."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        Path(self.tmp, "firebase-llms.txt").write_text(
            "- [Auth Overview](https://firebase.google.com/docs/auth/overview.md.txt): Overview of auth\n",
            encoding="utf-8",
        )
        pages_dir = Path(self.tmp) / "firebase-docs"
        pages_dir.mkdir()
        url = "https://firebase.google.com/docs/auth/overview.md.txt"
        filename = parse_firebase._url_to_cache_filename(url)
        (pages_dir / filename).write_text(
            "# Auth Overview\n"
            "\n"
            "Text mentioning zzzpreambleterm before any heading.\n"
            "\n"
            "## Introduction\n"
            "Authenticate users.\n",
            encoding="utf-8",
        )

    def test_top_hit_is_displayed_with_clarifying_suffix(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "search-content", "zzzpreambleterm",
            "--page-ref", "0", "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("Section: (top)  [before first heading]", out)

    def test_top_value_copied_into_content_resolves_the_preamble(self):
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "content", "0", "(top)",
            "--cache-dir", self.tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("zzzpreambleterm", out)
        self.assertIn("heading_path: (top)", out)
        self.assertNotIn("Authenticate users", out)


class GoldenOutputTest(unittest.TestCase):
    """Pin one representative command's FULL stdout verbatim so
    a future change to this independently-implemented output format shows
    up as an explicit, intentional diff here instead of silent drift (see
    the claude-docs/ai-sdk counterparts of this test)."""

    def test_content_full_output_is_pinned(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "firebase-llms.txt").write_text(
            "- [Firestore Query Limits](https://firebase.google.com/docs/firestore/query-limit.md.txt): Limits on queries\n",
            encoding="utf-8",
        )
        pages_dir = Path(tmp) / "firebase-docs"
        pages_dir.mkdir()
        url = "https://firebase.google.com/docs/firestore/query-limit.md.txt"
        filename = parse_firebase._url_to_cache_filename(url)
        (pages_dir / filename).write_text(
            "# Firestore Query Limits\n"
            "\n"
            "Intro text before any heading.\n"
            "\n"
            "## Limits\n"
            "Max 100 results per query.\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "content", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        # firebase's content now gets the same subsection-hint
        # block (before AND after the body) that claude-docs
        # already had.
        hint_block = (
            "\n"
            "--- Top-level sections (1) ---\n"
            "  - Limits\n"
            "\n"
            'Next: parse-firebase.py content 0 "<heading_path from above>"\n'
        )
        expected = (
            "# doc_title: Firestore Query Limits\n"
            "# source: https://firebase.google.com/docs/firestore/query-limit.md.txt\n"
            "---\n"
            + hint_block
            + "# Firestore Query Limits\n"
            "\n"
            "Intro text before any heading.\n"
            "\n"
            "## Limits\n"
            "Max 100 results per query.\n"
            + hint_block
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
        Path(tmp, "firebase-llms.txt").write_text(
            "- [Guide](https://firebase.google.com/docs/guide.md.txt): Guide\n",
            encoding="utf-8",
        )
        pages_dir = Path(tmp) / "firebase-docs"
        pages_dir.mkdir()
        url = "https://firebase.google.com/docs/guide.md.txt"
        filename = parse_firebase._url_to_cache_filename(url)
        (pages_dir / filename).write_text(
            "# Guide\n\n"
            "## Client\n"
            "text\n"
            "### Examples\n"
            "client examples\n\n"
            "## Server\n"
            "text\n"
            "### Examples\n"
            "server examples\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "sections", "0", "--cache-dir", tmp,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("[L3] Client/Examples", out)
        self.assertIn("[L3] Server/Examples", out)
        self.assertNotIn("[L3] Examples", out)

    def test_no_subsection_hints_suppresses_both_occurrences(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "firebase-llms.txt").write_text(
            "- [Doc](https://firebase.google.com/docs/doc.md.txt): desc\n",
            encoding="utf-8",
        )
        pages_dir = Path(tmp) / "firebase-docs"
        pages_dir.mkdir()
        url = "https://firebase.google.com/docs/doc.md.txt"
        filename = parse_firebase._url_to_cache_filename(url)
        (pages_dir / filename).write_text(
            "# Doc\n\n## Options\ntext\n", encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "content", "0",
            "--cache-dir", tmp, "--no-subsection-hints",
        ])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Top-level sections", out)
        self.assertNotIn("Next:", out)

    def test_content_longer_than_max_chars_is_truncated_with_narrow_hint(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "firebase-llms.txt").write_text(
            "- [Doc](https://firebase.google.com/docs/doc.md.txt): desc\n",
            encoding="utf-8",
        )
        pages_dir = Path(tmp) / "firebase-docs"
        pages_dir.mkdir()
        url = "https://firebase.google.com/docs/doc.md.txt"
        filename = parse_firebase._url_to_cache_filename(url)
        (pages_dir / filename).write_text(
            "# Doc\n\n"
            "0123456789 0123456789 0123456789 0123456789 0123456789\n",
            encoding="utf-8",
        )
        code, out, err = _loader.run_cli(parse_firebase, [
            "parse-firebase.py", "content", "0",
            "--cache-dir", tmp, "--max-chars", "20",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("chars truncated", out)
        self.assertIn('narrow with parse-firebase.py content 0 "<heading_path>"', out)
        self.assertNotIn("0123456789 0123456789 0123456789 0123456789 0123456789", out)


if __name__ == "__main__":
    unittest.main()
