"""0.6.0 の設定体系: on/off スイッチ・レビュアー選択・mode・完了通知。

env 未設定時に 0.5.0 と同じ挙動になること (回帰) を各項目で押さえる。
"""
import json
import os
import unittest

from _testutil import FENCED_CLEAN, FINDINGS, PLAN, HookTestCase

SESSION = "sess-settings"


class TestEnabledSwitch(HookTestCase):
    """`EXTERNAL_AI_PLAN_REVIEW` と `EXTERNAL_AI_REVIEW_MAX=0` は AND で効く。"""

    def test_unset_runs_review_as_before(self):
        """回帰: どちらも未設定なら従来どおりレビューして block する。"""
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))

    def test_plan_review_zero_disables(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW"] = "0"
        self.assertNotBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(self.cursor_calls, [], "無効化したのにレビュアーが走った")
        self.assertEqual(self.marker(SESSION), ("", 0), "枠を消費してはいけない")

    def test_legacy_review_max_zero_still_disables(self):
        """後方互換: 0.2.0 からの `EXTERNAL_AI_REVIEW_MAX=0` は新スイッチに上書きされない。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "0"
        os.environ["EXTERNAL_AI_PLAN_REVIEW"] = "1"
        self.assertNotBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(self.cursor_calls, [])

    def test_falsy_spellings(self):
        for value in ("0", "false", "off", "no", "OFF"):
            with self.subTest(value=value):
                os.environ["EXTERNAL_AI_PLAN_REVIEW"] = value
                self.assertEqual(self.entry.review_enabled(), False)

    def test_unparsable_value_keeps_default(self):
        """fail-open: 解釈できない値でレビューが黙って止まらない。"""
        os.environ["EXTERNAL_AI_PLAN_REVIEW"] = "maybe"
        self.assertTrue(self.entry.review_enabled())


class TestReviewerSelection(HookTestCase):
    """`EXTERNAL_AI_PLAN_REVIEW_REVIEWERS` は事前チェックと実行で同じ集合を使うこと。"""

    def test_unset_runs_both(self):
        """回帰: 未設定なら従来どおり両方走る。"""
        self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(len(self.codex_calls), 1)

    def test_single_reviewer_runs_alone(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_REVIEWERS"] = "cursor"
        data = self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.codex_calls, [], "外したレビュアーが走った")
        self.assertNotIn("Codex レビュー", data["reason"])

    def test_whitespace_and_case_are_tolerated(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_REVIEWERS"] = " CODEX , cursor "
        self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(len(self.codex_calls), 1)

    def test_unknown_only_is_noop_and_keeps_slot(self):
        """タイプミスで既定の全件に fallback しない。枠も消費しない。"""
        os.environ["EXTERNAL_AI_PLAN_REVIEW_REVIEWERS"] = "codx"
        output = self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.codex_calls, [])
        self.assertEqual(self.marker(SESSION), ("", 0), "枠を消費してはいけない")
        self.assertIn("未知のレビュアー名", json.loads(output)["systemMessage"])

    def test_unknown_alongside_known_still_runs_known(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_REVIEWERS"] = "cursor,gemini"
        data = self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.codex_calls, [])
        self.assertIn("gemini", data["systemMessage"])


class TestSlotExhaustionNotice(HookTestCase):
    """上限到達で黙って素通りしないこと (この batch が潰そうとしている「無言」そのもの)。"""

    def test_cap_reached_tells_the_user(self):
        """上限はプラン単位。同じプランへ非連続に戻ってきた
        ときに到達を通知する (別プランの block では到達しない)。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        plan_a = PLAN + "\nプラン A\n"
        plan_b = PLAN + "\nプラン B\n"
        self.assertBlocked(self.exitplan(SESSION, plan_a, FINDINGS, FINDINGS))
        self.assertBlocked(self.exitplan(SESSION, plan_b, FINDINGS, FINDINGS))  # 別プランは別枠

        output = self.exitplan(SESSION, plan_a, FINDINGS, FINDINGS)  # A に戻ると上限到達
        message = self.assertNotBlocked(output).get("systemMessage", "")
        self.assertIn("EXTERNAL_AI_REVIEW_MAX=1", message)
        self.assertIn("見送り", message)
        self.assertEqual(self.cursor_calls, [], "上限到達なのにレビュアーが走った")

    def test_same_plan_recheck_stays_quiet(self):
        """同一プランの再確認は結果が変わらないので黙る (通知がノイズにならない)。"""
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS), "")


