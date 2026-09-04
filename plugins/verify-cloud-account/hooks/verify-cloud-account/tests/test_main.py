"""`__main__.py` の stdin → stdout E2E スモーク。

クラウド CLI は起動しない (未設定 project の deny は verify 前に決まり、readonly /
非対象コマンドは検証しない)。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

_PKG_DIR = Path(__file__).resolve().parent.parent


def _load_entry_module():
    """`__main__.py` を `__main__` 以外の名前でロードする (import 名の衝突回避)。

    `python3 <pkg_dir>` (subprocess E2E) 以外に、内部エラー時の fail-open を
    プロセスを跨がず直接ユニットテストするために使う。
    """
    spec = importlib.util.spec_from_file_location(
        "verify_cloud_account_entry", _PKG_DIR / "__main__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class TestMainInternalErrorFailOpen(unittest.TestCase):
    """内部バックログ: dispatch() の未捕捉例外は exit 1 の無音 fail-open ではなく、
    additionalContext の warn として明示し、stderr にも同じ理由を出す。"""

    def test_dispatch_exception_emits_warn_json_and_stderr(self):
        module = _load_entry_module()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "cwd": "/tmp",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(module, "dispatch", side_effect=RuntimeError("boom")):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr):
                module.main()
        out = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        self.assertIn("内部エラーのため検証をスキップしました", out["additionalContext"])
        self.assertIn("RuntimeError", out["additionalContext"])
        self.assertIn("boom", out["additionalContext"])
        # permissionDecision (deny) は含まない = 実行を阻止しない (fail-open)。
        self.assertNotIn("permissionDecision", out)
        self.assertIn("内部エラーのため検証をスキップしました", stderr.getvalue())
        self.assertIn("RuntimeError", stderr.getvalue())

    def test_no_exception_does_not_write_stderr(self):
        """正常系 (DEBUG 無効) では stderr に何も書かない (回帰防止)。"""
        module = _load_entry_module()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "cwd": "/tmp",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(module, "dispatch", return_value=None):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr):
                module.main()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
