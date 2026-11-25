"""テストの harness (test_guard.py の GuardBase) の前提を破ったテストが、FAIL になることの検査。

GuardBase は suite を速くする仕組みを 2 つ持つ。どちらも前提を破ると、古い repo や古い detect の結果で判定したまま
suite が green で通りうる (前提がコメントにしか無かったころ、破る陽性対照 5 つが suite を OK のまま通った):

- 共有の側 (SHARE_REPO = True): クラスの全テストで 1 つの repo を使う。repo とその周り (一時 dir) に書かない
  → テストの後に一時 dir の中を比べ、変わっていたらそのテストを FAIL にする (tree_state)
- 覚えの側: detect の結果を cwd ごとに覚える。worktree を足す・消す・動かす・`.git` を書き換えたら forget_family() を呼ぶ
  → 覚えた値に当てるたびに、detect の結果を決めるファイルの形を比べ、変わっていたら FAIL にする (family_fingerprint)

ここでは前提を破るテストを GuardBase の子クラスとして実際に走らせ、ERROR ではなく FAIL になることを確かめる。
前提を守るテスト (読むだけ・worktree の中での普段の作業・forget_family を呼んだもの) は通ることも確かめる。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401
from _testutil import sh

import family
import test_guard
from test_guard import bash, decision, evaluate

SHARED = "SHARE_REPO のクラスのテストが"
REMEMBERED = "forget_family() を呼ぶ"


def _probe(body, share: bool) -> unittest.TestResult:
    """body(test) を、GuardBase の子クラスの 1 件のテストとして走らせた結果。"""

    class Probe(test_guard.GuardBase):
        SHARE_REPO = share

        def test_body(self):
            body(self)

    # TestSuite.run は終わりに module の fixture も畳む (unittest 全体で 1 つの doModuleCleanups を呼ぶ)。外側の suite の
    # module cleanup を先に走らせないよう、class の fixture だけを自分で回す
    result = unittest.TestResult()
    Probe.setUpClass()
    try:
        Probe("test_body").run(result)
    finally:
        Probe.tearDownClass()
        Probe.doClassCleanups()
    return result


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, os.path.join(tmp, "lnk"))
        except (OSError, NotImplementedError):
            return False
    return True


class ProbeCase(unittest.TestCase):
    def assert_fails(self, result: unittest.TestResult, message: str, *details: str) -> None:
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.errors, [], "ERROR は検出ではない (テストが最後まで走っていない)")
        self.assertEqual(len(result.failures), 1, result.failures)
        text = result.failures[0][1]
        self.assertIn(message, text)
        for detail in details:
            self.assertIn(detail, text)

    def assert_passes(self, result: unittest.TestResult) -> None:
        self.assertEqual(result.testsRun, 1)
        self.assertEqual((result.failures, result.errors), ([], []))


class SharedRepoCheckTest(ProbeCase):
    """SHARE_REPO のクラスのテストが、共有の repo とその周りに書いたら FAIL にする。"""

    def _shared(self, body) -> unittest.TestResult:
        def run(t):
            self.assertEqual(decision(evaluate(bash(f'git -C "{t.main}" reset --hard', t.a))), "deny")  # 覚えも作る (本物の detect)
            body(t)

        return _probe(run, share=True)

    def test_write_in_repo(self):
        result = self._shared(lambda t: (t.a / "x.py").write_text("x\n", encoding="utf-8"))
        self.assert_fails(result, SHARED, os.path.join("repo", ".claude", "worktrees", "a", "x.py"))

    def test_rewrite_with_same_size(self):
        result = self._shared(lambda t: (t.main / "README.md").write_text("# DEMO\n", encoding="utf-8"))
        self.assert_fails(result, SHARED, os.path.join("repo", "README.md"))

    def test_remove_from_repo(self):
        result = self._shared(lambda t: (t.main / "README.md").unlink())
        self.assert_fails(result, SHARED, os.path.join("repo", "README.md"))

    def test_git_write_in_repo(self):
        result = self._shared(lambda t: sh(t.a, "branch", "probe"))
        self.assert_fails(result, SHARED, "probe")

    def test_file_beside_repo(self):
        result = self._shared(lambda t: (t.main.parent / "sibling.txt").write_text("x\n", encoding="utf-8"))
        self.assert_fails(result, SHARED, "sibling.txt")

    def test_dir_beside_repo(self):
        result = self._shared(lambda t: (t.main.parent / "sibling").mkdir())
        self.assert_fails(result, SHARED, "sibling")

    @unittest.skipUnless(_can_symlink(), "symlink を作れない環境")
    def test_symlink_beside_repo(self):
        result = self._shared(lambda t: (t.main.parent / "lnk").symlink_to(t.b))
        self.assert_fails(result, SHARED, "lnk")

    def test_reads_pass(self):
        # 読むだけのテストは通す (detect の git・家族の読み・判定)
        def body(t):
            self.assertEqual(family.detect(str(t.a)).home, family.norm(t.a))
            self.assertIsNone(evaluate(bash("git reset --hard", t.a)))
            self.assertEqual(decision(evaluate(test_guard.write(t.b / "x.py", t.a))), "deny")
            (t.main / "README.md").read_text(encoding="utf-8")

        self.assert_passes(self._shared(body))

    def test_rewrite_with_same_content_passes(self):
        # 中身の変わらない書き直しは、同じクラスの他のテストに漏れないので止めない (mtime は比べない)
        self.assert_passes(self._shared(lambda t: (t.main / "README.md").write_text("# demo\n", encoding="utf-8")))


class RememberedFamilyCheckTest(ProbeCase):
    """detect の覚えに当てたとき、覚えた後に worktree の形が変わっていたら FAIL にする (forget_family の呼び忘れ)。"""

    def _changed(self, change, cwd=lambda t: t.a, forget: bool = False) -> unittest.TestResult:
        def body(t):
            here = cwd(t)
            self.assertEqual(decision(evaluate(bash(f'git -C "{t.main}" reset --hard', here))), "deny")  # 覚える
            change(t)
            if forget:
                t.forget_family()
            evaluate(bash(f'git -C "{t.main}" reset --hard', here))

        return _probe(body, share=False)

    def test_add_without_forget(self):
        result = self._changed(lambda t: sh(t.main, "worktree", "add", "-q", "-b", "lane-c", str(Path(t._tmp.name) / "c")))
        self.assert_fails(result, REMEMBERED)

    def test_remove_without_forget(self):
        self.assert_fails(self._changed(lambda t: sh(t.main, "worktree", "remove", "--force", str(t.b))), REMEMBERED)

    def test_move_without_forget(self):
        result = self._changed(lambda t: sh(t.main, "worktree", "move", str(t.b), str(Path(t._tmp.name) / "moved")))
        self.assert_fails(result, REMEMBERED)

    def test_gitdir_rewritten_without_forget(self):
        # `git worktree repair` のように、admin dir の gitdir だけが別の場所を指す (worktree の一覧の root が変わる)
        def change(t):
            gitdir = t.main / ".git" / "worktrees" / "wt-b" / "gitdir"
            self.assertTrue(gitdir.is_file())
            gitdir.write_text(str(Path(t._tmp.name) / "elsewhere" / ".git") + "\n", encoding="utf-8")

        self.assert_fails(self._changed(change), REMEMBERED)

    def test_git_file_rewritten_without_forget(self):
        def change(t):
            (t.b / ".git").unlink()
            (t.b / ".git").write_text("", encoding="utf-8")

        self.assert_fails(self._changed(change), REMEMBERED)

    @unittest.skipUnless(_can_symlink(), "symlink を作れない環境")
    def test_git_file_made_symlink_without_forget(self):
        # 中身は同じまま `.git` を symlink にする (Family.gitdirs は symlink の行き先で持つ)
        def change(t):
            (t.b / ".git").rename(t.b / ".git-real")
            (t.b / ".git").symlink_to(".git-real")

        self.assert_fails(self._changed(change), REMEMBERED)

    @unittest.skipUnless(_can_symlink(), "symlink を作れない環境")
    def test_add_in_symlinked_git_dir_layout_without_forget(self):
        # main の `.git` が別の場所の git dir への symlink の配置でも、worktree を足したら止める
        def body(t):
            real = Path(t._tmp.name) / "real.git"
            (t.main / ".git").rename(real)
            (t.main / ".git").symlink_to(real)
            self.assertEqual(decision(evaluate(bash(f'git -C "{t.b}" reset --hard', t.a))), "deny")
            sh(t.main, "worktree", "add", "-q", "-b", "lane-c", str(Path(t._tmp.name) / "c"))
            evaluate(bash(f'git -C "{t.b}" reset --hard', t.a))

        self.assert_fails(_probe(body, share=False), REMEMBERED)

    def test_repo_made_above_cwd_without_forget(self):
        # cwd の上 (自分の worktree の下) に repo を作ると、cwd はその repo の中になる
        def setup(t):
            deep = t.a / "sub" / "deep"
            deep.mkdir(parents=True)
            return deep

        result = self._changed(lambda t: sh(t.a / "sub", "init", "-q"), cwd=setup)
        self.assert_fails(result, REMEMBERED)

    def test_cwd_removed_without_forget(self):
        def setup(t):
            sub = t.a / "sub"
            sub.mkdir()
            return sub

        self.assert_fails(self._changed(lambda t: (t.a / "sub").rmdir(), cwd=setup), REMEMBERED)

    def test_forget_after_add_passes(self):
        def body(t):
            c = Path(t._tmp.name) / "c"
            self.assertIsNone(evaluate(bash(f'git -C "{c}" reset --hard', t.a)))  # まだ worktree ではない
            sh(t.main, "worktree", "add", "-q", "-b", "lane-c", str(c))
            t.forget_family()
            self.assertEqual(decision(evaluate(bash(f'git -C "{c}" reset --hard', t.a))), "deny")

        self.assert_passes(_probe(body, share=False))

    def test_work_inside_worktrees_passes(self):
        # worktree の中での普段の作業 (書き込み・commit・branch の切り替え・lock) は、覚えた Family を変えない
        def change(t):
            (t.a / "x.py").write_text("x\n", encoding="utf-8")
            sh(t.a, "add", "-A")
            sh(t.a, "commit", "-q", "-m", "x")
            sh(t.a, "switch", "-q", "-c", "other")
            (t.main / "README.md").write_text("changed\n", encoding="utf-8")
            sh(t.main, "worktree", "lock", str(t.b))

        self.assert_passes(self._changed(change))


if __name__ == "__main__":
    unittest.main()
