"""``< target`` 入力リダイレクトの target 抽出と handle() 経由判定 (0.3.2)。

heredoc (``<<``), fd dup (``<&N``), process substitution (``<(...)``), 数値 fd
前置 (``0<``) は regex で除外され、抽出後は後段の ``ask_or_allow`` に倒る。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from handlers.bash_handler import _extract_input_redirect_targets, handle


def _make_envelope(cmd: str, cwd: str, mode: str = "default") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "description": "test"},
        "cwd": cwd,
        "permission_mode": mode,
    }


def _decision(resp: dict) -> str | None:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


class TestExtractInputRedirectTargets(unittest.TestCase):
    """regex の単体テスト。"""

    def test_simple_target(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat < .env"),
            [".env"],
        )

    def test_target_with_extension(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat < .env.local"),
            [".env.local"],
        )

    def test_no_space_no_match(self):
        # `cat<.env` は空白無しなので regex は拾わない
        self.assertEqual(_extract_input_redirect_targets("cat<.env"), [])

    def test_heredoc_excluded(self):
        self.assertEqual(_extract_input_redirect_targets("cat << EOF"), [])
        self.assertEqual(_extract_input_redirect_targets("cat <<EOF"), [])

    def test_process_sub_excluded(self):
        # `<(` の `(` は \s+ にマッチしないので抽出されない
        self.assertEqual(
            _extract_input_redirect_targets("cat <(cat .env)"),
            [],
        )

    def test_fd_dup_excluded(self):
        self.assertEqual(_extract_input_redirect_targets("cat <&2"), [])

    def test_digit_fd_prefix_excluded(self):
        # `0<` は数値 fd 前置 → 除外
        self.assertEqual(_extract_input_redirect_targets("cat 0< .env"), [])

    def test_multiple_targets(self):
        self.assertEqual(
            _extract_input_redirect_targets(
                "cat < .env && cat < .env.local"
            ),
            [".env", ".env.local"],
        )


class _BaseHandle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        os.makedirs(self.xdg)
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestHandleInputRedirect(_BaseHandle):
    """handle() 経由の挙動。target 一致なら deny 固定、それ以外は ask_or_allow。"""

    def test_cat_lt_dotenv_deny(self):
        r = handle(_make_envelope("cat < .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_lt_dotenv_local_deny(self):
        r = handle(_make_envelope("cat < .env.local", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_lt_readme_default_ask(self):
        # target 非機密 → 後段の hard_stop ask_or_allow に倒れる
        r = handle(_make_envelope("cat < README.md", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_cat_lt_readme_auto_allow(self):
        r = handle(_make_envelope("cat < README.md", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_heredoc_default_ask(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_heredoc_auto_allow(self):
        r = handle(
            _make_envelope("cat <<EOF\nhello\nEOF", self.tmp, mode="auto")
        )
        self.assertEqual(r, {})

    def test_process_sub_default_ask(self):
        # `<(` は process sub。`(` も別の hard-stop として効くが、いずれにせよ ask
        r = handle(_make_envelope("cat <(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_dotenv_glob_target_deny(self):
        r = handle(_make_envelope("cat < .env*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_dotenv_example_target_default_ask(self):
        # target literal は exclude 決着 → 機密判定 False。後段 ask_or_allow に倒れる
        r = handle(_make_envelope("cat < .env.example", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bypass_dotenv_lt_deny(self):
        r = handle(_make_envelope("cat < .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")


if __name__ == "__main__":
    unittest.main()
