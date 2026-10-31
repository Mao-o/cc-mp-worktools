"""応答の途中のレビュー (0.16.0。既定オフ) の受け入れ基準テスト。

PostToolUse の背景 hook (`--phase mid-turn`, `asyncRewake`) が、pending の diff が閾値を
超えたときだけレビューに出し、Stop と並走しないこと。設計は CLAUDE.md の
「応答の途中のレビュー (0.16.0)」節。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION_A = "sess-aaaa"
SESSION_B = "sess-bbbb"
FINDING = "1. **critical** — off-by-one in loop bound"
ON = {"EXTERNAL_AI_POST_REVIEW_MIDTURN": "1"}


class MidTurnTestCase(HookTestCase):
    def mid_turn(self, session_id: str, results, *, inside=None) -> tuple[int, str, str]:
        """mid-turn phase を 1 回起動する。返り値は (exit code, stderr, stdout)。

        `results` は送信順の `cursor.review()` の戻り値 (足りなければ最後の値)。
        `inside(n)` は n 回目の送信の最中に呼ばれる (並走の再現用)。
        """
        answers = list(results) if isinstance(results, list) else [results]
        calls: list[str] = []

        def fake_review(diff_text: str, *, cwd: str | None = None):
            calls.append(diff_text)
            if inside is not None:
                inside(len(calls))
            return answers.pop(0) if len(answers) > 1 else answers[0]

        stdin = io.StringIO(json.dumps({"session_id": session_id, "cwd": self.repo}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cursor, "review", side_effect=fake_review), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            code = self.entry.main(["--phase", "mid-turn"])
        self.midturn_calls = calls
        return code, err.getvalue(), out.getvalue()

    def big_edit(self, session_id: str, stem: str = "big") -> list[str]:
        """24 KiB のファイルを 2 つ書く (送る diff の合計が 40 KB を超え、塊 2 つになる)。

        1 ファイルにしないのは、32 KB 超の diff は先頭だけを送る (= 送る量が閾値に届かない)
        ため。
        """
        return [
            self.edit(session_id, f"{stem}-{i}.txt", _testutil.content_kib(24, f"{stem}{i}"))
            for i in (1, 2)
        ]

    def raw_state(self, session_id: str) -> dict:
        path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{session_id}.json"
        )
        with open(path) as f:
            return json.load(f)


class TestDisabledByDefault(MidTurnTestCase):
    """既定オフ: 閾値を超えても何もしない (外部 AI CLI の起動回数を増やさない)。"""

    def test_unset_does_not_review_over_threshold(self):
        paths = self.big_edit(SESSION_A)
        code, err, out = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.midturn_calls, [])
        self.assertEqual(paths, self.pending(SESSION_A), "pending を消費してはいけない")

    def test_unset_does_not_touch_state(self):
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertFalse(
            os.path.exists(os.path.join(self.tmpdir, "post-implementation-review")),
            "オフのときは state ディレクトリも作らない",
        )

    def test_hook_switch_off_wins_over_midturn(self):
        """`EXTERNAL_AI_POST_REVIEW=0` なら、MIDTURN=1 で pending が閾値を超えていても送らない。"""
        paths = self.big_edit(SESSION_A)
        env = dict(ON, EXTERNAL_AI_POST_REVIEW="0")
        with mock.patch.dict(os.environ, env):
            code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(code, 0)
        self.assertEqual(self.midturn_calls, [])
        self.assertEqual(paths, self.pending(SESSION_A))

    def test_unset_stop_does_not_merge_stored_notices(self):
        """オフのときの Stop は 0.15.x と同じ出力 (応答の途中の通知を読まない)。"""
        self.edit(SESSION_A, "a.txt", "a\n")
        self.state.add_midturn_notice(SESSION_A, "残っていた通知")
        message = self.assertNotBlocked(self.stop(SESSION_A, "REVIEW_CLEAN"))
        self.assertNotIn("応答の途中のレビュー", message)


class TestThreshold(MidTurnTestCase):
    """閾値を超えたときだけ背景でレビューに出す。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_below_default_threshold_is_not_reviewed(self):
        path = self.edit(SESSION_A, "small.txt", _testutil.content_kib(4))
        code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(code, 0)
        self.assertEqual(self.midturn_calls, [])
        self.assertIn(path, self.pending(SESSION_A))

    def test_over_default_threshold_is_reviewed_and_claimed(self):
        self.big_edit(SESSION_A)
        code, err, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(code, 0, "指摘なしでは Claude を起こさない")
        self.assertEqual(len(self.midturn_calls), 2, "約 50 KB は 40 KB の塊 2 つ")
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertNotIn("実装直後レビュー結果", err)
        # レビュー済みになったので、直後の Stop は同じ内容を送らない
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_threshold_boundary_is_inclusive(self):
        self.edit(SESSION_A, "a.txt", _testutil.content_kib(2))
        size = self.entry._pending_diff_bytes(SESSION_A, self.repo)
        self.assertGreater(size, 0)
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MIDTURN_BYTES": str(size + 1)}):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
            self.assertEqual(self.midturn_calls, [], "閾値未満")
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MIDTURN_BYTES": str(size)}):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
            self.assertEqual(len(self.midturn_calls), 1, "閾値ちょうどで送る")

    def test_zero_threshold_falls_back_to_default(self):
        self.edit(SESSION_A, "a.txt", _testutil.content_kib(4))
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MIDTURN_BYTES": "0"}):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [], "0 は「毎回送る」ではなく既定")

    def test_excluded_files_do_not_count(self):
        """閾値は「送る量」で測る。除外ファイルの大きな diff では起動しない。"""
        self.big_edit(SESSION_A, "credentials")
        self.assertEqual(self.entry._pending_diff_bytes(SESSION_A, self.repo), 0)
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [])

    def test_already_reviewed_content_does_not_count(self):
        self.big_edit(SESSION_A)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        # 内容を変えずに書き直す (pending には再び積まれるが、hash はレビュー済み)
        self.big_edit(SESSION_A)
        self.assertEqual(self.entry._pending_diff_bytes(SESSION_A, self.repo), 0)
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [])

    def test_other_sessions_edits_do_not_count(self):
        self.big_edit(SESSION_B)
        self.edit(SESSION_A, "a.txt", "small\n")
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [])

    def test_cooldown_holds_mid_turn_review(self):
        self.big_edit(SESSION_A)
        self.state.mark_review_done(SESSION_A)
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC": "600"}):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [])
        self.assertTrue(self.pending(SESSION_A))

    def test_running_flag_is_cleared_after_review(self):
        self.big_edit(SESSION_A)
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertFalse(self.state.midturn_running(SESSION_A))


