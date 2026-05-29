"""git 進行情報 (#9): core/git.py ヘルパー、collector、header 描画。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.git_progress import GitProgressCollector
from core import git as gitmod
from core.context import AnalysisConfig, RepoContext
from renderer import render_header


def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _make_repo(tmp) -> Path:
    root = Path(tmp)
    _git(["init", "-b", "main"], root)
    (root / "a.txt").write_text("1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "first commit"], root)
    (root / "b.txt").write_text("2\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "second commit subject"], root)
    return root


class GitHelpersTest(unittest.TestCase):
    def test_current_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            self.assertEqual(gitmod.current_branch(root), "main")
            _git(["checkout", "-b", "feat/x"], root)
            self.assertEqual(gitmod.current_branch(root), "feat/x")

    def test_recent_commits_format_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            commits = gitmod.recent_commits(root, n=3)
            self.assertEqual(len(commits), 2)
            # newest first; "hash subject (relative date)"
            self.assertIn("second commit subject", commits[0])
            self.assertRegex(commits[0], r"^[0-9a-f]{7,} .+ \(.+ ago\)$")

    def test_no_upstream_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            self.assertIsNone(gitmod.ahead_behind(root))
            self.assertIsNone(gitmod.upstream_ref(root))

    def test_collector_populates_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["is_git_repo"] = True
            GitProgressCollector().collect(ctx)
            info = ctx.results.get("git_progress")
            self.assertIsNotNone(info)
            self.assertEqual(info["branch"], "main")
            self.assertEqual(len(info["recent_commits"]), 2)

    def test_collector_skipped_for_non_git(self):
        ctx = RepoContext(root=Path("/tmp"), config=AnalysisConfig())
        ctx.results["is_git_repo"] = False
        self.assertFalse(GitProgressCollector().should_run(ctx))


class RenderGitProgressTest(unittest.TestCase):
    def _render(self, git_progress):
        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        ctx.results["git_progress"] = git_progress
        return render_header(ctx)

    def test_default_branch_zero_delta_omits_branch_line(self):
        out = self._render({"branch": "main", "ahead": 0, "behind": 0,
                             "recent_commits": ["abc subject (1h ago)"]})
        self.assertNotIn("- branch:", out)
        self.assertIn("- recent_commits:", out)
        self.assertIn("  - abc subject (1h ago)", out)

    def test_default_branch_with_delta_shows_line(self):
        out = self._render({"branch": "main", "ahead": 2, "behind": 1,
                            "upstream": "origin/main"})
        self.assertIn("- branch: main (ahead 2, behind 1 vs origin/main)", out)

    def test_feature_branch_always_shown(self):
        out = self._render({"branch": "feat/foo", "ahead": 0, "behind": 0})
        self.assertIn("- branch: feat/foo", out)

    def test_ahead_only(self):
        out = self._render({"branch": "main", "ahead": 3, "behind": 0,
                            "upstream": "origin/main"})
        self.assertIn("- branch: main (ahead 3 vs origin/main)", out)

    def test_no_git_progress_no_lines(self):
        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        out = render_header(ctx)
        self.assertNotIn("branch:", out)
        self.assertNotIn("recent_commits", out)


if __name__ == "__main__":
    unittest.main()
