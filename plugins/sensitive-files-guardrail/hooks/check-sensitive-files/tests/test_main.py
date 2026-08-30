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

    def _add_nested_submodule(self) -> bool:
        """repo -> vendor -> vendor/deep の 2 段ネスト submodule を構成する
        (P2-1 回帰)。``vendor/deep/.env`` は vendor 自身の index ではなく
        deep 自身の index が持つため、親 repo からの `git rm --cached` は
        `vendor` にも `vendor/deep` にも効かない。案内は最も深い
        `vendor/deep` を指す必要がある。
        """
        (self.repo / "README.md").write_text("# super\n")
        _git(["add", "README.md"], str(self.repo))
        _git(["commit", "-m", "init"], str(self.repo))

        leaf = Path(self.tmp) / "leafrepo"
        leaf.mkdir()
        _init_repo(str(leaf))
        (leaf / ".env").write_text("LEAF_SECRET=v\n")
        _git(["add", ".env"], str(leaf))
        _git(["commit", "-m", "add env"], str(leaf))

        mid = Path(self.tmp) / "midrepo"
        mid.mkdir()
        _init_repo(str(mid))
        (mid / "README.md").write_text("# mid\n")
        _git(["add", "README.md"], str(mid))
        _git(["commit", "-m", "init"], str(mid))
        try:
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{leaf}", "deep",
                ],
                cwd=str(mid), check=True, capture_output=True,
            )
            _git(["commit", "-m", "add deep submodule"], str(mid))
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{mid}", "vendor",
                ],
                cwd=str(self.repo), check=True, capture_output=True,
            )
            _git(["commit", "-m", "add vendor submodule"], str(self.repo))
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "update", "--init", "--recursive",
                ],
                cwd=str(self.repo), check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            return False
        return True

    def test_nested_submodule_guidance_points_to_deepest_dir(self):
        if not self._add_nested_submodule():
            self.skipTest("git submodule add unsupported in this env")
        rc, out, _ = _run_main({"cwd": str(self.repo)})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        reason = payload["reason"]
        self.assertIn("vendor/deep/.env", reason)
        # 修正前は外側の `vendor` (親 repo の index には無い gitlink
        # ディレクトリなので `git rm --cached` が効かない) を誤って案内して
        # いた。最も深い `vendor/deep` を指すことを固定する (P2-1)。
        self.assertIn("行ってください: `vendor/deep`", reason)
        self.assertNotIn("行ってください: `vendor`", reason)


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


def _stdout_chars(entry, reason: str) -> int:
    """hook が実際に stdout に出す JSON の文字数 (外部レビュー R1 P2-A)。

    予算の単位は reason の生 byte ではなく**この値**。``_serialize`` は
    ``main()`` が print に渡すのと同じ関数なので、直列化方法がずれない。
    """
    return len(entry._serialize(reason))


