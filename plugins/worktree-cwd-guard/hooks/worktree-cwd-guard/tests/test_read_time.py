"""読み取りの時間: パスの深さに比例する解決 (`command._join_resolved`) と、読み取りの期限 (`command._READ_SECONDS`)。

相対パスの `cd a;` を続けると、行き先のパスが深くなる。realpath はパスの要素を先頭から lstat し直すので、移動のたびに呼ぶと深さの
2 乗の時間になり、hook が時間切れになるとコマンドはそのまま走る (0.9.3 まで。`cd a;` 2,000 回で約 6 秒、`x || cd d<i>;` 6 回の後に
`cd a;` 400 回で約 17 秒)。Python 3.13 以降の POSIX では正規化済みの基点からの差分だけを辿るようにし、それ以外の環境の相対パスと、
それでも遅い形 (状態の数 × スタックの深さなど) は、読み取りの期限で閉じる (期限を過ぎたら、それまでの判定に注意を足して返す)。
"""
from __future__ import annotations

import itertools
import json
import os
import posixpath
import random
import subprocess
import sys
import tempfile
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

_POSIX_REALPATH = os.path is posixpath
# 差分だけを辿る解決は、Python 3.13 以降の POSIX でだけ使う (3.12 以前の realpath は symlink の環で辿るのをやめ、不動点でない値を
# 返す。Windows の realpath は要素ごとに辿らない)。それ以外は realpath をそのまま呼び、遅い形は期限で閉じる
_FAST_JOIN = _POSIX_REALPATH and sys.version_info >= (3, 13)
_NO_FAST_JOIN = "差分だけを辿る解決は Python 3.13 以降の POSIX でだけ使う (それ以外は期限で閉じる: DeadlineTest)"


def cd_a(n: int) -> str:
    """`cd a;` (相対パス)。移動のたびに行き先が 1 段深くなる。"""
    return "cd a; " * n


def heredocs(n: int) -> str:
    """終わらない here-document を n 個。安全網が、旧来の読みと、読み直し (n - 1 回、上限 `_REREAD_LIMIT`) で読み直す。"""
    return "\n" + "\n".join(f"cat <<E{i}" for i in range(n))


def no_deadline():
    return mock.patch.object(command, "_READ_SECONDS", None)


def _outcome(fn, *args):
    try:
        return ("value", fn(*args))
    except Exception as e:  # noqa: BLE001 - 例外も結果として比べる (realpath と同じ例外を投げること)
        return ("raise", type(e).__name__)


