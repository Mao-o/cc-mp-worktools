"""`git worktree remove|move <worktree>` の `<worktree>` を、worktree の名前 (パスの末尾) として読む判定。

git はパスとして読む前に、`<worktree>` で終わる worktree のパス (区切りの直後から一致するもの) を数え、ちょうど 1 つなら
それを選ぶ。今の場所に関係なく、`git worktree remove --force b` は `.../b` の worktree を消す。比べ方は core.ignorecase で
変わる (macOS では既定で true)。`RealGitNameTest` が本物の git でこの前提を固定する。
"""
from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from _testutil import HermeticGitTestCase, hermetic_env, make_repo, sh

import command
import family
import test_guard
from test_guard import bash, decision, evaluate


def _add_worktree(main: Path, path: Path, branch: str) -> None:
    sh(main, "worktree", "add", "-q", "-b", branch, str(path))


class WorktreeNameTest(test_guard.GuardBase):
    """別の worktree を名前で指す形を止め、自分の worktree・他の repo・名前の一致しない形は止めない。"""

    def setUp(self):
        super().setUp()
        # A と同じ `.claude/worktrees` の下に置いた別の worktree (B は main の外の `wt-b`)
        self.c = self.main / ".claude" / "worktrees" / "c"
        _add_worktree(self.main, self.c, "lane-c")

    def _decision(self, cmd: str, cwd: Path | None = None):
        return decision(self._evaluate(cmd, cwd))

    def _evaluate(self, cmd: str, cwd: Path | None = None):
        try:
            return evaluate(bash(cmd, cwd or self.a))
        except Exception as e:  # noqa: BLE001 - 読みの例外も判定の誤りとして数える (ERROR ではなく FAIL にする)
            self.fail(f"{cmd!r} で例外: {e!r}")

    def test_name_of_another_worktree_is_denied(self):
        for cmd in (
            "git worktree remove --force c",
            "git worktree remove -f c",
            "git worktree remove c",
            "git worktree remove --force -- c",
            "git worktree remove --force worktrees/c",
            "git worktree remove --force .claude/worktrees/c",
            "git worktree remove --force wt-b",
            "git worktree move c ../moved",
            "git worktree move wt-b ../moved",
            f'cd "{self.main}"; git worktree remove --force c',
            f'cd "{self.main}" && git worktree remove --force wt-b',
            f'git -C "{self.main}" worktree remove --force c',
            f'git --work-tree="{self.main}" worktree remove --force c',
            f'GIT_DIR="{self.main}/.git" git worktree remove --force c',
            f'git --git-dir="{self.a}/.git" worktree remove --force c',
            # git dir が決まれば repo も決まる (今の場所が決まらなくても、名前は repo の worktree の一覧で読む)
            f'cd "$Y"; GIT_DIR="{self.a}/.git" git worktree remove --force c',
            # 作業ツリーが決まらなくても、repo は今の場所で決まる
            'git --work-tree="$X" worktree remove --force c',
            # 大文字小文字を区別しない git (macOS の既定) は、違う綴りでも同じ worktree を選ぶ
            "git worktree remove --force C",
            "git worktree remove --force .CLAUDE/Worktrees/C",
            f'git worktree remove --force "{str(family.norm(self.c))[:-1]}C"',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decision(cmd), "deny")

    def test_reason_names_the_worktree(self):
        out = self._evaluate("git worktree remove --force c", self.a)
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(f"`git worktree remove` が別の worktree ({family.norm(self.c)}) を対象にしている", reason)

    def test_own_worktree_and_other_subcommands_stay_allowed(self):
        for cmd in (
            "git worktree remove --force a",
            "git worktree remove --force worktrees/a",
            "git worktree remove --force .claude/worktrees/a",
            "git worktree remove --force A",
            "git worktree move a ../moved",
            "git worktree add ../d -b d",
            "git worktree list",
            "git worktree prune",
            "git worktree lock c",
            "git worktree unlock c",
            "git worktree repair",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self._evaluate(cmd))

    def test_words_that_do_not_end_a_worktree_path_stay_allowed(self):
        # git はこれらを名前として選ばず、パスとして読んで失敗する (`RealGitNameTest`)
        for cmd in (
            "git worktree remove --force t-b",
            "git worktree remove --force c/",
            "git worktree remove --force ./c",
            "git worktree remove --force x/c",
            "git worktree remove --force c/.",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self._evaluate(cmd))

    def test_name_is_not_read_in_another_repo_or_outside(self):
        # 他の repo の worktree の一覧には、同じ名前の worktree があっても別のもの
        other_main, _, other_b = make_repo(Path(self._tmp.name) / "other")
        other_c = other_main / ".claude" / "worktrees" / "c"
        _add_worktree(other_main, other_c, "other-c")
        outside = Path(self._tmp.name) / "plain"
        outside.mkdir()
        for cmd in (
            f'cd "{other_main}"; git worktree remove --force c',
            f'cd "{other_main}"; git worktree remove --force wt-b',
            f'git -C "{other_main}" worktree remove --force c',
            f'GIT_DIR="{other_main}/.git" git worktree remove --force c',
            f'git --git-dir="{other_main}/.git" worktree remove --force wt-b',
            f'cd "{outside}"; git worktree remove --force c',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self._evaluate(cmd))

    def test_unknown_place_is_a_note(self):
        # 今の場所や git dir が決まらなければ repo も決まらないので止めない。名前が別の worktree に一致するか、名前で決まらない
        # 相対パス (基点が決まらないので、綴りに関係なくどの worktree にも届きうる) なら、ほかの書き込み (`cd "$Y"; git reset --hard`)
        # と同じく注意を出す。
        # 共有 git dir の下の、どの checkout にも対応しない git dir は、git が repo として開けずに失敗する (ほかの書き込みと同じく注意)
        for cmd in (
            'cd "$Y"; git worktree remove --force c',
            'git -C "$Y" worktree remove --force c',
            'GIT_DIR="$X" git worktree remove --force c',
            f'GIT_DIR="{self.main}/.git/worktrees/nosuch" git worktree remove --force c',
            'cd "$Y"; git worktree move wt-b ../moved',
            'cd "$Y"; git worktree remove --force ../wt-b',
            'cd "$Y"; git worktree remove --force ./c',
            'cd "$Y"; git worktree remove --force c/',
            'cd "$Y"; git worktree remove --force x/../c',
            # どの worktree の末尾にも一致しない素の相対パスも、symlink を通れば別の worktree に届く (下の symlink の行)
            'cd "$Y"; git worktree remove --force zz',
            'cd "$Y"; git worktree remove --force x/c',
            # repo は決まるが、相対パスの基点の 1 つ (今の場所・作業ツリー) が決まらない
            f'GIT_DIR="{self.main}/.git"; export GIT_DIR; cd "$Y"; git worktree remove --force ../../wt-b',
            'git --work-tree="$X" worktree remove --force ../wt-b',
            # 対象の語も変数 (どの worktree の名前にもパスにもなりうる)
            'cd "$Y"; git worktree remove --force "$Z"',
            'git -C "$Y" worktree remove --force "$Z"',
            'cd "$Y"; git worktree remove --force "../$Z"',
            'cd "$Y"; git worktree move "$Z" ../moved',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decision(cmd), "context")
        # 注意の文面は、ほかの書き込み (`cd "$Y"; git reset --hard`) と同じく、決まらない行き先を挙げる
        out = command.analyze('cd "$Y"; git worktree remove --force c', str(self.a), family.detect(str(self.a)))
        self.assertEqual([(f.blocked, f.message) for f in out], [(False, "`git worktree remove` の対象 (cd の移動先) を静的に解決できない")])
        # repo が決まり、名前で別の worktree に決まれば止める (注意は足さない。止める理由には注意が載らないので、読みの結果で見る)
        cmd = f'cd "$Y"; GIT_DIR="{self.a}/.git" git worktree remove --force c'
        self.assertEqual(self._decision(cmd), "deny")
        out = command.analyze(cmd, str(self.a), family.detect(str(self.a)))
        self.assertEqual([f.blocked for f in out], [True], [f.message for f in out])
        # symlink を通る相対パス (最後が別の worktree への symlink の `lnk`、途中が別の worktree の親への symlink の `wt/wt-b`) は、
        # 綴りが worktree の root の末尾に一致しない。今の場所が決まれば realpath で別の worktree に届くことを読んで止め、
        # 決まらなければ注意を出す (git は名前で決まらないとパスを realpath で比べ、その worktree を消す)
        (self.main / "lnk").symlink_to(self.b)
        (self.main / "wt").symlink_to(self.b.parent)
        for rel in ("lnk", f"wt/{self.b.name}"):
            with self.subTest(rel=rel):
                self.assertEqual(self._decision(f'cd "{self.main}"; git worktree remove --force {rel}'), "deny")
                self.assertEqual(self._decision(f'cd "$Y"; git worktree remove --force {rel}'), "context")
        # 自分の worktree の名前と、repo の外の絶対パスは、何も出さない
        outside = Path(self._tmp.name) / "plain"
        for cmd in (
            'cd "$Y"; git worktree remove --force a',
            'cd "$Y"; git worktree move a ../moved',
            f'cd "$Y"; git worktree remove --force "{outside}"',
            # 対象の語が無い形と、remove / move 以外
            'cd "$Y"; git worktree remove --force',
            'cd "$Y"; git worktree add ../d -b d',
            'cd "$Y"; git worktree add "$Z"',
            'cd "$Y"; git worktree list',
            'cd "$Y"; git worktree prune',
        ):
            with self.subTest(cmd=cmd):
                out = self._evaluate(cmd)
                self.assertIsNone(out)

    def test_variable_operand_is_a_note(self):
        # 今の場所が決まっていても、対象の語が変数ならどの worktree の名前にもパスにもなりうる。git が読む repo がこの repo なら、
        # ほかの書き込み (`git -C "$X" reset --hard`) と同じく注意を出す (止めない)
        for cmd in (
            'git worktree remove --force "$Z"',
            'git worktree remove -f -- "$Z"',
            'git worktree remove --force "../$Z"',
            'git worktree remove --force "${Z}"',
            'git worktree move "$Z" ../moved',
            f'cd "{self.main}"; git worktree remove --force "$Z"',
            f'git -C "{self.main}" worktree remove --force "$Z"',
            f'GIT_DIR="{self.main}/.git" git worktree remove --force "$Z"',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decision(cmd), "context")
        out = command.analyze('git worktree remove --force "$Z"', str(self.a), family.detect(str(self.a)))
        self.assertEqual([(f.blocked, f.message) for f in out], [(False, "`git worktree remove` の対象 ($Z) を静的に解決できない")])
        # git が読む repo が他の repo か repo の外なら、この repo の worktree には届かない。自分の worktree を変数の先へ動かす形、
        # remove / move 以外、対象の語が無い形も、何も出さない
        other_main, _, _ = make_repo(Path(self._tmp.name) / "other")
        outside = Path(self._tmp.name) / "plain"
        outside.mkdir()
        for cmd in (
            f'cd "{other_main}"; git worktree remove --force "$Z"',
            f'git -C "{other_main}" worktree remove --force "$Z"',
            f'cd "{outside}"; git worktree remove --force "$Z"',
            'git worktree move a "$Z"',
            'git worktree add "$Z"',
            'git worktree lock "$Z"',
            'git worktree remove --force',
        ):
            with self.subTest(cmd=cmd):
                out = self._evaluate(cmd)
                self.assertIsNone(out)

    def test_ambiguous_name_is_read_as_a_path(self):
        # 2 つの worktree が同じ名前で終わると、git は名前で選ばずにパスとして読む
        _add_worktree(self.main, Path(self._tmp.name) / "x" / "c", "lane-xc")
        self.assertIsNone(self._evaluate("git worktree remove --force c", self.a))
        self.assertEqual(self._decision("git worktree remove --force worktrees/c"), "deny")
        self.assertEqual(self._decision("git worktree remove --force x/c"), "deny")
        # 今の場所が決まらなければ、パスとして読んだ先も決まらない (2 つのどちらでもありうるので注意)
        self.assertEqual(self._decision('cd "$Y"; git worktree remove --force c'), "context")

    def test_symlinked_places(self):
        # 自分の場所を symlink から読んでも、自分の worktree の名前は止めない
        link_a = Path(self._tmp.name) / "link-a"
        link_a.symlink_to(self.a)
        self.assertEqual(self._decision("git worktree remove --force c", link_a), "deny")
        self.assertIsNone(self._evaluate("git worktree remove --force a", link_a))
        # symlink を通して作った worktree は、git が realpath で持つ。realpath の末尾で止め、symlink の綴りは名前にならない
        real = Path(self._tmp.name) / "real"
        real.mkdir()
        link = Path(self._tmp.name) / "lnk"
        link.symlink_to(real)
        _add_worktree(self.main, link / "e", "lane-e")
        self.forget_family()  # worktree の一覧が変わった (GuardBase の detect の覚えを捨てる)
        self.assertEqual(self._decision("git worktree remove --force e"), "deny")
        self.assertEqual(self._decision("git worktree remove --force real/e"), "deny")
        self.assertIsNone(self._evaluate("git worktree remove --force lnk/e", self.a))

    # 以下 2 件は characterization: 同じコマンドの手前の操作で worktree の一覧が変わる形は、意図的に止めない
    # (名前は hook の時点の一覧で数え、git は走るときの一覧で数える。CHANGELOG の「受け入れている」に記載)。
    def test_name_made_unique_by_an_earlier_prune_is_not_denied(self):
        stale = Path(self._tmp.name) / "x" / "c"
        _add_worktree(self.main, stale, "lane-xc")
        shutil.rmtree(stale.parent)  # prunable。一覧にはまだ並ぶので、hook には `c` が 2 つに見える
        cmd = "git worktree prune && git worktree remove --force c"
        self.assertIsNone(self._evaluate(cmd))

    def test_name_made_unique_by_removing_own_worktree_is_not_denied(self):
        _add_worktree(self.main, Path(self._tmp.name) / "y" / "a", "lane-ya")
        cmd = f'cd "{self.main}" && git worktree remove --force .claude/worktrees/a && git worktree remove --force a'
        self.assertIsNone(self._evaluate(cmd))


