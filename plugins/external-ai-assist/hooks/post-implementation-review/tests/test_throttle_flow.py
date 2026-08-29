"""0.6.0 の頻度制御と完了通知。

- `EXTERNAL_AI_POST_REVIEW_MIN_LINES` / `_COOLDOWN_SEC` の見送りは **pending を消費しない**
- 完了時に所要時間と結果を `systemMessage` で出す
- env 未設定なら 0.5.0 と同じ挙動 (各項目に回帰テストを置く)
"""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION = "sess-throttle"
FINDINGS = "1. **直接影響** — 何か壊れる"


def _lines(count: int, tag: str = "x") -> str:
    return "".join(f"{tag}{i}\n" for i in range(count))


class TestMinLines(HookTestCase):
    """変更行数がしきい値未満のターンはレビューを見送る (typo 1 行で課金しない)。"""

    def test_unset_reviews_even_one_line(self):
        """回帰: 未設定 (既定 0) なら 1 行の変更でも従来どおりレビューする。"""
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

    def test_small_change_is_skipped_and_pending_survives(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "50"
        path = self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop(SESSION, "REVIEW_CLEAN"))

        self.assertNotReviewed()
        self.assertIn("EXTERNAL_AI_POST_REVIEW_MIN_LINES=50", message)
        self.assertIn("次のレビューにまとめます", message)
        self.assertEqual(self.pending(SESSION), [path], "見送りで pending を消費している")

    def test_accumulated_edits_eventually_cross_threshold(self):
        """見送った変更は捨てられず、後続の編集と合わせて 1 回でレビューされる。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "20"
        self.edit(SESSION, "a.py", _lines(3))
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertNotReviewed()

        self.edit(SESSION, "b.py", _lines(40))
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py", "b.py")
        self.assertEqual(self.pending(SESSION), [], "レビュー後は pending が空")

    def test_large_change_is_reviewed(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "20"
        self.edit(SESSION, "a.py", _lines(40))
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

    def test_invalid_value_falls_back_to_disabled(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "たくさん"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

    def test_counts_changed_lines_not_headers(self):
        count = self.entry._count_changed_lines(
            ["--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n context\n"]
        )
        self.assertEqual(count, 3)

    def test_comment_lines_with_space_are_counted(self):
        """`-- コメント` (SQL / Lua / Haskell) の削除は `--- コメント` になる。

        接頭辞 `"--- "` / `"+++ "` で弾くとここが 0 行になり、コメントだけ消した
        ターンが `MIN_LINES` に引っかかって黙って skip される。接頭辞では中身の行と
        ファイルヘッダを原理的に区別できないので、最初の `@@` 以降だけを数える。
        """
        count = self.entry._count_changed_lines(
            [
                "diff --git a/q.sql b/q.sql\nindex 111..222 100644\n"
                "--- a/q.sql\n+++ b/q.sql\n@@ -1,4 +1,3 @@\n"
                "--- 旧コメント A\n"  # `-- 旧コメント A` の削除
                "--- 旧コメント B\n"  # `-- 旧コメント B` の削除
                "+++ 新コメント C\n"  # `++ 新コメント C` の追加
                " unchanged\n"
            ]
        )
        self.assertEqual(count, 3)

    def test_multiple_hunks_and_no_hunk_sections(self):
        two_hunks = (
            "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
            "@@ -10,2 +10,2 @@\n-c\n+d\n"
        )
        self.assertEqual(self.entry._count_changed_lines([two_hunks]), 4)
        # binary 差分など hunk を持たない section は 0 行
        self.assertEqual(
            self.entry._count_changed_lines(["Binary files a/x.png and b/x.png differ\n"]), 0
        )

    def test_content_lines_starting_with_dashes_are_counted(self):
        """`--` で始まる中身の行を消すと `---foo` になる。ヘッダと取り違えないこと。

        取り違えると、CLI オプションの説明や SQL コメントだけを消した差分が 0 行と
        数えられ、MIN_LINES に引っかかって実質的な変更が黙って skip される。
        """
        count = self.entry._count_changed_lines(
            [
                "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n"
                "---- 旧コメント\n"
                "-- 残る行\n"
                "+++追加された行\n"
            ]
        )
        self.assertEqual(count, 3)

    def test_min_lines_uses_the_corrected_count(self):
        """`--` 始まりの行だけを消したターンが「変更 0 行」扱いにならないこと。

        削除行は `-` + `--quiet` = `---quiet` になる。ヘッダ (`--- a/path`) と
        取り違えると 0 行と数えられ、しきい値に引っかかって黙って skip される。
        """
        _testutil.write(self.repo, "flags.md", "--verbose\n--quiet\n--debug\n--trace\n")
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "flags")

        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "3"
        self.edit(SESSION, "flags.md", "--verbose\n")  # 3 行削除
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("flags.md")


class TestCooldown(HookTestCase):
    """前回レビューから一定時間はレビューを見送る (毎ターン発火の抑制)。"""

    def test_unset_reviews_every_turn(self):
        """回帰: 未設定 (既定 0) なら連続ターンでも従来どおり毎回レビューする。"""
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

        self.edit(SESSION, "b.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("b.py")

    def test_second_turn_within_cooldown_is_skipped(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"] = "600"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

        path = self.edit(SESSION, "b.py", "v1\n")
        message = self.assertNotBlocked(self.stop(SESSION, "REVIEW_CLEAN"))
        self.assertNotReviewed()
        self.assertIn("EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC", message)
        self.assertEqual(self.pending(SESSION), [path], "見送りで pending を消費している")

    def test_expired_cooldown_reviews_everything_at_once(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"] = "600"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")

        self.edit(SESSION, "b.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertNotReviewed()

        # cooldown 明け相当まで時計を進める
        with mock.patch.object(time, "time", return_value=time.time() + 601):
            self.edit(SESSION, "c.py", "v1\n")
            self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("b.py", "c.py")

    def test_no_notice_when_nothing_pending(self):
        """編集していないターンで cooldown 通知を出さない (通知がノイズにならないこと)。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"] = "600"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")

        self.assertEqual(self.stop(SESSION, "REVIEW_CLEAN"), "", "編集なしのターンで通知が出た")

    def test_failed_review_still_starts_cooldown(self):
        """失敗も外部 CLI を起動した事実に変わりないので cooldown の起点になる。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"] = "600"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, None)
        self.assertReviewed("a.py")

        self.edit(SESSION, "b.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_cooldown_is_per_session(self):
        """状態ファイルがセッション単位なので cooldown もセッション単位。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"] = "600"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop(SESSION, "REVIEW_CLEAN")

        self.edit("other-session", "b.py", "v1\n")
        self.stop("other-session", "REVIEW_CLEAN")
        self.assertReviewed("b.py")