class TestDelivery(MidTurnTestCase):
    """指摘は exit 2 + stderr (asyncRewake)。要約は次の Stop の systemMessage に回す。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_findings_wake_claude_with_reason_only(self):
        self.big_edit(SESSION_A)
        code, err, out = self.mid_turn(SESSION_A, FINDING)
        self.assertEqual(code, self.entry.REWAKE_EXIT_CODE)
        self.assertEqual(out, "", "stdout には何も出さない")
        self.assertIn("## 実装直後レビュー結果 (Cursor, 差分レビュー, 応答の途中)", err)
        self.assertIn(FINDING, err)
        self.assertNotIn("[post-implementation-review]", err, "debug log を Claude に見せない")

    def test_findings_in_block_mode_are_delivered_too(self):
        self.big_edit(SESSION_A)
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MODE": "block"}):
            code, err, _ = self.mid_turn(SESSION_A, FINDING)
        self.assertEqual(code, self.entry.REWAKE_EXIT_CODE)
        self.assertIn(FINDING, err)

    def test_clean_result_writes_logs_but_not_reason(self):
        self.big_edit(SESSION_A)
        code, err, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(code, 0)
        self.assertIn("[post-implementation-review]", err, "exit 0 では log を残す")

    def test_failure_restores_pending(self):
        paths = self.big_edit(SESSION_A)
        code, _, _ = self.mid_turn(SESSION_A, None)
        self.assertEqual(code, 0)
        self.assertEqual(paths, self.pending(SESSION_A))

    def test_summary_is_shown_at_the_next_stop(self):
        self.big_edit(SESSION_A)
        self.mid_turn(SESSION_A, FINDING)
        self.edit(SESSION_A, "later.txt", "later\n")
        message = self.assertNotBlocked(self.stop(SESSION_A, "REVIEW_CLEAN"))
        self.assertIn("応答の途中のレビュー: 差分レビュー完了", message)
        self.assertIn("指摘あり", message)
        self.assertEqual(message.count("差分レビュー完了"), 2, "Stop 自身の要約も残す")
        self.assertReviewed("later.txt")
        self.assertNotIn("big-1.txt", self.review_calls[0])
        # 1 回出したら消える
        self.edit(SESSION_A, "later2.txt", "later2\n")
        message = self.assertNotBlocked(self.stop(SESSION_A, "REVIEW_CLEAN"))
        self.assertNotIn("応答の途中のレビュー", message)

    def test_exclusion_notice_is_not_lost(self):
        """除外は恒久 (pending に戻らない) ので、通知を Stop に運ばないと黙って消える。"""
        self.big_edit(SESSION_A)
        self.edit(SESSION_A, ".env", "TOKEN=x\n")
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertTrue(output, "直後の Stop が除外の通知を出していない")
        self.assertIn(".env", json.loads(output).get("systemMessage", ""))

    def test_notice_store_is_capped(self):
        for i in range(self.state.MAX_MIDTURN_NOTICES + 3):
            self.state.add_midturn_notice(SESSION_A, f"n{i}")
        stored = self.state.take_midturn_notices(SESSION_A)
        self.assertEqual(len(stored), self.state.MAX_MIDTURN_NOTICES)
        self.assertEqual(stored[-1], f"n{self.state.MAX_MIDTURN_NOTICES + 2}")
        self.assertEqual(self.state.take_midturn_notices(SESSION_A), [])


class TestScriptExitCode(MidTurnTestCase):
    """スクリプトの外枠が main の exit code をそのまま返し、例外では 0 に倒す。"""

    def test_exit_code_is_propagated(self):
        with mock.patch.object(self.entry, "main", return_value=2):
            self.assertEqual(self.entry._run_as_script(), 2)

    def test_exception_falls_back_to_zero(self):
        err = io.StringIO()
        with mock.patch.object(self.entry, "main", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stderr", err):
            self.assertEqual(self.entry._run_as_script(), 0)
        self.assertIn("boom", err.getvalue())

    def test_held_log_is_written_when_mid_turn_raises(self):
        def boom(_payload):
            self.entry.log("raise の前の log")
            raise RuntimeError("boom")

        err = io.StringIO()
        stdin = io.StringIO(json.dumps({"session_id": SESSION_A, "cwd": self.repo}))
        with mock.patch.object(self.entry, "handle_mid_turn", side_effect=boom), \
                mock.patch("sys.argv", ["hook", "--phase", "mid-turn"]), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stderr", err):
            self.assertEqual(self.entry._run_as_script(), 0)
        self.assertIn("raise の前の log", err.getvalue())
        self.assertIn("boom", err.getvalue())

    def test_other_phases_return_zero(self):
        self.edit(SESSION_A, "a.txt", "a\n")
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(json.dumps({"session_id": SESSION_A, "cwd": self.repo}))
        with mock.patch.object(self.cursor, "review", return_value=FINDING), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            self.assertEqual(self.entry.main(["--phase", "stop"]), 0)
        self.assertBlocked(out.getvalue())


class TestNoConcurrencyWithStop(MidTurnTestCase):
    """cursor lock で Stop と直列化し、Stop が来たら残りを引き継ぐ。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mid_turn_skips_while_lock_held(self):
        paths = self.big_edit(SESSION_A)
        with self.state.cursor_lock(self.repo) as acquired:
            self.assertTrue(acquired)
            code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(code, 0)
        self.assertEqual(self.midturn_calls, [])
        self.assertEqual(paths, self.pending(SESSION_A))

    def test_stop_during_mid_turn_hands_off_the_rest(self):
        self.big_edit(SESSION_A)
        seen: dict = {}

        def during(n: int) -> None:
            if n == 3:
                seen["running_in_handoff"] = self.state.midturn_running(SESSION_A)
            if n != 1:
                return
            # 応答の途中のレビューが走っている間に、応答が終わって Stop が来る
            self.edit(SESSION_A, "rest.txt", "rest of the response\n")
            seen["stop_output"] = self.stop(SESSION_A, FINDING)
            seen["stop_calls"] = list(self.review_calls)

        code, err, _ = self.mid_turn(SESSION_A, ["REVIEW_CLEAN", "REVIEW_CLEAN", FINDING],
                                     inside=during)
        self.assertIn("stop_calls", seen, "応答の途中のレビューが走っていない")
        self.assertEqual(seen["stop_calls"], [], "Stop はレビューを並走させない")
        message = self.assertNotBlocked(seen["stop_output"])
        self.assertIn(self.entry.NOTICE_HANDED_OFF, message)
        self.assertEqual(len(self.midturn_calls), 3, "閾値超えの 2 塊 + 引き継いだ残り 1 回")
        self.assertIn("rest.txt", self.midturn_calls[2])
        self.assertNotIn("big-1.txt", self.midturn_calls[2])
        self.assertNotIn("big-2.txt", self.midturn_calls[2])
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertEqual(code, self.entry.REWAKE_EXIT_CODE, "残りの指摘も Claude に届ける")
        self.assertIn("## 実装直後レビュー結果 (Cursor, 差分レビュー)", err)
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])
        self.assertTrue(seen["running_in_handoff"], "引き継いだ分を送る間も印を立てておく")
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_below_threshold_measurement_does_not_hold_the_lock(self):
        """閾値未満の計測はロックを握らない (同じ Bash の commit レビューを見送らせない)。"""
        self.edit(SESSION_A, "small.txt", "x\n")
        seen: list[bool] = []
        real = self.entry._pending_diff_bytes

        def measuring(*args):
            with self.state.cursor_lock(self.repo) as got:
                seen.append(got)
            return real(*args)

        with mock.patch.object(self.entry, "_pending_diff_bytes", side_effect=measuring):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(seen, [True], "計測中に別の取得者がロックを取れない")
        self.assertEqual(self.midturn_calls, [])
        self.assertFalse(self.state.midturn_running(SESSION_A), "閾値未満では印を立てない")

    def test_remeasures_under_the_lock(self):
        """印を立ててからロックを取るまでに Stop が送り終えたら、残りが閾値未満なら送らない。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        real_begin = self.state.begin_midturn

        def begin(sid):
            run_id = real_begin(sid)
            if "stop" not in seen:
                # 印は立った・ロックはまだ: ここで Stop が来てロックを取る
                seen["stop"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                seen["stop_calls"] = list(self.review_calls)
                self.edit(SESSION_A, "small.txt", "x\n")
            return run_id

        with mock.patch.object(self.state, "begin_midturn", side_effect=begin):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(seen["stop_calls"]), 2, "Stop が自分でロックを取って 2 塊を送る")
        self.assertEqual(self.midturn_calls, [], "測り直すと閾値未満")
        self.assertEqual([os.path.join(self.repo, "small.txt")], self.pending(SESSION_A))

    def test_window_a_stop_right_after_lock_acquired(self):
        """ロックを取った直後 (claim の前) に Stop が来ても、Stop は印を見て引き継ぐ。"""
        self.big_edit(SESSION_A)
        self.edit(SESSION_A, "rest.txt", "rest\n")
        seen: dict = {}
        real_lock = self.state.cursor_lock
        from contextlib import contextmanager

        @contextmanager
        def lock(root):
            with real_lock(root) as acquired:
                if acquired and "stop" not in seen:
                    seen["stop"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                    seen["stop_calls"] = list(self.review_calls)
                yield acquired

        with mock.patch.object(self.state, "cursor_lock", side_effect=lock):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(seen["stop_calls"], [])
        self.assertIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(seen["stop"]))
        self.assertTrue(any("rest.txt" in call for call in self.midturn_calls))
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])

    def test_window_b_stop_after_flag_cleared(self):
        """印を下ろした直後に Stop が来たら、ロックはもう放されていて Stop が自分で見る。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        real_end = self.state.end_midturn

        def end(sid, run_id):
            real_end(sid, run_id)
            if "stop" not in seen:
                self.edit(SESSION_A, "rest.txt", "rest\n")
                seen["stop"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                seen["stop_calls"] = list(self.review_calls)

        with mock.patch.object(self.state, "end_midturn", side_effect=end):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(self.midturn_calls), 2)
        self.assertIn("stop_calls", seen, "印が下ろされていない")
        self.assertEqual(len(seen["stop_calls"]), 1, "Stop が残りを自分で見る")
        self.assertIn("rest.txt", seen["stop_calls"][0])
        self.assertEqual(self.pending(SESSION_A), [])

    def test_window_b_stop_retries_when_flag_is_gone(self):
        """Stop の 1 回目が失敗した後、印を読む前に持ち主が印を下ろしロックを放したら、
        Stop は依頼を書かずにもう 1 回だけ試して自分で見る。"""
        self.edit(SESSION_A, "rest.txt", "rest\n")
        stack = contextlib.ExitStack()
        self.assertTrue(stack.enter_context(self.state.cursor_lock(self.repo)))
        owner = self.state.begin_midturn(SESSION_A)
        real_running = self.state.midturn_running

        def running(sid):
            # 持ち主が終わる: 印を下ろす → ロックを放す → 依頼を読む (無い)
            self.state.end_midturn(sid, owner)
            stack.close()
            self.assertFalse(self.state.take_stop_handoff(sid))
            return real_running(sid)

        with mock.patch.object(self.state, "midturn_running", side_effect=running):
            output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("rest.txt")
        self.assertNotIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(output))
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])

    def test_stop_retry_reviews_itself_when_lock_was_released(self):
        """依頼を書いた後の再試行でロックが取れたら、Stop が自分で残りを見る。"""
        self.edit(SESSION_A, "a.txt", "a\n")
        self.state.begin_midturn(SESSION_A)
        real = self.entry._stop_review_under_lock
        attempts: list[int] = []

        def first_fails(*args):
            attempts.append(1)
            return None if len(attempts) == 1 else real(*args)

        with mock.patch.object(self.entry, "_stop_review_under_lock", side_effect=first_fails):
            output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(attempts), 2)
        self.assertReviewed("a.txt")
        self.assertNotIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(output))
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"], "取った側が依頼を消す")

    def test_lock_held_by_another_session_keeps_the_old_silent_skip(self):
        self.edit(SESSION_A, "a.txt", "a\n")
        with self.state.cursor_lock(self.repo) as acquired:
            self.assertTrue(acquired)
            output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(output, "")
        self.assertNotReviewed()
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])

    def test_stale_handoff_does_not_force_an_extra_review(self):
        """Stop が自分でロックを取ったら依頼を消す (次の応答の途中のレビューが閾値を
        無視して余分に走らない)。"""
        self.edit(SESSION_A, "a.txt", "a\n")
        self.state.request_stop_handoff(SESSION_A)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])
        self.edit(SESSION_A, "b.txt", "b\n")
        self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(self.midturn_calls, [])
        self.assertIn(os.path.join(self.repo, "b.txt"), self.pending(SESSION_A))

    def test_handoff_without_lock_is_left_to_the_holder(self):
        """引き継ぎの再試行でロックが取れなければ、何も送らない (持ち主が見る)。"""
        self.big_edit(SESSION_A)
        real_lock = self.state.cursor_lock
        entered: list[int] = []
        from contextlib import contextmanager

        @contextmanager
        def _held():
            yield False

        def lock(root):
            entered.append(1)
            if len(entered) == 1:
                return real_lock(root)
            return _held()

        def during(n: int) -> None:
            if n == 1:
                self.edit(SESSION_A, "rest.txt", "rest\n")
                self.state.request_stop_handoff(SESSION_A)

        with mock.patch.object(self.state, "cursor_lock", side_effect=lock):
            code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN", inside=during)
        self.assertEqual(code, 0)
        self.assertEqual(len(entered), 2, "依頼を読んでロックを取りに行った")
        self.assertEqual(len(self.midturn_calls), 2, "閾値超えの 2 塊だけ")
        self.assertIn(os.path.join(self.repo, "rest.txt"), self.pending(SESSION_A))
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_handoff_is_read_even_when_the_first_lock_fails(self):
        """1 回目のロックが取れなくても依頼は読む (取れた側が残りを見る)。"""
        self.big_edit(SESSION_A)
        self.state.request_stop_handoff(SESSION_A)
        real_lock = self.state.cursor_lock
        entered: list[int] = []
        from contextlib import contextmanager

        @contextmanager
        def _held():
            yield False

        def lock(root):
            entered.append(1)
            return _held() if len(entered) == 1 else real_lock(root)

        with mock.patch.object(self.state, "cursor_lock", side_effect=lock):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(self.midturn_calls), 2, "引き継ぎで残り (2 塊) を送る")
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])

    def test_stale_running_flag_is_ignored(self):
        self.edit(SESSION_A, "a.txt", "a\n")
        old = time.time() - self.state.MIDTURN_STALE_SEC - 5
        with mock.patch.object(self.state.time, "time", return_value=old):
            self.state.begin_midturn(SESSION_A)
        self.assertFalse(self.state.midturn_running(SESSION_A))


