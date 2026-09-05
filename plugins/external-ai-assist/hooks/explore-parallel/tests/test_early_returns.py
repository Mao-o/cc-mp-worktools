"""`_main()` の早期 return 群 (tool_use_id 欠落 / prompt 欠落 / アナライザ利用不可)。

`test_cursor_launch.py` の `test_non_explore_agent_does_not_start_cursor` と対になる、
残りの no-op 経路。cursor は起動せず、常に PATH 先頭の偽 CLI で「起動していないこと」を
確認する。
"""
import os
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload


class TestEmptyToolUseId(HookTestCase):
    def test_pre_is_noop_when_tool_use_id_is_empty(self):
        self.fake_cursor()
        output = self.run_hook("pre", explore_payload(""))

        self.assertEqual(output, "")
        # argv ファイルは偽 cursor が非同期に書くため、同期的に書かれる pid ファイルで判定する
        _, pid_file = self.state.paths(self.cursor.NAME, "")
        self.assertFalse(pid_file.exists(), "tool_use_id が空なのに cursor が起動した")

    def test_post_is_noop_when_tool_use_id_is_empty(self):
        self.fake_cursor()
        result_file, _ = self.state.paths(self.cursor.NAME, "")
        result_file.write_text("leftover")
        output = self.run_hook("post", explore_payload(""))

        self.assertEqual(output, "")
        self.assertTrue(result_file.exists(), "tool_use_id が空なのに post() が走った")


class TestEmptyPrompt(HookTestCase):
    def test_pre_skips_launch_when_prompt_is_empty(self):
        argv_file = self.fake_cursor()
        output = self.run_hook("pre", explore_payload("tu-empty-prompt", prompt=""))

        self.assertEqual(output, "")
        self.assertFalse(os.path.exists(argv_file), "prompt が空なのに cursor が起動した")
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-empty-prompt")
        self.assertFalse(pid_file.exists(), "起動していないのに pid ファイルがある")


class TestAnalyzerUnavailable(HookTestCase):
    """`is_available()` が False (CLI 不在等) なアナライザは pre/post どちらも no-op。"""

    def test_pre_skips_unavailable_analyzer(self):
        argv_file = self.fake_cursor()
        with mock.patch.object(self.cursor, "is_available", return_value=False):
            output = self.run_hook("pre", explore_payload("tu-unavail"))

        self.assertEqual(output, "")
        self.assertFalse(os.path.exists(argv_file), "利用不可のはずの cursor が起動した")
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-unavail")
        self.assertFalse(pid_file.exists())

    def test_post_skips_unavailable_analyzer_even_if_result_exists(self):
        """post 時点で不可になった (直前まで available だった) 場合も何も注入しない。"""
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-unavail-post")
        result_file.write_text("leftover result")

        with mock.patch.object(self.cursor, "is_available", return_value=False):
            output = self.run_hook("post", explore_payload("tu-unavail-post"))

        self.assertEqual(output, "", "利用不可のアナライザの結果を注入してしまっている")
        # is_available=False で post() 自体を呼ばないので、掃除もされない (現状の契約)。
        self.assertTrue(result_file.exists())


if __name__ == "__main__":
    unittest.main()