@unittest.skipUnless(_FAST_JOIN, _NO_FAST_JOIN)
class JoinResolvedTest(unittest.TestCase):
    """`_join_resolved(base, rel)` が `norm(os.path.join(base, rel))` と同じ値 (か同じ例外) になる。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        top = family.norm(tmp.name)
        real = os.path.join(top, "real")
        base = os.path.join(real, "base")
        os.makedirs(os.path.join(base, "d", "e"))
        with open(os.path.join(base, "f"), "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.link(os.path.join(base, "f"), os.path.join(base, "h"))  # hard link
            for name, target in (
                ("ld", "d"),  # 行き先の側の symlink (dir)
                ("lrel", "d/e"),
                ("lf", "f"),  # file への symlink
                ("lup", ".."),  # 上への symlink
                ("ldot", "."),
                ("ldang", "nosuch"),  # 行き先の無い symlink
                ("lloop", "lloop"),  # 自分を指す symlink
                ("lmut1", "lmut2"),  # 互いを指す symlink
                ("lmut2", "lmut1"),
                ("labs", os.path.join(base, "d")),  # 絶対パスの symlink
                ("lchain", "ld"),  # symlink を指す symlink
                ("ldd", "../base/d/./e/"),  # 行き先に `..` / `.` / 末尾の `/` を含む symlink
            ):
                os.symlink(target, os.path.join(base, name))
            os.symlink(real, os.path.join(top, "lpar"))  # 基点の親の側の symlink (`lpar/base` は `real/base`)
            os.symlink(base, os.path.join(base, "d", "e", "lback"))  # 深い場所から上の dir への symlink
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlink / hard link を作れない: {e!r}")
        self.top, self.base = top, base
        # 大文字小文字だけ違う名前 (`D` / `E`)。区別しない FS では `d` / `e` と同じ dir で、realpath も綴りを変えない
        self.names = [
            "d", "e", "f", "h", "ld", "lrel", "lf", "lup", "ldot", "ldang", "lloop", "lmut1", "labs", "lchain", "ldd",
            "lback", "lpar", "real", "base", "D", "E", "..", ".", "", "...", "zz", "a\x00b",
        ]
        self.bases = [family.norm(p) for p in (top, base, os.path.join(base, "d", "e"), os.path.join(base, "zz", "yy"), os.path.join(base, "ld"))]

    def _check(self, base: str, rel: str) -> None:
        if rel.startswith("/"):
            return  # 絶対パスは `_resolve` が先に読む (`_join_resolved` には渡らない)
        want = _outcome(lambda: family.norm(os.path.join(base, rel)))
        got = _outcome(command._join_resolved, base, rel)
        self.assertEqual(got, want, (base, rel))

    def test_matches_realpath_exhaustively_up_to_two_parts(self):
        for base in self.bases:
            for parts in itertools.product(self.names, repeat=2):
                self._check(base, "/".join(parts))
            for name in self.names:
                self._check(base, name)

    def test_matches_realpath_on_random_paths(self):
        rng = random.Random(1124)
        for _ in range(3000):
            base = rng.choice(self.bases)
            rel = "/".join(rng.choice(self.names) for _ in range(rng.randint(3, 7)))
            self._check(base, rel)

    def test_the_four_placements(self):
        # 行き先の側の symlink / 基点の親の側の symlink / hard link / 大文字小文字だけ違う名前を、`_resolve` の入り口で読む
        n = family.norm
        self.assertEqual(command._resolve(self.base, "ld/e"), n(os.path.join(self.base, "d", "e")))
        self.assertEqual(command._resolve(self.top, "lpar/base/d"), n(os.path.join(self.base, "d")))
        self.assertEqual(command._resolve(self.base, "h"), os.path.join(self.base, "h"))
        self.assertEqual(command._resolve(self.base, "D/E"), n(os.path.join(self.base, "D", "E")))
        self.assertEqual(command._resolve(self.base, "lup/base/lrel"), n(os.path.join(self.base, "d", "e")))


def _old_step(here: str, rel: str) -> str:
    """0.9.3 までの解決 (移動のたびに全要素を realpath し直す)。"""
    return family.norm(rel) if rel.startswith("/") else family.norm(os.path.join(here, rel))


@unittest.skipUnless(_POSIX_REALPATH, "symlink の環の扱いは POSIX の realpath の規則 (Windows は realpath をそのまま呼ぶ)")
class ResolveChainTest(unittest.TestCase):
    """移動を続けた `here = _resolve(here, rel)` の値が、0.9.3 までの式の連鎖と同じになる。

    Python 3.12 以前の realpath は symlink の環に当たると辿るのをやめ、残りを字面のまま返す (`<wt>/m1/../to-main` が、symlink の
    `to-main` を含んだまま返る)。その値は realpath の不動点でないので、そこを基点に差分だけを辿ると次の移動で値が変わる。
    1 回の解決 (`JoinResolvedTest`) は不動点の基点から比べるので、ここで連鎖を比べる。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        top = family.norm(tmp.name)
        wt, main, other = (os.path.join(top, name) for name in ("wt", "main", "other"))
        for d in (os.path.join(main, "sub", "deep"), os.path.join(wt, "x", "y"), other):
            os.makedirs(d)
        locked = os.path.join(wt, "locked")
        os.makedirs(locked)
        try:
            for path, target in (
                (os.path.join(wt, "to-main"), main),
                (os.path.join(wt, "to-main-rel"), "../main"),
                (os.path.join(wt, "lloop"), "lloop"),  # 自分を指す symlink
                (os.path.join(wt, "m1"), "m2"),  # 互いを指す symlink
                (os.path.join(wt, "m2"), "m1"),
                (os.path.join(wt, "dang"), "nosuch/zz"),
                (os.path.join(wt, "up"), ".."),
                (os.path.join(wt, "dot"), "."),
                (os.path.join(wt, "chain"), "to-main"),
                (os.path.join(wt, "toloop-then-up"), "lloop/.."),
                (os.path.join(wt, "x", "back"), "../.."),
                (os.path.join(wt, "x", "loopdeep"), "../x/loopdeep"),
                (os.path.join(main, "to-wt"), wt),
                (os.path.join(main, "to-other"), "../other"),
                (os.path.join(main, "lloop"), "lloop"),
                (os.path.join(locked, "to-main"), main),
            ):
                os.symlink(target, path)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlink を作れない: {e!r}")
        os.chmod(locked, 0)  # 中を lstat できない dir
        self.addCleanup(os.chmod, locked, 0o755)
        self.top, self.wt, self.main = top, family.norm(wt), family.norm(main)
        self.names = [
            "to-main", "to-main-rel", "lloop", "m1", "m2", "dang", "up", "dot", "to-wt", "to-other", "x", "y", "back",
            "loopdeep", "chain", "toloop-then-up", "locked", "sub", "deep", "main", "wt", "other", "MAIN", "To-Main",
            "..", "..", ".", "", "zz", "a",
        ]
        self.bases = [family.norm(p) for p in (wt, main, top, os.path.join(wt, "x"))]

    def _chain(self, base: str, steps: list[str]) -> tuple[list, list]:
        """steps を old (0.9.3 までの式) と new (`_resolve`) で持ち回り、各段の結果 (値か例外の型) を返す。"""
        old, new = [], []
        here_old = here_new = base
        for rel in steps:
            o, n = _outcome(_old_step, here_old, rel), _outcome(command._resolve, here_new, rel)
            old.append(o)
            new.append(n)
            if o != n or o[0] == "raise":
                break
            here_old, here_new = o[1], n[1]
        return old, new

    def test_moves_after_a_symlink_loop(self):
        # 環 (`m1` <-> `m2` / `lloop`) を `..` で横切った先の値を基点にした移動 (レビューと再現で挙がった形)
        for base, steps in (
            (self.wt, ["m1/../to-main", "."]),
            (self.wt, ["m1/../to-main", "sub"]),
            (self.wt, ["lloop/x/../../to-main", "sub"]),
            (self.wt, [f"{self.wt}/m1/../to-main", "."]),
            (self.wt, ["m1", "../to-main-rel/sub"]),
            (self.wt, ["m1///deep", "../../to-main"]),
        ):
            with self.subTest(steps=steps):
                old, new = self._chain(base, steps)
                self.assertEqual(new, old)

    def test_chained_moves_match_the_old_formula(self):
        rng = random.Random(1124)
        mismatches = []
        for _ in range(4000):
            base = rng.choice(self.bases)
            steps = ["/".join(rng.choice(self.names) for _ in range(rng.randint(1, 4))) for _ in range(rng.randint(2, 5))]
            old, new = self._chain(base, steps)
            if old != new:
                mismatches.append((base, steps, old[-1], new[-1]))
        self.assertEqual(len(mismatches), 0, mismatches[:3])

    def test_fast_path_only_where_realpath_returns_fixed_points(self):
        # 差分だけを辿る解決を使う版では、環を横切った値も realpath の不動点 (そこを基点にしてよい)
        if not command._REALPATH_SETTLES:
            self.skipTest("この Python では差分だけを辿る解決を使わない")
        for rel in ("m1/../to-main", "lloop/x/../../to-main", "m1///deep", "m1", "toloop-then-up/to-main"):
            with self.subTest(rel=rel):
                value = family.norm(os.path.join(self.wt, rel))
                self.assertEqual(family.norm(value), value)

    def test_without_the_fast_path_realpath_is_called_each_time(self):
        # 差分だけを辿る解決を使わない版 (3.12 以前) は、0.9.3 と同じ式で解決する
        def unused(*args):
            raise self.failureException(f"差分だけを辿る解決を使った: {args!r}")

        with mock.patch.object(command, "_REALPATH_SETTLES", False), mock.patch.object(command, "_join_resolved", unused):
            for rel in ("m1/../to-main", "x/y", "to-main/sub", "..", "zz/../x"):
                with self.subTest(rel=rel):
                    self.assertEqual(command._resolve(self.wt, rel), _old_step(self.wt, rel))


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

    def test_moves_after_a_symlink_loop(self):
        # Python 3.12 以前の realpath は symlink の環 (`m1` <-> `m2`) で辿るのをやめ、`<a>/m1/../to-main` を `<a>/to-main` (main への
        # symlink を含む) のまま返す。次の移動では、0.9.3 と同じく全要素を辿り直して main に届く (どの版でも止める)
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


