"""状態の集合の上限 (`command._STATE_LIMIT`)。

`&&` / `||` の分岐ごとに行き先かスタックが割れる形は、状態 (作業ディレクトリ・pushd のスタック・成否) が倍々に増える。
hook が時間切れになると Claude Code はコマンドをそのまま走らせるので、上限を超えたら行き先の決まらない状態にまとめ、
後ろの git は注意だけにする (止めない)。上限に届かない形の読みは変えない。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
import command
import family
import test_guard

RESET = "reset --hard"
GIT = f"git {RESET}"
_BASE = os.path.join(family.norm(tempfile.gettempdir()), "wcg-state-limit")
MAIN = os.path.join(_BASE, "repo")
A = os.path.join(MAIN, ".claude", "worktrees", "a")
B = os.path.join(_BASE, "wt-b")
_HOOKS_JSON = Path(__file__).resolve().parents[2] / "hooks.json"


# 分岐ごとに状態が倍々に増える形。どれも後ろにコマンドを続けられる区切りで終わる
def or_cd(n: int) -> str:
    """`x || cd d<i>;` (相対パス)。行き先が `x` の成否で割れる。"""
    return "".join(f"x || cd d{i}; " for i in range(n))


def pushd_and_bg(n: int) -> str:
    """`pushd a<i> && pushd b<i> && true &`。zsh は `&` の手前の pushd を今の shell に残す。"""
    return "".join(f"pushd a{i} && pushd b{i} && true & " for i in range(n))


def pushd_or_and_bg(n: int) -> str:
    """`pushd /d<i> || pushd /e<i> && true &` (絶対パス)。スタックが割れる。"""
    return "".join(f"pushd /d{i} || pushd /e{i} && true & " for i in range(n))


def or_pushd_abs(n: int) -> str:
    """`x || pushd /t/d<i>;` (絶対パス)。行き先は同じでもスタックが割れる。"""
    return "".join(f"x || pushd /t/d{i}; " for i in range(n))


def or_pushd_n(n: int) -> str:
    """`x || pushd -n /t/d<i>;`。bash の `pushd -n` は移動せずにスタックへ積むので、作業ディレクトリは同じままスタックが割れる。"""
    return "".join(f"x || pushd -n /t/d{i}; " for i in range(n))


def pipe_pushd(n: int) -> str:
    """`x | pushd /t/d0 | ... ;` (pipeline の要素の pushd)。0.9.2 の時点で既に増えない形 (増えないことを固定する)。"""
    return "x | " + " | ".join(f"pushd /t/d{i}" for i in range(n)) + "; "


def _limit_exponent() -> int:
    """状態がちょうど上限になる `or_cd` の回数 (`or_cd(k)` の状態は 2 ** k)。"""
    k = command._STATE_LIMIT.bit_length() - 1
    assert 2**k == command._STATE_LIMIT, command._STATE_LIMIT
    return k


class BoundedBase(unittest.TestCase):
    """コマンドを読むたびに、状態の集合が上限に収まっていることを確かめる。

    `_flush` に入るたびに数え、上限を超えたらその場で落とす (上限が効かない版では倍々に増えて固まる前に止める)。
    git を起動しない組み立ての checkout の一覧で読む。
    """

    def setUp(self):
        super().setUp()
        n = family.norm
        self.fam = family.Family(home=n(A), roots=(n(MAIN), n(A), n(B)), common_dir=n(os.path.join(MAIN, ".git")))
        self.peak = 0
        self.collapses = 0
        test = self
        flush = command._Analyzer._flush
        cap = command._cap

        def bounded_flush(analyzer, seg, next_is_pipe):
            sizes = (len(analyzer.alts), len(analyzer.failed_moves), len(analyzer.list_start[0]))
            test.peak = max(test.peak, *sizes)
            if max(sizes) > command._STATE_LIMIT:
                raise test.failureException(f"状態の集合が上限 ({command._STATE_LIMIT}) を超えた: {sizes}")
            return flush(analyzer, seg, next_is_pipe)

        def counted_cap(alts, stacks):
            out = cap(alts, stacks)
            if out is not alts:
                test.collapses += 1
            return out

        for target, name, repl in ((command._Analyzer, "_flush", bounded_flush), (command, "_cap", counted_cap)):
            patcher = mock.patch.object(target, name, repl)
            patcher.start()
            self.addCleanup(patcher.stop)

    def findings(self, cmd: str) -> list[command.Finding]:
        self.peak = 0
        self.collapses = 0
        try:
            return command.analyze(cmd, family.norm(A), self.fam)
        except Exception as e:  # noqa: BLE001 - 読みの例外も読みの誤りとして数える (ERROR ではなく FAIL)
            self.fail(f"読みが例外を投げた: {e!r}")

    def verdict(self, cmd: str) -> str | None:
        out = self.findings(cmd)
        if any(f.blocked for f in out):
            return "deny"
        return "note" if out else None


class StateLimitTest(BoundedBase):
    def test_branching_shapes_end_as_a_note(self):
        # 上限を超えた後の git は、行き先の決まらない移動の後ろと同じく注意だけにする (止めない)
        for name, head in (
            ("or_cd 18", or_cd(18)),
            ("or_cd 40", or_cd(40)),
            ("pushd_and_bg 12", pushd_and_bg(12)),
            ("pushd_and_bg 24", pushd_and_bg(24)),
            ("pushd_or_and_bg 18", pushd_or_and_bg(18)),
            ("pushd_or_and_bg 36", pushd_or_and_bg(36)),
            ("or_pushd_abs 18", or_pushd_abs(18)),
        ):
            with self.subTest(name=name):
                out = self.findings(head + GIT)
                self.assertGreater(self.collapses, 0)
                self.assertEqual([f.blocked for f in out], [False])
                self.assertIn("cd の移動先", out[0].message)

    def test_pipeline_pushd_does_not_grow(self):
        # pipeline の途中の要素はサブシェルで動くので、最後の要素の pushd だけが残る。行き先は repo の外
        for n in (18, 40):
            with self.subTest(n=n):
                self.assertIsNone(self.verdict(pipe_pushd(n) + GIT))
                self.assertEqual(self.collapses, 0)

    def test_over_the_limit_is_a_note_even_if_a_reading_reaches_main(self):
        # 受け入れている見逃し: `x` がずっと成功すれば main で git が走るが、上限を超えた後は止めない (時間切れの素通しを避ける)
        for name, head in (("or_cd", or_cd(18)), ("or_pushd_abs", or_pushd_abs(18))):
            with self.subTest(name=name):
                self.assertEqual(self.verdict(f'cd "{MAIN}"; {head}{GIT}'), "note")
        # bash の `&` は list の開始時点 (main) に戻すので、状態は増えずに main で読む (止める)
        self.assertEqual(self.verdict(f'cd "{MAIN}"; {pushd_or_and_bg(18)}{GIT}'), "deny")

    def test_boundary_of_the_limit(self):
        # 状態がちょうど上限になる形までは今までどおり読み、上限を 1 回の分岐で超えた形から注意にする
        k = _limit_exponent()
        for n, want in ((k - 1, "deny"), (k, "deny"), (k + 1, "note")):
            with self.subTest(n=n):
                self.assertEqual(self.verdict(f'cd "{MAIN}"; {or_cd(n)}{GIT}'), want)
        self.verdict(f'cd "{MAIN}"; {or_cd(k)}{GIT}')
        self.assertEqual((self.peak, self.collapses), (command._STATE_LIMIT, 0))

    def test_zsh_and_chain_before_background(self):
        # zsh の `&` は手前の `cd <dir> &&` を今の shell に残すので、`&` で戻す候補は移動ごとに 1 つ増える。移動が 64 個
        # (候補は最初の場所を含めて 65) で上限を超える (README の「目的と限界」)
        def chain(n: int) -> str:
            return "".join(f"cd /t/d{i} && " for i in range(n))

        self.assertEqual(self.verdict(f'cd "{MAIN}" && {chain(command._STATE_LIMIT - 2)}true & {GIT}'), "deny")
        self.assertEqual(self.collapses, 0)
        self.assertEqual(self.verdict(f'cd "{MAIN}" && {chain(command._STATE_LIMIT - 1)}true & {GIT}'), "note")
        self.assertGreater(self.collapses, 0)

    def test_absolute_targets_are_read_after_the_limit(self):
        head = or_cd(18)
        self.assertEqual(self.verdict(f'{head}cd "{MAIN}" && {GIT}'), "deny")
        self.assertEqual(self.verdict(f'{head}git -C "{MAIN}" {RESET}'), "deny")
        self.assertEqual(self.verdict(f'{head}pushd "{B}" && {GIT}'), "deny")
        # 自分の worktree に戻る形と読み取りは止めない
        self.assertIsNone(self.verdict(f'{head}cd "{A}" && {GIT}'))
        self.assertIsNone(self.verdict(f"{head}git status"))

    def test_collapse_keeps_both_outcomes(self):
        # 上限ちょうどの状態で `x` を読むと成否で 2 倍になり、`||` の手前でまとめる。失敗の側も残すので `||` の右を読む
        k = _limit_exponent()
        self.assertEqual(self.verdict(f'{or_cd(k)}x || git -C "{MAIN}" {RESET}'), "deny")
        self.assertGreater(self.collapses, 0)

    def test_failed_moves_are_bounded(self):
        # 移動の失敗の経路 (`cd n ||` の右) も上限でまとめる。右辺の `x` で成否が割れて 2 倍になる
        k = _limit_exponent()
        self.assertEqual(self.verdict(f'cd "{MAIN}"; {or_cd(k)}cd n || x || {GIT}'), "note")
        self.assertGreater(self.collapses, 0)

    def test_failed_moves_with_split_stacks_are_bounded(self):
        # 移動の失敗の経路 (`cd <dir> ||` の右) を上限でまとめるときも、スタックの割れた状態をまとめる。`cd /t/h<i>; x || pushd /t/same;`
        # は、作業ディレクトリを揃えたままスタックだけを割る (積むのは手前の場所)。全状態で同じ作業ディレクトリ (main) は残すので、
        # `|| popd` が失敗した経路の git は main で動く
        k = _limit_exponent()
        split = "pushd /t/x; " + "".join(f"cd /t/h{i}; x || pushd /t/same; " for i in range(k)) + f'cd "{MAIN}"; '
        self.assertEqual(self.verdict(f"{split}cd /t/n || x || popd || {GIT}"), "deny")
        self.assertGreater(self.collapses, 0)
        # 対照: まとめる前 (状態が上限ちょうど) も同じ
        self.assertEqual(self.verdict(f"{split}cd /t/n || popd || {GIT}"), "deny")

    def test_collapse_keeps_shared_place_and_stack(self):
        k = _limit_exponent()
        for n in (k + 1, 18):
            with self.subTest(n=n):
                # 全状態で同じスタック (`pushd /t/x` で積んだ自分の worktree) は残す。popd はそこへ戻り、bash / zsh -f とも
                # git は自分の worktree で動く
                self.assertIsNone(self.verdict(f'pushd /t/x; {or_cd(n)}cd "{MAIN}"; popd; {GIT}'))
                self.assertGreater(self.collapses, 0)
                # 割れたスタックも、どの状態でも積まれている深さは残す (popd は失敗せず、行き先が決まらないので注意)
                self.assertEqual(self.verdict(f'pushd /t/x; {or_pushd_abs(n)}cd "{MAIN}"; popd; {GIT}'), "note")
                self.assertGreater(self.collapses, 0)
                # どの状態でも積まれているとは言えない深さは残さない (`x` がずっと成功すればスタックは空で、popd は失敗して main に残る)
                self.assertEqual(self.verdict(f'{or_pushd_abs(n)}cd "{MAIN}"; popd; {GIT}'), "deny")
                # 全状態で同じ作業ディレクトリは残す (bash の `pushd -n` は移動せずにスタックだけを割る)
                self.assertIsNone(self.verdict(f"{or_pushd_n(n)}{GIT}"))
                self.assertGreater(self.collapses, 0)
                self.assertEqual(self.verdict(f'cd "{MAIN}"; {or_pushd_n(n)}{GIT}'), "deny")
        # スタックが main を指していれば、まとめた後も popd の行き先で止める
        self.assertEqual(self.verdict(f'cd "{MAIN}"; pushd /t/x; {or_cd(18)}popd; {GIT}'), "deny")
        self.assertGreater(self.collapses, 0)

    def test_collapse_keeps_the_depth_per_outcome(self):
        # 成功の側だけが pushd で積んだ時点でまとめても、成功の側には失敗の側の浅いスタックを持たせない
        # (持たせると popd が「失敗して main に残る」読みになり、止めすぎる)。bash / zsh -f とも git は main で動かない
        k = _limit_exponent()
        for head in (or_cd(k - 1), or_pushd_abs(k - 1)):
            for tail in (f'cd "{MAIN}" && popd; {GIT}', f'cd "{MAIN}"; popd; {GIT}'):
                cmd = f"{head}x && pushd /t/z && x && {tail}"
                with self.subTest(cmd=cmd[-60:]):
                    self.assertNotEqual(self.verdict(cmd), "deny")
                    self.assertGreater(self.collapses, 0)
        # 本当にスタックが空の側があれば、まとめた後も止める (`x && x && cd <main> && popd` の popd は失敗して main に残る)
        self.assertEqual(self.verdict(f'{or_cd(k - 1)}x && true && x && cd "{MAIN}" && popd; {GIT}'), "deny")
        # 作業ディレクトリも成否ごとに決める。成功の側はどれも main にいるので、`&&` の後ろの git は main で動く (失敗の側の
        # 割れた作業ディレクトリに引かれて決まらないものにしない)
        self.assertEqual(self.verdict(f'{or_cd(k - 1)}x && pushd "{MAIN}" && x && {GIT}'), "deny")
        self.assertGreater(self.collapses, 0)

    def test_collapse_keeps_every_shared_depth(self):
        # どの状態でも 2 段積まれていれば、まとめた後も 2 段残す (2 回目の popd も失敗しない。行き先は分からないので注意)。
        # 1 段しか残さないと、2 回目の popd が「失敗して main に残る」読みになり止めすぎる
        k = _limit_exponent()
        two = f"pushd /t/x; pushd /t/y; {or_pushd_abs(k + 1)}"
        self.assertEqual(self.verdict(f'{two}popd; cd "{MAIN}"; popd; {GIT}'), "note")
        self.assertGreater(self.collapses, 0)
        # 3 回目の popd は、どの状態でも積まれているとは言えない段を外す (`x` がずっと成功すればスタックは 2 段で、空の popd は main に残る)
        self.assertEqual(self.verdict(f'{two}popd; popd; cd "{MAIN}"; popd; {GIT}'), "deny")
        self.assertGreater(self.collapses, 0)

    def test_collapse_keeps_exported_git_dir(self):
        # `&` で戻す時点をまとめても、export 済みの GIT_DIR と、export していない代入は残す (上限を超えた後も git dir の行き先は読む)
        k = _limit_exponent()
        self.assertEqual(self.verdict(f'export GIT_DIR="{MAIN}/.git"; {or_cd(k)}x; true & git add .'), "deny")
        self.assertGreater(self.collapses, 0)
        self.assertEqual(self.verdict(f'GIT_DIR="{MAIN}/.git"; {or_cd(k)}x; true & export GIT_DIR; git add .'), "deny")
        self.assertGreater(self.collapses, 0)

    def test_worktree_operand_after_the_limit_is_a_note(self):
        # 上限を超えた後は git が読む repo と今の場所が決まらない。名前が別の worktree に一致するか、`..` を含む相対パスなら
        # 注意を出す (止めない。0.9.2 は行き先ごとに読んで止めていた)
        k = _limit_exponent()
        rel = Path(os.path.relpath(B, A)).as_posix()
        for rm in ("git worktree remove --force wt-b", "git worktree move wt-b /tmp/zz", f"git worktree remove --force {rel}"):
            with self.subTest(rm=rm):
                self.assertEqual(self.verdict(f"{or_cd(k)}{rm}"), "deny")
                self.assertEqual(self.collapses, 0)
                self.assertEqual(self.verdict(f"{or_cd(k + 1)}{rm}"), "note")
                self.assertGreater(self.collapses, 0)
                self.assertEqual(self.verdict(f'cd "{MAIN}"; {or_cd(18)}{rm}'), "note")
        # git dir が決まって repo が決まっても、相対パスの基点の 1 つ (今の場所) が決まらなければ注意
        gd = f'export GIT_DIR="{MAIN}/.git"; '
        self.assertEqual(self.verdict(f"{gd}{or_cd(k)}git worktree remove --force {rel}"), "deny")
        self.assertEqual(self.verdict(f"{gd}{or_cd(18)}git worktree remove --force {rel}"), "note")
        self.assertGreater(self.collapses, 0)
        # どの worktree の末尾にも一致しない素の相対パスも、今の場所が決まらなければ注意 (symlink を通れば別の worktree に届く。
        # test_worktree_operand_through_a_symlink_after_the_limit)
        self.assertEqual(self.verdict(f"{or_cd(18)}git worktree remove --force zz"), "note")
        self.assertGreater(self.collapses, 0)
        # 変数の語は、上限に関係なく注意 (どの worktree の名前にもパスにもなりうる)
        for n in (k, 18):
            with self.subTest(n=n):
                self.assertEqual(self.verdict(f'{or_cd(n)}git worktree remove --force "$Y"'), "note")
        # 自分の worktree の名前と repo の外の絶対パスは、上限を超えても何も出さない
        outside = os.path.join(_BASE, "zz")  # ドライブ付きの絶対パス (Windows の `/t/zz` はドライブの無い相対の扱いになる版がある)
        for rm in ("git worktree remove --force a", f'git worktree remove --force "{outside}"'):
            with self.subTest(rm=rm):
                self.assertIsNone(self.verdict(f"{or_cd(18)}{rm}"))
                self.assertGreater(self.collapses, 0)

    def test_worktree_operand_through_a_symlink_after_the_limit(self):
        # symlink を通る相対パス (`wt/feature` の `wt` が別の worktree の親への symlink、最後が別の worktree への symlink の `lnk`) は、
        # 綴りが worktree の root の末尾に一致しない。上限までは今の場所から realpath で別の worktree に届くことを読んで止め
        # (0.9.2 と同じ読み)、上限を超えて今の場所が決まらなくなっても、通さずに注意を出す。合成の checkout の一覧では symlink を
        # 辿れないので、実在の dir で組む
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = family.norm(tmp.name)
        main = os.path.join(base, "repo")
        own = os.path.join(main, ".claude", "worktrees", "a")
        other = os.path.join(base, "elsewhere", "feature")
        for d in (own, other, os.path.join(main, ".git")):
            os.makedirs(d)
        try:
            os.symlink(os.path.dirname(other), os.path.join(main, "wt"))
            os.symlink(other, os.path.join(main, "lnk"))
        except OSError as e:  # symlink を作れない環境 (権限の無い Windows など)
            self.skipTest(f"symlink を作れない: {e!r}")
        self.fam = family.Family(home=own, roots=(main, own, other), common_dir=os.path.join(main, ".git"))
        k = _limit_exponent()

        def verdict(cmd: str) -> str | None:
            self.collapses = 0
            out = command.analyze(cmd, own, self.fam)
            if any(f.blocked for f in out):
                return "deny"
            return "note" if out else None

        for rel in ("wt/feature", "lnk"):
            rm = f"git worktree remove --force {rel}"
            with self.subTest(rel=rel):
                self.assertEqual(verdict(f'cd "{main}"; {or_cd(k)}{rm}'), "deny")
                self.assertEqual(self.collapses, 0)
                self.assertEqual(verdict(f'cd "{main}"; {or_cd(k + 1)}{rm}'), "note")
                self.assertGreater(self.collapses, 0)
                self.assertEqual(verdict(f'cd "{main}"; {or_cd(18)}{rm}'), "note")
        # 対照: 名前 (パスの末尾) が別の worktree に一致する形は、上限を超えても注意
        self.assertEqual(verdict(f'cd "{main}"; {or_cd(k + 1)}git worktree remove --force feature'), "note")

    def test_documented_limit_matches(self):
        root = Path(__file__).resolve().parents[3]
        readme = (root / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"が {command._STATE_LIMIT} を超える", readme)
        self.assertIn(f"`&&` で移動を {command._STATE_LIMIT} 個以上", readme)
        self.assertIn(f"上限 ({command._STATE_LIMIT})", (root / "AGENTS.md").read_text(encoding="utf-8"))

    @mock.patch.object(command, "_READ_SECONDS", None)  # 読み切る時間を測る (期限で打ち切ると伸び方を測れない)
    def test_time_grows_linearly(self):
        # 上限に張り付いた形は、繰り返しの数に比例して時間が伸びる (倍々にならない)。小さい側も上限に届いていることを確かめる
        small, large = 300, 1200
        for name, shape in (("or_cd", or_cd), ("pushd_and_bg", pushd_and_bg), ("pushd_or_and_bg", pushd_or_and_bg),
                            ("or_pushd_abs", or_pushd_abs)):
            with self.subTest(name=name):
                for n in (small, large):
                    self.assertEqual(self.verdict(shape(n) + GIT), "note")
                    self.assertGreater(self.collapses, 0)
                best = {small: float("inf"), large: float("inf")}
                for _ in range(3):
                    for n in (small, large):
                        cmd = shape(n) + GIT
                        t = time.perf_counter()
                        self.findings(cmd)
                        best[n] = min(best[n], time.perf_counter() - t)
                times = (best[small], best[large])
                self.assertLess(times[1], 5.0, times)
                # 4 倍の入力で、線形なら約 4 倍、2 乗なら約 16 倍
                self.assertLess(times[1] / times[0], 8.0, times)
                self.assertGreater(times[1] / times[0], 1.5, times)


class StackIdentityTest(BoundedBase):
    """スタックは中身で比べる (同じ中身のスタックは同じ状態、違う中身は違う状態)。"""

    def test_same_stack_from_both_outcomes_is_one_state(self):
        # `x;` で成否の割れた 2 つの状態が同じ pushd を読むと、同じ中身のスタックになって 1 つの状態に戻る (倍々に増えない)
        n = 2 * _limit_exponent()
        cmd = f'cd "{MAIN}"; ' + "".join(f"x; pushd /t/a{i}; " for i in range(n)) + "popd; " * n + GIT
        self.assertEqual(self.verdict(cmd), "deny")
        self.assertEqual((self.peak, self.collapses), (2, 0))

    def test_same_second_item_from_both_outcomes_is_one_state(self):
        # 同じスタックに、前に積んだのとは違う場所を積む形 (`pushd /t/a; popd` で 1 つ目を積んでから、別の場所から積み直す) でも、
        # 成否の割れた 2 つの状態は同じ中身のスタックになって 1 つの状態に戻る
        n = 2 * _limit_exponent()
        unit = "pushd /t/a; popd; cd /t/c{i}; x; pushd /t/b{i}; "
        cmd = f'cd "{MAIN}"; pushd /t/start; ' + "".join(unit.format(i=i) for i in range(n)) + "popd; " * (n + 1) + GIT
        self.assertEqual(self.verdict(cmd), "deny")  # 積んだ順の逆に戻り、最後の popd で main に戻る
        self.assertEqual((self.peak, self.collapses), (2, 0))

    def test_same_item_on_different_stacks(self):
        # 同じ場所 (main) を、中身の違う 2 つのスタック (自分の worktree を積んだもの / 空のもの) に積む。2 つ目は空のスタックに
        # 積んだので、popd 2 回目は空のスタックの popd (失敗して main に残る) になる
        cmd = f'pushd "{MAIN}"; pushd /t/q; popd; popd; cd "{MAIN}"; pushd /t/r; popd; popd; {GIT}'
        self.assertEqual(self.verdict(cmd), "deny")
        # 対照: 同じ中身のスタックに積み直せば、同じスタックになる (popd 2 回目は自分の worktree に戻る)
        cmd = f'pushd "{MAIN}"; pushd /t/q; popd; pushd /t/r; popd; popd; {GIT}'
        self.assertIsNone(self.verdict(cmd))

    def test_popd_plus_n_on_an_empty_stack_keeps_it_empty(self):
        # 空に見えるスタックの `popd +N` の後も、スタックは空のまま。次の popd は失敗してその場 (main) に残る
        self.assertEqual(self.verdict(f'popd +1; cd "{MAIN}"; popd; {GIT}'), "deny")
        # 対照: 1 段積んだスタックの `popd +1` も外す (残りは空)
        self.assertEqual(self.verdict(f'pushd /t/x; popd +1; cd "{MAIN}"; popd; {GIT}'), "deny")


class HookTimeTest(test_guard.GuardBase):
    """hook 全体 (git の起動を含む) が、hooks.json の timeout より十分短い時間で注意を返す。"""

    def _run_hook(self, cmd: str) -> tuple[float, str | None]:
        payload = json.dumps(test_guard.bash(cmd, self.a), ensure_ascii=False).encode("utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WORKTREE_CWD_GUARD")}
        t = time.perf_counter()
        try:
            r = subprocess.run([sys.executable, str(test_guard._PKG)], input=payload, capture_output=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail(f"60 秒を超えた: {cmd[:80]!r}")
        elapsed = time.perf_counter() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode("utf-8")) if r.stdout.strip() else None
        return elapsed, test_guard.decision(out)

    def test_branching_shapes_finish_well_within_the_timeout(self):
        hooks = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
        entries = hooks["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["hooks"]), 1)
        timeout = entries[0]["hooks"][0]["timeout"]
        for name, cmd, want in (
            ("or_cd", or_cd(40) + GIT, "context"),
            ("pushd_and_bg", pushd_and_bg(24) + GIT, "context"),
            ("pushd_or_and_bg", pushd_or_and_bg(40) + GIT, "context"),
            # 閉じない here-document の安全網で、別の読みでも読み直す形
            ("pushd_and_bg + heredoc", pushd_and_bg(24) + GIT + "\ncat <<E", "context"),
            ("pipe_pushd", pipe_pushd(40) + GIT, None),
        ):
            with self.subTest(name=name):
                elapsed, got = self._run_hook(cmd)
                self.assertEqual(got, want)
                self.assertLess(elapsed, timeout / 2, elapsed)


if __name__ == "__main__":
    unittest.main()
