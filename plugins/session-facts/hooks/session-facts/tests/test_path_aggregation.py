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


if __name__ == "__main__":
    unittest.main()
