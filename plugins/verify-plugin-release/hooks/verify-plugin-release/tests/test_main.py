"""__main__.py (PreToolUse hook の入口) を subprocess で実行する end-to-end テスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401
from _testutil import bump, commit_all, make_marketplace, sh, write, write_json

_PKG = Path(__file__).resolve().parent.parent


def run_hook(payload: dict | str, env_extra: dict | None = None) -> dict | None:
    env = {k: v for k, v in os.environ.items() if k != "VERIFY_PLUGIN_RELEASE_MODE"}
    env.update(env_extra or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run(
        [sys.executable, str(_PKG)],
        input=data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_marketplace(Path(self._tmp.name) / "repo", ["alpha"])
        # validate は claude の有無で SKIP / WARN が変わるが、どちらも deny には効かない
        write_json(self.root, ".claude/verify-plugin-release.json", {"fetch": False})
        sh(self.root, "switch", "-q", "-c", "feat")

    def tearDown(self):
        self._tmp.cleanup()

    def decision(self, out: dict | None) -> str | None:
        return None if out is None else out["hookSpecificOutput"].get("permissionDecision")

    def test_unrelated_command_is_silent(self):
        self.assertIsNone(run_hook(bash("git status", self.root)))
        self.assertIsNone(run_hook({"tool_name": "Read", "tool_input": {}}))
        self.assertIsNone(run_hook("not json"))

    def test_incomplete_release_is_denied(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash("gh pr create -t x -b y", self.root))
        self.assertEqual(self.decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("version[alpha]", reason)
        self.assertIn("changelog[alpha]", reason)

    def test_complete_release_is_allowed_silently_or_with_context(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        out = run_hook(bash("gh pr create -t x -b y", self.root))
        self.assertNotEqual(self.decision(out), "deny")

    def test_draft_and_warn_mode_do_not_block(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash("gh pr create --draft -t x", self.root))
        self.assertIsNone(self.decision(out))
        self.assertIn("version[alpha]", out["hookSpecificOutput"]["additionalContext"])
        out = run_hook(bash("gh pr create -t x", self.root), {"VERIFY_PLUGIN_RELEASE_MODE": "warn"})
        self.assertIsNone(self.decision(out))
        self.assertIsNone(run_hook(bash("gh pr create -t x", self.root), {"VERIFY_PLUGIN_RELEASE_MODE": "off"}))

    def test_broken_config_is_denied(self):
        write(self.root, ".claude/verify-plugin-release.json", "{")
        out = run_hook(bash("gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("ゲートを完了できなかった", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_into_repo_is_followed(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash(f'cd "{self.root}" && gh pr create -t x', self.root.parent))
        self.assertEqual(self.decision(out), "deny")

    def test_non_plugin_repo_is_ignored(self):
        other = Path(self._tmp.name) / "plain"
        other.mkdir()
        sh(other, "init", "-q", "-b", "main")
        self.assertIsNone(run_hook(bash("gh pr create -t x", other)))

    def test_outside_git_is_ignored(self):
        outside = Path(self._tmp.name) / "nogit"
        outside.mkdir()
        self.assertIsNone(run_hook(bash("gh pr create -t x", outside), {"GIT_CEILING_DIRECTORIES": self._tmp.name}))


class ManualCheckTest(unittest.TestCase):
    def test_manual_check_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_marketplace(Path(tmp) / "repo", ["alpha"])
            write_json(root, ".claude/verify-plugin-release.json", {"fetch": False})
            sh(root, "switch", "-q", "-c", "feat")
            write(root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
            commit_all(root, "change")
            r = subprocess.run(
                [sys.executable, str(_PKG), "check", str(root)],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("FAIL  version[alpha]", r.stdout)


if __name__ == "__main__":
    unittest.main()