class TestBuildReasonOutputCharBudget(unittest.TestCase):
    """内部バックログ + 外部レビュー R1 P2-A: stdout JSON の文字数予算。

    公式 hooks reference は ``additionalContext`` / ``systemMessage`` /
    素の stdout を 10,000 文字上限と明記するが、Stop hook の ``reason`` は
    この列挙に含まれていない。名指しされていない以上、保守的にこの値を
    安全マージンとして採用した (``MAX_OUTPUT_CHARS``)。**静的な案内文**を
    先に確保し、path 由来の可変長部分 (ファイル列挙・恒久除外レシピの ``!``
    行・basename 形の併記・submodule ディレクトリ一覧) を畳む。静的案内は
    truncate しない (レシピ行を予算対象に移した経緯は R2 P2-B /
    ``TestRecipeLinesInBudget``)。

    0.27.0 当初は reason の生 UTF-8 byte で予算化していたが、``json.dumps``
    のエスケープを無視していたため ``\\`` / ``"`` を多く含むファイル名で
    枠を破った (``TestSerializedJsonBudget`` が回帰テスト)。ここの assert も
    すべて「直列化後の文字数」に揃えてある — byte 値は proxy でしかなく、
    docs が上限を課しているのは文字数の方であるため。
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
        self.assertLess(_stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS)

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
        # 膨らまない (マーカーは短い固定形なので数百文字の余裕で十分)。
        # そして最終的な stdout は docs の 10,000 文字上限を割らない。
        self.assertLess(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS + 200
        )
        self.assertLess(_stdout_chars(entry, reason), 10_000)

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
        # 両セクションで省略件数が明示される。
        self.assertEqual(
            reason.count("more files; see git status"), 2, msg=reason[-400:]
        )
        # tracked (処理順で先) が予算を独占して untracked を 0 件表示に
        # 追い込んでいないこと (untracked は .gitignore 追加だけで対処できる
        # ので、tracked の分量に関わらず実例が見えるべき)。
        tracked_shown = re.findall(r"\n  - t/key\d+\.pem", reason)
        untracked_shown = re.findall(r"\n  - u/key\d+\.pem", reason)
        self.assertGreater(len(tracked_shown), 0)
        self.assertGreater(len(untracked_shown), 0)

    def test_submodule_dirs_display_collapses_and_keeps_file_enumeration(self):
        """P2-2 回帰: submodule 案内行 (dirs_display) は distinct dir 数に
        比例して伸びる代わりに先頭 10 件 + 省略件数へ畳まれ、tracked
        ファイル列挙を 0 件に潰さないこと。

        修正前は distinct submodule dir を全件連結しており、この行が
        `skeleton` (= 固定部分扱い) に入るため予算の外側で無制限に伸びた。
        n=700 は「修正前コード (commit 7a38366) で実際に shown_files が
        厳密に 0 になる」ことを事前に実測して選んだ値 (n=300 では 162 件
        残ってしまい、0 件潰れの再現にならず assertGreater が空振りする) —
        こうしておかないと後段の assertGreater が何も検出しない飾りになる。
        """
        entry = _load_entry()
        n = 700
        tracked = [f"submod{i}/.env" for i in range(n)]
        submodule_by_path = {p: p.split("/")[0] for p in tracked}
        reason = entry._build_reason(
            tracked=tracked,
            untracked=[],
            session_scoped=False,
            root_offset_="",
            submodule_by_path=submodule_by_path,
        )
        # submodule 案内は先頭 10 件 + 省略件数マーカーに畳まれる (修正前は
        # マーカーが存在せず distinct dir 全件が生で連結されていた)
        self.assertIn(f"({n - 10} more)", reason)
        shown_guidance_dirs = re.findall(r"`(submod\d+)`", reason)
        self.assertLessEqual(len(shown_guidance_dirs), 10)
        # tracked ファイル列挙が 0 件に潰れない主張の実測: 修正前コード
        # (commit 7a38366) にこの n=700 と同じ入力を通すと shown_files=0
        # (固定部分だけで当時の byte 予算 9,216 を使い切るため。予算は
        # その後 MAX_OUTPUT_CHARS = 直列化後の文字数へ移行した)。
        shown_files = re.findall(r"\n  - submod\d+/\.env", reason)
        self.assertGreater(len(shown_files), 0)
        # 上記 2 つの表層チェックより、この 1 行が実際の回帰検出力を持つ:
        # dirs_display を畳んだ結果、修正前コードで同じ入力が生成した
        # reason (11,477 byte、実測) を大きく下回り、10,000 字の枠にも
        # 収まる。畳まなければ dirs_display だけで数千文字規模になり、
        # この境界を割ることはできない。
        # 測る単位は stdout JSON の文字数 (外部レビュー R1 P2-A で byte から
        # 移行。この入力は ASCII path + 日本語 skeleton なので、byte で見ると
        # 予算に対して過大請求になり境界の意味がぼやける)。
        self.assertLess(_stdout_chars(entry, reason), 10_000)

    def test_static_guidance_survives_when_budget_alone_is_impossible(self):
        # MAX_OUTPUT_CHARS を極端に小さくし、静的案内だけで予算を超える
        # 病的ケースを強制する。静的案内 (AskUserQuestion / 【恒久除外】の
        # 見出しと影響範囲 / session 注記) は truncate されず必ず残り、出力
        # 全体が予算を超えることを許容する (案内を失う方が実害が大きい)。
        # path 由来の行 (レシピの `!` 行やファイル列挙) はこの状況では畳まれる
        # — 実運用では静的部分が予算の 1/4 程度に収まるため到達しない
        # (`test_static_part_plus_worst_case_markers_fits_budget` が前提を固定)。
        entry = _load_entry()
        with mock.patch.object(entry, "MAX_OUTPUT_CHARS", 50):
            reason = entry._build_reason(
                tracked=["a.env", "b.env", "c.env"],
                untracked=[],
                session_scoped=True,
                root_offset_="",
            )
        self.assertGreater(_stdout_chars(entry, reason), 50)
        self.assertIn("AskUserQuestion ツールで各ファイルについて", reason)
        self.assertIn("【恒久除外】", reason)
        self.assertIn("再度 block しません", reason)
        self.assertIn("more files; see git status", reason)


def _escape_heavy_names(k: int, n: int) -> list[str]:
    """``\\`` と ``"`` だけで構成した POSIX 有効ファイル名を ``n`` 件返す。

    POSIX のファイル名に使えない byte は ``/`` と NUL だけなので、``\\`` も
    ``"`` も正当な名前である (git も追跡できる)。``json.dumps`` はこの 2 種を
    それぞれ 2 文字にエスケープするため、生 byte 数と直列化後の文字数が最大
    2 倍近く乖離する — P2-A が破っていたのはまさにこの入力クラス。
    """
    stem = ("\\" * (k // 2)) + ('"' * (k - k // 2))
    return [f"{stem}{i}.pem" for i in range(n)]


class TestSerializedJsonBudget(unittest.TestCase):
    """外部レビュー R1 P2-A 回帰: 予算は直列化後の文字数で計る。

    修正前は ``len(line.encode("utf-8")) + 1`` で予算化しており、``json.dumps``
    のエスケープ (``\\`` / ``"`` / 制御文字が 2 文字、改行が ``\\`` + ``n``) と
    wrapper の文字数を無視していた。結果として「reason は予算内なのに実際に
    stdout へ出る JSON は 10,000 文字超」が成立してしまい、ハーネスが出力を
    ファイルへ退避して preview に差し替える (= block 内容が Claude に届かない)
    恐れがあった。
    """

    def test_escape_heavy_filenames_stay_under_docs_limit(self):
        """``\\`` / ``"`` を多く含むファイル名 (修正前に枠を破った入力)。

        修正前コード (commit e6ac2ba) にこの同じ入力を通すと
        **reason 9,253 byte / stdout JSON 13,088 文字** (実測) で、byte 予算
        (9,216) は満たすのに 10,000 文字の枠を 3 千文字超過していた。修正後は
        同じ入力が 9,218 文字 (表示 147 件) に収まる。
        """
        entry = _load_entry()
        names = _escape_heavy_names(16, 4000)
        reason = entry._build_reason(
            tracked=[],
            untracked=names,
            session_scoped=False,
            root_offset_="",
        )
        self.assertLess(_stdout_chars(entry, reason), 10_000)
        self.assertLess(_stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS + 200)
        # 非空振りの担保: この入力は実際に溢れており、かつ実例が 0 件に
        # 潰れてもいない (予算がゼロになる形での「合格」ではない)。
        self.assertIn("more files; see git status", reason)
        self.assertGreater(reason.count("\n  - "), 0)
        # 生 byte で見ると予算内に「見えて」しまうことの明示 (この乖離こそが
        # 修正前の誤り。byte を assert の基準に戻さないための杭)。
        self.assertLess(len(reason.encode("utf-8")), entry.MAX_OUTPUT_CHARS)

    def test_escape_heavy_filenames_in_both_sections(self):
        entry = _load_entry()
        names = _escape_heavy_names(16, 4000)
        reason = entry._build_reason(
            tracked=names[:2000],
            untracked=names[2000:],
            session_scoped=True,
            root_offset_="",
        )
        self.assertLess(_stdout_chars(entry, reason), 10_000)
        self.assertEqual(reason.count("more files; see git status"), 2)

    def test_non_ascii_filenames_stay_under_docs_limit(self):
        """非 ASCII (日本語) ファイル名の床テスト。

        **開示**: この入力は修正前コードでも枠を破っていない (実測 5,365
        文字)。``ensure_ascii=False`` なので日本語は 1 文字 = 1 文字だが
        UTF-8 では 3 byte あり、byte 予算はむしろ**過大請求**していたため
        (安全側に外れていた)。したがってこれは「修正前に落ちる回帰テスト」
        ではなく、文字数予算へ移行したあとも枠を割らないことを固定する床
        テストである。移行によって表示件数は増える (この入力で実測 171 →
        347 件 = 約 2.0 倍) が、これは byte 予算の過大請求が解消された結果で
        意図した改善。参考: 同じ移行で ASCII path は 300 → 319 件 (ほぼ横ばい)、
        エスケープの多い名前は 233 → 147 件 (正しく高く請求されるため減る)。
        """
        entry = _load_entry()
        names = [f"機密/設定ファイル{i}.env" for i in range(3000)]
        reason = entry._build_reason(
            tracked=[],
            untracked=names,
            session_scoped=False,
            root_offset_="",
        )
        self.assertLess(_stdout_chars(entry, reason), 10_000)
        self.assertIn("more files; see git status", reason)
        self.assertGreater(reason.count("\n  - "), 0)
        # docs の上限を「文字数」と解釈した帰結の明示: 非 ASCII だけの reason
        # は UTF-8 byte では 10,000 を超えうる (定数コメントで開示済みの前提)。
        self.assertGreater(len(reason.encode("utf-8")), 10_000)

    def test_json_chars_matches_actual_serialization(self):
        """``_json_chars`` が ``json.dumps`` の実挙動と一致すること。

        手計算 (``count("\\\\") + count('"')`` 等) に置き換えられると制御文字
        や改行を取りこぼす。``json.dumps`` に数えさせている構造を固定する。
        """
        entry = _load_entry()
        for s in (
            "",
            "plain",
            'a"b',
            "a\\b",
            "a\nb",
            "a\tb",
            "a\x00b",
            "a\x1fb",
            "日本語",
            '  - \\\\\\"""x.pem',
        ):
            with self.subTest(s=s):
                self.assertEqual(
                    entry._json_chars(s),
                    len(json.dumps(s, ensure_ascii=False)) - 2,
                )

    def test_wrapper_plus_reason_equals_actual_stdout_length(self):
        """予算の分解 (wrapper + reason) が実出力長と厳密に一致すること。

        ``_WRAPPER_CHARS`` は空 reason の直列化長 (reason を囲む引用符込み)、
        ``_json_chars`` は引用符を除いた長さ。両者の和が ``_serialize`` の
        長さに一致しないと予算が静かにずれる。
        """
        entry = _load_entry()
        for reason in ("", "短い", 'a"b\\c\nd', "\n".join(f"  - x{i}" for i in range(50))):
            with self.subTest(reason=reason[:20]):
                self.assertEqual(
                    entry._WRAPPER_CHARS + entry._json_chars(reason),
                    len(entry._serialize(reason)),
                )


