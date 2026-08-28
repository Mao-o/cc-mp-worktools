"""巨大セグメントの長さガード (0.23.0)。

stdlib ``shlex`` は 1 トークンが長くなると超線形になる (200KB 単一トークンの
総 479ms のうち 341ms が ``shlex.py::read_token``)。DESIGN.md 設計原則 4 の
「Latency <100ms 目標」に対し、実測で 200KB = 396ms / 600KB = 2,570ms
(hook の 2 秒 timeout 超過) だった。

上限を超えたセグメントは tokenize せず ``ask_or_allow`` に倒す。hook が時間内に
自ら decision を返すため、timeout で出力ごと破棄される無音 fail-open を避けられる。
"""
from __future__ import annotations

import time
import unittest

from handlers import bash_handler
from handlers.bash_handler import _MAX_COMMAND_CHARS, handle


def _envelope(command: str, mode: str = "default") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/tmp",
        "permission_mode": mode,
        "session_id": "seg-guard",
    }


def _verdict(out: dict) -> str:
    if not out:
        return "allow"
    h = out.get("hookSpecificOutput", out)
    return h.get("permissionDecision") or "allow"


class TestSegmentSizeGuard(unittest.TestCase):
    def test_under_limit_is_analyzed_normally(self):
        """上限未満は従来どおり operand scan に到達する。"""
        cmd = "echo '" + "A" * 1000 + "'"
        self.assertEqual(_verdict(handle(_envelope(cmd))), "allow")

    def test_over_limit_falls_back_to_ask(self):
        cmd = "echo '" + "A" * (_MAX_COMMAND_CHARS + 100) + "'"
        self.assertEqual(_verdict(handle(_envelope(cmd))), "ask")

    def test_over_limit_is_lenient_in_autonomous_mode(self):
        """ask_or_allow なので auto では allow (他の静的解析不能ケースと同じ)。"""
        cmd = "echo '" + "A" * (_MAX_COMMAND_CHARS + 100) + "'"
        self.assertEqual(_verdict(handle(_envelope(cmd, "auto"))), "allow")

    def test_reason_explains_the_length_limit(self):
        cmd = "echo '" + "A" * (_MAX_COMMAND_CHARS + 100) + "'"
        out = handle(_envelope(cmd))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("長さ上限", reason)

    def test_composite_under_limit_still_reaches_deny(self):
        """上限内なら複合コマンドの deny 検出は従来どおり働く。

        0.11.0 の segment 単位再評価 (1 セグメントが解析不能でも他セグメントの
        deny を拾う) が壊れていないことの回帰。
        """
        filler = "echo '" + "A" * 1000 + "'"
        cmd = f"{filler} && cat /tmp/.env"
        self.assertLess(len(cmd), _MAX_COMMAND_CHARS)
        self.assertEqual(_verdict(handle(_envelope(cmd))), "deny")

    def test_over_limit_composite_is_ask_even_with_sensitive_operand(self):
        """**意図したトレードオフ**: 上限超過の複合コマンドは deny に到達しない。

        その operand を見つけるには ``_split_command_on_operators`` と
        ``_has_hard_stop`` を全文字に対して走らせる必要があり、それ自体が
        2 秒予算を超える (2MiB で 2 パス合計 1,399ms、超線形)。timeout すると
        hook の出力ごと破棄されて **decision が消える** ため、
        「時間内に ask を返す」方が「時間切れで何も返さない」より強い。

        64KB 超のコマンドに機密 operand を同居させる形は「うっかり」の範疇を
        超えるので、思想 1 の射程外として受容する。
        """
        big = "echo '" + "A" * (_MAX_COMMAND_CHARS + 100) + "'"
        cmd = f"{big} && cat /tmp/.env"
        self.assertEqual(_verdict(handle(_envelope(cmd))), "ask")

    def test_guard_runs_before_expensive_lexing(self):
        """ガードが segmentation より前にあること (到達コストの回帰)。

        segment ループ内に置くと、そこへ到達する前に 2 パスで予算を使い切る。
        2MiB で 2 パス合計 1,399ms だったので、上限は十分下回る必要がある。
        """
        cmd = "echo '" + "A" * (2 * 1024 * 1024) + "'"
        start = time.perf_counter()
        result = handle(_envelope(cmd))
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertEqual(_verdict(result), "ask")
        self.assertLess(
            elapsed_ms, 200,
            f"2MiB のコマンドに {elapsed_ms:.0f}ms — "
            "ガードが高価なパスより後ろにある可能性",
        )

    def test_guard_avoids_superlinear_cost(self):
        """上限超過の入力が実用的な時間で返ること。

        修正前は 600KB で 2,570ms (2 秒 timeout 超過)。上限は環境差で flaky に
        ならないよう十分な余裕を取る。
        """
        cmd = "echo '" + "A" * (600 * 1024) + "'"
        start = time.perf_counter()
        handle(_envelope(cmd))
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertLess(
            elapsed_ms, 1000,
            f"600KB のセグメントに {elapsed_ms:.0f}ms — ガードが効いていない可能性",
        )

    def test_policy_failure_stays_fail_closed_regardless_of_length(self):
        """patterns 読込失敗は長さに関わらず全 mode deny (fail-closed) のまま。

        長さガードを load_patterns より前に置くと、64KB 超のコマンドに限って
        fail-closed を迂回し ask_or_allow (= lenient mode では allow) になる。
        README の Fail-closed 表が保証する挙動の回帰。
        """
        def boom(**kwargs):
            raise FileNotFoundError("patterns.txt")

        original = bash_handler.load_patterns
        bash_handler.load_patterns = boom
        try:
            big = "echo '" + "A" * (_MAX_COMMAND_CHARS + 100) + "'"
            for cmd in ("cat /tmp/x.txt", big):
                for mode in ("default", "auto", "bypassPermissions"):
                    with self.subTest(size=len(cmd), mode=mode):
                        self.assertEqual(
                            _verdict(handle(_envelope(cmd, mode))), "deny"
                        )
        finally:
            bash_handler.load_patterns = original

    def test_limit_is_documented_constant(self):
        """上限がマジックナンバーではなく名前付き定数であること。"""
        self.assertEqual(_MAX_COMMAND_CHARS, 64 * 1024)
        self.assertTrue(hasattr(bash_handler, "_MAX_COMMAND_CHARS"))


if __name__ == "__main__":
    unittest.main()
