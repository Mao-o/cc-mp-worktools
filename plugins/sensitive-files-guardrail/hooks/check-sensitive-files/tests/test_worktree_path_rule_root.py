"""Stop hook: ``$CLAUDE_PROJECT_DIR`` が main を指したまま worktree で発火しても
path 形 rule が効き、レシピが worktree のパスを含まないこと (0.35.0)。

セッション途中で worktree に入る / sub-agent の ``isolation: worktree`` では
cwd が worktree、``$CLAUDE_PROJECT_DIR`` が main のままになりうる。0.34.x までは
``git ls-files`` の path を main 基準の root 相対 (``.claude/worktrees/x/...``) に
組み立てていたため、repo 同梱 tier で承認した ``!certs/aws.pem`` が一致せず、
レシピも ``!.claude/worktrees/x/certs/...`` (worktree を作り直すと効かない) を
案内していた。外部 worktree (main の外) では root 配下でないため basename 形の
案内しか出なかった。

実 git (``git worktree add``) で構成する。
"""
from __future__ import annotations

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

_TIER_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.txt"
_LOCAL_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
_CERT = "-----BEGIN CERTIFICATE-----\nMIIBpublic\n-----END CERTIFICATE-----\n"


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _load_entry():
    import importlib.util

    entry_path = Path(__file__).resolve().parent.parent / "__main__.py"
    spec = importlib.util.spec_from_file_location("check_entry_worktree", entry_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(hook_input: dict) -> str:
    entry = _load_entry()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(json.dumps(hook_input))
        sys.stdout = io.StringIO()
        rc = entry.main()
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    assert rc == 0
    return out


def _reason(out: str) -> str:
    return json.loads(out)["reason"]


class Base(unittest.TestCase):
    """main repo に ``certs/aws.pem`` / ``certs/other.pem`` を commit し、repo 同梱
    tier で ``!certs/aws.pem`` だけを承認する。worktree は nested
    (``.claude/worktrees/x``) と外部 (main の外) の 2 つ。"""

    tracked = ("aws.pem", "other.pem")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home), "USERPROFILE": str(self.home),
                "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        os.environ.pop("SFG_CASE_SENSITIVE", None)

        self.main = self.tmp / "repo"
        self.main.mkdir()
        _git(["init", "--initial-branch=main"], self.main)
        _git(["config", "user.name", "test"], self.main)
        _git(["config", "user.email", "test@example.com"], self.main)
        _git(["config", "commit.gpgsign", "false"], self.main)
        for name in self.tracked:
            cert = self.main / "certs" / name
            cert.parent.mkdir(parents=True, exist_ok=True)
            cert.write_text(_CERT)
        tier = self.main / _TIER_REL
        tier.parent.mkdir(parents=True)
        tier.write_text("!certs/aws.pem\n")
        _git(["add", "-A"], self.main)
        _git(["commit", "-m", "init"], self.main)
        self.wt = self.main / ".claude" / "worktrees" / "x"
        _git(["worktree", "add", "-b", "wt-x", str(self.wt)], self.main)
        self.ext = self.tmp / "repo-ext"
        _git(["worktree", "add", "-b", "wt-ext", str(self.ext)], self.main)
        self._sid = 0

    def _stop(self, cwd: Path, project_dir: Path) -> str:
        os.environ["CLAUDE_PROJECT_DIR"] = str(project_dir)
        self._sid += 1
        return _run_main({"cwd": str(cwd), "session_id": f"wt-{self._sid}"})


class TestStopAcrossCheckouts(Base):
    def test_worktree_cwd_with_main_project_dir(self):
        """セッション途中で worktree に入った形 (cwd = worktree、PD = main)。"""
        reason = _reason(self._stop(self.wt, self.main))
        self.assertIn("  - certs/other.pem", reason)
        self.assertNotIn("certs/aws.pem", reason)
        self.assertIn("  !certs/other.pem", reason)
        self.assertNotIn("!.claude/worktrees", reason)

    def test_external_worktree_cwd_with_main_project_dir(self):
        """main の外に作った worktree。0.34.x は「root 配下でない」で path 形 rule が
        効かず、案内も basename 形だけだった。"""
        reason = _reason(self._stop(self.ext, self.main))
        self.assertNotIn("certs/aws.pem", reason)
        self.assertIn("  !certs/other.pem", reason)
        self.assertNotIn("basename 形 — プロジェクト root を解決できない", reason)

    def test_worktree_session_is_unchanged(self):
        """``claude -w`` の形 (PD = worktree 自身)。0.32.0 から効いていた経路。"""
        reason = _reason(self._stop(self.wt, self.wt))
        self.assertNotIn("certs/aws.pem", reason)
        self.assertIn("  !certs/other.pem", reason)

    def test_recipe_from_a_worktree_silences_the_file_everywhere(self):
        """worktree で案内されたレシピを書けば、main でも worktree でも同じ
        1 ファイルが報告から消える。"""
        reason = _reason(self._stop(self.wt, self.main))
        self.assertIn("  !certs/other.pem", reason)
        local = self.home / _LOCAL_REL
        # 1 回目の Stop が stop-ack の state を同じディレクトリに作っている
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(f"[project:{self.main}]\n!certs/other.pem\n")
        self.assertEqual(self._stop(self.wt, self.main), "")
        self.assertEqual(self._stop(self.main, self.main), "")
        self.assertEqual(self._stop(self.ext, self.main), "")


class TestStopQuietWhenOnlyApprovedFile(Base):
    tracked = ("aws.pem",)

    def test_no_block_in_any_checkout(self):
        for cwd, project_dir in (
            (self.main, self.main),
            (self.wt, self.main),
            (self.ext, self.main),
            (self.wt, self.wt),
        ):
            with self.subTest(cwd=cwd.name, project_dir=project_dir.name):
                self.assertEqual(self._stop(cwd, project_dir), "")


if __name__ == "__main__":
    unittest.main()