class TestClaimSurvivesException(HookTestCase):
    """レビュー中の例外で claim を握ったまま落ちないこと。

    落ちると in-flight に残り、TTL (900s) が切れるまでこのセッションの変更が
    pending へ戻らない = 15 分間レビューが沈黙する。「hook が無言で詰まる」という
    この batch の対象そのものなので、例外は必ず復元経路を通す。
    """

    def test_exception_restores_pending_and_does_not_propagate(self):
        path = self.edit(SESSION, "a.py", "v1\n")
        # 不正な timeout が Popen に渡ったときに実際に出る例外
        output = self.stop_raising(SESSION, ValueError("timeout must be a number"))

        self.assertEqual(self.pending(SESSION), [path], "例外で claim が失われている")
        self.assertIn("レビューを完了できませんでした", output)

    def test_next_turn_reviews_the_restored_paths(self):
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_raising(SESSION, RuntimeError("boom"))

        self.stop(SESSION, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

    def test_in_flight_is_cleared_so_ttl_is_not_needed(self):
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_raising(SESSION, RuntimeError("boom"))

        import json as _json
        import os as _os

        state_file = _os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{SESSION}.json"
        )
        with open(state_file) as f:
            self.assertEqual(_json.load(f)["in_flight"], {}, "in-flight が残っている")


class TestCompletionNotice(HookTestCase):
    """最大 5 分ブロックした結果を利用者に伝える (stderr は debug log 止まり)。"""

    def test_clean_reports_elapsed_and_file_count(self):
        self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop(SESSION, "REVIEW_CLEAN"))
        self.assertIn("[post-implementation-review]", message)
        self.assertIn("差分レビュー完了", message)
        self.assertIn("1 ファイル", message)
        self.assertIn("指摘なし", message)

    def test_block_carries_summary_alongside_decision(self):
        self.edit(SESSION, "a.py", "v1\n")
        data = self.assertBlocked(self.stop(SESSION, FINDINGS))
        self.assertIn("指摘あり", data["systemMessage"])
        self.assertIn("直接影響", data["reason"], "本文は reason 側に残っていること")

    def test_notice_never_contains_review_body(self):
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("直接影響", data["systemMessage"], "通知にレビュー本文を混ぜない")

    def test_failure_is_reported(self):
        self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop(SESSION, None))
        self.assertIn("結果を取得できず", message)
        self.assertIn("持ち越し", message)

    def test_no_output_when_nothing_was_edited(self):
        """回帰: 編集 0 件のターンは従来どおり完全に無出力。"""
        self.assertEqual(self.stop(SESSION, "REVIEW_CLEAN"), "")
        self.assertNotReviewed()


