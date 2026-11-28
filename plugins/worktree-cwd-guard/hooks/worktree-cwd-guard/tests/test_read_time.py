"""読み取りの時間: 読み取りの期限 (`command._READ_SECONDS`)。

相対パスの `cd a;` を続けると、行き先のパスが深くなる。相対パスの解決 (`command._resolve`) は 0.9.3 と同じく移動のたびに
realpath でパスの全要素を辿り直すので、深さの 2 乗の時間になる (`cd a;` 2,000 回で約 4 秒、`x || cd d<i>;` 6 回の後に
`cd a;` 400 回で約 15 秒。macOS の Python 3.14)。hook が時間切れになるとコマンドはそのまま走るので、こうした遅い形は読み取りの期限で閉じる
(期限を過ぎたら、それまでの判定に注意を足して返す)。pushd のスタックを深く重ねる形は、0.10.4 からスタックの深さに比例しない
時間で 1 回の移動を読む (`DeepStackTest`)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

import _testutil  # noqa: F401
import command
import family
import test_guard
import test_state_limit
from test_state_limit import GIT, MAIN, RESET, B, or_cd


def cd_a(n: int) -> str:
    """`cd a;` (相対パス)。移動のたびに行き先が 1 段深くなる。"""
    return "cd a; " * n


def pushd_abs(n: int) -> str:
    """`pushd /t/p<i>;` (絶対パス)。移動のたびにスタックが 1 段深くなる。"""
    return "".join(f"pushd /t/p{i}; " for i in range(n))


def heredocs(n: int) -> str:
    """終わらない here-document を n 個。安全網が、旧来の読みと、読み直し (n - 1 回、上限 `_REREAD_LIMIT`) で読み直す。"""
    return "\n" + "\n".join(f"cat <<E{i}" for i in range(n))


def no_deadline():
    return mock.patch.object(command, "_READ_SECONDS", None)


class SymlinkedPlaceDecisionTest(test_guard.GuardBase):
    """相対パスの途中の symlink は、どの段で現れても realpath で辿って持ち主を決める (本物の repo)。"""

    def test_symlink_inside_own_worktree_to_main(self):
        try:
            os.symlink(self.main, self.a / "to-main")
            os.symlink(self.a, self.main / "to-a")
        except OSError as e:
            self.skipTest(f"symlink を作れない: {e!r}")
        for cmd, want in (
            ("cd to-main && git reset --hard", "deny"),
            ("cd sub/../to-main; git reset --hard", "deny"),
            ("cd x; cd ..; cd to-main/.claude; git reset --hard", "deny"),
            ("git -C to-main/x reset --hard", "deny"),
            # 保護する側 (自分の worktree) への symlink は、main の中から辿っても自分の worktree
            (f'cd "{self.main}"; cd to-a; git reset --hard', None),
            ("cd to-main/to-a && git reset --hard", None),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(test_guard.decision(test_guard.evaluate(test_guard.bash(cmd, self.a))), want)

    @unittest.skipIf(os.name == "nt", "Windows のパスは `..` を字面で畳んでから辿る (ntpath.realpath も Win32 の API も同じ)")
    def test_dot_dot_after_a_symlink_climbs_from_its_target(self):
        # symlink の後ろの `..` は、辿った先から上がる (git -C の chdir は物理的に辿る)。`..` を字面で畳んでから辿ると、
        # 自分の worktree のままになり、main への git を通す
        (self.main / "sub").mkdir(exist_ok=True)
        try:
            os.symlink(self.main / "sub", self.a / "to-sub")
        except OSError as e:
            self.skipTest(f"symlink を作れない: {e!r}")
        for cmd in (
            "git -C to-sub/.. reset --hard",
            "git -C to-sub/../sub/.. reset --hard",
            "cd to-sub && git -C .. reset --hard",
            f'git -C "{self.a}/to-sub/.." reset --hard',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(test_guard.decision(test_guard.evaluate(test_guard.bash(cmd, self.a))), "deny")

    def test_moves_after_a_symlink_loop(self):
        # Python 3.12 以前の realpath は symlink の環 (`m1` <-> `m2`) で辿るのをやめ、`<a>/m1/../to-main` を `<a>/to-main` (main への
        # symlink を含む) のまま返す。次の移動では全要素を辿り直して main に届く (どの版でも止める)。前の解決の値を基点に、移動した
        # 分の要素だけを辿ると、`to-main` を辿らずに通す
        try:
            os.symlink(self.main, self.a / "to-main")
            os.symlink("m2", self.a / "m1")
            os.symlink("m1", self.a / "m2")
        except OSError as e:
            self.skipTest(f"symlink を作れない: {e!r}")
        (self.main / "sub").mkdir(exist_ok=True)
        for cmd in (
            "cd m1/../to-main; cd .; git reset --hard",
            "cd m1/../to-main; cd sub; git reset --hard",
            "cd m1/../to-main && cd sub && git reset --hard",
            "cd m1/../to-main; git -C sub reset --hard",
            "cd m1/../to-main; git -C . reset --hard",
            f'cd "{self.a}/m1/../to-main"; cd .; git reset --hard',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(test_guard.decision(test_guard.evaluate(test_guard.bash(cmd, self.a))), "deny")


class CachedLoopDecisionTest(test_guard.GuardBase):
    """checkout をまたぐ symlink の環を `..` で外した後の移動は、0.9.3 と同じ持ち主に決める (本物の repo)。

    main の中の `E -> <a>/S` と、自分の worktree の中の `S -> <main>/E` が環になる。Python 3.13 以降の realpath は 1 回の呼び出しの
    中で symlink の解決を覚えるので、`<main>/E` を基点に全要素を辿り直すと、環を展開したときの値 (main の中) を後の `S` にも使い、
    main への git として止める。前の解決の値を基点に、移動した分の要素だけを辿ると、`S` を自分の worktree から新しく解決して通す。
    Python 3.12 以前の realpath は環で辿るのをやめ、`S` の後ろを字面のまま返すので、0.9.3 と同じく自分の worktree のまま (止めない)。
    """

    def test_loop_across_checkouts(self):
        try:
            os.symlink(self.a / "S", self.main / "E")
            os.symlink(self.main / "E", self.a / "S")
            os.symlink(self.main, self.a / "to-main")
        except OSError as e:
            self.skipTest(f"symlink を作れない: {e!r}")
        want = "deny" if sys.version_info >= (3, 13) else None
        rel_a = os.path.relpath(self.a, self.main)
        for cmd in (
            f'cd "{self.main}/E"; cd ../{rel_a}/S/..; git reset --hard',
            f"cd to-main/E; cd ../{rel_a}/S/..; git reset --hard",
            f"cd to-main/E && cd ../{rel_a}/S && cd .. && git reset --hard",
            f"cd to-main/E; git -C ../{rel_a}/S/.. reset --hard",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(test_guard.decision(test_guard.evaluate(test_guard.bash(cmd, self.a))), want)


class DeepRelativePathTest(test_state_limit.BoundedBase):
    """相対パスを深くし続ける形は、深さの 2 乗の時間で読む (0.9.3 と同じ)。期限を過ぎたら、注意を 1 つ足して返す。

    どの形も、期限が無ければ読むのに 0.5 秒から 15 秒ほどかかる (macOS の Python 3.14。0.10.5 で realpath の覚えを読みの間で
    共有してから)。3 秒の期限のままだと形ごとに 3 秒かかるので、期限を短くして (最も短い形の 5 分の 1 ほど) 流す。
    """

    SECONDS = 0.1
    SHORTEST = "or_cd 6 + cd a 100 + heredoc 9"  # 期限が無いときに最も早く読み終える形

    def _shapes(self) -> tuple[tuple[str, str], ...]:
        k = test_state_limit._limit_exponent()
        return (
            ("cd a 2000", cd_a(2000) + GIT),
            ("cd a; (区切りの空白なし) 2000", "cd a;" * 2000 + GIT),
            ('cd "a b" 2000', 'cd "a b"; ' * 2000 + GIT),
            ("cd a/./b/../c 1000", "cd a/./b/../c; " * 1000 + GIT),
            ("or_cd 6 + cd a 400", or_cd(6) + cd_a(400) + GIT),
            ("or_cd 6 + cd a 100 + heredoc 9", or_cd(6) + cd_a(100) + GIT + heredocs(9)),
            # 状態を上限ちょうど (まとめない) に固定してから深くし、安全網の読み直しも掛ける (git の成否で状態が倍になるので前半を 1 つ減らす)
            ("or_cd k-1 + cd a 200 + heredoc 9", or_cd(k - 1) + cd_a(200) + GIT + heredocs(9)),
            # 0.9.3 が読み終えて止めていた形 (後ろの main への git)。期限より後ろなので注意になる (CHANGELOG の「判定が変わる形」)
            ("cd a 2000 + main", cd_a(2000) + f'cd "{MAIN}"; {GIT}'),
        )

    @_testutil.timing_test
    def test_deep_relative_moves_end_with_a_note(self):
        note = command._Deadline(self.SECONDS).finding()
        for name, cmd in self._shapes():
            with self.subTest(name=name):
                with mock.patch.object(command, "_READ_SECONDS", self.SECONDS):
                    t = time.perf_counter()
                    out = self.findings(cmd)
                    elapsed = time.perf_counter() - t
                self.assertEqual(out, [note])
                # 期限を過ぎた後に読むのは、1 つの状態の 1 回の読みまで
                self.assertLess(elapsed, self.SECONDS + 1.0)

    @_testutil.timing_test
    def test_shortest_shape_takes_three_times_the_deadline(self):
        # 上のテストは、どの形も期限に届くことを前提にする。読みが速くなると余裕が黙って縮み、速い機械で期限の手前に読み終えて
        # 落ちる (0.10.5 で realpath の覚えを読みの間で共有して、最も短い形が 14 秒ほどから 0.5 秒ほどになった)。
        # 最も短い形を期限なしで 1 回読み、期限の 3 倍以上かかることを確かめる
        shapes = dict(self._shapes())
        self.assertIn(self.SHORTEST, shapes)
        with no_deadline():
            t = time.perf_counter()
            self.findings(shapes[self.SHORTEST])
            elapsed = time.perf_counter() - t
        self.assertGreaterEqual(elapsed, 3 * self.SECONDS, elapsed)

    def test_findings_before_the_deadline_are_kept(self):
        # 期限の手前で見つけた停止は残す
        with mock.patch.object(command, "_READ_SECONDS", self.SECONDS):
            out = self.findings(f'git -C "{MAIN}" {RESET}; {cd_a(2000)}git -C "{B}" {RESET}')
        self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN)), (False, None)])
        self.assertIn("期限", out[1].message)

    def test_short_moves_are_read_to_the_end(self):
        # 期限 (3 秒) の手前で読み終える深さは、0.9.3 と同じく後ろの git まで読む (注意は出さない)。深さの 2 乗の時間なので、
        # Windows の realpath (存在しない要素ごとに API を呼び直す) でも期限に十分届かない深さにする (200 段は Windows の CI で届いた)
        out = self.findings(f'{cd_a(100)}cd "{MAIN}"; {GIT}')
        self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])


class DeepStackTest(test_state_limit.BoundedBase):
    """pushd のスタックを深く重ねる形は、1 回の移動をスタックの深さに比例しない時間で読む。

    0.10.3 までは push のたびにスタック (tuple) を複製し、状態のハッシュもスタックの深さに比例したので、状態の数 × 深さの 2 乗の
    時間になった (`x || cd d<i>;` 6 回の後に `pushd /t/p<i>;` 3,500 回で約 6 秒)。0.10.0 からは期限 (3 秒) で注意になり、
    期限より後ろにある停止を落としていた (0.10.0 の CHANGELOG の「判定が変わる形」)。
    """

    def _shapes(self) -> tuple[tuple[str, str], ...]:
        # 読みの数は Windows で倍になる (`_drive_readings`) ので、スタックの深さを読みの数で割り、読む量をそろえる。
        # 深さは、手元 (macOS の Python 3.14) で期限の 5% ほどを使う大きさにする。Windows の CI はこの形で手元の 6 倍から 9 倍ほど
        # 遅く、3,500 段 / 600 段 (手元で期限の 16% / 24%) では期限に届いた。深さの 2 乗の時間に戻る退行は、伸び方のテスト (下) が見る
        readings = len(command._drive_readings())
        k = test_state_limit._limit_exponent()
        return (
            # 最後の移動は bash だけが移動する `command cd <main>` (zsh の読みが前半を読み終えた後、bash の読みで止める)
            ("or_cd 6 + pushd 1000 + command cd", or_cd(k) + pushd_abs(1000 // readings) + f'command cd "{MAIN}"; {GIT}'),
            # 終わらない here-document 7 個の後ろの git は、安全網の 7 回目の読み直しだけが読む (重い前半を 9 回読む)
            ("or_cd 6 + pushd 120 + heredoc 7", or_cd(k) + pushd_abs(120 // readings) + heredocs(7) + f'\ngit -C "{MAIN}" {RESET}'),
        )

    def test_deep_stacks_stop_before_the_deadline(self):
        # 0.9.3 と同じく、期限 (3 秒) の手前で読み終えて止める (期限の注意は出さない)
        for name, cmd in self._shapes():
            with self.subTest(name=name):
                out = self.findings(cmd)
                self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])
                # 状態は上限ちょうどのまま、スタックだけが深くなる (まとめるのは、重ねた後の `cat` / zsh の `command cd` の成否で
                # 状態が倍になったところ)
                self.assertEqual(self.peak, command._STATE_LIMIT)

    @mock.patch.object(command, "_READ_SECONDS", None)  # 読み切る時間を測る (期限で打ち切ると伸び方を測れない)
    @_testutil.timing_test
    def test_time_grows_linearly_with_the_depth(self):
        # 8 倍の深さで、線形なら約 8 倍、2 乗なら約 64 倍。小さい側も大きい側も、状態が上限ちょうどの同じ経路を通る
        k = test_state_limit._limit_exponent()
        readings = len(command._drive_readings())  # Windows は読みが倍なので、読む量を `_shapes` とそろえる
        small, large = 500 // readings, 4000 // readings
        for n in (small, large):
            self.assertEqual(self.verdict(or_cd(k) + pushd_abs(n) + f'cd "{MAIN}"; {GIT}'), "deny")
            self.assertEqual((self.peak, self.collapses), (command._STATE_LIMIT, 0))
        best = {small: float("inf"), large: float("inf")}
        for runs, n in ((3, small), (2, large)):
            cmd = or_cd(k) + pushd_abs(n) + f'cd "{MAIN}"; {GIT}'
            for _ in range(runs):
                t = time.perf_counter()
                self.findings(cmd)
                best[n] = min(best[n], time.perf_counter() - t)
        times = (best[small], best[large])
        self.assertLess(times[1], 5.0, times)
        self.assertLess(times[1] / times[0], 16.0, times)
        self.assertGreater(times[1] / times[0], 3.0, times)

    def test_state_independent_work_is_done_once(self):
        # 1 つの移動を状態ごとに読むところで、状態に関係しない処理を状態の数だけ繰り返さない (64 状態なら 64 倍の時間になり、
        # 上の形が期限の手前に収まらない)。絶対パスの行き先の正規化は 1 回の読み取りの中で綴りごとに 1 回 (読みの間で覚えを共有する。
        # `RealpathOnceTest`)、option と語の読みは 1 つの読みの中でコマンドごとに 1 回
        k = test_state_limit._limit_exponent()
        n = 50
        dirs = [os.path.join(test_state_limit._BASE, f"p{i}") for i in range(n)]  # ドライブ付きの絶対パス (repo の外)
        cmd = or_cd(k) + "".join(f'pushd "{d}"; ' for d in dirs) + GIT
        norms: list[str] = []
        forms: list[str] = []
        norm, chdir_form = command.norm, command._chdir_form

        def counted_norm(path):
            norms.append(path)
            return norm(path)

        def counted_form(cmd_, *args):
            forms.append(cmd_)
            return chdir_form(cmd_, *args)

        with mock.patch.object(command, "norm", counted_norm), mock.patch.object(command, "_chdir_form", counted_form):
            self.assertIsNone(self.verdict(cmd))
        self.assertEqual((self.peak, self.collapses), (command._STATE_LIMIT, 0))
        readings = 3 * len(command._drive_readings())  # zsh の読みと bash の 2 つの読み × パスの綴りの読み
        targets = set(dirs)
        self.assertEqual(sum(p in targets for p in norms), n)
        # `cd d<i>` (k 個) と `pushd <dir>` (n 個) のコマンドごとに 1 回
        self.assertEqual(len(forms), (k + n) * readings)

    def test_popd_after_a_deep_stack(self):
        # 深いスタックの各段を popd で戻る読みは、積んだ順の逆に戻る (深さによらない)。main を積んだ段まで戻ると止める
        n = 2000
        cmd = f'cd "{MAIN}"; pushd /t/top; ' + pushd_abs(n)
        self.assertIsNone(self.verdict(cmd + "popd; " * n + GIT))  # /t/top にいる
        self.assertEqual(self.verdict(cmd + "popd; " * (n + 1) + GIT), "deny")  # main に戻る
        # main より下まで戻ると、スタックが空に見える popd は「失敗してその場に残る」と「行き先の分からない場所」の両方
        out = self.findings(cmd + "popd; " * (n + 2) + GIT)
        self.assertEqual([(f.blocked, f.root) for f in out if f.blocked], [(True, family.norm(MAIN))])


class RealpathOnceTest(test_state_limit.BoundedBase):
    """1 回の読み取り (`analyze_readings`) の中で、同じ綴りの realpath は 1 回だけ (`command._resolve` の norms)。

    読み (zsh と bash の 2 つ・here-document の読み直し・Windows のパスの綴りの読み) はどれも同じ場所から同じ移動を読むので、
    覚えを読みごとに持つと、同じ realpath を読みの数だけ繰り返す。Windows の realpath は存在しない要素ごとに API を呼び直すので重く、
    相対パスを 200 段深くする形と深いスタックの形が、Windows の CI でだけ期限 (3 秒) に届いた。
    ここでは Windows の読みの数 (`_drive_readings` が 2 つ) を手元で組み立てて確かめる。
    """

    def _count(self, cmd: str, drive_readings: tuple[bool, ...]) -> tuple[list[command.Finding], dict[str, int], int]:
        calls: dict[str, int] = {}
        analyzers = []
        real_norm, real_init = command.norm, command._Analyzer.__init__

        def counted_norm(path):
            calls[path] = calls.get(path, 0) + 1
            return real_norm(path)

        def counted_init(analyzer, *args, **kwargs):
            analyzers.append(analyzer)
            return real_init(analyzer, *args, **kwargs)

        with mock.patch.object(command, "norm", counted_norm), mock.patch.object(
            command._Analyzer, "__init__", counted_init
        ), mock.patch.object(command, "_drive_readings", lambda: drive_readings):
            out = self.findings(cmd)
        return out, calls, len(analyzers)

    def test_each_spelling_is_resolved_once(self):
        k = 3
        dirs = [os.path.join(test_state_limit._BASE, f"p{i}") for i in range(20)]
        outside = os.path.join(test_state_limit._BASE, "zz")  # どの worktree でもない絶対パス
        # Git Bash の読みで綴りが変わらないパスだけを使う (手元で Windows の読みの数を組み立てても、読みの中身は同じ)
        self.assertEqual([family._msys_drive(p) for p in (*dirs, outside, MAIN, B)], [*dirs, outside, MAIN, B])
        for name, cmd, want in (
            ("cd a 30 + main", f'{cd_a(30)}cd "{MAIN}"; {GIT}', True),
            ("or_cd 3 + cd a 10", or_cd(k) + cd_a(10) + GIT, False),
            ("pushd 20", "".join(f'pushd "{d}"; ' for d in dirs) + f'cd "{MAIN}"; {GIT}', True),
            ("git -C の相対パス", f'cd "{MAIN}"; git -C a -C a -C .. {RESET}', True),
            ("--git-dir と --work-tree", f'git --git-dir="{MAIN}/.git" --work-tree=a {RESET}', True),
            ("env -C", f"cd a; env -C ../.. {GIT}", True),
            ("pushd -n (bash の読みだけ)", f'pushd -n "{MAIN}"; popd; {GIT}', True),
            ("git worktree remove", f'git worktree remove "{B}"', True),
            # 今の場所が決まらない後ろの対象は、別の worktree でありうるかを見るところ (`_worktree_may_be_other`) でも正規化する
            ("git worktree remove (今の場所が決まらない)", f'cd "$X"; git worktree remove "{outside}"', False),
            ("heredoc の読み直し", f'{cd_a(10)}git -C "{B}" {RESET}' + heredocs(3), True),
        ):
            for drive_readings in ((False,), (False, True)):
                with self.subTest(name=name, drive_readings=drive_readings):
                    out, calls, analyzers = self._count(cmd, drive_readings)
                    self.assertEqual(any(f.blocked for f in out), want, out)
                    # 対照: 読みが複数あり (zsh・bash 2 つ × 綴りの読み、here-document は読み直しも)、realpath を通っている
                    self.assertGreaterEqual(analyzers, 3 * len(drive_readings))
                    self.assertTrue(calls)
                    self.assertEqual({p: c for p, c in calls.items() if c > 1}, {})

    def test_readings_of_the_cwd_share_the_memo(self):
        # 作業ディレクトリの読みが 2 つ (Windows の `/c/x` の cwd) でも、同じ綴りの realpath は 1 回
        calls: dict[str, int] = {}
        real_norm = command.norm

        def counted_norm(path):
            calls[path] = calls.get(path, 0) + 1
            return real_norm(path)

        cwd = family.norm(test_state_limit.A)
        with mock.patch.object(command, "norm", counted_norm):
            try:
                out = command.analyze_readings(f'{cd_a(10)}cd "{MAIN}"; {GIT}', ((cwd, self.fam), (cwd, self.fam)))
            except Exception as e:  # noqa: BLE001 - 読みの例外も読みの誤りとして数える (ERROR ではなく FAIL)
                self.fail(f"読みが例外を投げた: {e!r}")
        self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])
        self.assertTrue(calls)
        self.assertEqual({p: c for p, c in calls.items() if c > 1}, {})


class RealpathMemoBoundTest(test_state_limit.BoundedBase):
    """realpath の覚えの大きさは、相対パスを深くする形でも上限 (`command._NORMS_CHARS`) で止まる。

    覚えは 1 回の読み取りの間、途中の行き先 (相対パスは基点と繋いだ綴り) を全部持つので、上限が無いと深さの 2 乗 × 状態の数の
    大きさになる。上限を超えた分は覚えずに realpath を呼ぶだけで、判定の中身は変えない。
    """

    def _stored(self, cmd: str) -> tuple[list[command.Finding], int, int]:
        """(findings, 覚えの数, 覚えに入れた綴りと値の文字数の合計)。期限なしで読み切る。"""
        memos = []
        real_init = command._Analyzer.__init__

        def recording_init(analyzer, *args, **kwargs):
            real_init(analyzer, *args, **kwargs)
            memos.append(analyzer._norms)

        with mock.patch.object(command._Analyzer, "__init__", recording_init), no_deadline():
            out = self.findings(cmd)
        unique = list({id(m): m for m in memos}.values())
        return out, len(unique), sum(len(k) + len(v) for m in unique for k, v in m.items())

    def test_deep_relative_moves_do_not_fill_memory(self):
        # `x || cd d<i>;` 6 回の後に `cd <40 字>;` 100 回 (4 KB)。途中の行き先を全部覚えると 2,700 万字ほどになる
        out, memos, stored = self._stored(or_cd(6) + f"cd {'x' * 40}; " * 100 + f'cd "{MAIN}"; {GIT}')
        self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])
        self.assertEqual(memos, 1)
        self.assertLessEqual(stored, command._NORMS_CHARS)

    def test_shapes_that_failed_on_windows_stay_under_the_bound(self):
        # Windows の CI で期限に届いた形 (縮める前の深さ) は、上限の内側で覚えを使い切る (手元で上限の 3% ほど)。
        # 一時ディレクトリの綴りが長い OS でも収まるように、上限の 8 分の 1 に収まることを見る
        k = test_state_limit._limit_exponent()
        readings = len(command._drive_readings())
        for name, cmd in (
            ("cd a 200 + main", f'{cd_a(200)}cd "{MAIN}"; {GIT}'),
            ("or_cd 6 + pushd 3500 + command cd", or_cd(k) + pushd_abs(3500 // readings) + f'command cd "{MAIN}"; {GIT}'),
            ("or_cd 6 + pushd 600 + heredoc 7", or_cd(k) + pushd_abs(600 // readings) + heredocs(7) + f'\ngit -C "{MAIN}" {RESET}'),
        ):
            with self.subTest(name=name):
                out, memos, stored = self._stored(cmd)
                self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])
                self.assertEqual(memos, 1)
                self.assertGreater(stored, 0)
                self.assertLessEqual(stored, command._NORMS_CHARS // 8)

    def test_bound_does_not_change_findings(self):
        # 上限を小さくしても (0 は何も覚えない)、判定は上限の無いときと同じ。覚えた大きさは上限で止まる
        outside = os.path.join(test_state_limit._BASE, "zz")
        for name, cmd in (
            ("cd a 30 + main", f'{cd_a(30)}cd "{MAIN}"; {GIT}'),
            ("or_cd 3 + cd a 10", or_cd(3) + cd_a(10) + GIT),
            ("pushd + cd main", "".join(f'pushd "{test_state_limit._BASE}/p{i}"; ' for i in range(20)) + f'cd "{MAIN}"; {GIT}'),
            ("git -C の相対パス", f'cd "{MAIN}"; git -C a -C a -C .. {RESET}'),
            ("--git-dir と --work-tree", f'git --git-dir="{MAIN}/.git" --work-tree=a {RESET}'),
            ("env -C", f"cd a; env -C ../.. {GIT}"),
            ("git worktree remove", f'cd "$X"; git worktree remove "{outside}"; git worktree remove "{B}"'),
            ("heredoc の読み直し", f'{cd_a(10)}git -C "{B}" {RESET}' + heredocs(3)),
        ):
            with self.subTest(name=name):
                with mock.patch.object(command, "_NORMS_CHARS", 1 << 40):
                    want, _, unbounded = self._stored(cmd)
                self.assertGreater(unbounded, 0)
                for bound in (0, unbounded // 2):
                    with mock.patch.object(command, "_NORMS_CHARS", bound):
                        out, _, stored = self._stored(cmd)
                    self.assertEqual(out, want, bound)
                    self.assertLessEqual(stored, bound)


class _FakeClock:
    """`command.time` の差し替え。移動を 1 つ読むたびに 1 秒進む。"""

    def __init__(self):
        self.now = 0.0
        self.moves = 0

    def monotonic(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += 1
        self.moves += 1


class DeadlineTest(test_state_limit.BoundedBase):
    """読み取りが期限を過ぎたら、それ以上読まずに、それまでの判定に注意を 1 つ足して返す (止めない)。"""

    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        chdir = command._Analyzer._chdir
        clock = self.clock

        def ticking_chdir(analyzer, *args):
            clock.tick()
            return chdir(analyzer, *args)

        for target, name, repl in (
            (command, "time", types.SimpleNamespace(monotonic=self.clock.monotonic)),
            (command._Analyzer, "_chdir", ticking_chdir),
            (command, "_READ_SECONDS", 10),
        ):
            patcher = mock.patch.object(target, name, repl)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _note(self, out: list[command.Finding]) -> list[command.Finding]:
        return [f for f in out if "期限" in f.message]

    def test_findings_before_the_deadline_are_kept(self):
        # 期限までに見つけた停止は残し、期限より後ろの git は読まない。移動はちょうど期限の秒数だけ読む
        cmd = f'git -C "{MAIN}" {RESET}; {cd_a(50)}git -C "{B}" {RESET}'
        out = self.findings(cmd)
        self.assertEqual([f.root for f in out if f.blocked], [family.norm(MAIN)])
        notes = self._note(out)
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0].blocked)
        self.assertEqual(notes[0].message, "コマンドの読み取りが期限 (10 秒) を超えたため、途中から後ろの git を確かめていない")
        self.assertEqual(self.clock.moves, 10)

    def test_within_the_deadline_nothing_changes(self):
        # 読み (zsh の読みと bash の 2 つの読み。Windows ではパスの綴りの読みごとに倍) ごとに移動を 3 つずつ読み、期限の 1 秒手前で読み終える
        readings = 3 * len(command._drive_readings())
        with mock.patch.object(command, "_READ_SECONDS", 3 * readings + 1):
            out = self.findings(f'{cd_a(3)}git -C "{B}" {RESET}')
        self.assertEqual(self.clock.moves, 3 * readings)
        self.assertEqual([f.root for f in out], [family.norm(B)])
        self.assertEqual(self._note(out), [])
        self.assertIsNone(self.verdict("git status"))

    def test_states_of_one_move_stop_at_the_deadline(self):
        # 区切りの字句は、1 つの移動を状態の数だけ読む (`x || cd d<i>;` で割れた行き先ごと)。状態ごとにも期限を見て、過ぎたら
        # 残りの状態を読まない (字句ごとにしか見ないと、期限を過ぎた字句の状態を全部読む: 1 + 2 + 4 + 8 = 15)
        k = test_state_limit._limit_exponent()
        out = self.findings(or_cd(k) + f'cd "{MAIN}"; {GIT}')
        self.assertEqual(self.clock.moves, 10)
        self.assertEqual([f.blocked for f in out], [False])
        self.assertEqual(len(self._note(out)), 1)

    def test_slow_tokenizing_alone_does_not_use_up_the_deadline(self):
        # 字句に分ける処理 (正規化と shlex) は打ち切れない。期限は最初の読みを字句に分け終えてから数え、そこで期限を使い切って
        # どの git も読まずに注意だけにすることはしない (引用した 0.6 MB ほどの 1 語。0.9.3 は読み終えて止めていた)
        tokens = command._tokens

        def slow_tokens(*args, **kwargs):
            self.clock.now += 20
            return tokens(*args, **kwargs)

        with mock.patch.object(command, "_tokens", slow_tokens):
            out = self.findings(f'git -C "{MAIN}" {RESET}')
        self.assertEqual([(f.blocked, f.root) for f in out], [(True, family.norm(MAIN))])

    def test_deadline_starts_once(self):
        # 起点は最初の `start` だけ (安全網の読み直しが `start` を呼んでも期限を延ばさない)。始める前と、期限なしは過ぎない
        def run() -> list[bool]:
            deadline = command._Deadline(10)
            seen = [deadline.check()]
            self.clock.now = 100
            deadline.start()
            self.clock.now = 105
            deadline.start()
            seen.append(deadline.check())
            self.clock.now = 110
            seen.append(deadline.check())
            unbounded = command._Deadline(None)
            unbounded.start()
            self.clock.now = 10**9
            seen.append(unbounded.check())
            return seen

        try:
            seen = run()
        except Exception as e:  # noqa: BLE001 - 例外も期限の扱いの誤りとして数える
            self.fail(f"期限の扱いが例外を投げた: {e!r}")
        self.assertEqual(seen, [False, False, True, False])

    def test_nothing_is_read_after_the_deadline(self):
        with mock.patch.object(command, "_READ_SECONDS", 0):
            out = self.findings(f'git -C "{MAIN}" {RESET}')
            self.assertEqual([f.blocked for f in out], [False])
            self.assertEqual(len(self._note(out)), 1)
            # git を含まないコマンドは読まない (注意も出さない)
            self.assertEqual(self.findings("echo hi"), [])

    def test_safety_net_readings_stop_at_the_deadline(self):
        # 期限を過ぎたら、旧来の読みと here-document の読み直しも、字句に分けることからやめる
        cmd = f'git -C "{MAIN}" {RESET}' + heredocs(9)
        counts = {"tokens": 0, "normalizers": 0}
        tokens, normalizer_init = command._tokens, command._Normalizer.__init__

        def counted_tokens(*args, **kwargs):
            counts["tokens"] += 1
            return tokens(*args, **kwargs)

        def counted_init(self_, *args, **kwargs):
            counts["normalizers"] += 1
            return normalizer_init(self_, *args, **kwargs)

        with mock.patch.object(command, "_tokens", counted_tokens), mock.patch.object(command._Normalizer, "__init__", counted_init):
            with no_deadline():
                self.assertEqual(self.verdict(cmd), "deny")
            # 対照: 期限が無ければ、旧来の読みと読み直しを通す
            self.assertGreater(counts["tokens"], 2)
            self.assertGreater(counts["normalizers"], 2)
            counts.update(tokens=0, normalizers=0)
            with mock.patch.object(command, "_READ_SECONDS", 0):
                self.assertEqual(self.verdict(cmd), "note")
            self.assertEqual(counts, {"tokens": 1, "normalizers": 1})

    def test_deadline_inside_the_safety_net_readings(self):
        # 期限は安全網の読みにも効く。どちらの形も、正規化の読みは here-document の本文として後ろを読まない
        body = f"\n{cd_a(50)}git -C \"{B}\" {RESET}"
        for name, cmd in (
            # 区切り語に空白がある here-document は、旧来の読みが here-document と読まず、本文を読む
            ("旧来の読み", "git status <<'E X'" + body),
            # 識別子の区切り語は旧来の読みも本文を除くので、終わらない here-document を外した読み直しが本文を読む
            ("読み直し", "git status <<E" + body),
        ):
            with self.subTest(name=name):
                self.clock.now, self.clock.moves = 0.0, 0
                out = self.findings(cmd)
                self.assertEqual([f.blocked for f in out], [False])
                self.assertEqual(len(self._note(out)), 1)
                self.assertEqual(self.clock.moves, 10)
                with no_deadline():
                    self.assertEqual(self.verdict(cmd), "deny")

    def test_slow_shape_ends_with_a_note_at_the_deadline(self):
        # 状態を上限まで割ってからスタックを重ねる形も、期限を過ぎたら注意にする (期限は移動の種類によらない)
        k = test_state_limit._limit_exponent()
        cmd = or_cd(k) + pushd_abs(200) + f'cd "{MAIN}"; {GIT}'
        out = self.findings(cmd)
        self.assertEqual([f.blocked for f in out], [False])
        self.assertEqual(len(self._note(out)), 1)
        # 期限が無ければ読み切って止める
        with no_deadline():
            self.assertEqual(self.verdict(cmd), "deny")


class DeadlineInOneGitTest(test_state_limit.BoundedBase):
    """1 つの git の読みは、状態の数 x 語の数で重くなる (`git -C a -C a ...` を数万個並べた形)。状態ごとに期限を見る。"""

    def test_states_of_one_git_stop_at_the_deadline(self):
        clock = _FakeClock()
        git_targets = command._git_targets

        def ticking(*args, **kwargs):
            clock.tick()
            return git_targets(*args, **kwargs)

        k = test_state_limit._limit_exponent()
        with mock.patch.object(command, "time", types.SimpleNamespace(monotonic=clock.monotonic)), mock.patch.object(
            command, "_git_targets", ticking
        ), mock.patch.object(command, "_READ_SECONDS", 10):
            out = self.findings(or_cd(k) + "git -C a reset --hard")
        self.assertEqual(clock.moves, 10)
        self.assertEqual([f.blocked for f in out], [False])
        self.assertIn("期限", out[0].message)


class ReadTimeHookTest(test_guard.GuardBase):
    """hook 全体 (git の起動を含む) が、hooks.json の timeout の半分より短い時間で返る。"""

    def _run_hook(self, cmd: str) -> tuple[float, dict | None]:
        payload = json.dumps(test_guard.bash(cmd, self.a), ensure_ascii=False).encode("utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WORKTREE_CWD_GUARD")}
        t = time.perf_counter()
        try:
            r = subprocess.run([sys.executable, str(test_guard._PKG)], input=payload, capture_output=True, env=env, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail(f"60 秒を超えた: {cmd[:80]!r}")
        elapsed = time.perf_counter() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        return elapsed, json.loads(r.stdout.decode("utf-8")) if r.stdout.strip() else None

    def _timeout(self) -> float:
        hooks = json.loads(test_state_limit._HOOKS_JSON.read_text(encoding="utf-8"))
        entries = hooks["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["hooks"]), 1)
        return entries[0]["hooks"][0]["timeout"]

    @_testutil.timing_test
    def test_deep_relative_paths(self):
        timeout = self._timeout()
        for name, cmd in (
            ("cd a 2000", cd_a(2000) + GIT),
            ("or_cd 6 + cd a 400", or_cd(6) + cd_a(400) + GIT),
            ("or_cd 6 + cd a 100 + heredoc 9", or_cd(6) + cd_a(100) + GIT + heredocs(9)),
        ):
            with self.subTest(name=name):
                elapsed, out = self._run_hook(cmd)
                self.assertLess(elapsed, timeout / 2, elapsed)
                # 移動のたびに全要素を辿り直すので、期限の注意になる (速い機械なら、読み切って何も出さない)
                self.assertIn(test_guard.decision(out), (None, "context"))
                if out is not None:
                    self.assertIn(f"期限 ({command._READ_SECONDS:g} 秒)", out["hookSpecificOutput"]["additionalContext"])

    @_testutil.timing_test
    def test_shape_past_the_deadline_returns_a_note(self):
        # 期限が無ければ hook の timeout を大きく超える形 (状態の数 × 相対パスの深さの 2 乗。期限なしで 15 秒ほど (macOS の
        # Python 3.14)。realpath の覚えは上限 (`command._NORMS_CHARS`) を超えた先を覚えないので、覚えの共有ではほとんど速くならない)
        timeout = self._timeout()
        k = test_state_limit._limit_exponent()
        cmd = or_cd(k) + cd_a(400) + GIT
        elapsed, out = self._run_hook(cmd)
        self.assertEqual(test_guard.decision(out), "context")
        self.assertIn(f"期限 ({command._READ_SECONDS:g} 秒)", out["hookSpecificOutput"]["additionalContext"])
        self.assertLess(elapsed, timeout / 2, elapsed)
        self.assertLess(command._READ_SECONDS, timeout / 2)

    def test_documented_deadline_matches(self):
        root = test_state_limit._HOOKS_JSON.parents[1]
        want = f"期限 ({command._READ_SECONDS:g} 秒)"
        for name in ("README.md", "AGENTS.md"):
            with self.subTest(name=name):
                self.assertTrue(want in (root / name).read_text(encoding="utf-8"), f"{name} に {want} が無い")


if __name__ == "__main__":
    unittest.main()
