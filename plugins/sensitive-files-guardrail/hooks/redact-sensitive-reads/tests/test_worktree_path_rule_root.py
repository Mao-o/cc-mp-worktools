"""path 形 rule の基準 root を、評価するファイルのある checkout に読み替える
(0.35.0)。

``$CLAUDE_PROJECT_DIR`` が main repo を指したまま linked worktree のファイルを
触る経路 (セッション途中で worktree に入る / sub-agent の ``isolation:
worktree``) では、path 形 rule が main 基準の root 相対 path
(``.claude/worktrees/<name>/certs/aws.pem``) で照合されて一致せず、承認済みの
除外が効かなかった。worktree のセッションが main / 別 worktree のファイルを
絶対パスで触る逆方向も同じ。ユーザーが書いた path 形の include
(``secrets/**``) も同じ理由で別 checkout のコピーを保護していなかった。

layout は ``git worktree add`` の実構成を git 無しで再現する (出典は
test_patterns_loader.py の ``_make_worktree``)。実 git での end-to-end は
check-sensitive-files/tests/test_worktree_path_rule_root.py。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from _shared.patterns import resolve_path_rule_root
from core import output
from handlers import bash_handler, edit_handler, grep_handler, read_handler

_TIER_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.txt"
_LOCAL_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
_CERT = "-----BEGIN CERTIFICATE-----\nMIIBpublic\n-----END CERTIFICATE-----\n"


def _make_repo(parent: Path, name: str = "main") -> Path:
    """通常の checkout (``.git`` がディレクトリ)。"""
    root = parent / name
    (root / ".git").mkdir(parents=True)
    return root


def _add_worktree(main: Path, checkout: Path, name: str) -> Path:
    """``git worktree add`` 相当: 管理領域 ``<main>/.git/worktrees/<name>`` と、
    それを指す ``.git`` ファイルを持つ checkout を作る。"""
    admin = main / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {admin}\n")
    return checkout


def _decision(resp: dict) -> str | None:
    if output.is_allow(resp):
        return "allow"
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


def _reason(resp: dict) -> str:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecisionReason") or ""


class Base(unittest.TestCase):
    """HOME を隔離し、main repo と nested worktree (``.claude/worktrees/x``) を作る。"""

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
        self.main = _make_repo(self.tmp)
        self.wt = _add_worktree(self.main, self.main / ".claude" / "worktrees" / "x", "x")


class TestResolvePathRuleRoot(Base):
    def test_nested_worktree_file_maps_to_the_worktree_root(self):
        f = self.wt / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(self.wt))

    def test_external_worktree_file_maps_to_its_root(self):
        ext = _add_worktree(self.main, self.tmp / "ext", "ext")
        f = ext / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(ext))

    def test_main_file_from_a_worktree_root(self):
        """逆方向: worktree のセッション (root = worktree) が main のファイルを触る。"""
        f = self.main / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.wt)), str(self.main))

    def test_sibling_worktree(self):
        other = _add_worktree(self.main, self.tmp / "other", "other")
        f = other / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.wt)), str(other))

    def test_monorepo_offset_is_carried_to_the_other_checkout(self):
        """``$CLAUDE_PROJECT_DIR`` がサブディレクトリ (monorepo) なら、その位置を
        worktree 側に移す。"""
        root = self.main / "packages" / "app"
        root.mkdir(parents=True)
        f = self.wt / "packages" / "app" / "certs" / "aws.pem"
        self.assertEqual(
            resolve_path_rule_root(str(f), str(root)),
            str(self.wt / "packages" / "app"),
        )

    def test_same_checkout_returns_root_unchanged(self):
        main = str(self.main)
        self.assertEqual(resolve_path_rule_root(str(self.main / "certs" / "a.pem"), main), main)
        sub_root = str(self.main / "packages" / "app")
        self.assertEqual(
            resolve_path_rule_root(str(self.main / "packages" / "app" / "a.pem"), sub_root),
            sub_root,
        )
        self.assertEqual(resolve_path_rule_root(str(self.wt / "a.pem"), str(self.wt)), str(self.wt))

    def test_submodule_is_a_different_repository(self):
        """submodule の ``.git`` も ``gitdir:`` ファイルだが共有 git dir が違う
        (``commondir`` が無い = gitdir 自身) ので読み替えない。"""
        (self.main / ".git" / "modules" / "sub").mkdir(parents=True)
        sub = self.main / "sub"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        f = sub / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(self.main))

    def test_nested_separate_repository_is_unchanged(self):
        vendored = _make_repo(self.main / "vendor", "lib")
        f = vendored / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(self.main))

    def test_stale_worktree_is_unchanged(self):
        """管理領域が消えた (prune 前の) worktree は同一 repository と見なさない
        = 従来どおりの判定 (除外は効かない側)。"""
        shutil.rmtree(self.main / ".git" / "worktrees" / "x")
        f = self.wt / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(self.main))

    def test_unresolvable_inputs_return_root_as_is(self):
        main = str(self.main)
        # 相対 path は「既に root 相対」(Stop hook の経路) なので触らない
        self.assertEqual(resolve_path_rule_root("certs/aws.pem", main), main)
        self.assertEqual(resolve_path_rule_root("", main), main)
        self.assertIsNone(resolve_path_rule_root(str(self.wt / "a.pem"), None))
        # どの checkout にも属さないファイル
        outside = self.tmp / "plain" / "a.pem"
        self.assertEqual(resolve_path_rule_root(str(outside), main), main)
        # root がどの checkout にも属さない
        loose_root = str(self.tmp / "loose")
        self.assertEqual(
            resolve_path_rule_root(str(self.wt / "a.pem"), loose_root), loose_root
        )

    def test_bare_repository_worktrees_are_one_repository(self):
        bare = self.tmp / "repo.git"
        checkouts = {}
        for name in ("a", "b"):
            admin = bare / "worktrees" / name
            admin.mkdir(parents=True)
            (admin / "commondir").write_text("../..\n")
            co = self.tmp / name
            co.mkdir()
            (co / ".git").write_text(f"gitdir: {admin}\n")
            checkouts[name] = co
        f = checkouts["b"] / "certs" / "aws.pem"
        self.assertEqual(
            resolve_path_rule_root(str(f), str(checkouts["a"])), str(checkouts["b"])
        )

    def test_relative_gitdir_pointer(self):
        """git 2.48+ の ``worktree.useRelativePaths`` は ``.git`` に相対の gitdir を書く。"""
        admin = self.main / ".git" / "worktrees" / "rel"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        co = self.main / ".claude" / "worktrees" / "rel"
        co.mkdir(parents=True)
        (co / ".git").write_text("gitdir: ../../../.git/worktrees/rel\n")
        f = co / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(self.main)), str(co))

    @unittest.skipIf(os.name == "nt", "symlink の作成に権限が要る")
    def test_symlinked_spelling_of_the_same_checkout(self):
        """同じ checkout を symlink 経由の字面で指す root でも、ファイル側の字面に
        合わせた root を返す (共有 git dir は実体で比較する)。"""
        link = self.tmp / "link"
        link.symlink_to(self.main, target_is_directory=True)
        f = self.main / "certs" / "aws.pem"
        self.assertEqual(resolve_path_rule_root(str(f), str(link)), str(self.main))

    @unittest.skipIf(os.name == "nt", "symlink の作成に権限が要る")
    def test_gitdir_pointer_spelled_differently_from_the_main_path(self):
        """git は ``.git`` ファイルに gitdir を絶対パスで書くので、main を symlink
        経由の字面 (macOS の ``/var`` → ``/private/var`` 等) で開いていると共有
        git dir の字面が食い違う。字面比較で終わらせず実体で比べること。"""
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp, target_is_directory=True)
        root = alias / "main"
        f = alias / "main" / ".claude" / "worktrees" / "x" / "certs" / "aws.pem"
        self.assertEqual(
            resolve_path_rule_root(str(f), str(root)),
            str(alias / "main" / ".claude" / "worktrees" / "x"),
        )


class TestHandlersAcrossCheckouts(Base):
    """repo 同梱 tier に ``!certs/aws.pem`` を commit した repo (main / worktree の
    両方に同じ内容がある) で、``$CLAUDE_PROJECT_DIR`` = main のまま worktree の
    ファイルを触る。"""

    def setUp(self):
        super().setUp()
        for checkout in (self.main, self.wt):
            tier = checkout / _TIER_REL
            tier.parent.mkdir(parents=True)
            tier.write_text("!certs/aws.pem\n")
            for name in ("aws.pem", "other.pem"):
                cert = checkout / "certs" / name
                cert.parent.mkdir(parents=True, exist_ok=True)
                cert.write_text(_CERT)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.main)

    def _envelope(self, tool: str, tool_input: dict, cwd: Path) -> dict:
        return {
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(cwd),
            "permission_mode": "default",
        }

    def _read(self, path: Path, cwd: Path) -> dict:
        return read_handler.handle(self._envelope("Read", {"file_path": str(path)}, cwd))

    def _write(self, path: Path, cwd: Path) -> dict:
        return edit_handler.handle(
            self._envelope("Write", {"file_path": str(path), "content": "x\n"}, cwd),
            tool_label="Write",
        )

    def _grep(self, path: Path, cwd: Path) -> dict:
        return grep_handler.handle(
            self._envelope("Grep", {"pattern": "BEGIN", "path": str(path)}, cwd)
        )

    def _bash(self, command: str, cwd: Path) -> dict:
        return bash_handler.handle(
            self._envelope("Bash", {"command": command, "description": "t"}, cwd)
        )

    def test_read_follows_the_approved_path_rule_in_the_worktree(self):
        for cwd in (self.wt, self.main):
            with self.subTest(cwd=cwd.name):
                approved = self._read(self.wt / "certs" / "aws.pem", cwd)
                self.assertEqual(_decision(approved), "allow")
                other = self._read(self.wt / "certs" / "other.pem", cwd)
                self.assertEqual(_decision(other), "deny")

    def test_read_main_copy_from_a_worktree_session(self):
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.wt)
        self.assertEqual(
            _decision(self._read(self.main / "certs" / "aws.pem", self.wt)), "allow"
        )
        self.assertEqual(
            _decision(self._read(self.main / "certs" / "other.pem", self.wt)), "deny"
        )

    def test_bash_operand_in_the_worktree(self):
        self.assertEqual(_decision(self._bash("cat certs/aws.pem", self.wt)), "allow")
        resp = self._bash("cat certs/other.pem", self.wt)
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!certs/other.pem` (この 1 ファイルだけ)", reason)
        self.assertNotIn("!.claude/worktrees", reason)

    def test_write_in_the_worktree_and_its_recipe(self):
        self.assertEqual(
            _decision(self._write(self.wt / "certs" / "aws.pem", self.wt)), "allow"
        )
        resp = self._write(self.wt / "certs" / "other.pem", self.wt)
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!certs/other.pem` (この 1 ファイルだけ)", reason)
        self.assertNotIn("!.claude/worktrees", reason)

    def test_grep_in_the_worktree_and_its_recipe(self):
        self.assertEqual(
            _decision(self._grep(self.wt / "certs" / "aws.pem", self.wt)), "allow"
        )
        resp = self._grep(self.wt / "certs" / "other.pem", self.wt)
        self.assertEqual(_decision(resp), "deny")
        # matched_target は入力の path をそのまま出すので、案内 (`!` 行) だけを見る
        reason = _reason(resp)
        self.assertIn("`!certs/other.pem` (この 1 ファイルだけ)", reason)
        self.assertNotIn("!.claude/worktrees", reason)

    def test_bash_skips_the_lookup_without_path_rules(self):
        """読み替えは path 形 rule の照合にしか効かないので、rules に path 形が
        無ければ operand ごとの ``.git`` 探索をしない (operand の多いコマンドの
        latency。既定 patterns.txt は basename 形だけ)。"""
        for checkout in (self.main, self.wt):
            (checkout / _TIER_REL).write_text("!aws.pem\n")
        calls = []

        def spy(path, root):
            calls.append(path)
            return root

        with mock.patch.object(bash_handler, "resolve_path_rule_root", spy):
            resp = self._bash("cat certs/aws.pem certs/b.txt certs/c.txt", self.wt)
        self.assertEqual(_decision(resp), "allow")
        self.assertEqual(calls, [])

    def test_path_include_rule_protects_the_worktree_copy(self):
        """保護側も同根: path 形の include は別 checkout のコピーを保護していな
        かった。basename が既定 rule に当たらないファイルで確かめる。"""
        local = self.home / _LOCAL_REL
        local.parent.mkdir(parents=True)
        local.write_text(f"[project:{self.main}]\nsecrets/**\n")
        target = self.wt / "secrets" / "app.yaml"
        target.parent.mkdir(parents=True)
        target.write_text("token: x\n")
        self.assertEqual(_decision(self._read(target, self.wt)), "deny")
        self.assertEqual(_decision(self._bash("cat secrets/app.yaml", self.wt)), "deny")


if __name__ == "__main__":
    unittest.main()