class TestTimeoutSetting(HookTestCase):
    """`EXTERNAL_AI_POST_REVIEW_TIMEOUT` の解決規則。"""

    def test_unset_uses_module_default(self):
        self.assertEqual(self.cursor.timeout_sec(), self.cursor.TIMEOUT_SEC)

    def test_env_lowers_timeout(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_TIMEOUT"] = "45"
        self.assertEqual(self.cursor.timeout_sec(), 45)

    def test_env_is_clamped_to_ceiling(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_TIMEOUT"] = "99999"
        self.assertEqual(self.cursor.timeout_sec(), self.cursor.MAX_TIMEOUT_SEC)

    def test_invalid_falls_back_to_default(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_TIMEOUT"] = "5 分"
        self.assertEqual(self.cursor.timeout_sec(), self.cursor.TIMEOUT_SEC)


class TestDisableSwitch(HookTestCase):
    """既存の無効化スイッチが 0.6.0 でも同じ意味であること。"""

    def test_unset_is_enabled(self):
        for key in ("EXTERNAL_AI_POST_REVIEW", "EXTERNAL_AI_POST_REVIEW_MAX"):
            os.environ.pop(key, None)
        self.assertTrue(self.entry.review_enabled())

    def test_new_switch_disables(self):
        for value in ("0", "false", "off", "no"):
            with self.subTest(value=value):
                os.environ["EXTERNAL_AI_POST_REVIEW"] = value
                self.assertFalse(self.entry.review_enabled())

    def test_legacy_max_zero_still_disables(self):
        os.environ.pop("EXTERNAL_AI_POST_REVIEW", None)
        os.environ["EXTERNAL_AI_POST_REVIEW_MAX"] = "0"
        self.assertFalse(self.entry.review_enabled())

    def test_new_switch_wins_over_dead_legacy_alias(self):
        """`_MAX` は撤廃済みの死んだ別名なので、新しい変数の指定が勝つ。"""
        os.environ["EXTERNAL_AI_POST_REVIEW"] = "1"
        os.environ["EXTERNAL_AI_POST_REVIEW_MAX"] = "0"
        self.assertTrue(self.entry.review_enabled())

    def test_legacy_nonzero_does_not_limit(self):
        os.environ.pop("EXTERNAL_AI_POST_REVIEW", None)
        os.environ["EXTERNAL_AI_POST_REVIEW_MAX"] = "2"
        self.assertTrue(self.entry.review_enabled())


class TestOutputMode(HookTestCase):
    """`EXTERNAL_AI_POST_REVIEW_MODE` (0.8.0 新設)。

    既定は `context` (`hookSpecificOutput.additionalContext`、hook error に見せない)。
    `block` にすると 0.7.0 までの top-level `decision: "block"` に戻せる。公式 docs
    (`Stop decision control` 節) は、`additionalContext` でも `decision: "block"` と
    同じループ保護 (`stop_hook_active` / 8 連続上限) が働くと明記しているため、
    どちらのモードでも Claude 側の効果 (継続して reason を読む) は同じ。
    """

    def test_default_mode_uses_additional_context_not_decision(self):
        """回帰: 未設定なら 0.8.0 の新既定 (additionalContext) を使う。"""
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("decision", data)
        self.assertNotIn("reason", data)
        specific = data["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "Stop")
        self.assertIn("直接影響", specific["additionalContext"])

    def test_block_mode_restores_legacy_top_level_decision(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertEqual(data["decision"], "block")
        self.assertIn("直接影響", data["reason"])
        self.assertNotIn("hookSpecificOutput", data)

    def test_unknown_mode_falls_back_to_context(self):
        """タイプミスで既定 (additionalContext) から外れない (fail-open)。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "whatever"
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("decision", data)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "Stop")

    def test_mode_does_not_affect_clean_review(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        self.edit(SESSION, "a.py", "v1\n")
        self.assertNotBlocked(self.stop(SESSION, "REVIEW_CLEAN"))

    def test_default_mode_notice_does_not_claim_claude_was_asked(self):
        """回帰: 既定 (additionalContext) は CLI 2.1.163 未満で無視されうるため、
        systemMessage は「Claude に対応を依頼しました」と断定しない (内部バックログの指摘)。
        """
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("依頼しました", data["systemMessage"])

    def test_block_mode_notice_claims_claude_was_asked(self):
        """回帰: MODE=block はハーネスが継続を保証するので、
        systemMessage で「Claude に対応を依頼しました」と言い切ってよい。
        """
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        self.edit(SESSION, "a.py", "v1\n")
        data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertIn("依頼しました", data["systemMessage"])


class TestVersionAwareMode(HookTestCase):
    """`EXTERNAL_AI_POST_REVIEW_MODE` の auto 解決が Claude Code の版数を見て
    fail-closed すること (Codex R1 P1: 2.1.163 未満の CLI で `additionalContext` が
    無視され、`state.complete_claim` 済みの指摘が永久に失われる問題への対応)。

    版数検出そのもの (`_claude_code_version` の 3 段) の単体テストは
    `test_version_detect.py`。ここでは `_claude_code_version` を直接 mock して
    「版数が与えられたときにモード解決がどちらに転ぶか」だけを検証する
    (`HookTestCase` の既定 pin は `CLAUDE_CODE_VERSION=9.9.9` = 常に対応版数)。
    """

    def test_auto_mode_falls_back_to_block_when_version_unsupported(self):
        with mock.patch.object(self.entry, "_claude_code_version", return_value=(2, 1, 100)):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertEqual(data["decision"], "block")
        self.assertNotIn("hookSpecificOutput", data)
        self.assertIn("2.1.100", data["systemMessage"])
        self.assertIn("additionalContext 非対応のため block で差し戻し", data["systemMessage"])

    def test_auto_mode_falls_back_to_block_when_version_unknown(self):
        with mock.patch.object(self.entry, "_claude_code_version", return_value=None):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertEqual(data["decision"], "block")
        self.assertIn("不明", data["systemMessage"])

    def test_auto_mode_uses_context_when_version_supported(self):
        with mock.patch.object(self.entry, "_claude_code_version", return_value=(2, 1, 163)):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("decision", data)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "Stop")

    def test_auto_literal_behaves_like_unset(self):
        """`MODE=auto` を明示しても、未設定と同じ自動選択になる。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = self.entry.MODE_AUTO
        with mock.patch.object(self.entry, "_claude_code_version", return_value=(2, 1, 163)):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("decision", data)

    def test_explicit_block_mode_ignores_supported_version(self):
        """明示 `block` は版数が対応していても勝つ (版数判定を飛ばす)。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        with mock.patch.object(self.entry, "_claude_code_version", return_value=(9, 9, 9)):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertEqual(data["decision"], "block")

    def test_explicit_block_mode_omits_version_fallback_reason(self):
        """明示 `block` では、たとえ版数が非対応でも auto フォールバックの付記文を
        混ぜない (利用者が意図して選んだモードに「版数のせいで block」という
        言い訳を添えると矛盾して見える)。
        """
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        with mock.patch.object(self.entry, "_claude_code_version", return_value=None):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertIn("依頼しました", data["systemMessage"])
        self.assertNotIn("additionalContext 非対応のため", data["systemMessage"])

    def test_explicit_context_mode_ignores_unsupported_version(self):
        """明示 `context` は版数が非対応・不明でも勝つ (利用者の責任)。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "context"
        with mock.patch.object(self.entry, "_claude_code_version", return_value=None):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertNotIn("decision", data)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "Stop")

    def test_auto_mode_falls_back_to_block_when_version_cannot_be_detected(self):
        """版数検出そのもの (env var 不在・EXECPATH に版数無し・subprocess 失敗) と
        モード解決の結合テスト。`_claude_code_version` を直接 mock せず、実際の検出
        経路をすべて塞いで確認する (`test_version_detect.py` の単体テストと違い、
        ここは配線まで含めた end-to-end)。

        `subprocess.run` は `claude` 呼び出しだけを失敗させ、`gitscan.py` が使う git
        呼び出しは実行にそのまま通す (全体を潰すと git status/diff まで壊れて
        別の理由で失敗する)。
        """
        real_run = subprocess.run

        def fake_run(argv, *a, **kw):
            if list(argv)[:1] == ["claude"]:
                raise FileNotFoundError()
            return real_run(argv, *a, **kw)

        os.environ.pop("CLAUDE_CODE_VERSION", None)
        os.environ["CLAUDE_CODE_EXECPATH"] = (
            "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        )
        with mock.patch("subprocess.run", side_effect=fake_run):
            self.edit(SESSION, "a.py", "v1\n")
            data = json.loads(self.stop(SESSION, FINDINGS))
        self.assertEqual(data["decision"], "block")
        self.assertIn("不明", data["systemMessage"])

    def test_mode_does_not_affect_clean_review(self):
        with mock.patch.object(self.entry, "_claude_code_version", return_value=None):
            self.edit(SESSION, "a.py", "v1\n")
            self.assertNotBlocked(self.stop(SESSION, "REVIEW_CLEAN"))


if __name__ == "__main__":
    unittest.main()