class NameReadingTest(unittest.TestCase):
    """名前の読みを、git を起動せずに組んだ checkout の一覧で確かめる (大文字小文字だけが違う worktree などを作れない FS でも)。"""

    def _blocked(self, cmd: str, home: str, *roots: str) -> bool:
        n = family.norm
        fam = family.Family(home=n(home), roots=tuple(n(r) for r in (home, *roots)), common_dir=n("/r/repo/.git"))
        return any(f.blocked for f in command.analyze(cmd, home, fam))

    @unittest.skipIf(os.name == "nt", "Windows のパスは大文字小文字を区別しない (同じ checkout になる)")
    def test_case_sensitive_reading(self):
        # 大文字小文字を区別する git (Linux の既定) は `b` を `b` だけに一致させる。区別しない読みでは 2 つに一致して決まらない
        roots = ("/r/repo", "/r/repo/wt/B", "/r/repo/wt/b")
        self.assertTrue(self._blocked("git worktree remove --force b", "/r/repo/wt/a", *roots))
        self.assertTrue(self._blocked("git worktree remove --force B", "/r/repo/wt/a", *roots))

    def test_case_insensitive_reading(self):
        roots = ("/r/repo", "/r/repo/wt/c")
        for operand in ("C", "WT/C", "/r/repo/wt/C", "/R/Repo/WT/c"):
            with self.subTest(operand=operand):
                self.assertTrue(self._blocked(f"git worktree remove --force {operand}", "/r/repo/wt/a", *roots))

    def test_unicode_normalization(self):
        # macOS の git は引数を NFC にそろえる。NFD で書いた名前も NFC の worktree を選ぶ
        nfc = unicodedata.normalize("NFC", "caf\u00e9")
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertTrue(self._blocked(f"git worktree remove --force {nfd}", "/r/repo/wt/a", "/r/repo", f"/r/repo/wt/{nfc}"))
        self.assertTrue(self._blocked(f"git worktree remove --force {nfc}", "/r/repo/wt/a", "/r/repo", f"/r/repo/wt/{nfd}"))

    @unittest.skipIf(os.name == "nt", "Windows のパスの比べ方は normcase が決める")
    def test_non_ascii_case_is_not_folded(self):
        # git の大文字小文字を区別しない比べ方は ASCII だけ (`é` の worktree は `É` では選ばれない。実測)
        self.assertFalse(self._blocked("git worktree remove --force \u00c9", "/r/repo/wt/a", "/r/repo", "/r/repo/wt/\u00e9"))

    def test_own_name_and_ambiguous_names(self):
        self.assertFalse(self._blocked("git worktree remove --force a", "/r/repo/wt/a", "/r/repo", "/r/repo/wt/c"))
        # 自分の worktree と同じ名前で終わる別の worktree があると、名前では決まらない (パスとして読む)
        self.assertFalse(self._blocked("git worktree remove --force a", "/r/repo/wt/a", "/r/repo", "/r/other/a"))
        self.assertTrue(self._blocked("git worktree remove --force other/a", "/r/repo/wt/a", "/r/repo", "/r/other/a"))