class TestSerializedJsonBudgetE2E(BaseMainTest):
    """``main()`` を実際に通す側の検証 (HOME は ``BaseMainTest`` が tmp に隔離)。

    HOME を隔離しないと開発者の実 ``patterns.local.txt`` を読んでしまい、そこに
    ``!.env`` があるマシンでだけ block が起きず落ちる (環境依存の flaky)。
    """

    def test_main_stdout_uses_the_measured_serializer(self):
        """``main()`` の実出力が予算計測と同じ ``_serialize`` を通ること。

        片方だけ ``ensure_ascii`` を変える / 直接 ``json.dumps`` に戻す、と
        いった変更で「計った値と出す値が違う」状態に戻らないようにする杭。
        """
        entry = _load_entry()
        (self.repo / ".env").write_text("K=v\n")
        captured: dict[str, str] = {}
        real_serialize = entry._serialize

        def _spy(reason: str) -> str:
            captured["reason"] = reason
            return real_serialize(reason)

        with mock.patch.object(entry, "_serialize", _spy):
            old_stdin, old_stdout = sys.stdin, sys.stdout
            try:
                sys.stdin = io.StringIO(json.dumps({"cwd": str(self.repo)}))
                sys.stdout = io.StringIO()
                entry.main()
                out = sys.stdout.getvalue()
            finally:
                sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertIn("reason", captured)
        self.assertEqual(out, real_serialize(captured["reason"]) + "\n")
        self.assertEqual(json.loads(out)["decision"], "block")


