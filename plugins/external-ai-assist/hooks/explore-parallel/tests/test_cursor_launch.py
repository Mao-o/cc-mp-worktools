"""explore-parallel の cursor 起動引数と結果注入を PATH 先頭の偽 cursor で固定する。

zh5.2: Explore の裏で走る cursor agent は読み取り専用 (`--mode plan`) でなければならない。
0.4.0 までは `-p` 単独 (cursor-agent の help では書込・shell 実行可能) で起動していた。
"""
import json
import os
import sys
import unittest

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import _PKG_DIR, HookTestCase, explore_payload, load_entry

from _common import cursorcli

READ_ONLY_PREFIX = ["agent", "--trust", "--print", "--mode", "plan"]


class TestPreLaunchesReadOnlyCursor(HookTestCase):
    def test_pre_starts_cursor_agent_in_plan_mode_with_the_explore_prompt(self):
        argv_file = self.fake_cursor()
        output = self.run_hook("pre", explore_payload("tu-1", "find the auth flow"))
        self.reap_cursor("tu-1")

        self.assertEqual(output, "", "pre は何も出力しない")
        args = self.read_argv(argv_file)
        self.assertEqual(args[:5], READ_ONLY_PREFIX)
        self.assertEqual(len(args), 6, "プロンプトは最後の 1 引数")
        self.assertIn("find the auth flow", args[5])
        self.assertIn("セマンティック検索", args[5])

    def test_pre_argv_is_the_shared_readonly_argv(self):
        """3 hook 統一: argv は `_common.cursorcli.readonly_argv` と完全一致する。"""
        argv_file = self.fake_cursor()
        self.run_hook("pre", explore_payload("tu-2", "PROMPT-BODY"))
        self.reap_cursor("tu-2")

        args = self.read_argv(argv_file)
        expected = cursorcli.readonly_argv(
            self.cursor._PROMPT_TEMPLATE.format(prompt="PROMPT-BODY")
        )
        self.assertEqual([cursorcli.BINARY, *args], expected)

    def test_non_explore_agent_does_not_start_cursor(self):
        argv_file = self.fake_cursor()
        output = self.run_hook(
            "pre", explore_payload("tu-3", subagent_type="general-purpose")
        )

        self.assertEqual(output, "")
        self.assertFalse(os.path.exists(argv_file), "Explore 以外で cursor が起動した")
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-3")
        self.assertFalse(pid_file.exists())


class TestPostInjectsResult(HookTestCase):
    def test_post_returns_fake_cursor_output_as_additional_context_and_cleans_up(self):
        self.fake_cursor(output="## 関連コード\n- src/auth.py: 同じ概念を別名で実装\n")
        self.run_hook("pre", explore_payload("tu-4"))
        self.reap_cursor("tu-4")

        output = self.run_hook("post", explore_payload("tu-4"))

        data = json.loads(output)
        hso = data["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        self.assertIn("## Cursor Agent による補助調査結果", hso["additionalContext"])
        self.assertIn("src/auth.py", hso["additionalContext"])
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-4")
        self.assertFalse(result_file.exists(), "結果ファイルが掃除されていない")
        self.assertFalse(pid_file.exists(), "pid ファイルが掃除されていない")


class TestTimeoutBudget(unittest.TestCase):
    """post の待機上限が hooks.json の PostToolUse(Agent) timeout に収まること。

    超えるとハーネスの kill が先に来て、cursor の停止と結果ファイルの掃除に到達しない。
    """

    def test_post_wait_fits_in_hook_timeout(self):
        hooks = json.loads((_PKG_DIR.parent / "hooks.json").read_text(encoding="utf-8"))
        hook_timeout = None
        for entry in hooks["hooks"]["PostToolUse"]:
            if entry.get("matcher") == "Agent":
                hook_timeout = entry["hooks"][0]["timeout"]
        self.assertIsNotNone(hook_timeout)

        load_entry()
        cursor = sys.modules["cursor"]  # HookTestCase の patch は tearDown 済みで本番値
        # 待機ループは POLL_INTERVAL_SEC 刻みで waited を進めるため、最悪 1 刻み分超過する
        self.assertLess(
            cursor.TIMEOUT_SEC + cursor.POLL_INTERVAL_SEC,
            hook_timeout,
            "cursor の待機上限 + poll 1 刻みが PostToolUse(Agent) の hook timeout を超えている",
        )


if __name__ == "__main__":
    unittest.main()
