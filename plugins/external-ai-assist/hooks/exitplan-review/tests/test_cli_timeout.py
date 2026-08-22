"""cursor / codex ラッパを PATH 先頭の偽 CLI で検証する (argv / stdin / timeout / 失敗系)。

zh5.15: timeout 時に process group ごと停止し、stdout を継承した孫を取り残さない。
"""
import json
import os
import shlex
import time
import unittest
from unittest import mock

import _testutil
from _testutil import _PKG_DIR, HookTestCase

from _common import subproc

import codex
import cursor

# 偽 CLI が孫を起動して pid ファイルを書くまでの余裕を見て 1 秒 (遅い CI 対策)。
TIMEOUT = 1.0
GRACE = 0.5


def _write_script(path: str, body: str) -> None:
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


class FakeCliTestCase(HookTestCase):
    """TMPDIR/bin を PATH 先頭に置き、偽の `cursor` / `codex` を差し込む。"""

    def setUp(self) -> None:
        super().setUp()
        self.bin = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin)
        os.environ["PATH"] = self.bin + os.pathsep + os.environ.get("PATH", "")
        self._extra = [
            mock.patch.object(subproc, "KILL_GRACE_SEC", GRACE),
            mock.patch.object(self.cursor, "TIMEOUT_SEC", TIMEOUT),
            mock.patch.object(self.codex, "TIMEOUT_SEC", TIMEOUT),
        ]
        for p in self._extra:
            p.start()
        self._grandchildren: list[int] = []

    def tearDown(self) -> None:
        for pid in self._grandchildren:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        for p in self._extra:
            p.stop()
        super().tearDown()

    def fake(self, name: str, body: str) -> str:
        path = os.path.join(self.bin, name)
        _write_script(path, body)
        return path

    def hanging(self, name: str) -> str:
        """1 行出力後、孫 `sleep 30` に stdout を継承させて待つ偽 CLI。孫 pid をファイルに書く。"""
        pid_file = os.path.join(self.tmpdir, f"{name}-grandchild.pid")
        self.fake(
            name,
            "printf 'partial\\n'\nsleep 30 &\n" f"echo $! > {shlex.quote(pid_file)}\nwait\n",
        )
        return pid_file

    def grandchild(self, pid_file: str) -> int:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with open(pid_file) as f:
                    text = f.read().strip()
                if text:
                    pid = int(text)
                    self._grandchildren.append(pid)
                    return pid
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        raise AssertionError("孫の pid が書かれなかった")

    def assertDead(self, pid: int) -> None:
        """消えるか zombie になれば死亡扱い (PID 1 が reap しない環境では zombie が残る)。"""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if subproc.pid_is_zombie(pid):
                return
            time.sleep(0.05)
        self.fail(f"孫プロセス {pid} が取り残されている")


class TestReviewerTimeout(FakeCliTestCase):
    _ELAPSED_LIMIT = TIMEOUT + 3 * GRACE + 2.0

    def test_cursor_timeout_returns_none_and_kills_grandchild(self):
        pid_file = self.hanging("cursor")
        started = time.monotonic()
        result = self.cursor.review("plan body")
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, self._ELAPSED_LIMIT, "timeout 後に孫の EOF を待って止まっている")
        self.assertDead(self.grandchild(pid_file))

    def test_codex_timeout_returns_none_and_kills_grandchild(self):
        pid_file = self.hanging("codex")
        started = time.monotonic()
        result = self.codex.review("plan body")
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, self._ELAPSED_LIMIT)
        self.assertDead(self.grandchild(pid_file))


class TestTimeoutBudget(unittest.TestCase):
    """レビュアーの timeout + kill 猶予が hooks.json の ExitPlanMode timeout に収まること。

    超えるとハーネスの kill が先に来て、release_slot (枠を戻す) に到達しない。
    """

    def test_reviewer_timeouts_fit_in_hook_timeout(self):
        hooks = json.loads((_PKG_DIR.parent / "hooks.json").read_text())
        hook_timeout = None
        for entry in hooks["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "ExitPlanMode":
                hook_timeout = entry["hooks"][0]["timeout"]
        self.assertIsNotNone(hook_timeout)

        worst = max(cursor.TIMEOUT_SEC, codex.TIMEOUT_SEC) + 3 * subproc.KILL_GRACE_SEC
        self.assertLess(
            worst,
            hook_timeout,
            "レビュアー timeout + kill 猶予 (3 × KILL_GRACE_SEC) が ExitPlanMode の hook timeout を超えている",
        )


class TestReviewerArgvAndStdin(FakeCliTestCase):
    # argv は NUL 区切りで記録する (プロンプト本文に `---` や改行が含まれるため)
    _RECORD_ARGV = "for a in \"$@\"; do printf '%s\\0' \"$a\"; done > {argv_file}\n"

    def _read_argv(self, argv_file: str) -> list[str]:
        with open(argv_file) as f:
            return f.read().split("\0")[:-1]

    def test_cursor_runs_print_mode_read_only_with_plan_in_prompt(self):
        argv_file = os.path.join(self.tmpdir, "cursor.argv")
        self.fake(
            "cursor",
            self._RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
            + "printf 'REVIEW_CLEAN\\n'\n",
        )
        self.assertEqual(self.cursor.review("PLAN-BODY"), "REVIEW_CLEAN")

        args = self._read_argv(argv_file)
        self.assertEqual(args[:5], ["agent", "--trust", "--print", "--mode", "plan"])
        self.assertEqual(len(args), 6)
        self.assertIn("Planning Review — Cursor", args[5])
        self.assertIn("## レビュー対象プラン", args[5])
        self.assertIn("PLAN-BODY", args[5])

    def test_codex_gets_prompt_as_argument_and_plan_on_stdin(self):
        argv_file = os.path.join(self.tmpdir, "codex.argv")
        stdin_file = os.path.join(self.tmpdir, "codex.stdin")
        self.fake(
            "codex",
            self._RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
            + f"cat > {shlex.quote(stdin_file)}\n"
            "printf 'REVIEW_CLEAN\\n'\n",
        )
        self.assertEqual(self.codex.review("PLAN-BODY"), "REVIEW_CLEAN")

        args = self._read_argv(argv_file)
        self.assertEqual(args[:4], ["exec", "-s", "read-only", "--ephemeral"])
        self.assertEqual(len(args), 5)
        self.assertIn("Planning Review — Codex", args[4])
        with open(stdin_file) as f:
            self.assertEqual(f.read(), "PLAN-BODY")

    def test_nonzero_exit_is_none(self):
        self.fake("cursor", "printf 'boom'\nexit 2\n")
        self.assertIsNone(self.cursor.review("plan"))

    def test_empty_output_is_none(self):
        self.fake("codex", "cat > /dev/null\nprintf '  \\n'\n")
        self.assertIsNone(self.codex.review("plan"))

    def test_output_is_truncated_to_max_chars(self):
        self.fake("cursor", "head -c 20000 /dev/zero | tr '\\0' 'x'\n")
        result = self.cursor.review("plan")
        self.assertEqual(len(result), self.cursor.MAX_OUTPUT_BYTES)

    def test_missing_cli_is_none(self):
        os.environ["PATH"] = self.bin  # cursor / codex が無い PATH
        self.assertIsNone(self.cursor.review("plan"))
        self.assertIsNone(self.codex.review("plan"))


if __name__ == "__main__":
    unittest.main()
