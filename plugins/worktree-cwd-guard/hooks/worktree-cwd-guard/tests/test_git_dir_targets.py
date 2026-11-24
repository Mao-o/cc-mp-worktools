"""`GIT_DIR` / `--git-dir` が効いているときの書き込み先の判定。

期待値は本物の git で確かめる: 偽の git は `GIT_DIR` / `--work-tree` を見ないので「書かない」を誤る。
`RealGitTest` が前提 (git dir だけが効くと作業ツリーは今の場所になる。`<checkout>/.git` はその checkout の git dir になる) を固定する。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import family
import test_guard
from _testutil import hermetic_env, sh
from test_guard import bash, decision, evaluate

RESET = "reset --hard"


class GitDirOnlyWorkTreeTest(test_guard.GuardBase):
    """git dir だけが効くとき、作業ツリーは今の場所。今の場所が別の checkout なら止める。"""

    def _forms(self, gitdir, place: str = "", c: str = "", sub: str = RESET):
        return (
            f'{place}export GIT_DIR="{gitdir}"; git {c}{sub}',
            f'{place}GIT_DIR="{gitdir}"; export GIT_DIR; git {c}{sub}',
            f'{place}GIT_DIR="{gitdir}" git {c}{sub}',
            f'{place}env GIT_DIR="{gitdir}" git {c}{sub}',
            f'{place}git {c}--git-dir="{gitdir}" {sub}',
            f'{place}git --git-dir="{gitdir}" {c}{sub}',
            f'{place}git {c}--git-dir "{gitdir}" {sub}',
        )

    def test_own_git_dir_from_another_checkout_is_denied(self):
        own = self.a / ".git"
        linked = (self.a / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        for gitdir in (own, linked):
            for cmd in (
                *self._forms(gitdir, place=f'cd "{self.main}"; '),
                *self._forms(gitdir, c=f'-C "{self.main}" '),
                *self._forms(gitdir, place=f'cd "{self.b}"; ', sub="clean -fd"),
                *self._forms(gitdir, c=f'-C "{self.b}" ', sub="checkout -- ."),
            ):
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_export_then_cd_is_denied(self):
        self.assertEqual(decision(evaluate(bash(f'export GIT_DIR="{self.a}/.git"; cd "{self.main}"; git {RESET}', self.a))), "deny")

    def test_deny_names_the_checkout_of_the_work_tree(self):
        out = evaluate(bash(f'cd "{self.main}"; GIT_DIR="{self.a}/.git" git {RESET}', self.a))
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(f"`git reset` が別の checkout ({family.norm(self.main)}) を対象にしている", reason)

    def test_git_dir_outside_the_repo_writes_the_checkout_of_the_place(self):
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        self.assertEqual(decision(evaluate(bash(f'cd "{self.main}"; GIT_DIR="{other}/.git" git {RESET}', self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'GIT_DIR="{other}/.git" git {RESET}', self.a)))
        self.assertIsNone(evaluate(bash(f'cd "{other}"; GIT_DIR=.git git add .', self.a)))

    def test_explicit_work_tree_decides_instead_of_the_place(self):
        # 作業ツリーを指定すれば、今の場所は書かない
        own = self.a / ".git"
        for cmd in (
            f'cd "{self.main}"; git --git-dir="{own}" --work-tree="{self.a}" {RESET}',
            f'cd "{self.main}"; GIT_DIR="{own}" GIT_WORK_TREE="{self.a}" git {RESET}',
            f'cd "{self.main}"; export GIT_WORK_TREE="{self.a}"; GIT_DIR="{own}" git {RESET}',
            f'git -C "{self.main}" --git-dir="{own}" --work-tree="{self.a}" {RESET}',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))
        self.assertEqual(decision(evaluate(bash(f'git --git-dir="{own}" --work-tree="{self.main}" {RESET}', self.a))), "deny")

    def test_usual_forms_in_own_worktree_stay_allowed(self):
        sub = self.a / "sub"
        sub.mkdir()
        own = self.a / ".git"
        linked = (self.a / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        for gitdir in (own, linked, ".git", "./.git"):
            for cmd in (*self._forms(gitdir), *self._forms(gitdir, sub="add ."), *self._forms(gitdir, sub='commit -m x')):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(evaluate(bash(cmd, self.a)))
        for cmd in (*self._forms(own, place="cd sub; "), *self._forms(own, c="-C sub "), f'cat <<EOF | GIT_DIR="{own}" git commit -F -\nbody\nEOF'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_reads_from_another_checkout_stay_allowed(self):
        own = self.a / ".git"
        for sub in ("status", "log", "diff", "stash list"):
            for cmd in (*self._forms(own, c=f'-C "{self.main}" ', sub=sub), *self._forms(own, place=f'cd "{self.main}"; ', sub=sub)):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_unknown_place_with_git_dir(self):
        own = self.a / ".git"
        other = Path(self._tmp.name) / "other"
        # 今の場所が決まらなければ注意を出す。git dir が別の checkout なら、これまでどおり止める
        for cmd in (f'cd "$Y"; GIT_DIR="{own}" git {RESET}', f'cd "$Y"; GIT_DIR="{other}/.git" git add .'):
            with self.subTest(cmd=cmd):
                out = evaluate(bash(cmd, self.a))
                self.assertEqual(decision(out), "context")
                self.assertIn("cd の移動先", out["hookSpecificOutput"]["additionalContext"])
        for gitdir in (self.main / ".git", self.b / ".git"):
            with self.subTest(gitdir=gitdir):
                out = evaluate(bash(f'cd "$Y"; GIT_DIR="{gitdir}" git add .', self.a))
                self.assertEqual(decision(out), "deny")

    def test_allow_list_keeps_the_other_target(self):
        # 許可リストで今の場所を外しても、git dir の checkout で止める
        os.environ["WORKTREE_CWD_GUARD_ALLOW"] = str(self.main)
        out = evaluate(bash(f'cd "{self.main}"; GIT_DIR="{self.b}/.git" git {RESET}', self.a))
        self.assertEqual(decision(out), "deny")
        os.environ["WORKTREE_CWD_GUARD_ALLOW"] = str(self.b)
        self.assertEqual(decision(evaluate(bash(f'cd "{self.main}"; GIT_DIR="{self.b}/.git" git {RESET}', self.a))), "deny")
        os.environ["WORKTREE_CWD_GUARD_ALLOW"] = os.pathsep.join((str(self.main), str(self.b)))
        self.assertIsNone(evaluate(bash(f'cd "{self.main}"; GIT_DIR="{self.b}/.git" git {RESET}', self.a)))


class GitFileMappingTest(test_guard.GuardBase):
    """`<checkout>/.git` の形の git dir を、その checkout に対応付ける。"""

    def test_git_file_of_another_worktree_is_denied(self):
        b = self.b
        rel = os.path.relpath(b / ".git", self.a)
        for gitdir in (b / ".git", f"{b}/./.git", rel):
            for cmd in (
                f'export GIT_DIR="{gitdir}"; git add .',
                f'GIT_DIR="{gitdir}"; export GIT_DIR; git add .',
                f'GIT_DIR="{gitdir}" git {RESET}',
                f'git --git-dir="{gitdir}" add .',
                f'git --git-dir "{gitdir}" commit -m x',
                f'git --git-dir="{gitdir}" --work-tree="{self.a}" {RESET}',
                f'GIT_DIR="{gitdir}" GIT_WORK_TREE="{self.a}" git {RESET}',
            ):
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_reads_of_another_worktree_stay_allowed(self):
        for cmd in (f'GIT_DIR="{self.b}/.git" git status', f'git --git-dir="{self.b}/.git" log', f'git --git-dir="{self.b}/.git" stash list'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_own_git_file_stays_allowed(self):
        for cmd in (f'GIT_DIR="{self.a}/.git" git {RESET}', f'git --git-dir="{self.a}/.git" add .', "git --git-dir=.git commit -m x"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_family_maps_each_git_file_to_its_root(self):
        fam = family.detect(str(self.a))
        mapping = dict(fam.gitdirs)
        for root in (self.a, self.b):
            self.assertEqual(mapping.get(family.norm(root / ".git")), family.norm(root))
        self.assertEqual(mapping.get(family.norm(self.main / ".git")), family.norm(self.main))

    def test_symlinked_checkout_path(self):
        link = Path(self._tmp.name) / "link-b"
        link.symlink_to(self.b)
        self.assertEqual(decision(evaluate(bash(f'GIT_DIR="{link}/.git" git add .', self.a))), "deny")
        git_link = Path(self._tmp.name) / "git-link"
        git_link.symlink_to(self.b / ".git")
        self.assertEqual(decision(evaluate(bash(f'GIT_DIR="{git_link}" git add .', self.a))), "deny")

    def test_unreadable_git_file_is_still_mapped(self):
        # 中身の読めない `.git` ファイルでも、パスとの対応は残す。
        # Windows では `.git` ファイルを `w` で開くと PermissionError になる (隠しファイルのためと読んでいる) ので作り直す
        (self.b / ".git").unlink()
        (self.b / ".git").write_text("", encoding="utf-8")
        self.assertEqual(decision(evaluate(bash(f'GIT_DIR="{self.b}/.git" git add .', self.a))), "deny")


class WorktreeBaseTest(test_guard.GuardBase):
    """`git worktree remove|move` の相対パスは、git dir・作業ツリー・今の場所のどの基点でも別の worktree なら止める。"""

    SHARE_REPO = True  # repo とその周りに何も書かない (GuardBase.SHARE_REPO)

    def test_relative_path_from_any_base_is_denied(self):
        # 引用なしの語に書くので `/` 区切りにする (引用なしの `\` は shell が消し、`..\wt-b` は `..wt-b` になる。test_guard の NtpathEmulationTest)
        rel = Path(os.path.relpath(self.b, self.main)).as_posix()
        own = self.a / ".git"
        for cmd in (
            f'cd "{self.main}"; git --git-dir="{own}" --work-tree="{self.main}" worktree remove --force {rel}',
            f'cd "{self.main}"; GIT_DIR="{own}" GIT_WORK_TREE="{self.main}" git worktree remove --force {rel}',
            f'git -C "{self.main}" --git-dir="{own}" --work-tree=. worktree remove --force {rel}',
            f'git --git-dir="{own}" --work-tree="{self.main}" worktree remove --force {rel}',
            f'git --git-dir=.git --work-tree="{self.main}" worktree remove --force {rel}',
            f'cd "{self.main}"; git --git-dir="{own}" --work-tree="{self.main}" worktree move {rel} /tmp/x',
            f'cd "{self.b}"; git --git-dir="{own}" --work-tree=. worktree remove --force .',
            f'cd "{self.main}"; git --git-dir="{self.b}/.git" --work-tree="{self.main}" worktree remove --force {rel}',
            f'cd "{self.main}"; GIT_DIR="{own}" git worktree remove --force {rel}',
            f'GIT_DIR="{self.main}/.git" git worktree remove --force {Path(os.path.relpath(self.b, self.a)).as_posix()}',
            # git dir の checkout から読むと別の worktree になる形 (今の場所から読むと git は失敗する)。0.7.0 と同じに止める
            f'GIT_DIR="{self.main}/.git" git worktree remove --force {rel}',
            # 引用すれば OS の区切りのまま届く (Windows では `\` 区切り)
            f'cd "{self.main}"; GIT_DIR="{own}" git worktree remove --force "{os.path.relpath(self.b, self.main)}"',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_absolute_path_is_denied_when_no_base_is_known(self):
        for cmd in (f'cd "$Y"; git worktree remove --force "{self.b}"', f'git -C "$Y" worktree remove --force "{self.b}"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_own_worktree_and_other_subcommands_stay_allowed(self):
        own = self.a / ".git"
        for cmd in (
            f'git worktree remove --force "{self.a}"',
            f'git --git-dir=.git --work-tree=. worktree remove --force "{self.a}"',
            "git --git-dir=.git worktree add ../c -b c",
            f'GIT_DIR="{own}" git worktree prune',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))


@unittest.skipUnless(os.path.exists("/bin/bash"), "POSIX の shell が前提 (hook を呼ばず、git と shell の前提だけを確かめるテスト)")
class RealGitTest(unittest.TestCase):
    """本物の git と本物の shell で走らせ、判定の前提の「書く / 書かない」を固定する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = hermetic_env()
        self.main, self.a, self.b = test_guard.make_repo(Path(os.path.realpath(self._tmp.name)))
        for root, name in ((self.main, "main"), (self.a, "a"), (self.b, "b")):
            (root / "README.md").write_text(f"dirty {name}\n", encoding="utf-8")

    def _run(self, shell: list[str], cmd: str) -> None:
        subprocess.run([*shell, cmd], cwd=self.a, env=self.env, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

    def _shells(self):
        out = [["/bin/bash", "-c"]]
        zsh = shutil.which("zsh")
        if zsh:
            out.append([zsh, "-f", "-c"])
        return out

    def _index(self, root: Path) -> str:
        return sh(root, "ls-files", "-s")

    def test_git_dir_only_overwrites_the_work_tree_of_the_place(self):
        for shell in self._shells():
            for name, form in (
                ("export + -C", 'export GIT_DIR="{a}/.git"; git -C "{m}" reset --hard'),
                ("export + cd", 'export GIT_DIR="{a}/.git"; cd "{m}"; git reset --hard'),
                ("option + cd", 'cd "{m}"; git --git-dir="{a}/.git" reset --hard'),
            ):
                with self.subTest(shell=shell[0], form=name):
                    (self.main / "README.md").write_text("dirty main\n", encoding="utf-8")
                    self._run(shell, form.format(a=self.a, m=self.main))
                    self.assertEqual((self.main / "README.md").read_text(encoding="utf-8"), "# demo\n")

    def test_git_file_writes_the_index_of_that_checkout(self):
        for shell in self._shells():
            for i, form in enumerate(('export GIT_DIR="{b}/.git"; git add {f}', 'git --git-dir="{b}/.git" add {f}')):
                with self.subTest(shell=shell[0], form=form):
                    name = f"new-{Path(shell[0]).name}-{i}.txt"
                    (self.a / name).write_text("x\n", encoding="utf-8")
                    self.assertNotIn(name, self._index(self.b))
                    self._run(shell, form.format(b=self.b, f=name))
                    self.assertIn(name, self._index(self.b))
                    self.assertNotIn(name, self._index(self.a))

    def test_explicit_work_tree_leaves_the_place_alone(self):
        for shell in self._shells():
            with self.subTest(shell=shell[0]):
                self._run(shell, f'cd "{self.main}"; git --git-dir="{self.a}/.git" --work-tree="{self.a}" reset --hard')
                self.assertEqual((self.main / "README.md").read_text(encoding="utf-8"), "dirty main\n")
                self.assertEqual((self.a / "README.md").read_text(encoding="utf-8"), "# demo\n")
                (self.a / "README.md").write_text("dirty a\n", encoding="utf-8")

    def test_worktree_remove_takes_the_relative_path_from_the_place(self):
        # `.git` ファイルを git dir にしても、相対パスは今の場所から読まれて別の worktree が消える
        self.assertTrue(self.b.exists())
        rel = os.path.relpath(self.b, self.main)
        self._run(["/bin/bash", "-c"], f'cd "{self.main}"; git --git-dir="{self.a}/.git" --work-tree="{self.main}" worktree remove --force {rel}')
        self.assertFalse(self.b.exists())


if __name__ == "__main__":
    unittest.main()
