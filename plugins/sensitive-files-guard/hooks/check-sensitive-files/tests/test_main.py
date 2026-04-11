"""__main__.py (Stop hook エントリポイント) の挙動テスト。

- 通常の block 動作 (tracked/untracked セクション分け)
- patterns.txt 読込失敗時の fail-open (exit 0 + 空出力 + stderr warning)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("check_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _init_repo(cwd: str) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.name", "test"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "commit.gpgsign", "false"], cwd)


def _run_main(envelope: dict) -> tuple[int, str, str]:
    entry = _load_entry()
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    try:
        sys.stdin = io.StringIO(json.dumps(envelope))
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        rc = entry.main()
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return rc, out, err


class BaseMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.home_dir = Path(self.tmp) / "home"
        self.xdg_dir = Path(self.tmp) / "xdg"
        self.home_dir.mkdir()
        self.xdg_dir.mkdir()
        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.xdg_dir),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        _init_repo(str(self.repo))

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMainBlockReason(BaseMainTest):
    def test_tracked_and_untracked_sections(self):
        # tracked .env (gitignore 済み)
        (self.repo / ".env").write_text("KEY=v\n")
        _git(["add", ".env"], str(self.repo))
        _git(["commit", "-m", "add env"], str(self.repo))
        (self.repo / ".gitignore").write_text(".env\n")
        _git(["add", ".gitignore"], str(self.repo))
        _git(["commit", "-m", "gitignore"], str(self.repo))
        # untracked .env.production
        (self.repo / ".env.production").write_text("SECRET=v\n")

        rc, out, err = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        reason = payload["reason"]
        self.assertIn("【tracked】", reason)
        self.assertIn(".env", reason)
        self.assertIn("git rm --cached", reason)
        self.assertIn("【untracked】", reason)
        self.assertIn(".env.production", reason)

    def test_untracked_only(self):
        (self.repo / ".env").write_text("KEY=v\n")
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        self.assertNotIn("【tracked】", payload["reason"])
        self.assertIn("【untracked】", payload["reason"])

    def test_no_sensitive_files_no_output(self):
        (self.repo / "README.md").write_text("# hi\n")
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_stop_hook_active_skips(self):
        (self.repo / ".env").write_text("KEY=v\n")
        rc, out, _ = _run_main({"cwd": str(self.repo), "stop_hook_active": True})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_non_git_cwd_noop(self):
        non_git = Path(self.tmp) / "not-a-repo"
        non_git.mkdir()
        rc, out, _ = _run_main({"cwd": str(non_git)})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class TestMainFailOpen(BaseMainTest):
    """patterns.txt 読込失敗時は fail-open (exit 0 + 空出力 + stderr warning)。"""

    def test_permission_error_on_patterns_file(self):
        (self.repo / ".env").write_text("KEY=v\n")
        original_read_text = Path.read_text

        def fake_read_text(self_path: Path, *args, **kwargs):
            if self_path.name == "patterns.txt":
                raise PermissionError("mock permission denied")
            return original_read_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fake_read_text):
            rc, out, err = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("patterns_unavailable", err)
        self.assertIn("PermissionError", err)

    def test_oserror_on_patterns_file(self):
        (self.repo / ".env").write_text("KEY=v\n")
        original_read_text = Path.read_text

        def fake_read_text(self_path: Path, *args, **kwargs):
            if self_path.name == "patterns.txt":
                raise OSError("mock IO error")
            return original_read_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fake_read_text):
            rc, out, err = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("patterns_unavailable", err)

    def test_file_not_found_on_patterns(self):
        (self.repo / ".env").write_text("KEY=v\n")
        original_read_text = Path.read_text

        def fake_read_text(self_path: Path, *args, **kwargs):
            if self_path.name == "patterns.txt":
                raise FileNotFoundError("mock not found")
            return original_read_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fake_read_text):
            rc, out, err = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("patterns_unavailable", err)


if __name__ == "__main__":
    unittest.main()
