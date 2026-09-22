"""差分レビューの送信先の決め方 (`selection.py`)。

**この module の主役は「既定の送信先が広がらないこと」** (`TestDefaultDestination`)。
0.11.0 までこの hook の差分は cursor にしか行かなかったので、registry 化と戦略の追加で
そこが動いていないことを床として固定する。戦略・フォールバック・待ち時間の上限は
その上に乗る挙動として検証する。

外部 AI CLI は起動しない (`review()` を差し替えるか、偽の backend オブジェクトを使う)。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION = "sess-selection"
FINDINGS = "1. **直接影響** — 何か壊れる"


class BackendFlowTestCase(HookTestCase):
    """cursor / codex の両方を「入っている」状態にして Stop を回す基底クラス。"""

    def setUp(self) -> None:
        super().setUp()
        self.codex = sys.modules["codex"]
        self.selection = sys.modules["selection"]
        self._backend_patches = [
            mock.patch.object(self.codex, "is_available", return_value=True),
        ]
        for p in self._backend_patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._backend_patches:
            p.stop()
        super().tearDown()

    def stop_both(
        self,
        session_id: str,
        *,
        cursor_result: str | None = "REVIEW_CLEAN",
        codex_result: str | None = "REVIEW_CLEAN",
    ) -> str:
        """Stop hook を 1 回起動し、**両 backend の `review()` を差し替える**。

        どちらが呼ばれたかを `self.cursor_calls` / `self.codex_calls` で見る。
        """
        cursor_calls: list[str] = []
        codex_calls: list[str] = []

        def fake_cursor(diff_text: str, *, cwd: str | None = None):
            cursor_calls.append(diff_text)
            return cursor_result

        def fake_codex(diff_text: str, *, cwd: str | None = None):
            codex_calls.append(diff_text)
            return codex_result

        with mock.patch.object(
            self.cursor, "review", side_effect=fake_cursor
        ), mock.patch.object(self.codex, "review", side_effect=fake_codex):
            output = self.run_hook(
                "stop",
                {"session_id": session_id, "cwd": self.repo, "stop_hook_active": False},
            )
        self.cursor_calls = cursor_calls
        self.codex_calls = codex_calls
        return output

    def last_backend(self, session_id: str = SESSION) -> str:
        path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{session_id}.json"
        )
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return json.load(f).get("last_backend", "")


class TestDefaultDestination(BackendFlowTestCase):
    """**床**: `EXTERNAL_AI_POST_REVIEW_BACKENDS` 未設定なら送信先は cursor だけ。

    この plugin は差分を外部サービスへ送る。registry に backend を足したり戦略を
    増やしたりしても、**更新しただけの利用者の差分が新しい送信先へ行ってはならない**
    (0.11.0 と同じ送信先)。
    """

    def test_configured_defaults_to_cursor_only(self):
        chosen, unknown = self.selection.configured()
        self.assertEqual([b.NAME for b in chosen], ["cursor"])
        self.assertEqual(unknown, [])

    def test_default_strategy_for_a_single_backend_is_fixed(self):
        self.assertEqual(self.selection.strategy(1), self.selection.STRATEGY_FIXED)

    def test_codex_available_but_unset_backends_never_sends_to_codex(self):
        """codex が入っていても、2 ターン続けて cursor にしか行かないこと。

        **2 ターン回すのが肝**。1 ターンだけだと「前回の backend」が空なので、
        既定の候補集合を全 backend に広げて `alternate` を既定にする改変でも
        1 回目はたまたま cursor に当たってしまい、テストが素通りする。
        """
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.codex_calls, [], "既定で codex に送ってはいけない")

        self.edit(SESSION, "b.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(
            self.codex_calls, [], "2 ターン目 (前回 = cursor) でも codex に送ってはいけない"
        )

    def test_default_backends_constant_is_cursor_only(self):
        """定数そのものを固定する (ここを広げる変更は必ずこのテストに当たる)。"""
        self.assertEqual(self.selection.DEFAULT_BACKENDS, ("cursor",))

    def test_review_header_names_the_backend_that_answered(self):
        self.edit(SESSION, "a.py", "v1\n")
        data = self.assertBlocked(self.stop_both(SESSION, cursor_result=FINDINGS))
        self.assertIn("## 実装直後レビュー結果 (Cursor, 差分レビュー)", data["reason"])

    def test_successful_backend_is_recorded(self):
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(self.last_backend(), "cursor")

    def test_failed_backend_is_not_recorded(self):
        """失敗は「前回どこにレビューさせたか」ではないので記録しない。"""
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION, cursor_result=None)
        self.assertEqual(self.last_backend(), "")


class TestExplicitBackends(BackendFlowTestCase):
    """`EXTERNAL_AI_POST_REVIEW_BACKENDS` を自分で書いたときだけ送信先が変わる。"""

    def test_explicit_codex_only_sends_to_codex(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "codex"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.cursor_calls, [])

    def test_codex_review_header_says_codex(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "codex"
        self.edit(SESSION, "a.py", "v1\n")
        data = self.assertBlocked(
            self.stop_both(SESSION, codex_result=FINDINGS), backend="Codex"
        )
        self.assertIn("## 実装直後レビュー結果 (Codex, 差分レビュー)", data["reason"])

    def test_unknown_names_are_ignored_and_reported(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor,gemini"
        self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop_both(SESSION))
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertIn("未知の backend 名を無視", message)
        self.assertIn("gemini", message)

    def test_unknown_only_reviews_nothing_and_tells_the_user(self):
        """タイプミスで既定へ fallback しない。黙って止まりもしない。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursr"
        self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop_both(SESSION))
        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.codex_calls, [])
        self.assertIn("未知の backend 名を無視", message)
        self.assertIn("レビューできる backend がありません", message)

    def test_unknown_only_notifies_even_without_edits(self):
        """この設定では PostToolUse も記録しないので pending が常に空になる。

        「編集があるターンだけ通知する」という他の通知と同じ絞り方が使えないため、
        毎ターン出る側に倒してある (設定を直せば止まる通知)。絞り込みを足すなら
        pending 以外の判定材料が要る、ということをここで固定しておく。
        """
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursr"
        message = self.assertNotBlocked(self.stop_both(SESSION))
        self.assertIn("未知の backend 名を無視", message)

    def test_no_backend_installed_is_completely_silent(self):
        """回帰: 外部 AI CLI が 1 つも無い環境は 0.11.0 と同じく無出力。"""
        with mock.patch.object(self.cursor, "is_available", return_value=False), (
            mock.patch.object(self.codex, "is_available", return_value=False)
        ):
            self.edit(SESSION, "a.py", "v1\n")
            self.assertEqual(self.stop_both(SESSION), "")


