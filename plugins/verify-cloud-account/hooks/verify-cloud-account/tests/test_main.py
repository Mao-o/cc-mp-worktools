"""`__main__.py` の stdin → stdout E2E (PreToolUse / PostToolUse の振り分け)。

クラウド CLI は起動しない (未設定 project の deny は verify 前に決まり、PostToolUse は
検証しない)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401

_PKG_DIR = Path(__file__).resolve().parent.parent


class TestMainEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = Path(self.tmp) / "project"
        self.project.mkdir()
        self.cache_tmp = Path(self.tmp) / "cache"
        self.cache_tmp.mkdir()
        self.env = {
            **os.environ,
            "TMPDIR": str(self.cache_tmp),
            "CLAUDE_PROJECT_DIR": str(self.project),
        }

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def _epoch_file(self) -> Path:
        return self.cache_tmp / "cc-mp-verify-cloud-account" / "github.epoch"

    def test_post_tool_use_bumps_epoch_without_output(self):
        res = self._run(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "gh auth switch --user other"},
                "tool_response": {"stdout": "", "stderr": ""},
                "cwd": str(self.project),
            }
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertTrue(self._epoch_file().is_file())

    def test_post_tool_use_does_not_verify_or_deny(self):
        """未設定 project の write: PreToolUse は deny JSON、PostToolUse は無出力・無効化なし。"""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "cwd": str(self.project),
        }
        pre = self._run({**payload, "hook_event_name": "PreToolUse"})
        self.assertEqual(pre.returncode, 0, pre.stderr)
        self.assertIn('"deny"', pre.stdout)
        post = self._run({**payload, "hook_event_name": "PostToolUse"})
        self.assertEqual(post.returncode, 0, post.stderr)
        self.assertEqual(post.stdout, "")
        self.assertFalse(self._epoch_file().exists())

    def test_missing_event_name_is_treated_as_pre_tool_use(self):
        res = self._run(
            {"tool_input": {"command": "gh pr create"}, "cwd": str(self.project)}
        )
        self.assertIn('"deny"', res.stdout)

    def test_non_target_command_is_silent(self):
        res = self._run(
            {
                "hook_event_name": "PreToolUse",
                "tool_input": {"command": "git status"},
                "cwd": str(self.project),
            }
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
