"""cursor.post() の結果整形 (pid ファイル欠落 / 出力切詰 / timeout kill)。

`test_cursor_launch.py` は「正常系 (pre→post)」を通しで検証する。ここは
`cursor.post()` を直接呼び、境界ケース (pid ファイルが無い、出力が
MAX_OUTPUT_BYTES を超える、待機が TIMEOUT_SEC を超える) を単体で固定する。
"""
import os
import time
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload


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
        result_file.write_bytes(b"x" * (self.cursor.MAX_OUTPUT_BYTES * 2))

        result = self.cursor.post("tu-huge")

        body = result[len(self.cursor._CONTEXT_HEADER) :]
        self.assertEqual(len(body), self.cursor.MAX_OUTPUT_BYTES)


class TestReviewerTimeout(HookTestCase):
    """post の待機が TIMEOUT_SEC を超えたら SIGTERM して None を返す。"""

    def _write_hanging_cursor(self) -> None:
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\nexec sleep 30\n")
        os.chmod(path, 0o755)

    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def test_timeout_kills_process_and_returns_no_result(self):
        self._write_hanging_cursor()

        with mock.patch.object(self.cursor, "TIMEOUT_SEC", 0.2), mock.patch.object(
            self.cursor, "POLL_INTERVAL_SEC", 0.05
        ):
            self.run_hook("pre", explore_payload("tu-timeout"))
            _, pid_file = self.state.paths(self.cursor.NAME, "tu-timeout")
            pid = int(pid_file.read_text().strip())
            self._children.append(pid)

            output = self.run_hook("post", explore_payload("tu-timeout"))

        self.assertEqual(output, "", "timeout 後は additionalContext を出さない")

        # SIGTERM 送信後、子プロセスは終了するがテストプロセス (親) が reap するまで
        # zombie のまま残る。zombie は kill(pid, 0) に成功し続けるため、先に reap してから
        # 生死判定する (exitplan-review/tests/test_cli_timeout.py の assertDead と同じ配慮)。
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped == pid:
                break
            time.sleep(0.02)
        self.assertFalse(self._alive(pid), "timeout 後に cursor プロセスが残っている")
        self._children.remove(pid)


if __name__ == "__main__":
    unittest.main()
