"""cursor agent の起動 argv 契約 (内部バックログの指摘: 3 hook 共通で読み取り専用 `--mode plan`)。

`-p` / `--print` 単独は cursor-agent の help で「Has access to all tools, including write and
shell」とされる書込可能モード。`readonly_argv` がそこへ戻らないことを固定する。
"""
import unittest

import _testutil  # noqa: F401  (hooks/ を sys.path に載せる)

from _common import cursorcli

READ_ONLY_PREFIX = ["cursor", "agent", "--trust", "--print", "--mode", "plan"]


class TestReadonlyArgv(unittest.TestCase):
    def test_runs_print_mode_as_read_only_plan_mode(self):
        self.assertEqual(cursorcli.readonly_argv("PROMPT"), [*READ_ONLY_PREFIX, "PROMPT"])

    def test_prompt_is_passed_verbatim_as_the_last_single_argument(self):
        # フラグに見える本文 (改行 / `--mode agent` / `-p`) も分割されず 1 引数のまま末尾に付く
        prompt = "line 1\n--mode agent\n-p --trust"
        argv = cursorcli.readonly_argv(prompt)
        self.assertEqual(argv[:-1], READ_ONLY_PREFIX)
        self.assertEqual(argv[-1], prompt)

    def test_binary_name_matches_availability_check(self):
        self.assertEqual(cursorcli.readonly_argv("x")[0], cursorcli.BINARY)
        self.assertEqual(cursorcli.BINARY, "cursor")


if __name__ == "__main__":
    unittest.main()