def _long_nested_paths(count: int, length: int) -> list[str]:
    """深いネストで ``length`` 文字前後になる path を ``count`` 件返す。

    1 セグメントは 60 文字 (多くのファイルシステムの NAME_MAX 255 以内) に
    留め、長さは**深さ**で稼ぐ — 実在しうる形にしておかないと「非現実的な
    入力でしか起きない」と誤読される。``git ls-files`` はこの形の path を
    普通に返す。
    """
    seg = "d" * 60
    body = "/".join([seg] * (length // 61 + 1))[:length]
    return [f"seg{i}/{body}/.env" for i in range(count)]


_RECIPE_MARKER_RE = re.compile(r"^  \.\.\. \((\d+) more\)$", re.M)


class TestRecipeLinesInBudget(unittest.TestCase):
    """外部レビュー R2 P2-B 回帰: path 由来の行はすべて予算の対象。

    修正前は恒久除外レシピの ``!`` 行と basename 形の併記を「固定 tail」と
    して予算の外に置いていた。中身は path 由来で**可変長**なので、長い path
    が数本あるだけで枠を破る (3.5KB の path 3 本で 11,580 文字を実測。列挙
    予算を 0 に絞っても解消しない)。現在は静的な案内文だけが予算の外にある。
    """

    def _reason(self, entry, **kwargs) -> str:
        base = dict(
            tracked=[], untracked=[], session_scoped=True, root_offset_=""
        )
        base.update(kwargs)
        return entry._build_reason(**base)

    def test_long_recipe_paths_stay_under_budget(self):
        """3.5KB のネストした path 3 本 — 修正前に 11,580 文字だった入力。

        修正前コード (commit 1f92fd9) にこの入力を通すと直列化後 11,580 文字
        で、``MAX_OUTPUT_CHARS`` (9,216) も docs の 10,000 文字も超える。
        レシピ行が予算の外にあったため、ファイル列挙を 0 件に絞っても縮まら
        なかった (= 予算という仕組み自体が効かない入力クラス)。
        """
        entry = _load_entry()
        reason = self._reason(entry, tracked=_long_nested_paths(3, 3500))
        self.assertLessEqual(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS
        )
        # 空振り防止: この入力は実際に畳まれている
        self.assertIsNotNone(_RECIPE_MARKER_RE.search(reason))
        # 静的案内は残る (畳むのは path 由来の行だけ)
        self.assertIn("AskUserQuestion ツールで各ファイルについて", reason)
        self.assertIn("【恒久除外】", reason)
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        self.assertIn("再度 block しません", reason)

    def test_single_recipe_line_over_budget_becomes_length_note(self):
        """1 本の path だけで予算を超えるとき、長さを示す 1 行に置換する。"""
        entry = _load_entry()
        reason = self._reason(entry, tracked=_long_nested_paths(2, 12000))
        self.assertLessEqual(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS
        )
        self.assertIn("path が長すぎるため省略", reason)
        # 追記先セクションが分からなくなる事故を防ぐため、ヘッダーは残す
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)

    def test_normal_case_keeps_every_recipe_line(self):
        """通常の長さなら従来どおりレシピが全件出る (畳まない)。"""
        entry = _load_entry()
        names = [f"config/app{i}/.env" for i in range(20)]
        reason = self._reason(entry, tracked=names)
        for name in names:
            self.assertIn(f"  !{name}\n", reason)
        self.assertIsNone(_RECIPE_MARKER_RE.search(reason))

    def test_fitting_input_is_not_narrowed_by_the_new_budget(self):
        """予算に丸ごと収まる入力は、予算を導入した側で狭めないこと。

        自己レビューの旧版比較 (merge 済み実装と本 branch に同じコーパスを
        流して出力を突き合わせる) で見つかった退行の回帰テスト。basename 形の
        併記だけが折り畳みマーカー分を**無条件に**確保していたため、全件
        収まる入力でも枠が足りなくなり表示が減っていた (120 文字の basename
        20 件 + 短い path で 20 件 → 11 件、200 文字なら 0 件に潰れた)。

        「上限を超えない」ことだけを見る格子テストでは**原理的に検出できない**
        (減る方向は常に上限を満たすため)。旧版との出力ペア比較でしか分から
        ない型なので、境界値を床として固定する。
        """
        entry = _load_entry()
        names = [f"d/{'b' * 120}{i}.env" for i in range(20)]
        reason = self._reason(entry, tracked=names)
        # この入力は予算に収まる (= 畳む理由が無い)
        self.assertLessEqual(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS
        )
        alts_line = next(
            ln
            for ln in reason.split("\n")
            if ln.startswith("同名ファイルをすべて外したい")
        )
        self.assertEqual(alts_line.count("`!"), len(names), msg=alts_line[:200])
        self.assertNotIn("more)", alts_line)
        # レシピ行とファイル列挙も全件
        self.assertEqual(len(re.findall(r"^  !", reason, re.M)), len(names))
        self.assertEqual(len(re.findall(r"^  - ", reason, re.M)), len(names))
        self.assertIsNone(_RECIPE_MARKER_RE.search(reason))

    def test_recipe_marker_reports_a_single_truthful_count(self):
        """limit 由来と予算由来のマーカーが二重に出ない。

        ``exclude_recipe_lines`` は limit=20 で ``... (N more)`` を付ける。
        予算でさらに畳んだとき、その古い件数を残すと 2 つのマーカーが別々の
        数を主張して読み手がどちらも信用できなくなる。マーカーは 1 本、件数は
        「重複除去後の全件 − 表示できた件数」。
        """
        entry = _load_entry()
        names = [f"seg{i}/" + ("d" * 60 + "/") * 13 + ".env" for i in range(25)]
        reason = self._reason(entry, tracked=names)
        markers = _RECIPE_MARKER_RE.findall(reason)
        self.assertEqual(len(markers), 1, msg=reason[-600:])
        shown = len(re.findall(r"^  !seg\d+/", reason, re.M))
        self.assertEqual(shown + int(markers[0]), len(names))
        self.assertGreater(shown, 0)  # 予算がゼロに潰れていないこと
        self.assertGreater(int(markers[0]), 0)  # 実際に溢れていること

    def test_metachar_heavy_recipe_names_stay_under_budget(self):
        """``escape_glob`` 展開 × JSON エスケープの二重膨張でも枠を割らない。

        ``*`` / ``?`` / ``[`` は ``escape_glob`` が literal 化して長さが増え、
        ``\\`` / ``"`` は ``json.dumps`` が 2 文字に膨らませる。0.27.0 の
        byte → 文字数移行が潰したのと同じ誤算クラスが、新たに予算対象に
        なったレシピ行でも起きないことを固定する。
        """
        entry = _load_entry()
        stem = '*?[\\"' * 400
        names = [f"d{i}/{stem}.env" for i in range(6)]
        reason = self._reason(entry, tracked=names)
        self.assertLessEqual(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS
        )
        self.assertIn("【恒久除外】", reason)

    def test_long_submodule_dirs_stay_under_budget(self):
        """submodule 案内のディレクトリ一覧も path 由来 (件数 10 では足りない)。

        修正前は先頭 10 件へ畳むだけで 1 本の長さが無制限だったため、3KB の
        submodule dir が 10 本並ぶと静的部分だけで 30,000 文字規模になり、
        「静的部分は固定サイズ」という予算の前提が崩れていた。
        """
        entry = _load_entry()
        dirs = _long_nested_paths(12, 3000)
        tracked = [f"{d}/.env" for d in dirs]
        reason = self._reason(
            entry,
            tracked=tracked,
            submodule_by_path={p: p.rsplit("/", 1)[0] for p in tracked},
        )
        self.assertLessEqual(
            _stdout_chars(entry, reason), entry.MAX_OUTPUT_CHARS
        )
        self.assertIn("AskUserQuestion ツールで各ファイルについて", reason)

    def test_static_part_plus_worst_case_markers_fits_budget(self):
        """不変条件の前提: 静的部分 + 最悪ケースのマーカー < 予算。

        「path 由来の行を全部畳めば必ず予算内」は、定数から成る静的部分が
        予算より十分小さいことに依存する。案内文を増やしてこの前提が崩れたら
        ここで落ちる (黙って予算超過に戻らないための杭)。
        """
        entry = _load_entry()
        reason = self._reason(
            entry,
            tracked=[".env"],
            untracked=[".env.production"],
            submodule_by_path={".env": "sub"},
        )
        worst_markers = sum(
            entry._json_chars(t) + 2
            for t in (
                entry._OMITTED_FILES_TEMPLATE.format(n=10**6),
                entry._OMITTED_FILES_TEMPLATE.format(n=10**6),
                entry._OMITTED_RECIPE_TEMPLATE.format(n=10**6),
                entry._LONG_RECIPE_TEMPLATE.format(n=10**6),
            )
        )
        self.assertLess(
            _stdout_chars(entry, reason) + worst_markers,
            entry.MAX_OUTPUT_CHARS,
        )

    def test_output_never_exceeds_budget_over_grid(self):
        """不変条件の総当たり: 件数 × path 長 × basename 長 × root_offset
        × セクション配分のどの組でも直列化後が予算以下。

        軸を分けているのは、可変長の site が 4 つあり互いに独立に伸びるため:
        path 深さ (レシピ行) / basename 長 (併記行) / 件数 (ファイル列挙) /
        ``root_offset_=None`` (併記行の無い別形の tail)。1 軸だけの格子では
        2 site が素通りする。
        """
        entry = _load_entry()
        for count in (1, 3, 21):
            for path_len in (10, 1000, 3500, 12000):
                for base_len in (5, 250):
                    for root_off in ("", "sub/dir", None):
                        for split in ("tracked", "untracked", "both"):
                            with self.subTest(
                                count=count,
                                path_len=path_len,
                                base_len=base_len,
                                root_off=root_off,
                                split=split,
                            ):
                                base = "b" * base_len
                                names = [
                                    p.replace("/.env", f"/{base}{i}.env")
                                    for i, p in enumerate(
                                        _long_nested_paths(count, path_len)
                                    )
                                ]
                                if split == "tracked":
                                    tr, un = names, []
                                elif split == "untracked":
                                    tr, un = [], names
                                else:
                                    half = len(names) // 2 or 1
                                    tr, un = names[:half], names[half:]
                                reason = entry._build_reason(
                                    tracked=tr,
                                    untracked=un,
                                    session_scoped=True,
                                    root_offset_=root_off,
                                )
                                self.assertLessEqual(
                                    _stdout_chars(entry, reason),
                                    entry.MAX_OUTPUT_CHARS,
                                )
                                # 静的案内は畳まれない
                                self.assertIn("【恒久除外】", reason)


class TestAsciiStdoutEncoding(BaseMainTest):
    """外部レビュー R2 P2-A 回帰: stdout が非 UTF-8 でも判定を必ず出す。

    修正前は ``print`` が ``sys.stdout`` の encoding に従っていたため、
    ``PYTHONIOENCODING=ascii`` で hook が起動されると reason の日本語で
    ``UnicodeEncodeError`` が送出され **exit 1 / stdout 0 byte** になった
    (実測)。Stop hook としては block が 1 件も報告されない = 機密ファイルが
    素通りする。

    in-process の ``StringIO`` 差し替えでは ``encoding`` も ``buffer`` も
    持たないため、この失敗モードは**構造的に再現できない**。したがって
    subprocess で実際に環境変数を与えるこのクラスが唯一の防波堤になる。
    子プロセスは ``_testutil`` の bootstrap を継承しないので、``HOME`` を
    テスト用 tmp に明示して実 HOME (stop-ack の保存先) を汚さない。
    """

    ASCII_ENV = {"PYTHONIOENCODING": "ascii", "LC_ALL": "C"}

    def _run_hook(self, env_extra: dict) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["HOME"] = str(self.home_dir)
        env["XDG_CONFIG_HOME"] = str(self.xdg_dir)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(_ENTRY_PATH)],
            input=json.dumps({"cwd": str(self.repo)}).encode("utf-8"),
            capture_output=True,
            env=env,
        )

    def test_ascii_stdout_still_emits_block_decision(self):
        (self.repo / ".env").write_text("K=v\n")
        proc = self._run_hook(self.ASCII_ENV)
        self.assertEqual(
            proc.returncode, 0, msg=proc.stderr.decode("utf-8", "replace")
        )
        self.assertTrue(proc.stdout, msg="stdout が空 = block が届いていない")
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(payload["decision"], "block")
        self.assertIn("【セキュリティ確認】", payload["reason"])
        self.assertIn(".env", payload["reason"])

    def test_ascii_stdout_bytes_go_through_the_measured_serializer(self):
        """出力バイト列が ``_serialize(reason) + "\\n"`` と厳密に一致すること。

        予算を計った直列化と実際に書いたバイト列を結び付ける杭 (encoding を
        変える修正で片方だけ ``ensure_ascii`` に倒れると、予算計算が静かに
        外れる — 0.27.0 で ``_serialize`` を 1 箇所化したのと同じ理由)。
        """
        (self.repo / ".env").write_text("K=v\n")
        entry = _load_entry()
        proc = self._run_hook(self.ASCII_ENV)
        text = proc.stdout.decode("utf-8")
        self.assertEqual(text, entry._serialize(json.loads(text)["reason"]) + "\n")

    def test_ascii_and_utf8_stdout_are_byte_identical(self):
        """encoding が変わっても出力が同一 = 情報が落ちていないこと。"""
        (self.repo / ".env").write_text("K=v\n")
        utf8 = self._run_hook({"PYTHONIOENCODING": "utf-8"})
        ascii_ = self._run_hook(self.ASCII_ENV)
        self.assertEqual(utf8.returncode, 0)
        self.assertEqual(utf8.stdout, ascii_.stdout)


if __name__ == "__main__":
    unittest.main()
