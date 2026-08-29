"""ExitPlanMode hook の block / 非 block 判定 (sentinel の扱いとマーカーの状態遷移)。

cursor / codex は起動せず `review()` をモックする。
"""
import os
import unittest

import _testutil
from _testutil import FENCED_CLEAN, FENCED_CLEAN_WITH_PREAMBLE, FINDINGS, PLAN, HookTestCase

SESSION = "sess-plan-0001"


class TestCleanSentinelDoesNotBlock(HookTestCase):
    """zh5.1: コードフェンス付き REVIEW_CLEAN (+ 前置き) を指摘扱いして block しない。"""

    def test_real_world_fenced_clean_with_preamble(self):
        output = self.exitplan(SESSION, PLAN, FENCED_CLEAN_WITH_PREAMBLE, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.marker(SESSION), ("", 0), "clean なら枠を戻すこと")
        self.assertIsNone(self.review_copy(SESSION))

    def test_fenced_clean_from_both_reviewers(self):
        output = self.exitplan(SESSION, PLAN, FENCED_CLEAN, FENCED_CLEAN)
        self.assertNotBlocked(output)
        self.assertEqual(self.marker(SESSION), ("", 0))

    def test_clean_plan_can_be_reviewed_again_after_edit(self):
        """clean で枠が戻るので、プランを直して再提出しても上限に当たらない。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        self.exitplan(SESSION, PLAN, FENCED_CLEAN_WITH_PREAMBLE, FENCED_CLEAN)
        self.exitplan(SESSION, PLAN + "\n3. ドキュメント更新\n", FENCED_CLEAN, FENCED_CLEAN)
        self.assertEqual(len(self.cursor_calls), 1, "2 回目のプランもレビューされること")


class TestFindingsBlock(HookTestCase):
    def test_findings_block_with_reviewer_header(self):
        output = self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN")
        data = self.assertBlocked(output)
        self.assertIn("## Cursor レビュー", data["reason"])
        self.assertNotIn("## Codex レビュー", data["reason"])
        self.assertIn("認可境界が未定義", data["reason"])

        saved_hash, count = self.marker(SESSION)
        self.assertEqual(saved_hash, self.entry.plan_hash(PLAN.strip()))
        self.assertEqual(count, 1, "block 確定時は枠を消費したままにする")
        self.assertIn("認可境界が未定義", self.review_copy(SESSION) or "")

    def test_clean_cursor_but_codex_findings_blocks_with_codex_only(self):
        output = self.exitplan(SESSION, PLAN, FENCED_CLEAN_WITH_PREAMBLE, FINDINGS)
        data = self.assertBlocked(output)
        self.assertIn("## Codex レビュー", data["reason"])
        self.assertNotIn("## Cursor レビュー", data["reason"])

    def test_sentinel_followed_by_findings_still_blocks(self):
        output = self.exitplan(SESSION, PLAN, "REVIEW_CLEAN\n\n" + FINDINGS, "REVIEW_CLEAN")
        self.assertBlocked(output)

    def test_both_reviewers_failed_is_fail_open(self):
        output = self.exitplan(SESSION, PLAN, None, None)
        self.assertNotBlocked(output)
        self.assertEqual(self.marker(SESSION), ("", 0), "失敗時も枠を戻すこと")

    def test_reviewer_exception_does_not_break_the_other(self):
        def boom(_plan):
            raise RuntimeError("cursor exploded")

        with unittest.mock.patch.object(self.cursor, "review", side_effect=boom):
            output = self.exitplan(SESSION, PLAN, None, FINDINGS)
        data = self.assertBlocked(output)
        self.assertIn("## Codex レビュー", data["reason"])


class TestMarkerStateMachine(HookTestCase):
    def test_same_plan_is_not_reviewed_twice_after_block(self):
        self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN")
        output = self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [], "同一 hash は再レビューしない")

    def test_block_limit_is_enforced(self):
        """上限はプラン (hash) 単位。別プランには波及しない。

        0.6.0 までは単一のセッション累積カウンタだったため、別プランの block でも
        この上限が消費され、以後すべてのプランがレビュー無しで通っていた。
        """
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        plan_a = PLAN
        plan_b = PLAN + "\n修正済み\n"
        self.assertBlocked(self.exitplan(SESSION, plan_a, FINDINGS, "REVIEW_CLEAN"))
        # 別プランは別枠を持つので、A が上限でも B はレビューされ block される
        self.assertBlocked(self.exitplan(SESSION, plan_b, FINDINGS, "REVIEW_CLEAN"))

        # A 自身に戻ってくると、A 自身の上限 (1) に達しているので見送り
        output = self.exitplan(SESSION, plan_a, FINDINGS, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [])
        self.assertIn("上限", self.last_stderr)

    def test_release_after_partial_consumption_keeps_count(self):
        """block (count 1) → 別プラン clean (reserve→release で count 0 に戻る) の後も、
        元のプラン自身の count は保たれていること。

        0.3.1 までは release 後の本文 `"\\n1"` を hash="1" / count=0 と誤読し、上限が
        1 回分リセットされていた (テキスト形式時代の回帰保護)。JSON 化後も
        「release は decrement であって reset ではない」という核心は変わらない。
        """
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "2"
        plan_a = PLAN + "A"
        plan_b = PLAN + "B"
        self.assertBlocked(self.exitplan(SESSION, plan_a, FINDINGS, "REVIEW_CLEAN"))
        self.assertNotBlocked(self.exitplan(SESSION, plan_b, FENCED_CLEAN, "REVIEW_CLEAN"))
        self.assertEqual(self.marker_count_for(SESSION, plan_a), 1, "A の枠が保たれていない")
        self.assertEqual(self.marker_count_for(SESSION, plan_b), 0, "clean は枠を残さない")

        # A 自身の累積 (1) に対してもう 1 回分ブロックできる (上限 2)
        self.assertBlocked(self.exitplan(SESSION, plan_a, FINDINGS, "REVIEW_CLEAN"))
        self.assertEqual(self.marker_count_for(SESSION, plan_a), 2)

        # 別プランを挟んでから A に 3 回目戻ると、A 自身の上限に達しているので見送り
        self.assertNotBlocked(self.exitplan(SESSION, plan_b, FENCED_CLEAN, "REVIEW_CLEAN"))
        output = self.exitplan(SESSION, plan_a, FINDINGS, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [], "A 自身の上限 2 回に達したらレビューしない")

    def test_sessions_have_independent_markers(self):
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN"))
        self.assertBlocked(self.exitplan("sess-other", PLAN, FINDINGS, "REVIEW_CLEAN"))


class TestEarlyReturns(HookTestCase):
    def test_other_tool_is_ignored(self):
        output = self.run_hook(
            {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "ls"}}
        )
        self.assertNotBlocked(output)
        self.assertEqual(self.marker(SESSION), ("", 0))

    def test_max_zero_disables_reviews(self):
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "0"
        output = self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.codex_calls, [])

    def test_no_reviewer_available_skips(self):
        with unittest.mock.patch.object(
            self.cursor, "is_available", return_value=False
        ), unittest.mock.patch.object(self.codex, "is_available", return_value=False):
            output = self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.marker(SESSION), ("", 0), "マーカーを作らないこと")

    def test_empty_plan_or_session_skips(self):
        self.assertNotBlocked(self.exitplan(SESSION, "   ", FINDINGS, FINDINGS))
        self.assertNotBlocked(self.exitplan("", PLAN, FINDINGS, FINDINGS))
        self.assertEqual(self.cursor_calls, [])

    def test_invalid_stdin_is_ignored(self):
        import io
        import sys
        from unittest import mock

        with mock.patch.object(sys, "stdin", io.StringIO("not json")), mock.patch.object(
            sys, "stdout", io.StringIO()
        ) as out, mock.patch.object(sys, "stderr", io.StringIO()):
            try:
                self.entry.main()
            except SystemExit:
                pass
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
