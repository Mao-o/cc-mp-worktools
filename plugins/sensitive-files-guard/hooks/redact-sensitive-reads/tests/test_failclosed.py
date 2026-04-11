"""fail-closed 動作 (内部例外 → ask_or_deny) のテスト。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from core import output
from core.output import ask_or_deny, make_ask, make_deny


class TestOutputBuilders(unittest.TestCase):
    def test_make_deny_shape(self):
        r = make_deny("hello")
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecisionReason"], "hello"
        )

    def test_make_ask_shape(self):
        r = make_ask("hello")
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_truncate_over_4kb(self):
        big = "x" * 10000
        r = make_deny(big)
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), output.MAX_REASON_BYTES)
        self.assertTrue(reason.endswith("[truncated]"))

    def test_truncate_utf8_boundary(self):
        # マルチバイト文字が境界で切られても decode できる
        s = "あ" * 2000  # 3 byte * 2000 = 6000 byte
        r = make_deny(s)
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), output.MAX_REASON_BYTES)

    def test_ask_or_deny_bypass(self):
        r = ask_or_deny("reason", {"permission_mode": "bypassPermissions"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ask_or_deny_non_bypass(self):
        r = ask_or_deny("reason", {"permission_mode": "default"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_ask_or_deny_missing_mode(self):
        # permission_mode が無い envelope → ask
        r = ask_or_deny("reason", {})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")


class TestHandlerFailClosed(unittest.TestCase):
    def test_read_handler_with_invalid_path_type(self):
        # tool_input に file_path が無い → allow (判定できない)
        from handlers.read_handler import handle
        r = handle({"tool_input": {}, "cwd": "/tmp", "permission_mode": "default"})
        self.assertEqual(r, {})

    def test_read_handler_non_sensitive_file(self):
        from handlers.read_handler import handle
        r = handle({
            "tool_input": {"file_path": "/etc/hosts"},
            "cwd": "/tmp",
            "permission_mode": "default",
        })
        self.assertEqual(r, {})


if __name__ == "__main__":
    unittest.main()
