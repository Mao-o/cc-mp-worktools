"""`_main()` の早期 return 群 (tool_use_id 欠落 / prompt 欠落 / アナライザ利用不可)。

`test_cursor_launch.py` の `test_non_explore_agent_does_not_start_cursor` と対になる、
残りの no-op 経路。cursor は起動せず、常に PATH 先頭の偽 CLI で「起動していないこと」を
確認する。
"""
import os
import time
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
    """`is_available()` は pre (起動判断) のゲート。何も起動していなければ post も no-op
    になるが、起動済みの analyzer がいる場合は post が unavailable でも reap する
    (マージ前レビューの指摘: is_available() の可否と起動済みプロセスの後始末は無関係)。
    """

    def test_pre_skips_unavailable_analyzer(self):
        argv_file = self.fake_cursor()
        with mock.patch.object(self.cursor, "is_available", return_value=False):
            output = self.run_hook("pre", explore_payload("tu-unavail"))

        self.assertEqual(output, "")
        self.assertFalse(os.path.exists(argv_file), "利用不可のはずの cursor が起動した")
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-unavail")
        self.assertFalse(pid_file.exists())

    def test_post_is_noop_when_unavailable_and_nothing_was_started(self):
        """何も起動していなければ、post が unavailable でも何も注入しない (真の no-op)。"""
        with mock.patch.object(self.cursor, "is_available", return_value=False):
            output = self.run_hook("post", explore_payload("tu-unavail-nothing"))

        self.assertEqual(output, "")
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-unavail-nothing")
        self.assertFalse(result_file.exists())
        self.assertFalse(pid_file.exists())

    def test_post_reaps_started_analyzer_even_if_it_becomes_unavailable(self):
        """pre 時点では available だった cursor が、post までに PATH から消えても、
        起動済みの analyzer (pid ファイルが指す稼働中プロセス) を reap し、
        pid / 結果ファイルを掃除する。
        """
        cursor_path = os.path.join(self.bin, "cursor")
        with open(cursor_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\nexec sleep 30\n")
        os.chmod(cursor_path, 0o755)

        with mock.patch.object(self.cursor, "TIMEOUT_SEC", 0.2), mock.patch.object(
            self.cursor, "POLL_INTERVAL_SEC", 0.05
        ):
            # pre 時点では is_available() を mock しない (実際に PATH 先頭の偽 cursor で
            # 起動させる)。post だけ False にする — PATH から実体を消す方式は、開発機に
            # 本物の cursor が入っていると shutil.which が別パスへフォールバックして
            # 信頼できないため使わない。
            self.run_hook("pre", explore_payload("tu-unavail-post"))

            result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-unavail-post")
            self.assertTrue(pid_file.is_file(), "pre が pid ファイルを書いていない")
            pid = int(pid_file.read_text().strip())
            self._children.append(pid)

            with mock.patch.object(self.cursor, "is_available", return_value=False):
                output = self.run_hook("post", explore_payload("tu-unavail-post"))

        self.assertEqual(output, "", "結果を生成していないアナライザの additionalContext は空のはず")

        # SIGTERM 送信後、テストプロセスが reap するまで zombie として残るため、
        # 先に reap してから生死判定する (test_result_handling.py と同じ配慮)。
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped == pid:
                break
            time.sleep(0.02)
        try:
            os.kill(pid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False
        self.assertFalse(alive, "利用不可になった後も cursor プロセスが reap されず残っている (orphan)")
        self._children.remove(pid)

        self.assertFalse(pid_file.exists(), "利用不可になった analyzer の pid ファイルが残っている (orphan)")
        self.assertFalse(result_file.exists(), "利用不可になった analyzer の結果ファイルが残っている (orphan)")


if __name__ == "__main__":
    unittest.main()
