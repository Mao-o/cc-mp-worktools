"""レビュー対象の組み立て (パス解決 / 重複抑止 / truncate / REVIEW_CLEAN 判定)。"""
from __future__ import annotations

import os
import tempfile
import unittest

import _testutil
from _testutil import init_repo, load_entry, write


class ReviewSetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(os.path.join(self._tmp.name, "repo"))
        self.entry = load_entry()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestResolvePaths(ReviewSetTestCase):
    def test_drops_outside_worktree(self):
        rels, overflow = self.entry._resolve_paths(
            self.repo,
            [os.path.join(self.repo, "a.py"), "/etc/hosts", "/tmp/elsewhere.py"],
        )
        self.assertEqual(rels, ["a.py"])
        self.assertEqual(overflow, [])

    def test_dedupes_equivalent_paths(self):
        link = os.path.join(self._tmp.name, "link")
        os.symlink(self.repo, link)
        rels, _ = self.entry._resolve_paths(
            self.repo, [os.path.join(self.repo, "a.py"), os.path.join(link, "a.py")]
        )
        self.assertEqual(rels, ["a.py"])

    def test_overflow_is_carried_over_not_dropped(self):
        """上限超過分を黙って捨てない (silent truncation にしない)。"""
        count = self.entry.MAX_REVIEW_PATHS + 5
        claimed = [os.path.join(self.repo, f"f{i:03d}.py") for i in range(count)]
        rels, overflow = self.entry._resolve_paths(self.repo, claimed)

        self.assertEqual(len(rels), self.entry.MAX_REVIEW_PATHS)
        self.assertEqual(len(overflow), 5)
        self.assertEqual(
            sorted(rels) + sorted(os.path.basename(p) for p in overflow),
            sorted(rels) + sorted(f"f{i:03d}.py" for i in range(count))[-5:],
        )


class TestCollectDiffs(ReviewSetTestCase):
    def test_skips_paths_with_identical_hash(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        sections, submitted, hashes = self.entry._collect_diffs(
            self.repo, ["seed.txt"], {}
        )
        self.assertEqual(len(sections), 1)

        again = self.entry._collect_diffs(self.repo, ["seed.txt"], hashes)
        self.assertEqual(again[0], [], "同一 diff は再レビューに載せない")
        self.assertEqual(again[1], [])

    def test_changed_content_reappears(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        _, _, hashes = self.entry._collect_diffs(self.repo, ["seed.txt"], {})
        write(self.repo, "seed.txt", "alpha\nBETA\nGAMMA\n")
        sections, submitted, _ = self.entry._collect_diffs(
            self.repo, ["seed.txt"], hashes
        )
        self.assertEqual(len(sections), 1)
        self.assertEqual(submitted, [os.path.join(self.repo, "seed.txt")])

    def test_empty_diff_is_not_submitted(self):
        """commit 済み / revert 済みのパスはレビューにも復元対象にも入れない。"""
        sections, submitted, hashes = self.entry._collect_diffs(
            self.repo, ["seed.txt"], {}
        )
        self.assertEqual(sections, [])
        self.assertEqual(submitted, [])
        self.assertEqual(hashes, {})

    def test_untracked_and_tracked_are_both_collected(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        write(self.repo, "fresh.txt", "new file\n")
        sections, submitted, _ = self.entry._collect_diffs(
            self.repo, ["fresh.txt", "seed.txt"], {}
        )
        joined = "\n".join(sections)
        self.assertIn("fresh.txt", joined)
        self.assertIn("seed.txt", joined)
        self.assertEqual(len(submitted), 2)


class TestTruncate(ReviewSetTestCase):
    def test_short_diff_passes_through(self):
        self.assertEqual(self.entry._truncate("small"), "small")

    def test_long_diff_is_cut_with_marker(self):
        result = self.entry._truncate("x" * (self.entry.MAX_DIFF_BYTES + 500))
        self.assertLess(len(result.encode()), self.entry.MAX_DIFF_BYTES + 200)
        self.assertIn("diff truncated for review", result)

    def test_multibyte_boundary_does_not_raise(self):
        result = self.entry._truncate("あ" * self.entry.MAX_DIFF_BYTES)
        self.assertIn("diff truncated for review", result)


class TestIsCleanReview(ReviewSetTestCase):
    def test_bare_sentinel(self):
        self.assertTrue(self.entry.is_clean_review("REVIEW_CLEAN"))
        self.assertTrue(self.entry.is_clean_review("  `REVIEW_CLEAN`  "))
        self.assertTrue(self.entry.is_clean_review(""))

    def test_sentinel_with_trailing_findings_is_not_clean(self):
        self.assertFalse(
            self.entry.is_clean_review("REVIEW_CLEAN\n\n1. **直接影響** — 実は壊れる")
        )

    def test_findings_only(self):
        self.assertFalse(self.entry.is_clean_review("1. **直接影響** — 壊れる"))


class TestEditedPaths(ReviewSetTestCase):
    def test_absolute_file_path(self):
        target = os.path.join(self.repo, "a.py")
        self.assertEqual(
            self.entry._edited_paths({"file_path": target}, self.repo), [target]
        )

    def test_relative_path_is_joined_with_cwd(self):
        self.assertEqual(
            self.entry._edited_paths({"file_path": "a.py"}, self.repo),
            [os.path.join(self.repo, "a.py")],
        )

    def test_notebook_path_fallback(self):
        """NotebookEdit は現環境に非搭載だが、搭載環境の `notebook_path` も拾う。"""
        target = os.path.join(self.repo, "nb.ipynb")
        self.assertEqual(
            self.entry._edited_paths({"notebook_path": target}, self.repo), [target]
        )

    def test_missing_path(self):
        self.assertEqual(self.entry._edited_paths({}, self.repo), [])
        self.assertEqual(self.entry._edited_paths({"file_path": None}, self.repo), [])


if __name__ == "__main__":
    unittest.main()
