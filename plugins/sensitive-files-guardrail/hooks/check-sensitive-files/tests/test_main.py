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
import re
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


class TestSubmoduleGuidance(BaseMainTest):
    """内部バックログ: submodule 内 tracked ファイルに対する案内分岐。

    親 repo からの `git rm --cached` は submodule 配下のファイルには効かない
    (submodule は別の git index を持つため)。恒久除外レシピ以外に脱出路が
    無いまま毎ターン同じ block を受け続けるのを避けるため、submodule 配下の
    ファイルを検出したときは案内文を分岐し、submodule ディレクトリ内で対処
    する旨を追記する。
    """

    def _add_submodule(self) -> bool:
        (self.repo / "README.md").write_text("# super\n")
        _git(["add", "README.md"], str(self.repo))
        _git(["commit", "-m", "init"], str(self.repo))
        subrepo = Path(self.tmp) / "subrepo"
        subrepo.mkdir()
        _init_repo(str(subrepo))
        (subrepo / ".env").write_text("SUB_SECRET=v\n")
        _git(["add", ".env"], str(subrepo))
        _git(["commit", "-m", "add env"], str(subrepo))
        try:
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{subrepo}", "submod",
                ],
                cwd=str(self.repo),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return False
        _git(["commit", "-m", "add submod"], str(self.repo))
        return True

    def test_submodule_tracked_file_gets_branching_guidance(self):
        if not self._add_submodule():
            self.skipTest("git submodule add unsupported in this env")
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        reason = payload["reason"]
        self.assertIn("submod/.env", reason)
        self.assertIn("親 repo からは効きません", reason)
        self.assertIn("`submod`", reason)

    def test_non_submodule_tracked_file_has_no_branching_guidance(self):
        (self.repo / ".env").write_text("KEY=v\n")
        _git(["add", ".env"], str(self.repo))
        _git(["commit", "-m", "add env"], str(self.repo))
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        reason = json.loads(out)["reason"]
        self.assertIn(".env", reason)
        self.assertNotIn("親 repo からは効きません", reason)

    def test_untracked_only_has_no_branching_guidance(self):
        # tracked が空 (untracked のみ) のときは submodule 判定自体を行わない
        (self.repo / ".env").write_text("KEY=v\n")
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        reason = json.loads(out)["reason"]
        self.assertIn("【untracked】", reason)
        self.assertNotIn("親 repo からは効きません", reason)