class TestStrategies(BackendFlowTestCase):
    """`EXTERNAL_AI_POST_REVIEW_STRATEGY` の既定と各戦略の並べ替え。"""

    def setUp(self) -> None:
        super().setUp()
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor,codex"

    def test_default_strategy_for_two_backends_is_alternate(self):
        self.assertEqual(self.selection.strategy(2), self.selection.STRATEGY_ALTERNATE)

    def test_alternate_sends_the_second_turn_to_the_other_backend(self):
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(len(self.cursor_calls), 1, "1 ターン目は列挙順の先頭")
        self.assertEqual(self.codex_calls, [])
        self.assertEqual(self.last_backend(), "cursor")

        self.edit(SESSION, "b.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(self.cursor_calls, [], "2 ターン目は前回と別の backend に行く")
        self.assertEqual(len(self.codex_calls), 1)
        self.assertEqual(self.last_backend(), "codex")

    def test_fixed_keeps_sending_to_the_head_of_the_list(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_STRATEGY"] = "fixed"
        for rel in ("a.py", "b.py"):
            self.edit(SESSION, rel, "v1\n")
            self.stop_both(SESSION)
            self.assertEqual(len(self.cursor_calls), 1, f"{rel} で送信先が変わった")
            self.assertEqual(self.codex_calls, [])

    def test_fixed_honors_the_configured_order_when_codex_is_listed_first(self):
        """`codex,cursor` + fixed なら codex が第一候補 (registry の宣言順ではない)。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "codex,cursor"
        os.environ["EXTERNAL_AI_POST_REVIEW_STRATEGY"] = "fixed"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertEqual(len(self.codex_calls), 1, "列挙順の先頭 (codex) に行く")
        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.last_backend(), "codex")

    def test_unknown_strategy_falls_back_to_the_default(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_STRATEGY"] = "roundrobin"
        self.assertEqual(self.selection.strategy(2), self.selection.STRATEGY_ALTERNATE)
        self.assertEqual(self.selection.strategy(1), self.selection.STRATEGY_FIXED)

    def test_available_puts_usable_backends_first_without_dropping_the_rest(self):
        chosen, _ = self.selection.configured()
        with mock.patch.object(self.cursor, "is_available", return_value=False):
            order = self.selection.order(
                chosen, strategy_name=self.selection.STRATEGY_AVAILABLE
            )
        self.assertEqual([b.NAME for b in order], ["codex", "cursor"])

    def test_random_keeps_the_same_set(self):
        chosen, _ = self.selection.configured()
        order = self.selection.order(
            chosen, strategy_name=self.selection.STRATEGY_RANDOM
        )
        self.assertEqual(
            sorted(b.NAME for b in order), sorted(b.NAME for b in chosen)
        )

    def test_every_strategy_is_a_permutation_of_the_configured_set(self):
        """どの戦略も候補を落とさない (落とすとレビューが黙って止まる経路になる)。"""
        chosen, _ = self.selection.configured()
        for name in self.selection.STRATEGIES:
            with self.subTest(strategy=name):
                order = self.selection.order(
                    chosen, strategy_name=name, last="cursor"
                )
                self.assertEqual(
                    sorted(b.NAME for b in order), sorted(b.NAME for b in chosen)
                )


class TestFallback(BackendFlowTestCase):
    """失敗した backend の次を、**列挙した集合の中だけで**試す。"""

    def test_fallback_uses_the_next_backend_in_the_list(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor,codex"
        self.edit(SESSION, "a.py", "v1\n")
        data = self.assertBlocked(
            self.stop_both(SESSION, cursor_result=None, codex_result=FINDINGS),
            backend="Codex",
        )
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(len(self.codex_calls), 1)
        self.assertIn("cursor=失敗", data["systemMessage"])
        self.assertIn("codex=完了", data["systemMessage"])
        self.assertEqual(self.last_backend(), "codex")

    def test_fallback_never_leaves_the_configured_set(self):
        """**床**: 列挙していない backend には、第一候補が失敗しても行かない。"""
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor"
        path = self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(
            self.stop_both(SESSION, cursor_result=None, codex_result=FINDINGS)
        )
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.codex_calls, [], "列挙していない backend へ回してはいけない")
        self.assertIn("結果を取得できず", message)
        self.assertEqual(self.pending(SESSION), [path], "全滅時は pending に戻す")

    def test_all_backends_failing_restores_pending(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor,codex"
        path = self.edit(SESSION, "a.py", "v1\n")
        message = self.assertNotBlocked(
            self.stop_both(SESSION, cursor_result=None, codex_result=None)
        )
        self.assertEqual(self.pending(SESSION), [path])
        self.assertIn("cursor=失敗", message)
        self.assertIn("codex=失敗", message)
        self.assertEqual(self.last_backend(), "")


class _FakeBackend:
    """`timeout_sec()` / `review()` だけを持つ偽 backend (予算判定の単体検証用)。"""

    def __init__(self, name: str, timeout: float, result: str | None) -> None:
        self.NAME = name
        self._timeout = timeout
        self._result = result
        self.calls: list[str] = []

    def timeout_sec(self) -> float:
        return self._timeout

    def is_available(self, deadline=None) -> bool:
        return True

    def review(self, diff_text: str, *, cwd: str | None = None) -> str | None:
        self.calls.append(diff_text)
        return self._result


class TestTotalWaitBudget(unittest.TestCase):
    """フォールバックしても 1 回の Stop の待ち時間が上限を超えないこと。

    超えるとハーネスの hook timeout (Stop: 690 秒) の kill が自前の後始末より先に来て、
    claim が pending へ戻らず**このセッションの変更が TTL (900 秒) まで沈黙する**。
    """

    def setUp(self) -> None:
        import selection

        self.selection = selection

    def _run(self, backends_list, elapsed_at_second_attempt: float):
        clock = [0.0, elapsed_at_second_attempt, elapsed_at_second_attempt]
        return self.selection.run_review(
            "DIFF",
            cwd=None,
            chosen=backends_list,
            strategy_name=self.selection.STRATEGY_FIXED,
            now=lambda: clock.pop(0) if len(clock) > 1 else clock[0],
        )

    def test_second_backend_runs_when_the_budget_allows(self):
        first = _FakeBackend("cursor", 300, None)
        second = _FakeBackend("codex", 300, "FINDINGS")
        outcome = self._run([first, second], 100.0)
        self.assertEqual(outcome.backend, "codex")
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(outcome.skipped, [])

    def test_second_backend_is_skipped_when_the_budget_is_spent(self):
        first = _FakeBackend("cursor", 300, None)
        second = _FakeBackend("codex", 300, "FINDINGS")
        # 300 + 300 + 15 (kill 猶予) > 600 なので 2 つ目は起動しない
        outcome = self._run([first, second], 300.0)
        self.assertIsNone(outcome.text)
        self.assertEqual(second.calls, [], "残り予算が足りないのに起動した")
        self.assertEqual(outcome.skipped, ["codex"])

    def test_first_backend_is_never_gated(self):
        """第一候補は 0.11.0 と同じく無条件で起動する (挙動不変)。"""
        only = _FakeBackend("cursor", 600, "FINDINGS")
        outcome = self._run([only], 0.0)
        self.assertEqual(outcome.backend, "cursor")

    def test_worst_case_matches_the_single_backend_ceiling(self):
        """待ち時間の上限が 0.11.0 (cursor 単体) と同じであること。

        ここが増えると `hooks.json` の Stop timeout 690 秒の予算計算
        (`test_review_set.py::TestTimeoutBudgets`) が静かに破れる。
        """
        import cursor
        from _common import subproc

        self.assertEqual(
            self.selection.worst_case_wall_sec(),
            cursor.MAX_TIMEOUT_SEC + 3 * subproc.KILL_GRACE_SEC,
        )

    def test_all_backends_share_the_same_timeout_ceiling(self):
        """上限が backend ごとに違うと TTL と hook 予算が選択次第で動く。"""
        ceilings = {r.NAME: r.MAX_TIMEOUT_SEC for r in self.selection.REVIEWERS}
        self.assertEqual(len(set(ceilings.values())), 1, ceilings)


class TestSendLog(BackendFlowTestCase):
    """どの backend に何 (パス名とバイト数) を送ったかを hooklog に残す。"""

    def test_log_records_backend_and_manifest(self):
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION)
        self.assertIn("cursor に差分を送信", self.last_stderr)
        self.assertIn("a.py (", self.last_stderr)
        self.assertIn("bytes)", self.last_stderr)

    def test_log_does_not_contain_the_diff_body(self):
        self.edit(SESSION, "a.py", "TOP-SECRET-LINE\n")
        self.stop_both(SESSION)
        self.assertNotIn("TOP-SECRET-LINE", self.last_stderr)

    def test_fallback_logs_each_destination(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BACKENDS"] = "cursor,codex"
        self.edit(SESSION, "a.py", "v1\n")
        self.stop_both(SESSION, cursor_result=None, codex_result="REVIEW_CLEAN")
        self.assertIn("cursor に差分を送信", self.last_stderr)
        self.assertIn("codex に差分を送信", self.last_stderr)


class TestUnsetEnvKeepsLegacyBehaviour(BackendFlowTestCase):
    """回帰: backend 系の env を一切書かなければ 0.11.0 と同じ。"""

    def test_unset_env_reviews_with_cursor_and_blocks(self):
        _testutil.clear_plugin_env(
            keep={
                "EXTERNAL_AI_POST_REVIEW": "1",
                "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING": "1",
                **_testutil.CURSOR_COMMAND_ENV,
                **_testutil.NEUTRAL_EXCLUSION_ENV,
            }
        )
        self.edit(SESSION, "a.py", "v1\n")
        data = self.assertBlocked(self.stop_both(SESSION, cursor_result=FINDINGS))
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(self.codex_calls, [])
        self.assertIn("差分レビュー完了", data["systemMessage"])
        self.assertIn("1 ファイル", data["systemMessage"])


if __name__ == "__main__":
    unittest.main()
