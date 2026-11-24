"""読み取りの時間: パスの深さに比例する解決 (`command._join_resolved`) と、読み取りの期限 (`command._READ_SECONDS`)。

相対パスの `cd a;` を続けると、行き先のパスが深くなる。realpath はパスの要素を先頭から lstat し直すので、移動のたびに呼ぶと深さの
2 乗の時間になり、hook が時間切れになるとコマンドはそのまま走る (0.9.3 まで。`cd a;` 2,000 回で約 6 秒、`x || cd d<i>;` 6 回の後に
`cd a;` 400 回で約 17 秒)。正規化済みの基点からの差分だけを辿るようにし、それでも遅い形 (状態の数 × スタックの深さなど) は、
読み取りの期限で閉じる (期限を過ぎたら、それまでの判定に注意を足して返す)。
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

_POSIX_REALPATH = os.path is posixpath  # 差分だけを辿る解決は POSIX の realpath の規則 (Windows は期限だけで閉じる)


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


@unittest.skipUnless(_POSIX_REALPATH, "差分だけを辿る解決は POSIX の realpath の規則で、Windows は realpath をそのまま呼ぶ")
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

    @unittest.skipUnless(_POSIX_REALPATH, "Windows の realpath は要素ごとに辿らないので、期限だけで閉じる (DeadlineTest)")
    def test_relative_cd_chain(self):
        # 区切りの空白の有無、引用した語、`..` / `.` を含む語でも同じ
        for unit, small, large in (("cd a; ", 1000, 4000), ("cd a;", 1000, 4000), ('cd "a b"; ', 1000, 4000),
                                   ("cd a/./b/../c; ", 500, 2000)):
            with self.subTest(unit=unit):
                self._assert_linear(lambda k, unit=unit: unit * k + GIT, small, large, None)

    @unittest.skipUnless(_POSIX_REALPATH, "Windows の realpath は要素ごとに辿らないので、期限だけで閉じる (DeadlineTest)")
    def test_two_part_sweep_at_the_state_limit(self):
        # 前半で状態を上限ちょうど (まとめない) に固定し、後半で相対パスを深くし続ける。状態の数が係数に掛かる
        k = test_state_limit._limit_exponent()
        self._assert_linear(lambda m: or_cd(k) + cd_a(m) + GIT, 200, 800, None, peak=command._STATE_LIMIT)
        # 終わらない here-document を付け、安全網の読み直し (旧来の読みと 8 回の読み直し) の倍率も掛ける。git の成否で状態が
        # 倍になるので前半を 1 つ減らす (読み直しの読みは here-document の行の `cat` でまとめるので、まとめる回数は数で固定しない)
        self._assert_linear(
            lambda m: or_cd(k - 1) + cd_a(m) + GIT + heredocs(9), 50, 200, None, peak=command._STATE_LIMIT, collapses=None
        )

    @unittest.skipUnless(_POSIX_REALPATH, "Windows の realpath は要素ごとに辿らないので、期限だけで閉じる (DeadlineTest)")
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
        # zsh の読みと bash の 2 つの読みで移動を 3 つずつ (計 9) 読み、期限 (10) の手前で読み終える
        cmd = f'{cd_a(3)}git -C "{B}" {RESET}'
        out = self.findings(cmd)
        self.assertEqual([f.root for f in out], [family.norm(B)])
        self.assertEqual(self._note(out), [])
        self.assertIsNone(self.verdict("git status"))

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
                # POSIX は読み切って何も出さない。Windows は realpath が要素ごとに辿らないので、期限の注意になりうる
                if _POSIX_REALPATH:
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
