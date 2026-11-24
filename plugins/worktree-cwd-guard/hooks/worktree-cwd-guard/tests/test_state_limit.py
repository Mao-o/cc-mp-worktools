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

        def counted_cap(alts):
            out = cap(alts)
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
        return command.analyze(cmd, family.norm(A), self.fam)

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

    def test_documented_limit_matches(self):
        root = Path(__file__).resolve().parents[3]
        self.assertIn(f"が {command._STATE_LIMIT} を超える", (root / "README.md").read_text(encoding="utf-8"))
        self.assertIn(f"上限 ({command._STATE_LIMIT})", (root / "AGENTS.md").read_text(encoding="utf-8"))

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
