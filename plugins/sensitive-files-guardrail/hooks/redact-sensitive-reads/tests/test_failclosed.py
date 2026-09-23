"""fail-closed 動作 (内部例外 → ask_or_deny / ask_or_allow) のテスト。

0.3.2 で追加:
- ``ask_or_allow`` の三態判定 (default=ask, auto/bypass=allow)
- bash handler の ``patterns.txt`` 読込失敗 = 全 mode ``deny`` 固定
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from core.output import ask_or_allow, ask_or_deny, make_ask, make_deny


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
        s = "あ" * 2000
        r = make_deny(s)
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), output.MAX_REASON_BYTES)


class TestAskOrDeny(unittest.TestCase):
    """``ask_or_deny``: bypass = deny / それ以外 = ask (Read/Edit handler 用、無変更)。"""

    def test_bypass_returns_deny(self):
        r = ask_or_deny("reason", {"permission_mode": "bypassPermissions"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_default_returns_ask(self):
        r = ask_or_deny("reason", {"permission_mode": "default"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_missing_mode_returns_ask(self):
        r = ask_or_deny("reason", {})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_auto_returns_ask(self):
        # ask_or_deny は auto を lenient 扱いしない (旧仕様維持)
        r = ask_or_deny("reason", {"permission_mode": "auto"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")


class TestAskOrAllow(unittest.TestCase):
    """``ask_or_allow``: auto/bypass/plan = allow / それ以外 = ask (Bash handler 用、0.3.2 追加、plan は 0.13.0 で復活)。

    0.33.0: lenient に倒したときは ``additionalContext`` に固定 1 文が載る
    (``permissionDecision`` は出さないので判定は素の allow と同一)。判定の
    assert は module docstring の規約どおり ``output.is_allow`` で行う。
    ここは builder 単体 (「note を作る」側) の床で、**最終応答に残すかは呼出側**
    が決める — Bash handler は機密パターンらしい token を含む command だけに絞る
    (``handlers.bash_handler._gate_lenient_note``)。
    """

    def _assert_lenient_allow(self, r: dict) -> None:
        self.assertTrue(output.is_allow(r), msg=repr(r))
        self.assertIsNone(output.decision_of(r), msg=repr(r))
        hook = r["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hook)
        self.assertNotIn("permissionDecisionReason", hook)
        self.assertEqual(hook["additionalContext"], output.LENIENT_ALLOW_CONTEXT)
        # reason 引数は allow では Claude に届かないチャネルなので、載せない
        self.assertNotIn("reason", hook["additionalContext"])

    def test_auto_returns_allow(self):
        r = ask_or_allow("reason", {"permission_mode": "auto"})
        self._assert_lenient_allow(r)

    def test_bypass_returns_allow(self):
        r = ask_or_allow("reason", {"permission_mode": "bypassPermissions"})
        self._assert_lenient_allow(r)

    def test_default_returns_ask(self):
        r = ask_or_allow("reason", {"permission_mode": "default"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_accept_edits_returns_ask(self):
        # acceptEdits は Edit/Write 専用モード。Bash lenient の意図が無いため
        # 明示的に非 lenient 維持 (ask に倒る)。
        r = ask_or_allow("reason", {"permission_mode": "acceptEdits"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_dont_ask_returns_ask(self):
        # dontAsk は明示的な非 lenient 判断として既存方針を維持する (ask に倒る)。
        r = ask_or_allow("reason", {"permission_mode": "dontAsk"})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_plan_returns_allow(self):
        # 0.13.0: plan を LENIENT_MODES に再追加 (ユーザー実機 2026-05-18 で plan
        # mode 中の Bash PreToolUse hook 発火を確認)。plan mode は副作用が plan
        # 承認まで保留される dry-run 状態のため autonomous と同等に lenient 扱い。
        r = ask_or_allow("reason", {"permission_mode": "plan"})
        self._assert_lenient_allow(r)

    def test_missing_mode_returns_ask(self):
        r = ask_or_allow("reason", {})
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "ask")


class TestHandlerFailClosed(unittest.TestCase):
    def test_read_handler_with_invalid_path_type(self):
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


class _PatternsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        os.makedirs(self.xdg)
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"HOME": self.home, "USERPROFILE": self.home, "XDG_CONFIG_HOME": self.xdg},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBashPatternsUnavailableDeny(_PatternsBase):
    """0.3.2: Bash handler の patterns.txt 読込失敗は全 mode で deny 固定。

    patterns が読めない = 安全な policy が無い。bash は ``ask_or_allow`` を広く使う
    ため、policy 欠如時に lenient で素通りすることを避けて ``make_deny`` で全停止する。
    """

    def _patch_load(self):
        return mock.patch(
            "handlers.bash_handler.load_patterns",
            side_effect=FileNotFoundError("test"),
        )

    def _make_envelope(self, mode: str) -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": mode,
        }

    def test_default_deny(self):
        from handlers.bash_handler import handle
        with self._patch_load():
            r = handle(self._make_envelope("default"))
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_auto_deny(self):
        from handlers.bash_handler import handle
        with self._patch_load():
            r = handle(self._make_envelope("auto"))
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_bypass_deny(self):
        from handlers.bash_handler import handle
        with self._patch_load():
            r = handle(self._make_envelope("bypassPermissions"))
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "deny"
        )


class TestReadEditPatternsUnavailableUnchanged(_PatternsBase):
    """Read/Edit handler の patterns.txt 読込失敗は従来通り ``ask_or_deny`` (regression guard)。"""

    def _patch_load_read(self):
        return mock.patch(
            "handlers.read_handler.load_patterns",
            side_effect=OSError("test"),
        )

    def test_read_default_returns_ask(self):
        from handlers.read_handler import handle
        with self._patch_load_read():
            r = handle({
                "tool_input": {"file_path": str(Path(self.tmp) / ".env")},
                "cwd": self.tmp,
                "permission_mode": "default",
            })
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "ask"
        )

    def test_read_bypass_returns_deny(self):
        from handlers.read_handler import handle
        with self._patch_load_read():
            r = handle({
                "tool_input": {"file_path": str(Path(self.tmp) / ".env")},
                "cwd": self.tmp,
                "permission_mode": "bypassPermissions",
            })
        self.assertEqual(
            r["hookSpecificOutput"]["permissionDecision"], "deny"
        )


class TestMainCatchAll(_PatternsBase):
    """0.3.2 では ``__main__`` catch-all は無変更 (auto/bypass 緩和は 0.3.3 以降)。

    ``__main__._dispatch`` 内で例外が起きた場合、wrapper の except 節は ``ask_or_deny``
    を呼ぶ (auto では ask、bypass では deny)。0.3.2 では bash handler のみ ``ask_or_allow``
    化したので、catch-all の lenient 化は別リリースに分離。
    """

    def test_unchanged_for_now(self):
        # 単に core/output.py の ask_or_deny の挙動を再確認するだけの guard test
        from core.output import ask_or_deny
        self.assertEqual(
            ask_or_deny("x", {"permission_mode": "auto"})
            ["hookSpecificOutput"]["permissionDecision"],
            "ask",
        )
        self.assertEqual(
            ask_or_deny("x", {"permission_mode": "bypassPermissions"})
            ["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )


if __name__ == "__main__":
    unittest.main()
