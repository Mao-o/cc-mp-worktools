"""core/tree.py のテスト: chain 圧縮 (#3) と dynamic depth 選択 (#1)。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from core.tree import build_dir_tree, render_tree, select_tree_lines, truncate_lines


class RenderCompressionTest(unittest.TestCase):
    def test_single_child_chain_is_collapsed(self):
        tree = build_dir_tree(["a/b/c/f.py"], max_depth=5)
        lines = render_tree(tree)
        self.assertEqual(lines, ["└── a/b/c/"])

    def test_branch_stops_compression(self):
        tree = build_dir_tree(["a/b/x.py", "a/c/y.py"], max_depth=5)
        lines = render_tree(tree)
        # 'a' has two children -> not merged; b and c are leaf dirs.
        self.assertEqual(lines, ["└── a/", "    ├── b/", "    └── c/"])

    def test_compression_then_branch(self):
        # api -> routers (single) -> {articles, follows}
        tree = build_dir_tree(
            ["api/routers/articles.py", "api/routers/follows.py"], max_depth=5
        )
        lines = render_tree(tree)
        self.assertEqual(lines, ["└── api/routers/"])  # files are not dirs; routers is leaf dir

    def test_compression_reduces_line_count(self):
        paths = ["a/b/c/d/e/f.py"]
        tree = build_dir_tree(paths, max_depth=5)
        compressed = render_tree(tree, compress=True)
        expanded = render_tree(tree, compress=False)
        self.assertEqual(len(compressed), 1)
        self.assertEqual(len(expanded), 5)
        self.assertLess(len(compressed), len(expanded))

    def test_compress_disabled_matches_legacy_nesting(self):
        tree = build_dir_tree(["a/b/file.py"], max_depth=5)
        self.assertEqual(
            render_tree(tree, compress=False),
            ["└── a/", "    └── b/"],
        )


class DepthCapTest(unittest.TestCase):
    def test_depth_cap_limits_rendered_parts(self):
        tree = build_dir_tree(["a/b/c/d/f.py"], max_depth=5)
        # depth_cap=2 -> at most 2 path parts shown (compressed into one row)
        self.assertEqual(render_tree(tree, depth_cap=2), ["└── a/b/"])

    def test_depth_cap_one_is_top_level_only(self):
        tree = build_dir_tree(
            ["x/deep/nested/f.py", "y/also/deep/g.py", "z/h.py"], max_depth=5
        )
        self.assertEqual(
            render_tree(tree, depth_cap=1),
            ["├── x/", "├── y/", "└── z/"],
        )

    def test_depth_cap_recurses_at_branch(self):
        tree = build_dir_tree(["a/b/x.py", "a/c/y.py", "a/d/deep/z.py"], max_depth=5)
        lines = render_tree(tree, depth_cap=2)
        self.assertEqual(lines, ["└── a/", "    ├── b/", "    ├── c/", "    └── d/"])


class SelectTreeLinesTest(unittest.TestCase):
    def _wide_paths(self, n_top, n_sub):
        return [
            f"top{i:03d}/sub{j:03d}/file.py"
            for i in range(n_top)
            for j in range(n_sub)
        ]

    def test_expands_to_max_depth_when_room(self):
        # A small, deep single chain fits comfortably -> deepest depth chosen.
        paths = ["a/b/c/d/e/f.py"]
        lines, depth = select_tree_lines(paths, max_lines=100, min_depth=1, max_depth=5)
        self.assertEqual(depth, 5)
        self.assertEqual(lines, ["└── a/b/c/d/e/"])

    def test_expands_to_depth_4(self):
        # Distinct branches at every level so compression cannot collapse them;
        # depth 4 stays under the limit, exercising the >3 expansion path.
        paths = [f"l1_{a}/l2_{b}/l3_{c}/l4_{d}/f.py"
                 for a in range(2) for b in range(2) for c in range(2) for d in range(2)]
        lines, depth = select_tree_lines(paths, max_lines=100, min_depth=1, max_depth=4)
        self.assertEqual(depth, 4)
        self.assertLessEqual(len(lines), 100)

    def test_contracts_when_crowded(self):
        # 20 top * 10 sub = 220 dir rows at depth 2 -> must drop to depth 1 (20 rows).
        paths = self._wide_paths(20, 10)
        lines, depth = select_tree_lines(paths, max_lines=100, min_depth=1, max_depth=5)
        self.assertEqual(depth, 1)
        self.assertEqual(len(lines), 20)

    def test_never_exceeds_max_lines_without_truncation_marker(self):
        paths = self._wide_paths(30, 30)
        lines, depth = select_tree_lines(paths, max_lines=100, min_depth=1, max_depth=5)
        self.assertLessEqual(len(lines), 100)
        self.assertFalse(any("omitted" in ln for ln in lines))

    def test_truncates_only_when_even_min_depth_overflows(self):
        # 150 top-level dirs > 100 even at depth 1 -> truncation marker appears.
        paths = [f"d{i:03d}/f.py" for i in range(150)]
        lines, depth = select_tree_lines(paths, max_lines=100, min_depth=1, max_depth=5)
        self.assertEqual(depth, 1)
        self.assertEqual(len(lines), 101)  # 100 + truncation marker
        self.assertIn("omitted", lines[-1])

    def test_line_count_is_monotonic_in_depth(self):
        paths = self._wide_paths(8, 6)
        tree = build_dir_tree(paths, max_depth=5)
        counts = [len(render_tree(tree, depth_cap=d)) for d in range(1, 6)]
        self.assertEqual(counts, sorted(counts), f"non-monotonic: {counts}")

    def test_fixed_depth_skips_dynamic_search(self):
        paths = ["a/b/c/d/e/f.py"]
        lines, depth = select_tree_lines(paths, max_lines=100, fixed_depth=2)
        self.assertEqual(depth, 2)
        self.assertEqual(lines, ["└── a/b/"])

    def test_fixed_depth_one_for_subtree_mode(self):
        paths = ["plugins/a/deep/f.py", "plugins/b/g.py", "infra/h.py"]
        lines, depth = select_tree_lines(paths, max_lines=100, fixed_depth=1)
        self.assertEqual(depth, 1)
        self.assertEqual(lines, ["├── infra/", "└── plugins/"])

    def test_empty_paths(self):
        lines, depth = select_tree_lines([], max_lines=100)
        self.assertEqual(lines, [])


class TruncateLinesTest(unittest.TestCase):
    def test_under_limit_unchanged(self):
        self.assertEqual(truncate_lines(["a", "b"], 5), ["a", "b"])

    def test_over_limit_appends_marker(self):
        out = truncate_lines([str(i) for i in range(10)], 4)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[-1], "... (6 more lines omitted)")


if __name__ == "__main__":
    unittest.main()
