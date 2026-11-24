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

# 判定の行列の土台は test_bash_handler.py に 1 つだけ置き、ここでは import して使う
# (test_matcher_path_rules.py が test_matcher.py の DEFAULT_RULES を使うのと同じ形)
from test_bash_handler import DenyMatrix, assert_matrix_tables_wired


def _decision(r: dict) -> str | None:
    """``permissionDecision`` を取り出す (無ければ ``None``)。

    ``r["hookSpecificOutput"]["permissionDecision"]`` を直接書くと、判定が allow
    (= 空 dict) に退行したとき ``KeyError`` になり、テストが assertion failure では
    なく error で落ちる (``test_e2e._decision`` と同じ理由)。
    """
    return (r.get("hookSpecificOutput") or {}).get("permissionDecision")


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
        self.assertEqual(_decision(r), "deny")

    # (ラベル = 畳む前のテスト名, envelope)
    ASK_CASES = (
        ("test_default_returns_ask", {"permission_mode": "default"}),
        ("test_missing_mode_returns_ask", {}),
        # ask_or_deny は auto を lenient 扱いしない (旧仕様維持)
        ("test_auto_returns_ask", {"permission_mode": "auto"}),
    )

    def test_returns_ask(self):
        self.assertTrue(self.ASK_CASES)
        for case, envelope in self.ASK_CASES:
            with self.subTest(case=case, envelope=envelope):
                r = ask_or_deny("reason", envelope)
                self.assertEqual(_decision(r), "ask")


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
        self.assertIn("hookSpecificOutput", r, msg=repr(r))
        hook = r["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hook)
        self.assertNotIn("permissionDecisionReason", hook)
        self.assertEqual(hook["additionalContext"], output.LENIENT_ALLOW_CONTEXT)
        # reason 引数は allow では Claude に届かないチャネルなので、載せない
        self.assertNotIn("reason", hook["additionalContext"])

    # (ラベル = 畳む前のテスト名, envelope)。lenient allow に倒れる mode
    LENIENT_CASES = (
        ("test_auto_returns_allow", {"permission_mode": "auto"}),
        ("test_bypass_returns_allow", {"permission_mode": "bypassPermissions"}),
        # 0.13.0: plan を LENIENT_MODES に再追加 (ユーザー実機 2026-05-18 で plan
        # mode 中の Bash PreToolUse hook 発火を確認)。plan mode は副作用が plan
        # 承認まで保留される dry-run 状態のため autonomous と同等に lenient 扱い。
        ("test_plan_returns_allow", {"permission_mode": "plan"}),
    )

    # (ラベル = 畳む前のテスト名, envelope)。ask に倒れる mode
    ASK_CASES = (
        ("test_default_returns_ask", {"permission_mode": "default"}),
        # acceptEdits は Edit/Write 専用モード。Bash lenient の意図が無いため
        # 明示的に非 lenient 維持 (ask に倒る)。
        ("test_accept_edits_returns_ask", {"permission_mode": "acceptEdits"}),
        # dontAsk は明示的な非 lenient 判断として既存方針を維持する (ask に倒る)。
        ("test_dont_ask_returns_ask", {"permission_mode": "dontAsk"}),
        ("test_missing_mode_returns_ask", {}),
    )

    def test_returns_allow(self):
        self.assertTrue(self.LENIENT_CASES)
        for case, envelope in self.LENIENT_CASES:
            with self.subTest(case=case, envelope=envelope):
                r = ask_or_allow("reason", envelope)
                self._assert_lenient_allow(r)

    def test_returns_ask(self):
        self.assertTrue(self.ASK_CASES)
        for case, envelope in self.ASK_CASES:
            with self.subTest(case=case, envelope=envelope):
                r = ask_or_allow("reason", envelope)
                self.assertEqual(_decision(r), "ask")


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


class TestBashPatternsUnavailableDeny(DenyMatrix, _PatternsBase):
    """0.3.2: Bash handler の patterns.txt 読込失敗は全 mode で deny 固定。

    patterns が読めない = 安全な policy が無い。bash は ``ask_or_allow`` を広く使う
    ため、policy 欠如時に lenient で素通りすることを避けて ``make_deny`` で全停止する。

    判定は ``DenyMatrix`` が mode 6 列で見る (畳む前は default / auto / bypass の 3 列)。
    読込の失敗は setUp で立て、テストの間ずっと効かせる。
    """

    # 読込に失敗させるので、何を打っても deny になる (畳む前と同じ ``echo hi``)
    DENY_CASES = (
        ("test_default_deny / test_auto_deny / test_bypass_deny", "echo hi"),
    )

    def setUp(self):
        super().setUp()
        patcher = self._patch_load()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_load(self):
        return mock.patch(
            "handlers.bash_handler.load_patterns",
            side_effect=FileNotFoundError("test"),
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


class TestMatrixTablesAreWired(unittest.TestCase):
    """このファイルの表 (``DENY_CASES``) を持つクラスが土台と ``_PatternsBase`` を
    継承していること (``test_bash_handler.TestMatrixTablesAreWired`` と同じ検査)。"""

    def test_every_table_has_its_mixin(self):
        assert_matrix_tables_wired(self, globals(), _PatternsBase)


if __name__ == "__main__":
    unittest.main()