class TestMainSessionAck(BaseMainTest):
    """0.19.0 (内部バックログ): session 単位の once-only 化。

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
        # 表示は従来通り cwd 相対 (`git rm --cached <path>` を cwd で実行できる)。
        # 0.24.0: 恒久除外レシピだけは root 相対 (path 形は root 基準で効くため)
        sub = self._track_in_sub()
        out = _run_main({"cwd": str(sub), "session_id": "sess-disp"})[1]
        reason = json.loads(out)["reason"]
        self.assertIn("  - .env", reason)
        self.assertNotIn("  - sub/.env", reason)
        self.assertIn("!sub/.env", reason)

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

    def test_submodule_cwd_shares_ack_with_superproject(self):
        # superproject から --recurse-submodules で拾う `submod/.env` と、submodule
        # 内 cwd (toplevel = submodule root) で拾う `.env` は同じ物理ファイル →
        # 同じ digest で再 block しない (Codex R4 P2-1)
        (self.repo / "README.md").write_text("# super\n")
        _git(["add", "README.md"], str(self.repo))
        _git(["commit", "-m", "init"], str(self.repo))
        subrepo = Path(self.tmp) / "subrepo"
        subrepo.mkdir()
        _init_repo(str(subrepo))
        (subrepo / ".env").write_text("SUB_SECRET=v\n")
        _git(["add", ".env"], str(subrepo))
        _git(["commit", "-m", "add env"], str(subrepo))
        try:
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{subrepo}", "submod",
                ],
                cwd=str(self.repo),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            self.skipTest("git submodule add unsupported in this env")
        _git(["commit", "-m", "add submod"], str(self.repo))
        submod = self.repo / "submod"

        super_env = {"cwd": str(self.repo), "session_id": "sess-submod"}
        sub_env = {"cwd": str(submod), "session_id": "sess-submod"}
        out = _run_main(super_env)[1]
        self.assertTrue(self._blocked(out))
        self.assertIn("submod/.env", json.loads(out)["reason"])
        self.assertEqual(_run_main(sub_env)[1], "")
        # 逆方向: submodule 内で先に block → superproject では同じ集合なので黙る
        super_env2 = {"cwd": str(self.repo), "session_id": "sess-submod2"}
        sub_env2 = {"cwd": str(submod), "session_id": "sess-submod2"}
        out = _run_main(sub_env2)[1]
        self.assertTrue(self._blocked(out))
        self.assertIn("  - .env", json.loads(out)["reason"])
        self.assertEqual(_run_main(super_env2)[1], "")

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


# 0.26.0 (予算導入前) の ``_build_reason(tracked=[".env"],
# untracked=[".env.production"], session_scoped=False, root_offset_="")`` の
# 出力を ``git archive main`` 経由で採取したもの (内部バックログ)。
# byte 予算のための restructure が既存の見た目 (セクション順・空行位置) を
# 変えていないことのピン留め — substring 突合だけでは「順序が入れ替わった」
# 類の退行を検出できないため。
_EXPECTED_SMALL_REASON = '【セキュリティ確認】\n\n【tracked】以下のファイルは git で追跡中で、機密パターンに一致します:\n  - .env\n対応: `.gitignore` に追加した上で `git rm --cached <path>` を実行してください (index から外すだけで実ファイルは残ります)。\n\n【untracked】以下のファイルは機密パターンに一致し、まだ `.gitignore` 未登録です:\n  - .env.production\n対応: `.gitignore` に追加するか、意図的に管理対象とするか確認してください。\n\nAskUserQuestion ツールで各ファイルについてユーザーに確認してください:\n  選択肢1: 「.gitignore に追加」 (Recommended)\n  選択肢2: 「意図的に管理対象とする」\n\n【恒久除外】「意図的に管理対象とする」が選ばれた場合は、ユーザーの承認を得た上で `~/.claude/sensitive-files-guardrail/patterns.local.txt` に次を追記します ($CLAUDE_PROJECT_DIR は展開されないので、プロジェクト root の絶対パスを literal に書く (例: [project:/abs/path/to/repo])。全プロジェクト共通にしたい場合のみヘッダー無しの行に書く)。影響範囲: path 形 (`!<root 相対パス>`) は**その 1 ファイルだけ** (root 配下のみ)。basename 形 (`!<名前>`) は同じ名前のファイルが**すべて**対象で、**同名ディレクトリの配下も外れます** (配下が別の include 行に単独一致する場合はそちらが優先)。`[project:]` は rule の読込先を決めるだけなので、basename 形は**このセッションが触る絶対パス全部** (他プロジェクト含む) に効きます。外れるのは Stop の報告だけでなく **Read / Bash / Edit / Write の保護そのもの**です。追記内容 (path 形 — 承認した 1 ファイルだけを外す):\n  [project:$CLAUDE_PROJECT_DIR]\n  !/.env\n  !/.env.production\n同名ファイルをすべて外したい場合だけ basename 形にする: `!.env` / `!.env.production`'


class TestBuildReasonByteBudget(unittest.TestCase):
    """内部バックログ: block reason の byte 予算。

    公式 hooks reference が明記する「hook の stdout 文字列は 10,000 文字が
    上限」に対する安全マージン (``MAX_REASON_BYTES``)。固定 tail
    (AskUserQuestion 案内 + 恒久除外レシピ) を先に確保し、ファイル列挙
    (``  - path`` 行) だけを畳む。tail 自体は truncate しない。
    """

    def test_byte_identical_to_pre_budget_output_for_small_input(self):
        entry = _load_entry()
        reason = entry._build_reason(
            tracked=[".env"],
            untracked=[".env.production"],
            session_scoped=False,
            root_offset_="",
        )
        self.assertEqual(reason, _EXPECTED_SMALL_REASON)

    def test_small_input_has_no_collapse_marker(self):
        entry = _load_entry()
        reason = entry._build_reason(
            tracked=[".env"],
            untracked=[".env.production"],
            session_scoped=False,
            root_offset_="",
        )
        self.assertNotIn("more files", reason)
        self.assertLess(len(reason.encode("utf-8")), entry.MAX_REASON_BYTES)

    def test_large_untracked_list_collapses_but_keeps_tail(self):
        entry = _load_entry()
        many = [f"secrets/key{i}.pem" for i in range(2000)]
        reason = entry._build_reason(
            tracked=[],
            untracked=many,
            session_scoped=False,
            root_offset_="",
        )
        self.assertIn("more files; see git status", reason)
        # tail (AskUserQuestion / 恒久除外レシピ) は畳まれず必ず残る
        self.assertIn("AskUserQuestion ツールで各ファイルについて", reason)
        self.assertIn("【恒久除外】", reason)
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        # 折り畳みマーカー自体の分だけ予算を超えることはあるが、無制限には
        # 膨らまない (マーカーは短い固定形なので数百 byte の余裕で十分)。
        self.assertLess(
            len(reason.encode("utf-8")), entry.MAX_REASON_BYTES + 200
        )

    def test_collapse_count_matches_omitted_files(self):
        entry = _load_entry()
        many = [f"secrets/key{i}.pem" for i in range(2000)]
        reason = entry._build_reason(
            tracked=[],
            untracked=many,
            session_scoped=False,
            root_offset_="",
        )
        shown = len(re.findall(r"\n  - secrets/key\d+\.pem", reason))
        m = re.search(r"\.\.\. \((\d+) more files; see git status\)", reason)
        self.assertIsNotNone(m)
        omitted = int(m.group(1))
        self.assertEqual(shown + omitted, len(many))
        self.assertGreater(shown, 0)  # 予算が完全にゼロになっていないこと
        self.assertGreater(omitted, 0)  # このケースは実際に溢れていること

    def test_both_lists_collapse_independently_when_both_huge(self):
        entry = _load_entry()
        tracked_many = [f"t/key{i}.pem" for i in range(1500)]
        untracked_many = [f"u/key{i}.pem" for i in range(1500)]
        reason = entry._build_reason(
            tracked=tracked_many,
            untracked=untracked_many,
            session_scoped=False,
            root_offset_="",
        )
        # tracked が先に予算を使うため、untracked 側は 0 件表示 (全件省略)
        # になりうるが、いずれにせよ両セクションで省略件数が明示される。
        self.assertEqual(
            reason.count("more files; see git status"), 2, msg=reason[-400:]
        )

    def test_tail_survives_even_when_fixed_part_alone_exceeds_budget(self):
        # MAX_REASON_BYTES を極端に小さくし、固定 tail だけで予算を超える
        # 病的ケースを強制する。tail (AskUserQuestion / 恒久除外レシピ /
        # session 注記) は truncate されず必ず残り、reason 全体が予算を
        # 超えることを許容する (次善のトレードオフ、と docstring に明記済み)。
        entry = _load_entry()
        with mock.patch.object(entry, "MAX_REASON_BYTES", 50):
            reason = entry._build_reason(
                tracked=["a.env", "b.env", "c.env"],
                untracked=[],
                session_scoped=True,
                root_offset_="",
            )
        self.assertGreater(len(reason.encode("utf-8")), 50)
        self.assertIn("AskUserQuestion ツールで各ファイルについて", reason)
        self.assertIn("【恒久除外】", reason)
        self.assertIn("再度 block しません", reason)
        self.assertIn("more files; see git status", reason)


if __name__ == "__main__":
    unittest.main()
