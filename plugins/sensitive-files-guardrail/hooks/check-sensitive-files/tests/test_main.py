"""__main__.py (Stop hook エントリポイント) の挙動テスト。

- 通常の block 動作 (tracked/untracked セクション分け)
- patterns.txt 読込失敗時の fail-open (exit 0 + 空出力 + stderr warning)
- session 単位の once-only 化と恒久除外レシピ (0.19.0, ``TestMainSessionAck``)
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


class TestMainSessionAck(BaseMainTest):
    """0.19.0 (bd_092a232e-snw.2): session 単位の once-only 化。

    同一 session で同じ (status, path) 集合なら 2 回目以降は exit 0。新しい
    ファイルが増えた / status が変わったときだけ再 block。session_id が無い /
    不正なら従来通り毎回 block。state は HOME 配下 (``BaseMainTest`` が tmp に
    隔離)。block reason には ``[project:$CLAUDE_PROJECT_DIR]`` + ``!<basename>``
    の恒久除外レシピを載せ、絶対パスは出さない。
    """

    def _track(self, name: str) -> None:
        (self.repo / name).write_text("KEY=v\n")
        _git(["add", name], str(self.repo))
        _git(["commit", "-m", f"add {name}"], str(self.repo))

    def _state_dir(self) -> Path:
        return self.home_dir / ".claude" / "sensitive-files-guardrail" / "stop-ack"

    @staticmethod
    def _blocked(out: str) -> bool:
        return bool(out.strip()) and json.loads(out)["decision"] == "block"

    def test_same_session_second_stop_is_silent(self):
        self._track(".env")
        env = {"cwd": str(self.repo), "session_id": "sess-1"}
        rc, out, _ = _run_main(env)
        self.assertEqual(rc, 0)
        self.assertTrue(self._blocked(out))
        self.assertTrue((self._state_dir() / "sess-1").is_file())
        rc, out, _ = _run_main(env)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_new_file_reblocks_then_silent_again(self):
        self._track(".env")
        env = {"cwd": str(self.repo), "session_id": "sess-2"}
        self.assertTrue(self._blocked(_run_main(env)[1]))
        (self.repo / ".env.production").write_text("SECRET=v\n")
        out = _run_main(env)[1]
        self.assertTrue(self._blocked(out))
        self.assertIn(".env.production", json.loads(out)["reason"])
        # 3 回目: 集合が増えていないので黙る
        self.assertEqual(_run_main(env)[1], "")

    def test_status_change_untracked_to_tracked_reblocks(self):
        (self.repo / ".env").write_text("KEY=v\n")
        env = {"cwd": str(self.repo), "session_id": "sess-3"}
        out = _run_main(env)[1]
        self.assertTrue(self._blocked(out))
        self.assertIn("【untracked】", json.loads(out)["reason"])
        _git(["add", ".env"], str(self.repo))
        _git(["commit", "-m", "track env"], str(self.repo))
        out = _run_main(env)[1]
        self.assertTrue(self._blocked(out))
        self.assertIn("【tracked】", json.loads(out)["reason"])

    def test_resolved_file_does_not_reblock_remaining(self):
        # 2 件報告 → 1 件対応 (削除) → 残り 1 件は報告済みなので黙る
        self._track(".env")
        (self.repo / ".env.production").write_text("SECRET=v\n")
        env = {"cwd": str(self.repo), "session_id": "sess-4"}
        self.assertTrue(self._blocked(_run_main(env)[1]))
        (self.repo / ".env.production").unlink()
        self.assertEqual(_run_main(env)[1], "")

    def test_missing_session_id_blocks_every_time(self):
        self._track(".env")
        env = {"cwd": str(self.repo)}
        self.assertTrue(self._blocked(_run_main(env)[1]))
        self.assertTrue(self._blocked(_run_main(env)[1]))
        self.assertFalse(self._state_dir().exists())

    def test_unsafe_session_id_is_ignored(self):
        self._track(".env")
        for bad in ("../escape", "a/b", "", ".hidden", 123, None):
            env = {"cwd": str(self.repo), "session_id": bad}
            self.assertTrue(self._blocked(_run_main(env)[1]), msg=repr(bad))
            self.assertTrue(self._blocked(_run_main(env)[1]), msg=repr(bad))
        self.assertFalse(self._state_dir().exists())
        self.assertFalse((self.home_dir / ".claude" / "escape").exists())

    def test_other_repo_in_same_session_blocks_again(self):
        # state は cwd を scope に含むため、別 repo の同名 .env は報告済み扱いに
        # ならない (同一 session 内で cd した場合)
        self._track(".env")
        env = {"cwd": str(self.repo), "session_id": "sess-x"}
        self.assertTrue(self._blocked(_run_main(env)[1]))
        other = Path(self.tmp) / "other-repo"
        other.mkdir()
        _init_repo(str(other))
        (other / ".env").write_text("KEY=v\n")
        _git(["add", ".env"], str(other))
        _git(["commit", "-m", "add env"], str(other))
        out = _run_main({"cwd": str(other), "session_id": "sess-x"})[1]
        self.assertTrue(self._blocked(out))
        # 元の repo に戻っても報告済みのまま (集合は union で保持)
        self.assertEqual(_run_main(env)[1], "")

    def _track_in_sub(self) -> Path:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / ".env").write_text("KEY=v\n")
        _git(["add", "sub/.env"], str(self.repo))
        _git(["commit", "-m", "add sub env"], str(self.repo))
        return sub

    def test_subdirectory_cwd_shares_ack_with_root(self):
        # root と sub で同じ物理ファイルは同じ digest (Codex R2 P2-2):
        # root で block → sub に cd しても再 block しない (逆方向も同じ)
        sub = self._track_in_sub()
        root_env = {"cwd": str(self.repo), "session_id": "sess-sub"}
        sub_env = {"cwd": str(sub), "session_id": "sess-sub"}
        self.assertTrue(self._blocked(_run_main(root_env)[1]))
        self.assertEqual(_run_main(sub_env)[1], "")
        root_env2 = {"cwd": str(self.repo), "session_id": "sess-sub2"}
        sub_env2 = {"cwd": str(sub), "session_id": "sess-sub2"}
        self.assertTrue(self._blocked(_run_main(sub_env2)[1]))
        self.assertEqual(_run_main(root_env2)[1], "")

    def test_subdirectory_reason_keeps_cwd_relative_paths(self):
        # 表示は従来通り cwd 相対 (`git rm --cached <path>` を cwd で実行できる)
        sub = self._track_in_sub()
        out = _run_main({"cwd": str(sub), "session_id": "sess-disp"})[1]
        reason = json.loads(out)["reason"]
        self.assertIn("  - .env", reason)
        self.assertNotIn("sub/.env", reason)

    def test_root_file_found_after_moving_from_sub_reblocks(self):
        # sub で block した集合に root 直下の .env は含まれない → root に戻ると
        # 新規として再 block (サブディレクトリのスキャンは従来通り subtree のみ)
        sub = self._track_in_sub()
        (self.repo / ".env").write_text("KEY=v\n")
        _git(["add", ".env"], str(self.repo))
        _git(["commit", "-m", "add root env"], str(self.repo))
        self.assertTrue(
            self._blocked(_run_main({"cwd": str(sub), "session_id": "sess-up"})[1])
        )
        out = _run_main({"cwd": str(self.repo), "session_id": "sess-up"})[1]
        self.assertTrue(self._blocked(out))
        self.assertIn("sub/.env", json.loads(out)["reason"])

    def test_literal_dollar_project_header_applies(self):
        # `$` を含む repo パスの [project:] セクションは正当 (Codex R2 P2-1)。
        # placeholder 扱いで黙って落ちると、その repo の除外が効かず block が続く
        repo = Path(self.tmp) / "proj$prod"
        repo.mkdir()
        _init_repo(str(repo))
        (repo / ".env").write_text("KEY=v\n")
        _git(["add", ".env"], str(repo))
        _git(["commit", "-m", "add env"], str(repo))
        d = self.home_dir / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True)
        (d / "patterns.local.txt").write_text(f"[project:{repo}]\n!.env\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            _, out, err = _run_main(
                {"cwd": str(repo), "session_id": "sess-dollar"}
            )
        self.assertEqual(out, "")
        self.assertNotIn("local_patterns_header_invalid", err)

    def test_different_session_blocks_again(self):
        self._track(".env")
        out_a = _run_main({"cwd": str(self.repo), "session_id": "s-a"})[1]
        out_b = _run_main({"cwd": str(self.repo), "session_id": "s-b"})[1]
        self.assertTrue(self._blocked(out_a))
        self.assertTrue(self._blocked(out_b))

    def test_state_dir_unwritable_still_blocks(self):
        # state 機構の失敗は「従来通り block」側に倒す (block が消える方向にしない)
        self._track(".env")
        parent = self.home_dir / ".claude" / "sensitive-files-guardrail"
        parent.mkdir(parents=True)
        (parent / "stop-ack").write_text("not a dir\n")
        env = {"cwd": str(self.repo), "session_id": "sess-5"}
        self.assertTrue(self._blocked(_run_main(env)[1]))
        self.assertTrue(self._blocked(_run_main(env)[1]))

    def test_reason_has_recipe_and_session_note_without_absolute_path(self):
        self._track(".env")
        (self.repo / "server.pem").write_text("cert\n")
        env = {"cwd": str(self.repo), "session_id": "sess-6"}
        reason = json.loads(_run_main(env)[1])["reason"]
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        self.assertIn("!.env", reason)
        self.assertIn("!server.pem", reason)
        self.assertIn("patterns.local.txt", reason)
        self.assertIn("git rm --cached <path>", reason)
        self.assertIn("再度 block しません", reason)
        self.assertNotIn(str(self.repo), reason)
        self.assertNotIn(str(self.home_dir), reason)

    def test_reason_without_session_has_no_session_note(self):
        self._track(".env")
        reason = json.loads(_run_main({"cwd": str(self.repo)})[1])["reason"]
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        self.assertNotIn("再度 block しません", reason)

    def test_state_file_stores_digests_not_paths(self):
        self._track(".env")
        _run_main({"cwd": str(self.repo), "session_id": "sess-7"})
        content = (self._state_dir() / "sess-7").read_text()
        self.assertNotIn(".env", content)
        self.assertTrue(content.strip())
        for line in content.splitlines():
            self.assertRegex(line, r"^[0-9a-f]{64}$")

    def test_state_failure_is_reported_on_stderr(self):
        self._track(".env")
        parent = self.home_dir / ".claude" / "sensitive-files-guardrail"
        parent.mkdir(parents=True)
        (parent / "stop-ack").write_text("not a dir\n")
        _, out, err = _run_main({"cwd": str(self.repo), "session_id": "sess-9"})
        self.assertTrue(self._blocked(out))
        self.assertIn("stop_ack_unavailable", err)

    def test_unexpanded_project_header_in_local_patterns_warns(self):
        # 案内の `$CLAUDE_PROJECT_DIR` を literal に残すと除外は効かない (block
        # 維持) が、黙らず stderr で原因を出す (L2 review)
        self._track(".env")
        d = self.home_dir / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True)
        (d / "patterns.local.txt").write_text(
            "[project:$CLAUDE_PROJECT_DIR]\n!.env\n"
        )
        _, out, err = _run_main({"cwd": str(self.repo), "session_id": "sess-10"})
        self.assertTrue(self._blocked(out))
        self.assertIn("local_patterns_header_invalid", err)
        self.assertIn("project_header_unexpanded_placeholder", err)

    def test_correct_project_header_silences_stop(self):
        # 実パスで書けば除外が効いて報告されない (レシピの成功経路)
        self._track(".env")
        d = self.home_dir / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True)
        (d / "patterns.local.txt").write_text(f"[project:{self.repo}]\n!.env\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            _, out, err = _run_main(
                {"cwd": str(self.repo), "session_id": "sess-11"}
            )
        self.assertEqual(out, "")
        self.assertNotIn("local_patterns_header_invalid", err)

    def test_stop_hook_active_still_wins(self):
        self._track(".env")
        env = {
            "cwd": str(self.repo),
            "session_id": "sess-8",
            "stop_hook_active": True,
        }
        self.assertEqual(_run_main(env)[1], "")
        self.assertFalse(self._state_dir().exists())


if __name__ == "__main__":
    unittest.main()
