"""`__main__.py` の stdin → stdout E2E スモーク。

クラウド CLI は起動しない (未設定 project の deny は verify 前に決まり、readonly /
非対象コマンドは検証しない)。
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

    def _run(self, command: str) -> subprocess.CompletedProcess:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.project),
        }
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def test_unconfigured_write_emits_deny_json(self):
        res = self._run("gh pr create")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("未設定", out["permissionDecisionReason"])

    def test_readonly_login_is_silent_and_invalidates(self):
        res = self._run("gh auth login --skip-ssh-key")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertTrue(
            (self.cache_tmp / "cc-mp-verify-cloud-account" / "github.epoch").is_file()
        )

    def test_non_target_command_is_silent(self):
        res = self._run("git status")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_invalid_json_is_silent(self):
        res = subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input="{not json",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)


if __name__ == "__main__":
    unittest.main()