class NtpathNameTest(unittest.TestCase):
    """`os.path` を ntpath に差し替え、Windows の区切りと大文字小文字で名前を読む (git は起動しない)。"""

    MAIN = "C:\\Users\\other\\repo"
    A = MAIN + "\\.claude\\worktrees\\a"
    C = MAIN + "\\.claude\\worktrees\\c"

    def setUp(self):
        for patcher in (mock.patch.object(os, "path", ntpath), mock.patch.object(os, "sep", "\\")):
            patcher.start()
            self.addCleanup(patcher.stop)
        n = family.norm
        self.fam = family.Family(home=n(self.A), roots=(n(self.MAIN), n(self.A), n(self.C)), common_dir=n(self.MAIN + "\\.git"))

    def _blocked(self, cmd: str) -> bool:
        return any(f.blocked for f in command.analyze(cmd, self.A, self.fam))

    def test_separators_and_case(self):
        for cmd in (
            "git worktree remove --force c",
            "git worktree remove --force worktrees/c",
            'git worktree remove --force "worktrees\\c"',
            "git worktree remove --force .CLAUDE/Worktrees/C",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._blocked(cmd))
        self.assertFalse(self._blocked("git worktree remove --force a"))
        self.assertFalse(self._blocked("git worktree remove --force s/c"))


