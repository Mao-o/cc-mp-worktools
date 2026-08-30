"""cursor/codex はリポジトリ root (payload の cwd ではなく worktree_root) で起動すること。

サブディレクトリで Claude Code を起動したセッションでは、hook プロセス自身の cwd と
リポジトリ root がずれる。cursor/codex をその cwd のまま起動すると、レビュー対象の
プランがリポジトリ全体を参照する前提と食い違う (内部バックログ)。
"""
import os
import tempfile
import unittest

import _testutil
from _testutil import FINDINGS, PLAN, HookTestCase

SESSION = "sess-cwd-0001"


class TestReviewerLaunchCwd(HookTestCase):
    def test_reviewers_are_invoked_with_worktree_root_as_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _testutil.init_repo(os.path.join(tmp, "repo"))
            sub = os.path.join(repo, "sub")
            os.makedirs(sub)

            self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS, cwd=sub)

            self.assertEqual(self.cursor_cwds, [repo])
            self.assertEqual(self.codex_cwds, [repo])

    def test_outside_git_repo_falls_back_to_none(self):
        """git 外 (worktree_root が None) なら cwd を渡さず、レビュアーの既定に委ねる。"""
        self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)  # cwd=self.tmpdir (非 git)
        self.assertEqual(self.cursor_cwds, [None])
        self.assertEqual(self.codex_cwds, [None])


if __name__ == "__main__":
    unittest.main()
