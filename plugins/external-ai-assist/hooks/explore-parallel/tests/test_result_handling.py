"""cursor.post() の結果整形 (pid ファイル欠落 / 出力切詰)。

`test_cursor_launch.py` は「正常系 (pre→post)」を通しで検証する。ここは
`cursor.post()` を直接呼び、境界ケース (pid ファイルが無い、出力が
MAX_OUTPUT_BYTES を超える) を単体で固定する。待機が TIMEOUT_SEC を超えた経路は
`test_orphan_gc.TestGroupKill` (孫まで止まること) と `TestPostRetention` が見る。
"""
import unittest

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase


class TestPidFileMissing(HookTestCase):
    """pid ファイルが無くても結果ファイルがあれば読んで返す (現状の契約)。

    `pre()` は result_file と pid_file を必ずペアで書くので通常は起きないが、
    pid ファイルだけ何らかの理由で消えた場合の fallback 経路を固定する。
    """

    def test_post_returns_result_when_pid_file_is_missing(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-nopid")
        result_file.write_text("orphan result")
        self.assertFalse(pid_file.exists())

        result = self.cursor.post("tu-nopid")

        self.assertEqual(result, self.cursor._CONTEXT_HEADER + "orphan result")
        self.assertFalse(result_file.exists(), "結果ファイルが掃除されていない")

    def test_post_returns_none_when_neither_file_exists(self):
        self.assertIsNone(self.cursor.post("tu-nothing"))


class TestOutputTruncation(HookTestCase):
    def test_output_over_max_bytes_is_truncated(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-huge")
        result_file.write_bytes(b"x" * 16000)

        result = self.cursor.post("tu-huge")

        # 既定値は README (`EXTERNAL_AI_EXPLORE_MAX_RESULT_BYTES` = 8000) の契約なので定数を読まない
        body = result[len(self.cursor._CONTEXT_HEADER) :]
        note = self.cursor._TRUNCATION_NOTE.format(
            limit=8000, env=self.cursor.ENV_MAX_RESULT_BYTES
        )
        self.assertTrue(body.endswith(note), "切詰マーカーが付いていない")
        self.assertEqual(len(body[: -len(note)].rstrip("\n")), 8000)


if __name__ == "__main__":
    unittest.main()