class LinearTimeTest(test_state_limit.BoundedBase):
    """相対パスを深くし続ける形の時間が、深さに比例する (2 乗にならない)。期限を外して、読み切る時間を測る。"""

    def _best(self, cmd: str) -> float:
        best = float("inf")
        for _ in range(3):
            t = time.perf_counter()
            self.findings(cmd)
            best = min(best, time.perf_counter() - t)
        return best

    def _assert_linear(self, shape, small: int, large: int, want, peak: int | None = None, collapses: int | None = 0) -> None:
        with no_deadline():
            # 小さい側と大きい側が同じ経路を通ること (同じ判定・同じ回数だけ状態をまとめる・同じ状態の数)
            paths = []
            for k in (small, large):
                verdict = self.verdict(shape(k))
                paths.append((verdict, self.collapses, self.peak))
            self.assertEqual(paths[0], paths[1])
            self.assertEqual(paths[0][0], want)
            if collapses is not None:
                self.assertEqual(paths[0][1], collapses)
            if peak is not None:
                self.assertEqual(paths[0][2], peak)
            times = (self._best(shape(small)), self._best(shape(large)))
        self.assertLess(times[1], 5.0, times)
        # 4 倍の入力で、線形なら約 4 倍、2 乗なら約 16 倍
        self.assertLess(times[1] / times[0], 8.0, times)
        self.assertGreater(times[1] / times[0], 1.5, times)

    @unittest.skipUnless(_FAST_JOIN, _NO_FAST_JOIN)
    def test_relative_cd_chain(self):
        # 区切りの空白の有無、引用した語、`..` / `.` を含む語でも同じ
        for unit, small, large in (("cd a; ", 1000, 4000), ("cd a;", 1000, 4000), ('cd "a b"; ', 1000, 4000),
                                   ("cd a/./b/../c; ", 500, 2000)):
            with self.subTest(unit=unit):
                self._assert_linear(lambda k, unit=unit: unit * k + GIT, small, large, None)

    @unittest.skipUnless(_FAST_JOIN, _NO_FAST_JOIN)
    def test_two_part_sweep_at_the_state_limit(self):
        # 前半で状態を上限ちょうど (まとめない) に固定し、後半で相対パスを深くし続ける。状態の数が係数に掛かる
        k = test_state_limit._limit_exponent()
        self._assert_linear(lambda m: or_cd(k) + cd_a(m) + GIT, 200, 800, None, peak=command._STATE_LIMIT)
        # 終わらない here-document を付け、安全網の読み直し (旧来の読みと 8 回の読み直し) の倍率も掛ける。git の成否で状態が
        # 倍になるので前半を 1 つ減らす (読み直しの読みは here-document の行の `cat` でまとめるので、まとめる回数は数で固定しない)
        self._assert_linear(
            lambda m: or_cd(k - 1) + cd_a(m) + GIT + heredocs(9), 50, 200, None, peak=command._STATE_LIMIT, collapses=None
        )

    @unittest.skipUnless(_FAST_JOIN, _NO_FAST_JOIN)
    def test_reported_shapes_finish_quickly(self):
        # 内部バックログに挙がった形が、期限に当たらずに 2 秒より短く読み終わる
        for name, cmd in (
            ("cd a 2000", cd_a(2000) + GIT),
            ("or_cd 6 + cd a 400", or_cd(6) + cd_a(400) + GIT),
            ("or_cd 6 + cd a 100 + heredoc 9", or_cd(6) + cd_a(100) + GIT + heredocs(9)),
        ):
            with self.subTest(name=name):
                t = time.perf_counter()
                out = self.findings(cmd)
                elapsed = time.perf_counter() - t
                self.assertEqual(out, [])
                self.assertLess(elapsed, 2.0)


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
        # 状態の数 × スタックの深さで遅くなる形 (差分の解決では速くならない) も、期限で注意にする
        k = test_state_limit._limit_exponent()
        cmd = or_cd(k) + "".join(f"pushd /t/p{i}; " for i in range(200)) + f'cd "{MAIN}"; {GIT}'
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
                # 差分だけを辿る解決 (Python 3.13 以降の POSIX) は読み切って何も出さない。それ以外は realpath を移動のたびに
                # 呼ぶので、期限の注意になりうる
                if _FAST_JOIN:
                    self.assertIsNone(out)
                else:
                    self.assertIn(test_guard.decision(out), (None, "context"))

    def test_shape_past_the_deadline_returns_a_note(self):
        # 期限が無ければ hook の timeout を大きく超える形 (状態の数 × スタックの深さ。期限なしで 30 秒ほど)
        timeout = self._timeout()
        k = test_state_limit._limit_exponent()
        cmd = or_cd(k) + "".join(f"pushd /t/p{i}; " for i in range(8000)) + GIT
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
