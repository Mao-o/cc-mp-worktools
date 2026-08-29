"""cursor ラッパの起動引数を PATH 先頭の偽 cursor で固定する。

内部バックログの指摘: 3 hook とも cursor agent は読み取り専用 (`--mode plan`) で起動する。この hook は
0.2.0 から付いているが argv を assert するテストが無かった (他のテストは `review()` を
mock する) ので、`-p` 単独 (書込可能) に戻らないことをここで固定する。
"""
import os
import shlex
import tempfile
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

from _common import subproc

import cursor

# argv は NUL 区切りで記録する (プロンプト本文に改行や `---` が含まれるため)
_RECORD_ARGV = "for a in \"$@\"; do printf '%s\\0' \"$a\"; done > {argv_file}\n"


class TestReviewArgv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self._tmp.name, "bin")
        os.makedirs(self.bin)
        self._env = mock.patch.dict(
            os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        )
        self._env.start()
        # 偽 cursor は即終了する。万一ハングしても本番値 (600s + kill 猶予) を待たない
        self._patches = [
            mock.patch.object(cursor, "TIMEOUT_SEC", 5),
            mock.patch.object(subproc, "KILL_GRACE_SEC", 0.5),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_review_runs_cursor_agent_read_only_with_the_diff_in_the_prompt(self):
        argv_file = os.path.join(self._tmp.name, "cursor.argv")
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                + _RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
                + "printf 'REVIEW_CLEAN\\n'\n"
            )
        os.chmod(path, 0o755)

        self.assertEqual(cursor.review("DIFF-BODY"), "REVIEW_CLEAN")

        with open(argv_file, encoding="utf-8") as f:
            args = f.read().split("\0")[:-1]
        self.assertEqual(args[:5], ["agent", "--trust", "--print", "--mode", "plan"])
        self.assertEqual(len(args), 6, "プロンプトは最後の 1 引数")
        self.assertIn("## レビュー対象 git diff", args[5])
        self.assertIn("DIFF-BODY", args[5])


if __name__ == "__main__":
    unittest.main()
