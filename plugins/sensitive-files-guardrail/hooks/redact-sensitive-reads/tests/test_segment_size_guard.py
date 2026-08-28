"""巨大セグメントの長さガード (0.21.0)。

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
from handlers.bash_handler import _MAX_SEGMENT_CHARS, handle


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
        cmd = "echo '" + "A" * (_MAX_SEGMENT_CHARS + 100) + "'"
        self.assertEqual(_verdict(handle(_envelope(cmd))), "ask")

    def test_over_limit_is_lenient_in_autonomous_mode(self):
        """ask_or_allow なので auto では allow (他の静的解析不能ケースと同じ)。"""
        cmd = "echo '" + "A" * (_MAX_SEGMENT_CHARS + 100) + "'"
        self.assertEqual(_verdict(handle(_envelope(cmd, "auto"))), "allow")

    def test_reason_explains_the_length_limit(self):
        cmd = "echo '" + "A" * (_MAX_SEGMENT_CHARS + 100) + "'"
        out = handle(_envelope(cmd))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("長さ上限", reason)

    def test_other_segments_still_reach_deny(self):
        """巨大セグメントがあっても、他セグメントの deny 検出は続く。

        ガードは ``continue`` で pending_ask に積むだけで early return しない
        (0.11.0 の segment 単位再評価の設計を維持していること)。
        """
        big = "echo '" + "A" * (_MAX_SEGMENT_CHARS + 100) + "'"
        cmd = f"{big} && cat /tmp/.env"
        self.assertEqual(_verdict(handle(_envelope(cmd))), "deny")

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

    def test_limit_is_documented_constant(self):
        """上限がマジックナンバーではなく名前付き定数であること。"""
        self.assertEqual(_MAX_SEGMENT_CHARS, 64 * 1024)
        self.assertTrue(hasattr(bash_handler, "_MAX_SEGMENT_CHARS"))


if __name__ == "__main__":
    unittest.main()
