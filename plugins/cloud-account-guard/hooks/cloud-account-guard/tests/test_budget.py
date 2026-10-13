"""`core/budget.py` (hook 1 回分の実時間予算) のテスト。

**なぜ実時間を測らないか**: 「複合コマンドの直列 verify が hook timeout を
超えないこと」を実時間で測るテストは、CI の負荷次第で落ちる flaky になるうえ、
成功しても「その回はたまたま間に合った」以上のことを言わない。代わりに
**予算計算そのもの**を固定する:

- `worst_case_seconds()` が `hooks/hooks.json` の timeout を下回る (機械照合)
- 各 service が subprocess に渡す timeout が `budget.call_timeout()` 経由である
  (= 残り予算で頭打ちになる)
- 1 回の verify() が行う subprocess 呼び出し数が `MAX_CALLS_PER_VERIFY` 以下
  (上限見積りの前提そのもの)

これで「予算 + 超過分 < hook timeout」という不等式の各項が実装と結び付く。
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import budget  # noqa: E402
from services import ALL as ALL_SERVICES  # noqa: E402

_PKG_DIR = Path(__file__).resolve().parents[1]
_HOOKS_JSON = _PKG_DIR.parent / "hooks.json"

# 各 service の verify() が **最も多く** subprocess を呼ぶ期待値の形。
# gcloud の dict だけが project / account の 2 回を呼ぶ (他は 1 回)。
_MAX_CALL_EXPECTED = {
    "github": "expected-user",
    "firebase": "expected-project",
    "aws": "123456789012",
    "gcloud": {"project": "expected-project", "account": "user@example.com"},
    "kubectl": "expected-context",
}


def _fake_run(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class BudgetTestCase(unittest.TestCase):
    """予算はモジュール global なので、どのテストも後始末する。"""

    def setUp(self):
        budget.clear()
        self.addCleanup(budget.clear)


class TestBudgetPrimitives(BudgetTestCase):
    def test_unset_budget_returns_default_timeout(self):
        """予算未設定 (builder 等) では既定 timeout をそのまま使う。"""
        self.assertIsNone(budget.remaining())
        self.assertFalse(budget.expired())
        self.assertEqual(budget.call_timeout(15), 15)

    def test_call_timeout_is_capped_by_remaining(self):
        budget.start(3.0)
        self.assertLessEqual(budget.call_timeout(15), 3.0)
        self.assertGreater(budget.call_timeout(15), 0.0)

    def test_call_timeout_keeps_default_when_budget_is_larger(self):
        budget.start(60.0)
        self.assertEqual(budget.call_timeout(10), 10)

    def test_call_timeout_never_goes_below_floor(self):
        """0 秒 timeout は「起動した瞬間に必ず TimeoutExpired」= 検証不能な deny。

        残りが尽きても下限を切った「短いが意味のある試行」にする。
        """
        budget.start(0.0)
        self.assertEqual(
            budget.call_timeout(10), budget.MIN_CALL_TIMEOUT_SECONDS
        )
        self.assertGreater(budget.MIN_CALL_TIMEOUT_SECONDS, 0.0)
        budget.start(-100.0)
        self.assertGreater(budget.call_timeout(10), 0.0)
        self.assertEqual(
            budget.call_timeout(10), budget.MIN_CALL_TIMEOUT_SECONDS
        )

    def test_expired_reflects_deadline(self):
        budget.start(60.0)
        self.assertFalse(budget.expired())
        budget.start(0.0)
        self.assertTrue(budget.expired())

    def test_clear_restores_unbudgeted_behaviour(self):
        budget.start(0.0)
        self.assertTrue(budget.expired())
        budget.clear()
        self.assertFalse(budget.expired())
        self.assertIsNone(budget.remaining())
        self.assertEqual(budget.call_timeout(15), 15)

    def test_start_reads_constant_at_call_time(self):
        """既定引数に束縛しない (テストが定数を差し替えて予算切れを再現できる)。"""
        with mock.patch.object(budget, "TOTAL_BUDGET_SECONDS", 0.0):
            budget.start()
            self.assertTrue(budget.expired())


class TestBudgetFitsHookTimeout(BudgetTestCase):
    """予算 + 超過分が `hooks/hooks.json` の timeout を下回ることを機械で固定する。

    内部バックログ: hook が timeout すると出力が破棄されて tool call が
    そのまま進む (公式仕様上の fail-open)。CLI 未検出も CLI timeout も deny に
    倒している以上、ここだけ無音で通るのは判定表の穴になる。
    """

    def _hook_timeout_seconds(self) -> float:
        data = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
        entries = data["hooks"]["PreToolUse"]
        timeouts = [
            hook["timeout"]
            for entry in entries
            for hook in entry["hooks"]
            if "timeout" in hook
        ]
        self.assertTrue(timeouts, "hooks.json に timeout が宣言されていない")
        return float(min(timeouts))

    def test_worst_case_is_below_hook_timeout(self):
        hook_timeout = self._hook_timeout_seconds()
        self.assertLess(
            budget.worst_case_seconds(),
            hook_timeout,
            "予算 + 超過見積りが hook timeout 以上。hooks.json の timeout を"
            "上げるか TOTAL_BUDGET_SECONDS を下げること",
        )

    def test_worst_case_accounts_for_calls_started_before_expiry(self):
        """超過見積りは「締切直前に始まった verify の分」を必ず含む。

        予算切れの判定は verify の**手前**でしかできないため、
        `worst_case == TOTAL_BUDGET` にしてしまうと不等式が嘘になる。
        """
        self.assertGreaterEqual(
            budget.worst_case_seconds(10.0),
            10.0 + budget.MAX_CALLS_PER_VERIFY * budget.MIN_CALL_TIMEOUT_SECONDS,
        )


class TestServicesHonourBudget(BudgetTestCase):
    """全 service が subprocess timeout を `budget.call_timeout()` 経由で決める。

    1 つでも直値 (`timeout=15` 等) に戻ると、残り予算に関係なくその service が
    フルの timeout を使えてしまい、上の不等式が崩れる。
    """

    def _timeouts_used(self, service, expected) -> list[float]:
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="")
        ) as run:
            service.verify(expected, "/nonexistent-project")
        self.assertTrue(
            run.called,
            f"{service.ACCOUNT_KEY}: verify() が CLI を起動していない",
        )
        return [call.kwargs["timeout"] for call in run.call_args_list]

    def test_every_service_caps_timeout_by_remaining_budget(self):
        budget.start(3.0)
        for service in ALL_SERVICES:
            with self.subTest(service=service.ACCOUNT_KEY):
                expected = _MAX_CALL_EXPECTED[service.ACCOUNT_KEY]
                for timeout in self._timeouts_used(service, expected):
                    self.assertLessEqual(
                        timeout,
                        3.0,
                        f"{service.ACCOUNT_KEY} が残り予算を超える timeout を"
                        "渡している (budget.call_timeout を経由していない)",
                    )
                    self.assertGreaterEqual(
                        timeout, budget.MIN_CALL_TIMEOUT_SECONDS
                    )

    def test_every_service_uses_full_timeout_without_budget(self):
        """予算が無いとき (builder 等) は既定 timeout が縮まない。"""
        for service in ALL_SERVICES:
            with self.subTest(service=service.ACCOUNT_KEY):
                expected = _MAX_CALL_EXPECTED[service.ACCOUNT_KEY]
                for timeout in self._timeouts_used(service, expected):
                    self.assertGreaterEqual(
                        timeout,
                        10,
                        f"{service.ACCOUNT_KEY} の既定 timeout が縮んでいる",
                    )


class TestVerifyCallCountContract(BudgetTestCase):
    """1 回の verify() の subprocess 呼び出し数 <= `MAX_CALLS_PER_VERIFY`。

    超過見積り (`worst_case_seconds`) はこの本数を前提にしている。service を
    追加・変更して呼び出しが増えたら、定数を上げて hook timeout との不等式を
    引き直す必要がある — それをここで気付けるようにする。
    """

    def test_no_service_exceeds_max_calls_per_verify(self):
        for service in ALL_SERVICES:
            with self.subTest(service=service.ACCOUNT_KEY):
                expected = _MAX_CALL_EXPECTED[service.ACCOUNT_KEY]
                with mock.patch(
                    "subprocess.run", return_value=_fake_run(stdout="")
                ) as run:
                    service.verify(expected, "/nonexistent-project")
                self.assertLessEqual(
                    run.call_count,
                    budget.MAX_CALLS_PER_VERIFY,
                    f"{service.ACCOUNT_KEY}: verify() が "
                    f"{run.call_count} 回 CLI を呼んでいる",
                )

    def test_gcloud_dict_really_makes_two_calls(self):
        """定数 (=2) が空振りでないこと: 実際に 2 回呼ぶ service が存在する。

        「上限以下」だけを測ると、全 service が 1 回になったときに定数が
        過大なまま気付けない (見積りが緩む方向)。
        """
        from services import gcloud

        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="")
        ) as run:
            gcloud.verify(
                {"project": "p", "account": "a"}, "/nonexistent-project"
            )
        self.assertEqual(run.call_count, budget.MAX_CALLS_PER_VERIFY)


if __name__ == "__main__":
    unittest.main()
