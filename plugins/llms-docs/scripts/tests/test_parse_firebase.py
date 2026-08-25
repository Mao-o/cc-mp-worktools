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


if __name__ == "__main__":
    unittest.main()
