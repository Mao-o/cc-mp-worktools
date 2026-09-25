"""worktree-cwd-guard の判定テスト (使い捨ての main checkout + linked worktree 上で実行する)。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from _testutil import make_repo

import importlib.util

import family

_PKG = Path(__file__).resolve().parent.parent


def _load_entry():
    # `__main__` という名前は実行中の unittest を指すため、ファイルから別名で読み込む
    spec = importlib.util.spec_from_file_location("worktree_cwd_guard_entry", _PKG / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


evaluate = _load_entry().evaluate


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def write(path: Path | str, cwd: Path) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "x"}, "cwd": str(cwd)}


def decision(out: dict | None) -> str | None:
    return None if out is None else out["hookSpecificOutput"].get("permissionDecision", "context")


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.main, self.a, self.b = make_repo(Path(self._tmp.name))
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("WORKTREE_CWD_GUARD_MODE", "WORKTREE_CWD_GUARD_ALLOW"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    # --- 対象の判定 ---------------------------------------------------------

    def test_detect_only_in_linked_worktree(self):
        self.assertIsNone(family.detect(str(self.main)))
        fam = family.detect(str(self.a))
        self.assertEqual(fam.home, family.norm(self.a))
        self.assertEqual(set(fam.roots), {family.norm(p) for p in (self.main, self.a, self.b)})
        outside = Path(self._tmp.name) / "plain"
        outside.mkdir()
        self.assertIsNone(family.detect(str(outside)))

    def test_nested_worktree_is_its_own_owner(self):
        # worktree A は main checkout の中にあるが、A 配下のパスの持ち主は A
        fam = family.detect(str(self.a))
        self.assertEqual(family.owner(family.norm(self.a / "x.py"), fam), family.norm(self.a))
        self.assertEqual(family.owner(family.norm(self.main / "x.py"), fam), family.norm(self.main))

    # --- Bash ---------------------------------------------------------------

    def test_git_write_into_other_checkout_is_denied(self):
        for cmd in (
            f'cd "{self.main}" && git checkout -b oops',
            f'git -C "{self.main}" switch -c oops',
            f'git -C "{self.b}" commit -m x',
            f"git --work-tree={self.main} reset --hard",
            f'git --git-dir="{self.main / ".git"}" checkout main',
            f'git worktree remove "{self.b}"',
            f"cd {self.main}; git stash",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_reads_and_own_worktree_are_allowed(self):
        for cmd in (
            "git status",
            "git commit -m ok",
            f'git -C "{self.main}" log --oneline -3',
            f'git -C "{self.main}" diff',
            "cd sub && git add .",
            f'cd "{self.a}" && git reset --hard',
            "git worktree add ../c",
            "echo git checkout",
            "ls",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_unresolved_target_only_warns(self):
        for cmd in ('cd "$WT" && git checkout main', 'git -C "$REPO" reset --hard'):
            with self.subTest(cmd=cmd):
                out = evaluate(bash(cmd, self.a))
                self.assertEqual(decision(out), "context")
                self.assertIn("静的に解決できない", out["hookSpecificOutput"]["additionalContext"])

    def test_main_checkout_session_is_not_guarded(self):
        self.assertIsNone(evaluate(bash(f'git -C "{self.b}" checkout main', self.main)))

    # --- Write / Edit -------------------------------------------------------

    def test_file_write_into_other_checkout_is_denied(self):
        self.assertEqual(decision(evaluate(write(self.main / "README.md", self.a))), "deny")
        self.assertEqual(decision(evaluate(write(self.b / "new.py", self.a))), "deny")
        edit = {"tool_name": "Edit", "tool_input": {"file_path": "../../../README.md"}, "cwd": str(self.a)}
        self.assertEqual(decision(evaluate(edit)), "deny")

    def test_file_write_inside_own_worktree_or_outside_repo_is_allowed(self):
        self.assertIsNone(evaluate(write(self.a / "src" / "x.py", self.a)))
        self.assertIsNone(evaluate(write("rel/x.py", self.a)))
        self.assertIsNone(evaluate(write(Path(self._tmp.name) / "scratch.txt", self.a)))

    # --- 設定 ---------------------------------------------------------------

    def test_modes_and_allowlist(self):
        cmd = f'git -C "{self.main}" checkout -b oops'
        os.environ["WORKTREE_CWD_GUARD_MODE"] = "warn"
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "context")
        os.environ["WORKTREE_CWD_GUARD_MODE"] = "off"
        self.assertIsNone(evaluate(bash(cmd, self.a)))
        del os.environ["WORKTREE_CWD_GUARD_MODE"]
        os.environ["WORKTREE_CWD_GUARD_ALLOW"] = str(self.main)
        self.assertIsNone(evaluate(bash(cmd, self.a)))
        self.assertEqual(decision(evaluate(bash(f'git -C "{self.b}" checkout -b oops', self.a))), "deny")


class EntryTest(unittest.TestCase):
    """__main__ を subprocess で起動し、stdin / stdout の往復 (UTF-8) を確かめる。"""

    def test_deny_roundtrip_with_japanese(self):
        with tempfile.TemporaryDirectory() as tmp:
            main, a, _ = make_repo(Path(tmp))
            payload = json.dumps(bash(f'git -C "{main}" commit -m "日本語のメッセージ"', a), ensure_ascii=False)
            env = {k: v for k, v in os.environ.items() if not k.startswith("WORKTREE_CWD_GUARD")}
            r = subprocess.run(
                [sys.executable, str(_PKG)], input=payload.encode("utf-8"), capture_output=True, env=env, timeout=60
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout.decode("utf-8"))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_garbage_input_is_silent(self):
        r = subprocess.run([sys.executable, str(_PKG)], input=b"not json", capture_output=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()