class TestWhatTheBudgetActuallyCaps(HookTestCase):
    """`EXTERNAL_AI_REVIEW_MAX` が何の上限なのかを実装どおりに固定する。

    `main()` は指摘なし (`REVIEW_CLEAN`) / レビュアー失敗のとき `release_slot()` を呼び、
    count を戻して hash も消す。つまりこの上限が数えているのは **外部 AI の呼び出し回数
    ではなく「指摘ありで返ってきた回数」** (block モードなら差し戻した回数)。

    README がここを取り違えると、最悪待ち時間の見積もりごと嘘になるのでテストで縛る。
    """

    def test_clean_reviews_do_not_consume_the_budget(self):
        """指摘なしなら何プランでもレビューが走る (枠を戻すため)。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "2"
        for i in range(4):
            self.exitplan(SESSION, f"{PLAN}\n案 {i}\n", FENCED_CLEAN, FENCED_CLEAN)
            self.assertEqual(len(self.cursor_calls), 1, f"{i} 回目でレビューが走らなかった")
        self.assertEqual(self.marker(SESSION), ("", 0), "clean は枠も hash も残さない")

    def test_failed_reviews_do_not_consume_the_budget(self):
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "2"
        for i in range(3):
            self.exitplan(SESSION, f"{PLAN}\n案 {i}\n", None, None)
            self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.marker(SESSION), ("", 0))

    def test_only_findings_consume_the_budget(self):
        """指摘ありだけが枠を減らし、そのプラン自身が上限に達すると以後は見送る
        (同じプランへ非連続に戻ってきた場合)。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "2"
        plan = f"{PLAN}\n対象プラン\n"
        other = f"{PLAN}\n無関係な別プラン\n"

        self.assertBlocked(self.exitplan(SESSION, plan, FINDINGS, FENCED_CLEAN))
        self.assertBlocked(self.exitplan(SESSION, other, FINDINGS, FENCED_CLEAN))  # last をずらす
        self.assertBlocked(self.exitplan(SESSION, plan, FINDINGS, FENCED_CLEAN))
        self.assertEqual(
            self.marker_count_for(SESSION, plan), 2, "指摘ありが 2 回分カウントされていない"
        )

        self.assertBlocked(self.exitplan(SESSION, other, FINDINGS, FENCED_CLEAN))  # 再度ずらす
        output = self.exitplan(SESSION, plan, FINDINGS, FENCED_CLEAN)
        self.assertNotBlocked(output)
        self.assertEqual(self.cursor_calls, [], "上限到達後にレビューが走った")

    def test_different_plans_each_get_their_own_budget(self):
        """異なるプランは互いの block 回数を
        消費しない。0.6.0 までは単一のセッション累積カウンタだったため、無関係な
        プランを (この上限設定なら) 1 回 block すると以後**すべての**プランが
        レビュー無しで通っていた。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        for i in range(4):
            self.assertBlocked(
                self.exitplan(SESSION, f"{PLAN}\n案 {i}\n", FINDINGS, FENCED_CLEAN)
            )

    def test_clean_then_findings_still_has_full_budget(self):
        """clean が枠を食わないので、その後の別プランで所定回数ブロックできる。"""
        os.environ["EXTERNAL_AI_REVIEW_MAX"] = "1"
        self.exitplan(SESSION, f"{PLAN}\n案 A\n", FENCED_CLEAN, FENCED_CLEAN)
        self.assertBlocked(self.exitplan(SESSION, f"{PLAN}\n案 B\n", FINDINGS, FENCED_CLEAN))


class TestContextMode(HookTestCase):
    """`EXTERNAL_AI_PLAN_REVIEW_MODE=context` は差し戻さず所見だけ渡す。"""

    def test_default_mode_blocks(self):
        """回帰: 未設定なら従来どおり差し戻す
        (0.8.0 から hookSpecificOutput.permissionDecision: "deny" 形式。
        deprecated だった top-level decision:block は使わない — 検証は assertBlocked に集約)。
        """
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))

    def test_default_mode_does_not_use_deprecated_top_level_fields(self):
        """0.8.0 移行の固定: 公式 docs (`PreToolUse decision control`) は

        "PreToolUse previously used top-level decision and reason fields, but these are
        deprecated for this event. ... The deprecated values "approve" and "block" map to
        "allow" and "deny" respectively."

        と明記している。0.7.0 まではこの deprecated な top-level 形式 (`decision: "block"`)
        のまま動いていたが、0.8.0 で `hookSpecificOutput.permissionDecision: "deny"` +
        `permissionDecisionReason` に移行した。廃止した形式が復活しないことをここで固定する。
        """
        data = json.loads(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertNotIn("decision", data)
        self.assertNotIn("reason", data)
        specific = data["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn(
            "## クロスレビュー結果 (ExitPlanMode)", specific["permissionDecisionReason"]
        )
        self.assertNotIn(
            "additionalContext", specific, "deny 経路で additionalContext は不要"
        )

    def test_context_mode_injects_additional_context(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_MODE"] = "context"
        data = json.loads(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))

        self.assertNotIn("decision", data, "context モードで block してはいけない")
        specific = data["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        # `permissionDecision` を返さないことは安全性の要件。`"allow"` を返すと
        # ExitPlanMode の承認ゲートが飛び、利用者がプランを見ないまま実装に入る
        self.assertNotIn(
            "permissionDecision",
            specific,
            "承認判断を返してはいけない (プランの承認ゲートを飛ばしてしまう)",
        )
        self.assertIn("## クロスレビュー結果 (ExitPlanMode)", specific["additionalContext"])
        self.assertIn("差し戻していません", specific["additionalContext"])

    def test_context_mode_still_dedupes_same_plan(self):
        """同一プランの二重レビューは context モードでも起きない (枠は共通)。"""
        os.environ["EXTERNAL_AI_PLAN_REVIEW_MODE"] = "context"
        self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)
        self.assertEqual(len(self.cursor_calls), 0, "2 回目でレビュアーが再実行された")

    def test_clean_review_does_not_inject(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_MODE"] = "context"
        self.assertNotBlocked(self.exitplan(SESSION, PLAN, FENCED_CLEAN, FENCED_CLEAN))

    def test_unknown_mode_falls_back_to_block(self):
        os.environ["EXTERNAL_AI_PLAN_REVIEW_MODE"] = "whatever"
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))


class TestCompletionNotice(HookTestCase):
    """完了時に所要時間と結果を `systemMessage` で出す (無言でブロックしない)。"""

    def test_block_carries_summary_alongside_decision(self):
        data = self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FENCED_CLEAN))
        message = data["systemMessage"]
        self.assertIn("[exitplan-review]", message)
        self.assertIn("クロスレビュー完了", message)
        self.assertIn("cursor=指摘あり", message)
        self.assertIn("codex=clean", message)
        self.assertIn("プランを差し戻し", message)

    def test_clean_run_reports_without_blocking(self):
        data = self.assertNotBlocked(self.exitplan(SESSION, PLAN, FENCED_CLEAN, FENCED_CLEAN))
        self.assertIn("指摘なし", data["systemMessage"])
        self.assertIn("cursor=clean", data["systemMessage"])

    def test_failed_reviewer_is_reported_as_failure(self):
        data = self.assertNotBlocked(self.exitplan(SESSION, PLAN, None, None))
        self.assertIn("cursor=失敗", data["systemMessage"])

    def test_notice_never_contains_review_body(self):
        """通知に外部 AI の生出力を混ぜない (要約と件数のみ)。"""
        data = self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertNotIn("services/auth.py", data["systemMessage"])
        self.assertIn("services/auth.py", data["reason"], "本文側には残っていること")

    def test_elapsed_is_formatted(self):
        self.assertEqual(self.entry.notify.format_elapsed(0), "0秒")
        self.assertEqual(self.entry.notify.format_elapsed(59.9), "59秒")
        self.assertEqual(self.entry.notify.format_elapsed(60), "1分00秒")
        self.assertEqual(self.entry.notify.format_elapsed(252), "4分12秒")


if __name__ == "__main__":
    unittest.main()
