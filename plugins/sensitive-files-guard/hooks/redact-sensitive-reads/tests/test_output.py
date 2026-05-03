"""core/output.py のテスト (L3 / L4, 0.4.3)。

- ``HookSpecificOutput`` / ``HookResponse`` TypedDict は実行時には dict なので、
  shape が壊れていないかを直接検証する。
- ``is_allow(r)`` 述語は ``make_allow()`` の現行 ``{}`` 仕様と、将来の
  ``permissionDecision: "allow"`` 明示出力の両方で True を返すこと。
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from core import output


class TestMakeBuilders(unittest.TestCase):
    """make_deny / make_ask / make_allow の shape (L3 想定)。"""

    def test_make_deny_shape(self):
        r = output.make_deny("blocked")
        self.assertIn("hookSpecificOutput", r)
        hs = r["hookSpecificOutput"]
        self.assertEqual(hs["hookEventName"], "PreToolUse")
        self.assertEqual(hs["permissionDecision"], "deny")
        self.assertEqual(hs["permissionDecisionReason"], "blocked")

    def test_make_ask_shape(self):
        r = output.make_ask("paused")
        hs = r["hookSpecificOutput"]
        self.assertEqual(hs["permissionDecision"], "ask")
        self.assertEqual(hs["permissionDecisionReason"], "paused")

    def test_make_allow_is_empty_dict(self):
        # 現行仕様: {} を返す。将来 spec 変更時はここを更新し、is_allow 経由で
        # 検証する形に統一する。
        self.assertEqual(output.make_allow(), {})


class TestIsAllow(unittest.TestCase):
    """L4: is_allow(r) 述語の判定マトリクス。"""

    def test_empty_dict_is_allow(self):
        self.assertTrue(output.is_allow({}))

    def test_make_allow_result_is_allow(self):
        self.assertTrue(output.is_allow(output.make_allow()))

    def test_explicit_allow_decision_is_allow_forward_compat(self):
        # 将来 spec が "allow" 明示出力に拡張されても True
        future_shape = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "ok",
            }
        }
        self.assertTrue(output.is_allow(future_shape))

    def test_make_deny_is_not_allow(self):
        self.assertFalse(output.is_allow(output.make_deny("x")))

    def test_make_ask_is_not_allow(self):
        self.assertFalse(output.is_allow(output.make_ask("x")))

    def test_empty_hook_specific_output_is_allow(self):
        # hookSpecificOutput はあるが decision が無い (= 不完全な allow)
        self.assertTrue(output.is_allow({"hookSpecificOutput": {}}))

    def test_hook_specific_output_non_dict_is_allow(self):
        # 型が壊れているケース → allow 扱いで前進する設計
        self.assertTrue(
            output.is_allow({"hookSpecificOutput": "not a dict"})
        )

    def test_response_non_dict_is_not_allow(self):
        # response 自体が dict でない → False (allow と誤判定しない)
        for bad in (None, "string", 123, [1, 2]):
            self.assertFalse(
                output.is_allow(bad),  # type: ignore[arg-type]
                msg=f"unexpected allow for {bad!r}",
            )


class TestAskOrDeny(unittest.TestCase):
    """ask_or_deny: bypassPermissions では deny に倒す。"""

    def test_default_returns_ask(self):
        r = output.ask_or_deny("reason", {"permission_mode": "default"})
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "ask",
        )

    def test_bypass_returns_deny(self):
        r = output.ask_or_deny(
            "reason", {"permission_mode": "bypassPermissions"},
        )
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "deny",
        )


class TestAskOrAllow(unittest.TestCase):
    """ask_or_allow: auto / bypassPermissions / plan は allow に倒す。"""

    def test_default_returns_ask(self):
        r = output.ask_or_allow("reason", {"permission_mode": "default"})
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "ask",
        )

    def test_auto_returns_allow(self):
        r = output.ask_or_allow("reason", {"permission_mode": "auto"})
        self.assertTrue(output.is_allow(r))

    def test_bypass_returns_allow(self):
        r = output.ask_or_allow(
            "reason", {"permission_mode": "bypassPermissions"},
        )
        self.assertTrue(output.is_allow(r))

    def test_plan_returns_allow(self):
        r = output.ask_or_allow("reason", {"permission_mode": "plan"})
        self.assertTrue(output.is_allow(r))

    def test_acceptEdits_returns_ask(self):
        # acceptEdits は意図的に lenient しない (Bash 用ではない mode)
        r = output.ask_or_allow(
            "reason", {"permission_mode": "acceptEdits"},
        )
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "ask",
        )


class TestTruncate(unittest.TestCase):
    """reason の byte 上限 (3KB) 切り詰め挙動。"""

    def test_short_reason_unchanged(self):
        r = output.make_deny("short")
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecisionReason"], "short",
        )

    def test_long_reason_truncated(self):
        long_text = "あ" * 2000  # 6000 byte 相当 (UTF-8 で 3 byte/char)
        r = output.make_deny(long_text)
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), output.MAX_REASON_BYTES)
        self.assertTrue(reason.endswith("...[truncated]"))


if __name__ == "__main__":
    unittest.main()
