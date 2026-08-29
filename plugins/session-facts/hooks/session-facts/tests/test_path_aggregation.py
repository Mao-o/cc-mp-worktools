"""core/util.py aggregate_paths のテスト (#5: test_dir 共通祖先集約)。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from core.util import aggregate_paths


class AggregatePathsTest(unittest.TestCase):
    def test_single_path_unchanged(self):
        self.assertEqual(
            aggregate_paths(["src/foo/tests"]),
            ["src/foo/tests"],
        )

    def test_empty(self):
        self.assertEqual(aggregate_paths([]), [])

    def test_session_facts_three_test_dirs_collapse(self):
        paths = [
            "plugins/sensitive-files-guardrail/hooks/check-sensitive-files/tests",
            "plugins/sensitive-files-guardrail/hooks/redact-sensitive-reads/tests",
            "plugins/verify-cloud-account/hooks/verify-cloud-account/tests",
        ]
        self.assertEqual(aggregate_paths(paths), ["plugins/*/hooks/*/tests"])

    def test_constant_segments_stay_literal(self):
        # Only the 2nd segment differs.
        paths = ["pkg/a/tests", "pkg/b/tests", "pkg/c/tests"]
        self.assertEqual(aggregate_paths(paths), ["pkg/*/tests"])

    def test_duplicate_paths_deduped(self):
        paths = ["a/b/tests", "a/b/tests"]
        self.assertEqual(aggregate_paths(paths), ["a/b/tests"])

    def test_different_lengths_grouped_separately(self):
        paths = [
            "a/x/tests",
            "a/y/tests",
            "a/b/c/tests",
            "a/d/e/tests",
        ]
        result = aggregate_paths(paths)
        # length-3 group collapses, length-4 group collapses, two lines total.
        self.assertEqual(sorted(result), ["a/*/*/tests", "a/*/tests"])

    def test_single_member_length_group_verbatim(self):
        paths = ["a/b/tests", "c/d/tests", "lone/deep/path/tests"]
        result = aggregate_paths(paths)
        self.assertIn("lone/deep/path/tests", result)
        self.assertIn("*/*/tests", result)

    def test_dify_five_test_dirs_within_three_lines(self):
        # Acceptance: 5 test_dirs collapse to at most 3 lines.
        paths = [
            "api/tests",
            "web/tests",
            "worker/tests",
            "sdk/python/tests",
            "sdk/nodejs/tests",
        ]
        result = aggregate_paths(paths)
        self.assertLessEqual(len(result), 3)

    def test_fully_wildcarded_pattern_lists_originals_instead(self):
        # internal backlog: aggregate_paths used to collapse this group to a
        # single "*/*" line -- every segment differs, so no literal survives
        # anywhere and the pattern carries zero localization info. All 3
        # inputs share nothing but segment count, so all 3 are listed
        # verbatim instead (well under the 4-entry cap, no "+N more").
        paths = ["api/tests", "web/__tests__", "sdks/spec"]
        result = aggregate_paths(paths)
        self.assertNotIn("*/*", result)
        self.assertEqual(sorted(result), ["api/tests", "sdks/spec", "web/__tests__"])

    def test_fully_wildcarded_leftovers_are_capped(self):
        # 6 same-length paths where BOTH segments differ across the board
        # (no two share a leading directory, no two share a trailing one
        # either) -- the naive pattern is fully wildcarded ("*/*"), so all 6
        # would-be leftovers get capped at max_listed=4 plus a "+N more"
        # line instead of dumping all 6 raw paths.
        paths = [f"dir{i}/leaf{i}" for i in range(1, 7)]
        result = aggregate_paths(paths, max_listed=4)
        self.assertEqual(len(result), 5)  # 4 verbatim + 1 "+N more" line
        self.assertEqual(result[:4], ["dir1/leaf1", "dir2/leaf2", "dir3/leaf3", "dir4/leaf4"])
        self.assertEqual(result[4], "... (+2 more)")

    def test_wildcard_group_still_collapses_shared_prefix_subset(self):
        # Mixed group: 2 paths share a leading directory (collapse to a
        # useful pattern) while a 3rd has a unique leading directory (listed
        # verbatim) -- the prefix re-split keeps the useful aggregate
        # instead of falling back to raw-listing everything.
        paths = ["packages/a/tests", "packages/b/tests", "other/x/y"]
        result = aggregate_paths(paths)
        self.assertEqual(sorted(result), ["other/x/y", "packages/*/tests"])

    def test_pattern_with_surviving_literal_segment_is_not_touched(self):
        # A pattern that keeps at least one literal segment (here "tests" at
        # the tail) still localizes part of the path, so it is NOT treated
        # as degenerate even though the leading segment is a wildcard.
        paths = ["api/tests", "web/tests", "worker/tests"]
        result = aggregate_paths(paths)
        self.assertEqual(result, ["*/tests"])


if __name__ == "__main__":
    unittest.main()