@unittest.skipUnless(os.path.exists("/bin/bash"), "POSIX の shell が前提 (hook の判定と、git と shell の前提を確かめるテスト)")
class RealGitNameTest(HermeticGitTestCase):
    """本物の git と本物の shell で、名前で指した worktree が消える / 動くことと、その形を hook が止めることを固定する。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("WORKTREE_CWD_GUARD_MODE", "WORKTREE_CWD_GUARD_ALLOW"):
            os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = hermetic_env()
        self.base = Path(os.path.realpath(self._tmp.name))
        self.main, self.a, self.b = make_repo(self.base)
        self.c = self.main / ".claude" / "worktrees" / "c"
        _add_worktree(self.main, self.c, "lane-c")

    def _shells(self):
        out = [["/bin/bash", "-c"]]
        zsh = shutil.which("zsh")
        if zsh:
            out.append([zsh, "-f", "-c"])
        return out

    def _run(self, shell: list[str], cmd: str) -> None:
        subprocess.run([*shell, cmd], cwd=self.a, env=self.env, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

    def _lane_c(self) -> list[str]:
        """branch lane-c を持つ worktree のパス (無ければ空)。"""
        lines = sh(self.main, "worktree", "list", "--porcelain").splitlines()
        paths = [line[len("worktree ") :] for line in lines if line.startswith("worktree ")]
        branches = [line for line in lines if line.startswith(("branch ", "detached"))]
        self.assertEqual(len(paths), len(branches))
        return [p for p, br in zip(paths, branches) if br == "branch refs/heads/lane-c"]

    def _restore_c(self) -> None:
        where = self._lane_c()
        if where == [str(self.c)]:
            return
        if where:
            sh(self.main, "worktree", "move", where[0], str(self.c))
        else:
            sh(self.main, "worktree", "prune")
            sh(self.main, "worktree", "add", "-q", str(self.c), "lane-c")
        self.assertEqual(self._lane_c(), [str(self.c)])

    def _ignorecase(self) -> bool:
        r = subprocess.run(["git", "config", "--get", "core.ignorecase"], cwd=self.main, env=self.env, capture_output=True, text=True)
        return r.stdout.strip() == "true"

    def test_name_removes_or_moves_the_other_worktree(self):
        forms = [
            "git worktree remove --force c",
            "git worktree remove --force -- c",
            "git worktree remove --force worktrees/c",
            "git worktree remove --force .claude/worktrees/c",
            f'git worktree move c "{self.base}/moved"',
            f'cd "{self.main}"; git worktree remove --force c',
        ]
        if self._ignorecase():
            forms += ["git worktree remove --force C", f'git worktree remove --force "{self.c.parent}/C"']
        for shell in self._shells():
            for cmd in forms:
                with self.subTest(shell=shell[0], cmd=cmd):
                    self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
                    self.assertEqual(self._lane_c(), [str(self.c)])
                    self._run(shell, cmd)
                    self.assertNotEqual(self._lane_c(), [str(self.c)])
                    self._restore_c()

    def test_words_git_does_not_pick_leave_the_worktree(self):
        other_main, _, _ = make_repo(self.base / "other")
        other_c = other_main / ".claude" / "worktrees" / "c"
        _add_worktree(other_main, other_c, "other-c")
        for shell in self._shells():
            for cmd in (
                "git worktree remove --force c/",
                "git worktree remove --force ./c",
                "git worktree remove --force x/c",
                f'cd "{other_main}"; git worktree remove --force c',
            ):
                with self.subTest(shell=shell[0], cmd=cmd):
                    self.assertIsNone(evaluate(bash(cmd, self.a)))
                    self._run(shell, cmd)
                    self.assertEqual(self._lane_c(), [str(self.c)])
                    if not other_c.exists():
                        sh(other_main, "worktree", "prune")
                        sh(other_main, "worktree", "add", "-q", str(other_c), "other-c")


if __name__ == "__main__":
    unittest.main()
