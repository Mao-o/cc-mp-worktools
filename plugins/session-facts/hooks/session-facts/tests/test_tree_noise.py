"""core/tree.py: collapsed tool/test dirs, per-directory child cap, nested
dot-dir hiding (internal backlog joa.7)."""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from core.tree import build_dir_tree, render_tree, select_tree_lines


class CollapsedDirsTest(unittest.TestCase):
    def test_tool_dirs_and_test_dirs_are_collapsed_with_marker(self):
        paths = [".github/workflows/ci.yml", "src/a.py", "tests/unit/test_a.py", ".claude/x.md"]
        lines = render_tree(build_dir_tree(paths, 5))
        self.assertEqual(lines, ["├── .claude/", "├── .github/…", "├── src/", "└── tests/…"])

    def test_collapse_can_be_disabled(self):
        paths = [".github/workflows/ci.yml"]
        lines = render_tree(build_dir_tree(paths, 5), collapse=set())
        self.assertEqual(lines, ["└── .github/workflows/"])

    def test_nested_dot_dirs_are_hidden(self):
        paths = ["api/.idea/x", "api/core/a.py", "web/.storybook/main.ts", "web/app/p.tsx"]
        lines = render_tree(build_dir_tree(paths, 5))
        self.assertEqual(lines, ["├── api/core/", "└── web/app/"])

    def test_chain_compression_stops_at_collapsed_dir(self):
        paths = ["plugins/x/tests/unit/test_a.py"]
        lines = render_tree(build_dir_tree(paths, 5))
        self.assertEqual(lines, ["└── plugins/x/", "    └── tests/…"])


class ChildCapTest(unittest.TestCase):
    def test_wide_subdirectory_is_capped_below_root(self):
        paths = [f"docs/loc{i:02d}/index.md" for i in range(16)] + ["src/a.py"]
        lines = render_tree(build_dir_tree(paths, 5))
        self.assertEqual(lines[0], "├── docs/")
        self.assertIn("│   └── … (+6 more dirs)", lines)
        self.assertEqual(lines[-1], "└── src/")
        self.assertEqual(len(lines), 1 + 10 + 1 + 1)

    def test_root_level_is_never_capped(self):
        paths = [f"top{i:02d}/a.py" for i in range(25)]
        lines = render_tree(build_dir_tree(paths, 5))
        self.assertEqual(len(lines), 25)
        self.assertFalse(any("more dirs" in ln for ln in lines))

    def test_cap_keeps_other_top_level_dirs_at_depth_two(self):
        # One directory with 40 children used to push the whole tree to
        # depth 1; now that directory is capped and the others keep depth 2.
        paths = [f"docs/loc{i:02d}/x.md" for i in range(40)]
        paths += [f"api/{d}/a.py" for d in ("controllers", "services", "models")]
        lines, depth = select_tree_lines(paths, max_lines=30)
        self.assertGreaterEqual(depth, 2)
        self.assertIn("│   ├── controllers/", lines)


if __name__ == "__main__":
    unittest.main()
