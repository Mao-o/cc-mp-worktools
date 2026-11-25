"""worktree-cwd-guard の判定テスト (使い捨ての main checkout + linked worktree 上で実行する)。"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from _testutil import HermeticGitTestCase, make_repo

import importlib.util

import command
import family

_PKG = Path(__file__).resolve().parent.parent


def _load_entry():
    # `__main__` という名前は実行中の unittest を指すため、ファイルから別名で読み込む
    spec = importlib.util.spec_from_file_location("worktree_cwd_guard_entry", _PKG / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ENTRY = _load_entry()
evaluate = _ENTRY.evaluate


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def write(path: Path | str, cwd: Path) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "x"}, "cwd": str(cwd)}


def decision(out: dict | None) -> str | None:
    return None if out is None else out["hookSpecificOutput"].get("permissionDecision", "context")


def tree_state(base: Path) -> dict[str, tuple]:
    """base の下の全エントリの形 (symlink は辿らない)。ファイルは中身の hash、symlink は行き先、ディレクトリは種類だけ。

    GuardBase の SHARE_REPO の検査 (共有の repo とその周りに書かない) が、テストの前後で比べる。
    mtime は比べない: 中身の変わらない書き直し (git の index の refresh など) は他のテストに漏れないので、検査の対象にしない。
    """
    state: dict[str, tuple] = {}
    stack = [str(base)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                rel = os.path.relpath(entry.path, base)
                if entry.is_symlink():
                    state[rel] = ("link", os.readlink(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    state[rel] = ("dir",)
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    with open(entry.path, "rb") as f:
                        state[rel] = ("file", hashlib.sha256(f.read()).hexdigest())
                else:
                    state[rel] = ("other", entry.stat(follow_symlinks=False).st_mode)
    return state


def _git_marker(path: Path) -> tuple:
    """`.git` の形: symlink の行き先 (symlink でなければ None)、種類 (dir / file / missing)、ファイルなら中身。"""
    link = os.readlink(path) if os.path.islink(path) else None
    if path.is_dir():
        return (link, "dir", None)
    if path.is_file():
        return (link, "file", path.read_bytes())
    return (link, "missing", None)


def family_fingerprint(cwd: str, found: tuple) -> tuple:
    """detect の結果を決めるファイルの形。GuardBase の detect の覚えが古くなっていないかを、覚えた値に当てるたびに比べる。

    - cwd とその上の `.git` (最初に見つかるまで): linked worktree の中か、どの worktree か
    - 各 Family の `<common>/worktrees` の一覧と各 `gitdir` の中身: `git worktree list` の並び (add / remove / move / repair)
    - 各 checkout の `.git` (symlink の行き先・種類・中身): Family.gitdirs
    """
    start = Path(cwd)
    chain: list[tuple] = []
    for d in (start, *start.parents):
        chain.append(_git_marker(d / ".git"))
        if chain[-1][1] != "missing":
            break
    parts: list[tuple] = [(start.is_dir(), tuple(chain))]
    for _, fam in found:
        admin = Path(fam.common_dir) / "worktrees"
        names = sorted(os.listdir(admin)) if admin.is_dir() else []
        parts.append(tuple((n, _git_marker(admin / n / "gitdir")) for n in names))
        parts.append(tuple(_git_marker(Path(root) / ".git") for root in fam.roots))
    return tuple(parts)


class GuardBase(HermeticGitTestCase):
    """使い捨ての main checkout + linked worktree 2 つを用意する (他の test module も継承する)。"""

    # True にしたクラスは、クラスの全テストで 1 つの repo と detect の覚えを共有する。
    # repo とその周り (一時 dir) に何も書かないクラスだけに付ける (書くと、同じクラスの他のテストに漏れる)。
    # 書いたテストは、テストの後に一時 dir の中を比べて FAIL にする (tree_state)
    SHARE_REPO = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls.SHARE_REPO:
            tmp = tempfile.TemporaryDirectory()
            cls.addClassCleanup(tmp.cleanup)
            cls._shared_base = Path(tmp.name)
            cls._shared_repo = make_repo(cls._shared_base)
            cls._shared_families = {}

    def setUp(self):
        super().setUp()
        if self.SHARE_REPO:
            self._tmp = None
            self.main, self.a, self.b = self._shared_repo
            self._families = self._shared_families
            self.addCleanup(self._assert_shared_tree_kept, tree_state(self._shared_base))
        else:
            self._tmp = tempfile.TemporaryDirectory()
            self.main, self.a, self.b = make_repo(Path(self._tmp.name))
            self._families = {}
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("WORKTREE_CWD_GUARD_MODE", "WORKTREE_CWD_GUARD_ALLOW"):
            os.environ.pop(k, None)
        # evaluate は呼ぶたびに detect で git を 2 回起動する。テストの間は repo の形が変わらないので、cwd ごとに 1 回だけ
        # 本物の detect を走らせて覚える (entry の global を差し替えるので、evaluate だけに効く。family.detect を直接呼ぶテストと、
        # hook を subprocess で起動するテストは本物の detect を通る)。テストの途中で worktree を足す・消す・`.git` ファイルを
        # 書き換えた後に、同じ cwd で evaluate を呼ぶなら self.forget_family() を呼ぶ。
        # 呼び忘れは、覚えた値に当てるたびに detect の結果を決めるファイルの形 (family_fingerprint) を比べて FAIL にする
        real = _ENTRY.detect_readings

        def remembered(cwd):
            if cwd not in self._families:
                found = real(cwd)
                self._families[cwd] = (found, family_fingerprint(cwd, found))
                return found
            found, fingerprint = self._families[cwd]
            if family_fingerprint(cwd, found) != fingerprint:
                raise self.failureException(
                    f"detect の覚えが古い ({cwd}): 覚えた後に worktree の一覧か `.git` が変わった。"
                    "worktree を変えたら forget_family() を呼ぶ"
                )
            return found

        patcher = mock.patch.object(_ENTRY, "detect_readings", remembered)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_shared_tree_kept(self, before: dict[str, tuple]) -> None:
        after = tree_state(self._shared_base)
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if changed:
            self.fail(
                "SHARE_REPO のクラスのテストが、共有の repo とその周りを書き換えた "
                f"(書くテストは SHARE_REPO を外したクラスに置く): {changed[:10]}"
            )

    def forget_family(self):
        self._families.clear()

    def tearDown(self):
        self._env.stop()
        if self._tmp is not None:
            self._tmp.cleanup()


class GuardTest(GuardBase):
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

    def test_subdirectory_of_own_worktree_is_still_home(self):
        # 自分の worktree の下の subdirectory で作業しても、この session の worktree は worktree の root (`--show-toplevel`)。
        # 今の場所を root と読むと、subdirectory の外の自分の worktree (root の README.md や `cd ..`) への書き込みを、別の checkout として止める
        sub = self.a / "src" / "pkg"
        sub.mkdir(parents=True)
        fam = family.detect(str(sub))
        self.assertIsNotNone(fam)
        self.assertEqual(fam.home, family.norm(self.a))
        for payload in (
            write(sub / "x.py", sub),
            write("y.py", sub),
            write(self.a / "README.md", sub),
            write("../../README.md", sub),
            bash("git reset --hard", sub),
            bash("git add . && git commit -m x", sub),
            bash("cd ../.. && git reset --hard", sub),
            bash(f'git -C "{self.a}" reset --hard', sub),
            bash("git -C .. checkout -- .", sub),
        ):
            with self.subTest(payload=payload["tool_input"]):
                self.assertIsNone(evaluate(payload))
        # 別の checkout への書き込みは subdirectory からでも止める (上の None が、判定の対象外になって通ったのではないことの対照)
        for payload in (write(self.main / "README.md", sub), bash(f'git -C "{self.main}" reset --hard', sub), bash("cd ../../../../.. && git stash", sub)):
            with self.subTest(payload=payload["tool_input"]):
                self.assertEqual(decision(evaluate(payload)), "deny")

    # --- Bash ---------------------------------------------------------------

    def test_git_write_into_other_checkout_is_denied(self):
        for cmd in (
            f'cd "{self.main}" && git checkout -b oops',
            f'git -C "{self.main}" switch -c oops',
            f'git -C "{self.b}" commit -m x',
            f'git --work-tree="{self.main}" reset --hard',
            f'git --git-dir="{self.main / ".git"}" checkout main',
            f'git worktree remove "{self.b}"',
            f'cd "{self.main}"; git stash',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_subshell_cd_does_not_leak_out(self):
        # ( ... ) の中の cd は外に影響しない。外側の cd が生きている
        cmd = f'cd "{self.main}"; (cd "{self.a}"); git reset --hard'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # 逆に、サブシェルの中だけで main に移るなら外側の git は自分の worktree
        self.assertIsNone(evaluate(bash(f'(cd "{self.main}" && git log); git reset --hard', self.a)))
        self.assertEqual(decision(evaluate(bash(f'(cd "{self.main}" && git reset --hard)', self.a))), "deny")

    def test_cd_options_are_skipped(self):
        for cmd in (f'cd -- "{self.main}"; git reset --hard', f'cd -P "{self.main}" && git stash'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        out = evaluate(bash("cd - && git reset --hard", self.a))
        self.assertEqual(decision(out), "context")

    def test_bisect_is_a_write(self):
        self.assertEqual(decision(evaluate(bash(f'git -C "{self.main}" bisect start', self.a))), "deny")

    def test_wrapper_options_are_skipped(self):
        for cmd in (
            f'env -i git -C "{self.main}" reset --hard',
            f'env -u HOME git -C "{self.main}" stash',
            f'env -C "{self.main}" git checkout -b oops',
            f'env --chdir="{self.main}" git commit -m x',
            f'sudo -u me git -C "{self.main}" reset --hard',
            f'time -p git -C "{self.main}" stash',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'env -C "{self.a}" git reset --hard', self.a)))

    def test_conditional_cd_keeps_both_alternatives(self):
        # `&&` / `||` の後の cd は実行されないことがある。どちらの場合も考える
        for cmd in (
            f'test -d x || cd "{self.main}"; git reset --hard',
            f'true && cd "{self.main}"; git stash',
            f'make && cd "{self.main}" && git commit -m x',
            # 自分の worktree へ戻る cd が実行されないと main のまま
            f'cd "{self.main}"; test -d x && cd "{self.a}"; git reset --hard',
            f'cd "{self.main}" && (test -d x || cd "{self.a}"; git stash)',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # 先頭の cd は必ず実行されるので置き換え (自分の worktree に戻れば許す)
        self.assertIsNone(evaluate(bash(f'cd "{self.main}"; cd "{self.a}"; git reset --hard', self.a)))
        out = evaluate(bash('true && cd "$X"; git reset --hard', self.a))
        self.assertEqual(decision(out), "context")

    def test_git_env_variables_select_the_repo(self):
        gitdir_b = (self.b / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        for cmd in (
            f'GIT_DIR="{gitdir_b}" GIT_WORK_TREE="{self.b}" git reset --hard',
            f'env GIT_WORK_TREE="{self.main}" git checkout -- .',
            f'export GIT_DIR="{self.main / ".git"}"; git stash',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # option が環境変数より優先される / unset で外れる
        own = (self.a / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        self.assertIsNone(evaluate(bash(f'GIT_DIR="{gitdir_b}" git --git-dir="{own}" status', self.a)))
        self.assertIsNone(evaluate(bash(f'export GIT_DIR="{gitdir_b}"; unset GIT_DIR; git reset --hard', self.a)))

    def test_pipeline_and_background_cd_do_not_leak_out(self):
        # pipeline の要素と `&` のコマンドはサブシェルで動くので、中の cd は外に残らない
        for cmd in (
            f'cd "{self.main}"; cd "{self.a}" | cat; git reset --hard',
            f'cd "{self.main}"; echo x | cd "{self.a}"; git stash',
            f'cd "{self.main}"; cd "{self.a}" & git reset --hard',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'cd "{self.main}" | cat; git reset --hard 2>&1 | tail -1', self.a)))

    def test_last_pipeline_element_cd_may_remain(self):
        # zsh (Bash tool の shell) は pipeline の最後の要素を今の shell で動かすので、そこでの cd は残る
        # (bash ではサブシェルで残らない)。両方の読みで追い、どちらかで別 checkout への書き込みなら止める
        for cmd in (
            f'printf a | cd "{self.main}" && git commit -m x',
            f'cat <<EOF | cd "{self.main}" && git add .\nbody\nEOF',
            f'printf a | cd "{self.main}"; git reset --hard',
            f'printf a |& pushd "{self.main}" >/dev/null && git stash',
            f'printf a | {{ cd "{self.main}"; }}; git stash',
            # brace group の中の後ろの git は、どちらの shell でも移動先で動く
            f'printf a | {{ cd "{self.main}"; git commit -m x; }}',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # pipeline の途中の要素の cd は、どちらの shell でも残らない (従来どおり)
        for cmd in (
            f'printf a | cd "{self.main}" | cat; git reset --hard',
            f'cd "{self.main}" | cat && git commit -m x',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))
        # 最後の要素の cd が自分の worktree へ戻っても、bash の読み (残らない) では main のまま
        for cmd in (
            f'cd "{self.main}"; printf a | cd "{self.a}"; git reset --hard',
            # `|` の直後の改行は pipeline の続きなので、次の cd も最後の要素
            f'cd "{self.main}"; printf a |\n cd "{self.a}"; git reset --hard',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # 読み取りは止めない
        self.assertIsNone(evaluate(bash(f'printf a | cd "{self.main}" && git status', self.a)))

    def test_popd_returns_to_the_pushed_directory(self):
        cmd = f'cd "{self.main}"; pushd "{self.a}"; popd; git reset --hard'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'pushd "{self.main}"; popd; git reset --hard', self.a)))
        # 呼び出し前のスタックは分からないので、注意だけ出す
        self.assertEqual(decision(evaluate(bash("popd; git reset --hard", self.a))), "context")

    def test_conditional_chain_does_not_invent_states(self):
        # `cd main && cd A` は、どちらに転んでも最後は A にいる (成功なら戻る / 失敗なら動かない)
        for cmd in (
            f'cd "{self.main}" && cd "{self.a}"; git reset --hard',
            f'false || cd "{self.a}"; git reset --hard',
            f'cd "{self.main}" && (false || cd "{self.a}"; git stash)',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))
        # zsh の `&` は最後の pipeline だけを background にするので、手前の cd が今の shell に残る
        # (bash は list 全体が background で残らない)。list の中の git は判定する
        for cmd in (f'cd "{self.main}" && true & wait; git reset --hard', f'cd "{self.main}" && git stash & wait'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_every_target_is_checked_against_the_allowlist(self):
        os.environ["WORKTREE_CWD_GUARD_ALLOW"] = str(self.main)
        cmd = f'git -C "{self.main}" --work-tree="{self.b}" reset --hard'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_exported_later_and_env_clearing(self):
        gitdir_b = (self.b / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        cmd = f'GIT_DIR="{gitdir_b}"; GIT_WORK_TREE="{self.b}"; export GIT_DIR GIT_WORK_TREE; git reset --hard'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # export しない代入はコマンドに届かない
        self.assertIsNone(evaluate(bash(f'GIT_DIR="{gitdir_b}"; git reset --hard', self.a)))
        # env -i / env -u は手前の代入や export を消す
        for cmd in (
            f'GIT_DIR="{self.main / ".git"}" GIT_WORK_TREE="{self.main}" env -i /usr/bin/git reset --hard',
            f'export GIT_DIR="{self.main / ".git"}"; env -u GIT_DIR git reset --hard',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_heredoc_body_is_not_executed(self):
        for cmd in (
            f"cat > helper.sh <<'EOF'\ngit -C {self.main} reset --hard\nEOF\ngit status",
            f"cat <<-EOF > notes.md\n\tgit -C {self.main} checkout main\n\tEOF",
            f'cat > a.sh << "A" && cat > b.sh <<B\ngit -C {self.main} stash\nA\ngit -C {self.b} stash\nB',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))
        # 本文の後ろのコマンドは判定する
        cmd = f"cat > x.sh <<'EOF'\necho hi\nEOF\ngit -C \"{self.main}\" reset --hard"
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_submodule_and_sparse_checkout_writes(self):
        for cmd in (f'git -C "{self.main}" submodule update --init', f'git -C "{self.main}" sparse-checkout set src'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        for cmd in (f'git -C "{self.main}" submodule status', f'git -C "{self.main}" sparse-checkout list'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_branch_rename_is_a_write(self):
        self.assertEqual(decision(evaluate(bash(f'git -C "{self.main}" branch -m renamed', self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'git -C "{self.main}" branch --show-current', self.a)))

    def test_repo_side_and_work_tree_side_are_both_checked(self):
        # HEAD / index は main 側、作業ツリーは自分の worktree。main の HEAD が動くので止める
        cmd = f'git -C "{self.main}" --work-tree="{self.a}" checkout -b oops'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_linked_worktree_git_dir_maps_to_its_checkout(self):
        gitdir_b = (self.b / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        cmd = f'git --git-dir="{gitdir_b}" reset --hard'
        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        own = (self.a / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        self.assertIsNone(evaluate(bash(f'git --git-dir="{own}" reset --hard', self.a)))

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

    def test_path_aliases_are_the_same_checkout(self):
        # `/` 区切りは POSIX でも Windows でも同じ場所を指す
        fwd = str(self.main).replace("\\", "/")
        self.assertEqual(decision(evaluate(bash(f'git -C "{fwd}" reset --hard', self.a))), "deny")
        own = str(self.a).replace("\\", "/")
        self.assertIsNone(evaluate(bash(f'git -C "{own}" reset --hard', self.a)))

    @unittest.skipUnless(os.name == "nt", "Windows のパス表記 (大文字小文字 / 8.3 短縮名)")
    def test_windows_path_aliases(self):
        import ctypes

        def short(p: Path) -> str:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, len(buf))
            return buf.value if n else str(p)

        for alias in (str(self.main).upper(), str(self.main).lower(), short(self.main)):
            with self.subTest(alias=alias):
                self.assertEqual(decision(evaluate(bash(f'git -C "{alias}" reset --hard', self.a))), "deny")
                self.assertEqual(decision(evaluate(write(Path(alias) / "x.py", self.a))), "deny")
        for alias in (str(self.a).upper(), short(self.a)):
            with self.subTest(alias=alias):
                self.assertIsNone(evaluate(bash(f'git -C "{alias}" reset --hard', self.a)))
                self.assertIsNone(evaluate(write(Path(alias) / "x.py", self.a)))

    @unittest.skipUnless(os.name == "nt", "Windows の Git Bash の綴り")
    def test_git_bash_drive_path_on_windows(self):
        def git_bash(p: Path) -> str:
            drive, rest = os.path.splitdrive(str(p))
            if not drive.endswith(":"):
                self.skipTest(f"ドライブ文字の無いパス ({p})")
            return "/" + drive[0].lower() + rest.replace("\\", "/")

        self.assertEqual(decision(evaluate(bash(f'git -C "{git_bash(self.main)}" reset --hard', self.a))), "deny")
        self.assertEqual(decision(evaluate(bash(f'cd "{git_bash(self.b)}"; git reset --hard', self.a))), "deny")
        self.assertIsNone(evaluate(bash(f'git -C "{git_bash(self.a)}" reset --hard', self.a)))

    @unittest.skipUnless(os.name == "nt", "Windows の Git Bash の綴り")
    def test_git_bash_spelling_of_cwd_on_windows(self):
        # hook の入力の cwd が `/c/...` で届いても、本物の detect (git を起動する) が Git Bash の読み (`C:\...`) で linked worktree と分かる。
        # NtpathEmulationTest は detect を差し替えるので、ここだけが Windows の CI で本物の detect を通す
        drive, rest = os.path.splitdrive(str(self.a))
        if not drive.endswith(":"):
            self.skipTest(f"ドライブ文字の無いパス ({self.a})")
        here = "/" + drive[0].lower() + rest.replace("\\", "/")

        def run(data: dict) -> dict | None:
            try:
                return evaluate(data)
            except Exception as e:  # noqa: BLE001 - main() は例外を「内部エラー」の注意 (止めない) にする。ERROR ではなく FAIL で数える
                self.fail(f"`/c/...` の cwd ({here}) で evaluate が例外を投げた: {e!r}")

        self.assertEqual(decision(run(bash(f'git -C "{self.main}" reset --hard', here))), "deny")
        self.assertEqual(decision(run(bash("cd ../../.. && git reset --hard", here))), "deny")
        self.assertEqual(decision(run(write(self.b / "x.py", here))), "deny")
        self.assertIsNone(run(bash("git reset --hard", here)))

    def test_drive_like_path_off_windows_is_a_plain_path(self):
        # Windows でなければ `/c/x` はそのままのパスで、Git Bash の読み (`C:\x`) はしない
        # (POSIX で `c:/x` と読むと今の場所の下の相対パスになり、main の下として止めてしまう)
        for cmd in (f'cd "{self.main}"; git -C /c/x reset --hard', f'cd "{self.main}"; cd /c && git add .'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

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


def _windows_realpath(path: str) -> str:
    # 今のドライブを C: とした Windows の realpath (ドライブの無い `\c\x` は `C:\c\x`)。
    # POSIX の上の ntpath.realpath はドライブを補わず、実機の Windows は今のドライブで補うので、どの OS でも同じ読みにそろえる
    return ntpath.normpath(path if ntpath.splitdrive(path)[0] else "C:" + path)


class NtpathEmulationTest(unittest.TestCase):
    """`os.path` を ntpath に差し替え、Windows のパスの読みをどの OS でも確かめる (git は起動しない)。"""

    TMP = "C:\\Users\\other"
    MAIN = TMP + "\\repo"
    A = MAIN + "\\.claude\\worktrees\\a"
    B = TMP + "\\wt-b"

    def setUp(self):
        for patcher in (
            mock.patch.object(os, "path", ntpath),
            mock.patch.object(os, "sep", "\\"),
            mock.patch.object(os, "name", "nt"),
            mock.patch.object(ntpath, "realpath", _windows_realpath),
            mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\me", "USERNAME": "me"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fam = self._family()

    def _family(self, *extra: str) -> family.Family:
        n = family.norm
        return family.Family(
            home=n(self.A),
            roots=(n(self.MAIN), n(self.A), n(self.B), *(n(e) for e in extra)),
            common_dir=n(self.MAIN + "\\.git"),
        )

    def _blocked(self, cmd: str, homes: tuple[str, ...] = (), fam: family.Family | None = None) -> bool:
        # 実在するディレクトリを固定する (実機の `C:\Users` の中身に左右されないように)
        real = {family.norm(h) for h in homes}
        with mock.patch.object(ntpath, "isdir", lambda p: family.norm(p) in real):
            return any(f.blocked for f in command.analyze(cmd, self.A, fam or self.fam))

    def test_tilde_word_without_a_home_stays_under_the_place(self):
        # expanduser は `~+` を `C:\Users\+` と推測する。実際の shell は `~+` / `~+/x` で main に残り、
        # `~1` / `~-1` / 知らない名前は bash が失敗して main に残る (`~-` は OLDPWD だが、`cd ~-` を読まないのは POSIX も同じ)
        for word in ("~+", "~-", "~+/x", "~-/x", "~1", "~+1", "~-1", "~nosuch", "~nosuch/x"):
            for cmd in (f'cd "{self.MAIN}"; cd {word}; git reset --hard', f'cd "{self.MAIN}"; builtin cd {word}; git reset --hard'):
                with self.subTest(cmd=cmd):
                    self.assertTrue(self._blocked(cmd))

    def test_tilde_word_with_a_home_is_expanded(self):
        # 実在する home なら、その下の checkout を止める (`~other` は USERPROFILE の親の下の `other`)。
        # 引用した `~` は実際の shell では展開されないが、引用の有無を区別しない既存の読みで止める側に倒す
        for cmd in ("git -C ~other/repo reset --hard", "git -C ~other/wt-b reset --hard", 'git -C "~other\\repo" reset --hard'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._blocked(cmd, homes=(self.TMP,)))
                self.assertFalse(self._blocked(cmd))

    def test_plain_home_is_expanded(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": self.TMP}):
            self.assertTrue(self._blocked("git -C ~/repo reset --hard"))
            self.assertFalse(self._blocked(f'cd "{self.MAIN}"; cd ~; git reset --hard'))

    def test_separator_of_a_relative_worktree_path(self):
        rel = "..\\wt-b"
        self.assertTrue(self._blocked(f'cd "{self.MAIN}"; git worktree remove --force "{rel}"'))
        self.assertTrue(self._blocked(f'cd "{self.MAIN}"; git worktree remove --force ../wt-b'))
        # 引用なしの `\` は shell が消す (git が受け取るのは `..wt-b` で、別の worktree を指さない)
        self.assertFalse(self._blocked(f'cd "{self.MAIN}"; git worktree remove --force {rel}'))

    def test_git_bash_drive_path_names_the_drive(self):
        # Windows の Bash tool は Git Bash で動き、`/c/x` は `C:\x` (pwd もこの形を返す)。行き先を読むどの入り口でも止める
        main, b = "/c/Users/other/repo", "/c/Users/other/wt-b"
        for cmd in (
            f"git -C {main} reset --hard",
            f"cd {main}; git reset --hard",
            f'cd "{main}" && git commit -m x',
            "git -C /C/Users/other/repo reset --hard",  # ドライブの文字の大文字
            "cd /c; cd Users/other/repo; git reset --hard",  # ドライブだけ
            "cd /c/Users; git -C other/x/../repo reset --hard",  # 相対パスの途中の 1 文字の語はドライブではない
            f"env -C {main} git reset --hard",
            f"pushd {main}; git reset --hard",
            f"pushd -n {main}; popd; git reset --hard",
            f"git --git-dir={main}/.git reset --hard",
            f"GIT_DIR={main}/.git git reset --hard",
            f"git --work-tree={main} reset --hard",
            f"git worktree remove --force {b}",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(self._blocked(cmd))

    def test_git_bash_drive_path_needs_one_letter_and_a_separator(self):
        # `/cc/x` はドライブではない。自分の worktree を `/c/...` で書いた形は止めない
        for cmd in (
            "git -C /cc/Users/other/repo reset --hard",
            "git -C /d/Users/other/repo reset --hard",  # 別のドライブ (D:) は C: の checkout を指さない
            'cd "/c\n"; cd Users/other/repo; git reset --hard',  # 語の終わりの前の改行はドライブの終わりではない
            "git -C /c/Users/other/repo/.claude/worktrees/a reset --hard",
            "cd /c/Users/other/repo/.claude/worktrees/a && git add .",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(self._blocked(cmd))

    def test_windows_reading_of_a_drive_path_is_kept(self):
        # `/c/proj` の Windows の読み (今のドライブの `\c\proj`) も残す。`MSYS_NO_PATHCONV=1` などで変換を止めた git はそこを読み、
        # 読みの和にすれば、これまで止めていた形は止まったまま
        fam = self._family("C:\\c\\proj")
        self.assertTrue(self._blocked("git -C /c/proj reset --hard", fam=fam))
        self.assertTrue(self._blocked("cd /c/proj && git add .", fam=fam))
        self.assertFalse(self._blocked("git -C /c/proj reset --hard"))

    # --- Write / Edit のパスと許可リスト (evaluate を通す) ---------------------

    def _evaluate(self, data: dict, fam: family.Family | None = None, allow: str | None = None) -> dict | None:
        # detect は git を起動するので、固定の family を返す。外側の env の MODE / ALLOW は外す
        with (
            mock.patch.dict(os.environ),
            mock.patch.object(_ENTRY, "detect_readings", lambda cwd: ((cwd, fam or self.fam),)),
            mock.patch.object(ntpath, "isdir", lambda p: False),
            mock.patch.object(os, "pathsep", ";"),
        ):
            os.environ.pop("WORKTREE_CWD_GUARD_MODE", None)
            os.environ.pop("WORKTREE_CWD_GUARD_ALLOW", None)
            if allow is not None:
                os.environ["WORKTREE_CWD_GUARD_ALLOW"] = allow
            return evaluate(data)

    def _file(self, tool: str, path: str) -> dict:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return {"tool_name": tool, "tool_input": {key: path, "content": "x"}, "cwd": self.A}

    def test_git_bash_drive_path_in_file_tools(self):
        # Claude Code は file_path を hook の前に `C:\...` の形に展開して渡す (公式 docs)。`/c/...` の綴りのまま届いたときも、
        # Bash の行き先と同じく Git Bash の読み (`C:\x`) でも判定する
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            for path in (
                "/c/Users/other/repo/x.py",
                "/C/Users/other/repo/x.py",
                "/c/Users/other/wt-b/x.py",
                "/c/Users/other/repo/.claude/worktrees/a/../../../x.py",
                "C:\\Users\\other\\repo\\x.py",  # docs の形 (これまでも止めていた)
            ):
                with self.subTest(tool=tool, path=path):
                    self.assertEqual(decision(self._evaluate(self._file(tool, path))), "deny")
            for path in (
                "/c/Users/other/repo/.claude/worktrees/a/x.py",  # 自分の worktree
                "C:\\Users\\other\\repo\\.claude\\worktrees\\a\\x.py",
                "/cc/Users/other/repo/x.py",  # ドライブの綴りではない
                "/d/Users/other/repo/x.py",  # 別のドライブ (D:) は C: の checkout を指さない
                "/c/Users/other/scratch.txt",  # repo の外
            ):
                with self.subTest(tool=tool, path=path):
                    self.assertIsNone(self._evaluate(self._file(tool, path)))

    def test_windows_reading_of_a_drive_path_in_file_tools_is_kept(self):
        # `/c/proj/x.py` の Windows の読み (今のドライブの `\c\proj\x.py`) も残す (Bash の行き先と同じ和)
        fam = self._family("C:\\c\\proj")
        self.assertEqual(decision(self._evaluate(self._file("Write", "/c/proj/x.py"), fam)), "deny")
        self.assertIsNone(self._evaluate(self._file("Write", "/c/proj/x.py")))

    def test_file_tool_deny_names_the_file_once(self):
        # 2 つの読みが同じ綴りになる形 (docs の `C:\...`) で、同じ理由を 2 回並べない
        out = self._evaluate(self._file("Write", "C:\\Users\\other\\repo\\x.py"))
        self.assertEqual(decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason.count("配下のファイル"), 1, reason)

    def test_file_tool_deny_names_each_target_once(self):
        # 2 つの読みの綴りが違っても、行き先が同じなら同じ理由を 2 回並べない (ドライブの root より上に出る `..` の形)
        for path, extra in (
            ("/c/../Users/other/repo/x.py", ()),
            ("/C/../users/other/repo/x.py", ()),
            ("/c/x/../../Users/other/repo/x.py", ()),
            ("/c/../c/proj/x.py", ("C:\\c\\proj",)),  # どちらの読みでも `C:\c\proj\x.py`
        ):
            with self.subTest(path=path):
                out = self._evaluate(self._file("Write", path), self._family(*extra))
                self.assertEqual(decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(reason.count("配下のファイル"), 1, reason)

    def test_file_tool_both_readings_hit_other_checkouts(self):
        # `/c/Users/other/repo/x.py` の Windows の読み (`C:\c\Users\other\repo`) と Git Bash の読み (main) が別々の checkout に
        # 当たるとき、片方だけを許可リストに入れても、もう片方で止める (和の 2 つ目を読み落とさない)。両方の読みを許したら通す
        fam = self._family("C:\\c\\Users\\other\\repo")
        data = self._file("Write", "/c/Users/other/repo/x.py")
        for allow, left in (
            (None, ("c:\\c\\users\\other\\repo\\x.py", "c:\\users\\other\\repo\\x.py")),
            ("C:\\c\\Users\\other\\repo", ("c:\\users\\other\\repo\\x.py",)),
            ("C:\\Users\\other\\repo", ("c:\\c\\users\\other\\repo\\x.py",)),
        ):
            with self.subTest(allow=allow):
                out = self._evaluate(data, fam, allow=allow)
                self.assertEqual(decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(reason.count("配下のファイル"), len(left), reason)
                for target in left:
                    self.assertIn(f"({target})", reason)
        self.assertIsNone(self._evaluate(data, fam, allow="/c/Users/other/repo"))

    def test_allow_list_in_git_bash_spelling(self):
        # deny の行き先と同じ綴り (`/c/...`。Git Bash の pwd もこの形を返す) で書いた root を許す
        main_cmd, b_cmd = "git -C /c/Users/other/repo reset --hard", "git -C /c/Users/other/wt-b reset --hard"
        for allow in (
            "/c/Users/other/repo",
            "/C/Users/other/repo/",
            "C:\\Users\\other\\repo",
            "C:/Users/other/repo",
            "/c/x;/c/Users/other/repo",
        ):
            with self.subTest(allow=allow):
                self.assertIsNone(self._evaluate(bash(main_cmd, self.A), allow=allow))
                self.assertIsNone(self._evaluate(self._file("Write", "C:\\Users\\other\\repo\\x.py"), allow=allow))
                self.assertIsNone(self._evaluate(self._file("Edit", "/c/Users/other/repo/x.py"), allow=allow))
                self.assertEqual(decision(self._evaluate(bash(b_cmd, self.A), allow=allow)), "deny")
                self.assertEqual(decision(self._evaluate(self._file("Write", "C:\\Users\\other\\wt-b\\x.py"), allow=allow)), "deny")
        for allow in ("/cc/Users/other/repo", "/d/Users/other/repo", "/c/Users/other", "/c/Users/other/repo/.claude"):
            with self.subTest(allow=allow):
                self.assertEqual(decision(self._evaluate(bash(main_cmd, self.A), allow=allow)), "deny")
                self.assertEqual(decision(self._evaluate(self._file("Write", "C:\\Users\\other\\repo\\x.py"), allow=allow)), "deny")

    def test_windows_reading_of_a_drive_path_in_allow_list_is_kept(self):
        # 許可リストの `/c/proj` の Windows の読み (`C:\c\proj`) も残す。和にしたので、これまで許していた root は許したまま
        fam = self._family("C:\\c\\proj")
        self.assertIsNone(self._evaluate(self._file("Write", "C:\\c\\proj\\x.py"), fam, allow="/c/proj"))
        self.assertIsNone(self._evaluate(bash("git -C /c/proj reset --hard", self.A), fam, allow="/c/proj"))

    # --- hook の入力の cwd が Git Bash の綴りで届く (family.detect_readings) ----------

    GIT_BASH_A = "/c/Users/other/repo/.claude/worktrees/a"

    def _shadow_family(self) -> family.Family:
        # `/c/Users/other/repo/...` の Windows の読み (今のドライブの `\c\Users\...`) の側にも linked worktree がある世界
        n = family.norm
        shadow_main = "C:\\c\\Users\\other\\repo"
        return family.Family(
            home=n(shadow_main + "\\.claude\\worktrees\\a"), roots=(n(shadow_main), n(shadow_main + "\\.claude\\worktrees\\a")),
            common_dir=n(shadow_main + "\\.git"),
        )

    def _linked(self, *families: family.Family, failing: tuple[tuple[str, Exception], ...] = ()):
        """families の worktree の中 (root か、その下) の cwd だけを linked worktree と読む、1 つの読みの detect (git は起動しない)。

        cwd は今の OS の読み (ここでは今のドライブを C: とした Windows の読み) で読む。読みの和を取るのは detect_readings の側。
        failing の (正規化した dir, 例外) の中の cwd では、その例外を投げる
        """
        def detect_one(cwd):
            here = family.norm(cwd)
            for top, error in failing:
                if here == top or here.startswith(top + "\\"):
                    raise error
            for fam in families:
                if here == fam.home or here.startswith(fam.home + "\\"):
                    return fam
            return None

        return mock.patch.object(family, "_detect_one", detect_one)

    def _evaluate_at(
        self, data: dict, families: tuple[family.Family, ...], allow: str | None = None,
        failing: tuple[tuple[str, Exception], ...] = (),
    ) -> dict | None:
        # _evaluate と同じ env と差し替えで、detect だけを本物の detect_readings (読みの和) にする
        with (
            self._linked(*families, failing=failing),
            mock.patch.dict(os.environ),
            mock.patch.object(ntpath, "isdir", lambda p: False),
            mock.patch.object(os, "pathsep", ";"),
        ):
            os.environ.pop("WORKTREE_CWD_GUARD_MODE", None)
            os.environ.pop("WORKTREE_CWD_GUARD_ALLOW", None)
            if allow is not None:
                os.environ["WORKTREE_CWD_GUARD_ALLOW"] = allow
            return evaluate(data)

    def _payloads(self, cwd: str) -> list[tuple[dict, str | None]]:
        # (入力, 期待する判定)。相対パスは cwd から解決する
        def file(tool: str, path: str) -> dict:
            return {**self._file(tool, path), "cwd": cwd}

        return [
            (file("Write", "C:\\Users\\other\\repo\\x.py"), "deny"),
            (file("Edit", "/c/Users/other/wt-b/x.py"), "deny"),
            (file("Write", "../../../x.py"), "deny"),
            (file("NotebookEdit", "..\\..\\..\\..\\wt-b\\x.ipynb"), "deny"),
            (file("Write", "x.py"), None),
            (file("Write", "C:\\Users\\other\\repo\\.claude\\worktrees\\a\\x.py"), None),
            (bash("git -C C:/Users/other/repo reset --hard", cwd), "deny"),
            (bash("git -C /c/Users/other/wt-b commit -m x", cwd), "deny"),
            (bash("cd ../../.. && git reset --hard", cwd), "deny"),
            (bash("git -C ../../.. stash", cwd), "deny"),
            (bash("git reset --hard", cwd), None),
            (bash("cd ../../.. && git log", cwd), None),
        ]

    def test_git_bash_spelling_of_cwd_is_a_linked_worktree(self):
        # hook の入力の cwd が `/c/Users/...` で届いても、Git Bash の読み (`C:\Users\...`) で linked worktree と分かる。
        # Windows の読み (今のドライブの `\c\Users\...`) だけで読むと None になり、guard 全体が素通しになっていた
        with self._linked(self.fam):
            for cwd in (self.GIT_BASH_A, "/C/Users/other/repo/.claude/worktrees/a", self.GIT_BASH_A + "/src", self.A, "C:/Users/other/repo/.claude/worktrees/a"):
                with self.subTest(cwd=cwd):
                    self.assertEqual(family.detect(cwd), self.fam)
            for cwd in (
                "/cc/Users/other/repo/.claude/worktrees/a",  # ドライブの綴りではない
                "/d/Users/other/repo/.claude/worktrees/a",  # 別のドライブ
                "/c/Users/other/repo",  # main checkout
                "\\c\\Users\\other\\repo\\.claude\\worktrees\\a",  # Windows の読みでは今のドライブの `\c\...`
            ):
                with self.subTest(cwd=cwd):
                    self.assertIsNone(family.detect(cwd))
                    self.assertEqual(family.detect_readings(cwd), ())

    def test_git_bash_spelling_of_cwd_guards_like_the_windows_spelling(self):
        # `/c/...` の cwd でも別の checkout への書き込みを止め、相対パスはその読みの cwd (`C:\Users\...`) から解決する。
        # 判定と文面は `C:\...` の cwd と同じ (docs の形の cwd は 0.10.2 と同じ)
        payloads, windows = self._payloads(self.GIT_BASH_A), self._payloads(self.A)
        self.assertEqual(len(payloads), len(windows))
        for (data, expected), (same, _) in zip(payloads, windows):
            with self.subTest(data=data["tool_input"]):
                out = self._evaluate_at(data, (self.fam,))
                self.assertEqual(decision(out), expected)
                self.assertEqual(out, self._evaluate_at(same, (self.fam,)))
        # 自分の worktree の下の subdirectory を `/c/...` で書いた cwd も同じ
        sub = self.GIT_BASH_A + "/src"
        self.assertEqual(decision(self._evaluate_at(self._file("Write", "../../../../x.py") | {"cwd": sub}, (self.fam,))), "deny")
        self.assertIsNone(self._evaluate_at(self._file("Write", "../x.py") | {"cwd": sub}, (self.fam,)))
        self.assertEqual(decision(self._evaluate_at(bash("cd ../../../.. && git reset --hard", sub), (self.fam,))), "deny")
        self.assertIsNone(self._evaluate_at(bash("cd .. && git reset --hard", sub), (self.fam,)))

    def test_both_readings_of_cwd_in_different_worktrees(self):
        # cwd の 2 つの読みが別々の linked worktree に当たるときは、両方の読みで判定し、どちらかで別の checkout なら止める
        shadow = self._shadow_family()
        both = (shadow, self.fam)
        cwd = self.GIT_BASH_A
        with self._linked(*both):
            self.assertEqual(family.detect_readings(cwd), ((cwd, shadow), ("c:/Users/other/repo/.claude/worktrees/a", self.fam)))
            self.assertEqual(family.detect(cwd), shadow)  # 読みの順 (Windows の読みが先) の最初
        for path in ("C:\\Users\\other\\repo\\x.py", "C:\\c\\Users\\other\\repo\\x.py"):
            with self.subTest(path=path):
                self.assertEqual(decision(self._evaluate_at({**self._file("Write", path), "cwd": cwd}, both)), "deny")
        self.assertIsNone(self._evaluate_at({**self._file("Write", "x.py"), "cwd": cwd}, both))
        self.assertIsNone(self._evaluate_at(bash("git reset --hard", cwd), both))
        for data, targets in (
            ({**self._file("Write", "../../../x.py"), "cwd": cwd}, ("c:\\c\\users\\other\\repo\\x.py", "c:\\users\\other\\repo\\x.py")),
            (bash("cd ../../.. && git reset --hard", cwd), ("c:\\c\\users\\other\\repo", "c:\\users\\other\\repo")),
        ):
            with self.subTest(data=data["tool_input"]):
                out = self._evaluate_at(data, both)
                self.assertEqual(decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn(f"この session の worktree: {shadow.home} / {self.fam.home}\n", reason)
                self.assertEqual(reason.count("\n- "), 2, reason)
                for target in targets:
                    self.assertIn(f"({target})", reason)
                # 片方の読みの行き先を許しても、もう片方の読みで止める
                out = self._evaluate_at(data, both, allow="C:\\Users\\other\\repo")
                self.assertEqual(decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(reason.count("\n- "), 1, reason)
                self.assertIn(f"({targets[0]})", reason)
                self.assertIsNone(self._evaluate_at(data, both, allow="C:\\Users\\other\\repo;C:\\c\\Users\\other\\repo"))

    def test_readings_of_cwd_in_the_same_worktree_name_each_target_once(self):
        # cwd の 2 つの読みが同じ worktree に当たる形 (ドライブの root より上に出る `..`) で、同じ理由を 2 回並べない
        cwd = "/c/../Users/other/repo/.claude/worktrees/a"
        with self._linked(self.fam):
            self.assertEqual([f for _, f in family.detect_readings(cwd)], [self.fam, self.fam])
        for data in ({**self._file("Write", "C:\\Users\\other\\repo\\x.py"), "cwd": cwd}, bash("git -C C:/Users/other/repo reset --hard", cwd)):
            with self.subTest(data=data["tool_input"]):
                out = self._evaluate_at(data, (self.fam,))
                self.assertEqual(decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(reason.count("\n- "), 1, reason)
                self.assertIn(f"この session の worktree: {self.fam.home}\n", reason)

    def test_one_read_deadline_for_all_readings_of_cwd(self):
        # 読み取りの期限は、cwd の読みが 2 つになっても 1 つ (hook の時間の上限までの余裕を、読みの数で割らない)
        made = []

        class CountingDeadline(command._Deadline):
            def __init__(self, seconds):
                made.append(seconds)
                super().__init__(seconds)

        with mock.patch.object(command, "_Deadline", CountingDeadline):
            out = self._evaluate_at(bash("cd ../../.. && git reset --hard", self.GIT_BASH_A), (self._shadow_family(), self.fam))
        self.assertEqual(decision(out), "deny")
        self.assertEqual(made, [command._READ_SECONDS])

    # --- cwd の 1 つの読みの detect が例外を投げる -----------------------------

    GIT_BASH_READING_A = "c:/Users/other/repo/.claude/worktrees/a"  # GIT_BASH_A の Git Bash の読み

    def _no_raise(self, fn, *args, **kwargs):
        # main() は evaluate の例外を「内部エラー」の注意 (止めない) にする。ERROR ではなく FAIL で数える
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.fail(f"1 つの読みの例外で、ほかの読みの判定が消えた: {e!r}")

    @staticmethod
    def _skipped(here: str, error: Exception) -> str:
        return f"- 作業ディレクトリの読み ({here}) は内部エラーのため判定をスキップした: {type(error).__name__}: {error}"

    @staticmethod
    def _text(out: dict | None) -> list[str]:
        body = (out or {}).get("hookSpecificOutput", {})
        return (body.get("permissionDecisionReason") or body.get("additionalContext") or "").split("\n")

    def test_exception_in_one_reading_keeps_the_other_readings_verdict(self):
        # `/c/...` の Windows の読み (`C:\c\Users\...`) が linked worktree で、Git Bash の読み (`C:\Users\...`) の detect が例外を投げる。
        # 例外の読みは注意 1 行にし、ほかの読みで判定する (0.10.2 は Windows の読みだけで読んで止めていた。例外で evaluate ごと
        # 落とすと、main() の「内部エラー」の注意になって止めない)
        shadow = self._shadow_family()
        cwd, here = self.GIT_BASH_A, self.GIT_BASH_READING_A
        tag = "[worktree-cwd-guard] 対象を確認できなかった操作がある (止めていない)"
        for error in (PermissionError(13, "Access is denied", here), ValueError("embedded null byte")):
            failing = ((self.fam.home, error),)
            line = self._skipped(here, error)
            with self.subTest(error=error):
                with self._linked(shadow, failing=failing):
                    self.assertEqual(self._no_raise(family.detect_readings, cwd), ((cwd, shadow), (here, error)))
                    self.assertEqual(self._no_raise(family.detect, cwd), shadow)
            # ほかの読みで別の checkout: 判定は deny のまま、例外の読みの行が 1 行増える (例外を投げない世界の出力と比べる)
            for data in (bash("cd ../../.. && git reset --hard", cwd), {**self._file("Write", "../../../x.py"), "cwd": cwd}):
                with self.subTest(error=error, case="別の checkout", data=data["tool_input"]):
                    out = self._no_raise(self._evaluate_at, data, (shadow,), failing=failing)
                    self.assertEqual(decision(out), "deny")
                    base = self._text(self._evaluate_at(data, (shadow,)))
                    self.assertEqual(len(base), 4, base)  # 見出し・worktree・理由 1 行・案内
                    self.assertEqual(self._text(out), [*base[:3], line, base[3]])
            # ほかの読みで linked worktree の外: 何も出さないのではなく、例外の読みの注意を出す (見出しの worktree は無い)
            for data in (bash("cd ../../.. && git reset --hard", cwd), {**self._file("Write", "../../../x.py"), "cwd": cwd}):
                with self.subTest(error=error, case="linked worktree の外", data=data["tool_input"]):
                    self.assertIsNone(self._evaluate_at(data, ()))
                    out = self._no_raise(self._evaluate_at, data, (), failing=failing)
                    self.assertEqual(decision(out), "context")
                    self.assertEqual(self._text(out), [tag, line])
            # ほかの読みで自分の worktree: 止めずに、例外の読みの注意を出す
            for data in (bash("git reset --hard", cwd), {**self._file("Write", "x.py"), "cwd": cwd}):
                with self.subTest(error=error, case="自分の worktree", data=data["tool_input"]):
                    self.assertIsNone(self._evaluate_at(data, (shadow,)))
                    out = self._no_raise(self._evaluate_at, data, (shadow,), failing=failing)
                    self.assertEqual(decision(out), "context")
                    self.assertEqual(self._text(out), [tag, f"この session の worktree: {shadow.home}", line])

    def test_exception_in_the_windows_reading_keeps_the_git_bash_reading(self):
        # 読みの順が逆 (先の Windows の読みが例外、後の Git Bash の読みが linked worktree) でも、Git Bash の読みで判定する
        shadow = self._shadow_family()
        cwd = self.GIT_BASH_A
        error = PermissionError(13, "Access is denied", cwd)
        failing = ((shadow.home, error),)
        with self._linked(self.fam, failing=failing):
            self.assertEqual(self._no_raise(family.detect_readings, cwd), ((cwd, error), (self.GIT_BASH_READING_A, self.fam)))
            self.assertEqual(self._no_raise(family.detect, cwd), self.fam)
        out = self._no_raise(self._evaluate_at, bash("cd ../../.. && git reset --hard", cwd), (self.fam,), failing=failing)
        self.assertEqual(decision(out), "deny")
        text = self._text(out)
        self.assertEqual(len(text), 5, text)
        self.assertEqual(text[1], f"この session の worktree: {self.fam.home}")
        self.assertIn("(c:\\users\\other\\repo)", text[2])
        self.assertEqual(text[3], self._skipped(cwd, error))

    def test_exception_in_every_reading_is_raised(self):
        # どの読みでも detect を確かめられなければ、最初の読みの例外を投げ直す (main() が「内部エラー」の注意にする。0.10.2 と同じ)
        shadow = self._shadow_family()
        first, second = PermissionError(13, "first"), PermissionError(13, "second")
        data = bash("cd ../../.. && git reset --hard", self.GIT_BASH_A)
        with self.assertRaises(PermissionError) as raised:
            self._evaluate_at(data, (), failing=((shadow.home, first), (self.fam.home, second)))
        self.assertIs(raised.exception, first)
        # 読みが 1 つの cwd (`C:\...`) も同じ
        with self.assertRaises(PermissionError) as raised:
            self._evaluate_at(bash("cd ../../.. && git reset --hard", self.A), (), failing=((self.fam.home, second),))
        self.assertIs(raised.exception, second)


@unittest.skipIf(os.name == "nt", "POSIX のパスの読み")
class PosixDrivePathTest(unittest.TestCase):
    """POSIX の `/c/...` は、そこにある checkout をそのまま指す (git は起動しない)。"""

    def test_drive_like_root_is_read_as_is(self):
        n = family.norm
        main = "/c/Users/other/repo"
        a = main + "/.claude/worktrees/a"
        fam = family.Family(home=n(a), roots=(n(main), n(a)), common_dir=n(main + "/.git"))
        for cmd in (f"git -C {main} reset --hard", f"cd {main}; git reset --hard", f"git worktree remove --force {main}"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(f.blocked for f in command.analyze(cmd, a, fam)))

    def test_file_tools_and_allow_list_read_drive_like_path_as_is(self):
        # Write / Edit のパスと許可リストも、POSIX では `/c/x` をそのまま読む。Git Bash の読み (`c:/x`) を足すと
        # 今の場所の下の相対パスになり、そこを指してしまう
        n = family.norm
        main = "/c/Users/other/repo"
        a = main + "/.claude/worktrees/a"
        fam = family.Family(home=n(a), roots=(n(main), n(a)), common_dir=n(main + "/.git"))
        self.assertEqual(family.drive_spellings(main), (main,))
        self.assertEqual(_ENTRY._file_findings("/c/x.py", main, fam), [])
        with mock.patch.dict(os.environ), mock.patch.object(_ENTRY, "detect_readings", lambda cwd: ((cwd, fam),)):
            os.environ.pop("WORKTREE_CWD_GUARD_MODE", None)
            os.environ.pop("WORKTREE_CWD_GUARD_ALLOW", None)
            self.assertEqual(decision(evaluate(write(main + "/x.py", a))), "deny")
            os.environ["WORKTREE_CWD_GUARD_ALLOW"] = main
            self.assertEqual(_ENTRY._allowed_roots(), {n(main)})
            self.assertIsNone(evaluate(write(main + "/x.py", a)))


class EntryTest(HermeticGitTestCase):
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