class TestParallelMidTurnRunsInOneSession(MidTurnTestCase):
    """同じセッションで応答の途中のレビューが 2 本並列に走っても、先に終わった方が、
    もう一方の実行中の印を下ろさない (印は実行ごとに持つ)。

    ロックを持つ X がレビューしている間に、ロックを取れなかった Y が印を立てて下ろす。
    その後に来た Stop は、X がまだレビュー中なので引き継ぎに入ること。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_y_failing_the_lock_does_not_end_the_stop_handoff(self):
        self.big_edit(SESSION_A)
        seen: dict = {}

        def during(n: int) -> None:
            if n != 1:
                return
            # Y: 閾値以上の pending があり、ロックは X が握っているので取れずに終わる
            self.big_edit(SESSION_A, "more")
            y_code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN")
            seen["y_code"] = y_code
            seen["y_calls"] = list(self.midturn_calls)
            seen["running_after_y"] = self.state.midturn_running(SESSION_A)
            self.edit(SESSION_A, "rest.txt", "rest of the response\n")
            seen["stop_output"] = self.stop(SESSION_A, FINDING)
            seen["stop_calls"] = list(self.review_calls)

        code, _, _ = self.mid_turn(SESSION_A, "REVIEW_CLEAN", inside=during)
        self.assertIn("stop_calls", seen, "X のレビューの最中に Y と Stop を挟めていない")
        self.assertEqual((seen["y_code"], seen["y_calls"]), (0, []), "Y はロックを取れず何も送らない")
        self.assertTrue(seen["running_after_y"], "Y が終わっても X の印は残る")
        self.assertEqual(seen["stop_calls"], [], "Stop はレビューを並走させない")
        message = self.assertNotBlocked(seen["stop_output"])
        self.assertIn(self.entry.NOTICE_HANDED_OFF, message, "Stop が黙って skip した")
        self.assertEqual(code, 0)
        sent = "".join(self.midturn_calls)
        for name in ("big-1.txt", "more-1.txt", "more-2.txt", "rest.txt"):
            self.assertIn(name, sent, f"{name} が誰にも送られていない")
        self.assertEqual(self.pending(SESSION_A), [], "引き継いだ X が残りを送り切る")
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_stop_after_x_released_the_lock_reviews_itself_despite_y(self):
        """X がロックを放した後・印を下ろす前に Y が終わっても、Stop はロックを取れて自分で見る。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        real_take = self.state.take_stop_handoff

        def take(sid):
            if "stop" not in seen:
                y = self.state.begin_midturn(sid)
                self.state.end_midturn(sid, y)
                self.edit(SESSION_A, "rest.txt", "rest\n")
                seen["stop"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                seen["stop_calls"] = list(self.review_calls)
            return real_take(sid)

        with mock.patch.object(self.state, "take_stop_handoff", side_effect=take):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(seen["stop_calls"]), 1, "ロックは空いているので Stop が自分で見る")
        self.assertIn("rest.txt", seen["stop_calls"][0])
        self.assertNotIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(seen["stop"]))
        self.assertEqual(self.pending(SESSION_A), [])

    def test_the_mark_is_released_even_when_the_review_raises(self):
        """finally が自分の id を下ろす (例外で抜けても印を残さない)。"""
        self.big_edit(SESSION_A)
        other = self.state.begin_midturn(SESSION_A)
        with mock.patch.object(self.entry, "_review_claim", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertTrue(self.state.midturn_running(SESSION_A), "他の実行の印は残る")
        self.state.end_midturn(SESSION_A, other)
        self.assertFalse(self.state.midturn_running(SESSION_A), "自分の印は例外でも下りている")


class TestStopDuringTheHandedOffReview(MidTurnTestCase):
    """引き継いだ残りを送っている最中 (2 回目のロック区間) に Stop が来ても、依頼を残さない (0.17.0)。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _two_stops(self, seen: dict):
        """1 塊目の最中に 1 回目の Stop、引き継いだ分 (3 回目の送信) の最中に 2 回目の Stop。"""

        def during(n: int) -> None:
            if n == 1:
                self.edit(SESSION_A, "rest.txt", "rest\n")
                seen["stop1"] = self.stop(SESSION_A, "REVIEW_CLEAN")
            elif n == 3:
                self.edit(SESSION_A, "rest2.txt", "rest two\n")
                seen["stop2"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                seen["stop2_calls"] = list(self.review_calls)

        return during

    def test_the_second_request_is_served_in_the_same_run(self):
        """DoD: Stop 時点の差分が同じ応答の中でレビューされ、stop_handoff が残らない。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        code, err, _ = self.mid_turn(
            SESSION_A, ["REVIEW_CLEAN", "REVIEW_CLEAN", "REVIEW_CLEAN", FINDING],
            inside=self._two_stops(seen),
        )
        self.assertEqual(seen["stop2_calls"], [], "Stop はレビューを並走させない")
        self.assertIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(seen["stop2"]))
        self.assertEqual(len(self.midturn_calls), 4, "2 塊 + 引き継ぎ 2 回")
        self.assertIn("rest.txt", self.midturn_calls[2])
        self.assertIn("rest2.txt", self.midturn_calls[3])
        self.assertNotIn("rest.txt\n", self.midturn_calls[3])
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])
        self.assertEqual(code, self.entry.REWAKE_EXIT_CODE)
        self.assertIn(FINDING, err)
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_stop_right_after_the_last_check_reviews_itself(self):
        """最後の読み出し (依頼なし) の直後・印を下ろす前に Stop が来たら、Stop が自分で見る。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        real_take = self.state.take_stop_handoff

        def take(sid):
            value = real_take(sid)
            if not value and "fired" not in seen:
                seen["fired"] = True  # Stop の中の読み出しでは発火させない
                self.assertTrue(self.state.midturn_running(sid), "印はまだ立っている")
                self.edit(SESSION_A, "rest.txt", "rest\n")
                seen["stop"] = self.stop(SESSION_A, "REVIEW_CLEAN")
                seen["stop_calls"] = list(self.review_calls)
            return value

        with mock.patch.object(self.state, "take_stop_handoff", side_effect=take):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(seen["stop_calls"]), 1, "ロックは空いているので Stop が自分で見る")
        self.assertIn("rest.txt", seen["stop_calls"][0])
        self.assertNotIn(self.entry.NOTICE_HANDED_OFF, self.assertNotBlocked(seen["stop"]))
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])
        self.assertEqual(self.pending(SESSION_A), [])

    def test_no_room_puts_the_request_back_for_the_next_review(self):
        """もう 1 回送ると hook の timeout に掛かりうるなら送らず、依頼を書き戻して次に任せる。"""
        self.big_edit(SESSION_A)
        seen: dict = {}
        with mock.patch.object(self.entry, "_midturn_has_room", side_effect=[True, False]):
            self.mid_turn(SESSION_A, "REVIEW_CLEAN", inside=self._two_stops(seen))
        self.assertEqual(len(self.midturn_calls), 3, "2 回目の引き継ぎは送らない")
        self.assertEqual(self.pending(SESSION_A), [os.path.join(self.repo, "rest2.txt")])
        self.assertTrue(self.raw_state(SESSION_A)["stop_handoff"], "依頼は書き戻す")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("rest2.txt")
        self.assertIn(self.entry.NOTICE_HANDOFF_DEFERRED, self.assertNotBlocked(output))
        self.assertFalse(self.raw_state(SESSION_A)["stop_handoff"])

    def test_room_is_measured_against_the_hook_timeout(self):
        worst = self.entry._midturn_review_worst_sec()
        limit = self.entry.MIDTURN_HOOK_TIMEOUT_SEC
        for label, elapsed, expected in (
            ("余裕あり", limit - worst - 1, True),
            ("ちょうど", limit - worst, False),
            ("超過", limit, False),
        ):
            with self.subTest(label), mock.patch.object(
                self.entry, "_midturn_clock", return_value=100.0 + elapsed
            ):
                self.assertIs(self.entry._midturn_has_room(100.0), expected)


class TestMidTurnRunMarks(MidTurnTestCase):
    """実行ごとの印 (`begin_midturn` / `end_midturn` / `midturn_running`) の単体。"""

    def test_each_run_owns_its_mark(self):
        x = self.state.begin_midturn(SESSION_A)
        y = self.state.begin_midturn(SESSION_A)
        self.assertNotEqual(x, y)
        self.state.end_midturn(SESSION_A, y)
        self.assertTrue(self.state.midturn_running(SESSION_A), "y が下ろしても x が残る")
        self.state.end_midturn(SESSION_A, y)
        self.assertTrue(self.state.midturn_running(SESSION_A), "同じ id の 2 回目は何もしない")
        self.state.end_midturn(SESSION_A, "no-such-run")
        self.assertTrue(self.state.midturn_running(SESSION_A), "知らない id は何も消さない")
        self.state.end_midturn(SESSION_A, x)
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_the_first_to_start_can_finish_last(self):
        x = self.state.begin_midturn(SESSION_A)
        y = self.state.begin_midturn(SESSION_A)
        self.state.end_midturn(SESSION_A, x)
        self.assertTrue(self.state.midturn_running(SESSION_A), "x が先に終わっても y が残る")
        self.state.end_midturn(SESSION_A, y)
        self.assertFalse(self.state.midturn_running(SESSION_A))

    def test_a_stale_mark_does_not_hide_a_live_one(self):
        old = time.time() - self.state.MIDTURN_STALE_SEC - 5
        with mock.patch.object(self.state.time, "time", return_value=old):
            self.state.begin_midturn(SESSION_A)
        self.assertFalse(self.state.midturn_running(SESSION_A))
        live = self.state.begin_midturn(SESSION_A)
        self.assertTrue(self.state.midturn_running(SESSION_A))
        self.assertEqual(list(self.raw_state(SESSION_A)["midturn_runs"]), [live], "失効した印は掃除される")

    def test_marks_are_per_session(self):
        self.state.begin_midturn(SESSION_A)
        self.assertFalse(self.state.midturn_running(SESSION_B))

    def test_a_broken_marks_field_is_ignored(self):
        self.state.begin_midturn(SESSION_A)
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json")
        for broken in ([], "x", {"r": "now"}, {"r": None}):
            with self.subTest(broken=broken):
                with open(path, "w") as f:
                    json.dump({"v": 1, "midturn_runs": broken}, f)
                try:
                    running = self.state.midturn_running(SESSION_A)
                    self.state.end_midturn(SESSION_A, self.state.begin_midturn(SESSION_A))
                except Exception as e:  # noqa: BLE001 - 例外の漏れは検出として数える
                    self.fail(f"壊れた欄で例外が漏れた: {e!r}")
                self.assertFalse(running)

    def test_a_live_mark_after_a_stale_one_still_counts(self):
        """失効した印が先頭にあっても、後ろの生きた印を見落とさない。"""
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json")
        self.state.begin_midturn(SESSION_A)
        now = time.time()
        with open(path, "w") as f:
            json.dump({"v": 1, "midturn_runs": {"old": now - self.state.MIDTURN_STALE_SEC - 5,
                                                "live": now}}, f)
        self.assertTrue(self.state.midturn_running(SESSION_A))


class TestRegistration(unittest.TestCase):
    """hooks.json の登録形: 背景 (asyncRewake) で 4 つのツールに張り、timeout が 2 回分に収まる。"""

    def _entry(self) -> tuple[str, dict]:
        hooks = json.loads(
            (pathlib.Path(__file__).resolve().parents[2] / "hooks.json").read_text()
        )
        found = [
            (entry.get("matcher", ""), h)
            for entry in hooks["hooks"]["PostToolUse"]
            for h in entry["hooks"]
            if h.get("command", "").endswith("--phase mid-turn")
        ]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_registered_as_async_rewake(self):
        matcher, hook = self._entry()
        self.assertEqual(set(matcher.split("|")), {"Write", "Edit", "NotebookEdit", "Bash"})
        self.assertIs(hook.get("asyncRewake"), True)
        self.assertNotIn("async", hook, "async は exit 2 で起こさない (次ターンまで届かない)")

    def test_timeout_covers_two_reviews(self):
        entry = _testutil.load_entry()
        import gitscan
        import selection

        _, hook = self._entry()
        git_per_review = (
            gitscan.REV_PARSE_TIMEOUT_SEC * 2
            + gitscan.LS_FILES_TIMEOUT_SEC * 2
            + entry.COLLECT_BUDGET_SEC
            + gitscan.PATH_DIFF_TIMEOUT_SEC
        )
        per_review = git_per_review + selection.worst_case_wall_sec() + entry._VERSION_SUBPROCESS_TIMEOUT_SEC
        # 閾値の判定 (git のみ) + 閾値超えの分 + Stop から引き継いだ残り
        worst = entry.PER_TOOL_PROBE_BUDGET_SEC + git_per_review + 2 * per_review
        self.assertLess(worst, hook["timeout"])
        # 引き継ぎを繰り返すかの判定 (`_midturn_has_room`) は同じ値で測る
        self.assertEqual(entry._midturn_review_worst_sec(), per_review)
        self.assertEqual(entry.MIDTURN_HOOK_TIMEOUT_SEC, hook["timeout"])
        # 印の失効は、kill されずに走り続けうる最長 (hook の timeout) より長い
        self.assertGreater(sys.modules["state"].MIDTURN_STALE_SEC, hook["timeout"])


if __name__ == "__main__":
    unittest.main()
