"""同時起動数の上限と注入バイト数の上限 (0.11.0)。

Claude は 1 メッセージで複数の Explore を並列起動するのが通常で、0.10.0 までは
その数だけ無条件に cursor が同時に走り、注入量も Explore の本数に比例して増えていた。

- 起動数: `state.live_analyzer_count()` (生きている pid ファイルの数) が
  `__main__.max_concurrent()` に達していたら起動しない
- 直列化: 数え上げ〜起動を `state.launch_gate()` の flock で排他にする
  (PreToolUse hook 自体が同時に走るため)
- 注入量: 1 結果あたり `cursor.max_output_bytes()` で頭打ち。1 ターンの合計は
  「同時起動数 × これ」で決まる

cursor 本体は起動しない (PATH 先頭の偽 cursor と、枠を埋めるための `sleep`)。
"""
import fcntl
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload

ENV_MAX_CONCURRENT = "EXTERNAL_AI_EXPLORE_MAX_CONCURRENT"
ENV_MAX_RESULT_BYTES = "EXTERNAL_AI_EXPLORE_MAX_RESULT_BYTES"


class ConcurrencyTestCase(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except OSError:
                pass
            if proc.pid in self._children:
                self._children.remove(proc.pid)
        super().tearDown()

    # -- 枠を埋めるヘルパー ------------------------------------------------

    def occupy_slot(self, tool_use_id: str) -> int:
        """生きているプロセスを指す pid ファイルを 1 つ置き、その pid を返す。

        偽 cursor ではなく `sleep` を使う: ここで見たいのは「pid ファイルが指す
        プロセスが生きているか」だけで、argv の同一性 (停止経路の判定) は関係ない。
        """
        proc = subprocess.Popen(["sleep", "30"])
        self._procs.append(proc)
        self._children.append(proc.pid)
        _, pid_file = self.state.paths(self.cursor.NAME, tool_use_id)
        pid_file.write_text(str(proc.pid))
        return proc.pid

    def dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait(timeout=5)
        return proc.pid

    def launched(self, tool_use_id: str) -> bool:
        _, pid_file = self.state.paths(self.cursor.NAME, tool_use_id)
        return pid_file.is_file()


class TestConcurrencyCap(ConcurrencyTestCase):
    def test_second_launch_is_blocked_at_the_cap(self):
        argv_file = self.fake_cursor()
        self.occupy_slot("tu-running")

        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "1"}):
            output = self.run_hook("pre", explore_payload("tu-blocked"))

        self.assertEqual(output, "")
        self.assertFalse(
            self.launched("tu-blocked"), "上限に達しているのに 2 本目が起動した"
        )
        self.assertFalse(os.path.exists(argv_file), "上限に達しているのに cursor が走った")
        self.assertIn("同時起動上限", self.last_stderr)

    def test_launch_proceeds_below_the_cap(self):
        self.fake_cursor()
        self.occupy_slot("tu-running")

        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "2"}):
            self.run_hook("pre", explore_payload("tu-allowed"))

        self.assertTrue(self.launched("tu-allowed"), "枠が残っているのに起動していない")
        self.reap_cursor("tu-allowed")

    def test_dead_pid_file_does_not_hold_a_slot(self):
        """終了済みのプロセスを指す pid ファイル (GC 前の残骸) は枠を塞がない。"""
        self.fake_cursor()
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-dead")
        pid_file.write_text(str(self.dead_pid()))

        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "1"}):
            self.run_hook("pre", explore_payload("tu-after-dead"))

        self.assertTrue(self.launched("tu-after-dead"), "死んだ pid が枠を塞いでいる")
        self.reap_cursor("tu-after-dead")

    def test_default_cap_is_two(self):
        """既定 (env 未設定) では 2 本目まで起動し、3 本目は起動しない。"""
        self.fake_cursor()
        self.occupy_slot("tu-run-1")
        self.run_hook("pre", explore_payload("tu-second"))
        self.assertTrue(self.launched("tu-second"), "既定の上限 2 で 2 本目が起動しない")
        self.reap_cursor("tu-second")

        self.occupy_slot("tu-run-2")
        self.run_hook("pre", explore_payload("tu-third"))
        self.assertFalse(self.launched("tu-third"), "既定の上限 2 を超えて 3 本目が起動した")

    def test_invalid_cap_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "banana"}):
            self.assertEqual(
                self.entry.max_concurrent(), self.state.DEFAULT_MAX_CONCURRENT
            )
        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "0"}):
            self.assertEqual(
                self.entry.max_concurrent(),
                self.state.DEFAULT_MAX_CONCURRENT,
                "0 は『止める』ではなく既定に倒す (止めるのは EXTERNAL_AI_EXPLORE_PARALLEL=0)",
            )
        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "-3"}):
            self.assertEqual(
                self.entry.max_concurrent(), self.state.DEFAULT_MAX_CONCURRENT
            )


class TestLaunchGate(ConcurrencyTestCase):
    """数え上げ〜起動の直列化。`launch_gate()` が取れない回は起動しない。"""

    def test_contended_gate_skips_launch(self):
        argv_file = self.fake_cursor()
        self.state.BASE_DIR.mkdir(parents=True, exist_ok=True)
        holder = os.open(
            str(self.state.BASE_DIR / "launch.lock"), os.O_RDWR | os.O_CREAT, 0o600
        )
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with mock.patch.object(self.state, "LAUNCH_GATE_WAIT_SEC", 0.05):
                self.run_hook("pre", explore_payload("tu-contended"))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        self.assertFalse(
            self.launched("tu-contended"), "ロックを取れていないのに起動した"
        )
        self.assertFalse(os.path.exists(argv_file))
        self.assertIn("競合", self.last_stderr)

    def test_gate_is_released_after_use(self):
        """1 回使った後に別の呼び出しが取れること (ロックを握りっぱなしにしない)。"""
        self.fake_cursor()
        self.run_hook("pre", explore_payload("tu-first"))
        self.reap_cursor("tu-first")
        with self.state.launch_gate() as acquired:
            self.assertTrue(acquired, "前回の pre がロックを解放していない")

    def test_gate_falls_open_when_lock_file_cannot_be_created(self):
        """ロックファイルを作れない環境では直列化を諦めて進む (fail-open)。"""
        unwritable = Path(self._tmp.name) / "nope" / "deeper"
        with mock.patch.object(self.state, "BASE_DIR", unwritable), mock.patch.object(
            self.state.Path, "mkdir", side_effect=OSError("read-only")
        ):
            with self.state.launch_gate() as acquired:
                self.assertTrue(acquired)


class TestLiveAnalyzerCount(ConcurrencyTestCase):
    def test_counts_only_live_pid_files(self):
        self.assertEqual(self.state.live_analyzer_count(), 0)
        self.occupy_slot("tu-a")
        self.assertEqual(self.state.live_analyzer_count(), 1)
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-b")
        pid_file.write_text(str(self.dead_pid()))
        self.assertEqual(self.state.live_analyzer_count(), 1, "死んだ pid を数えている")

    def test_ignores_unparsable_and_non_pid_files(self):
        self.occupy_slot("tu-a")
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-broken")
        pid_file.write_text("not-a-pid")
        result_file.write_text("結果だけの残骸")
        (self.state.BASE_DIR / "launch.lock").write_text("")
        self.assertEqual(self.state.live_analyzer_count(), 1)

    def test_missing_state_dir_counts_zero(self):
        with mock.patch.object(
            self.state, "BASE_DIR", Path(self._tmp.name) / "missing"
        ):
            self.assertEqual(self.state.live_analyzer_count(), 0)


class TestInjectionBudget(ConcurrencyTestCase):
    def test_env_caps_the_injected_result(self):
        result_file, _ = self.state.paths(self.cursor.NAME, "tu-inject")
        result_file.write_bytes(b"y" * 5000)

        with mock.patch.dict(os.environ, {ENV_MAX_RESULT_BYTES: "100"}):
            output = self.run_hook("post", explore_payload("tu-inject"))

        body = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            len(body.replace(self.cursor._CONTEXT_HEADER, "")),
            100,
            "環境変数で指定した上限を超えて注入している",
        )

    def test_invalid_value_falls_back_to_default(self):
        for value in ("banana", "0", "-1", "1.5"):
            with mock.patch.dict(os.environ, {ENV_MAX_RESULT_BYTES: value}):
                self.assertEqual(
                    self.cursor.max_output_bytes(),
                    self.cursor.MAX_OUTPUT_BYTES,
                    f"{value!r} を上限として受理してしまった",
                )

    def test_turn_total_is_bounded_by_the_cap(self):
        """1 ターンの注入合計 = 同時起動数 × 1 結果あたりの上限。

        この関係が崩れる (= 上限を掛け合わせても合計が抑えられない) 形に変えるときは、
        README / CLAUDE.md の記述も直す必要があるため、算術として固定しておく。
        """
        with mock.patch.dict(
            os.environ, {ENV_MAX_CONCURRENT: "2", ENV_MAX_RESULT_BYTES: "8000"}
        ):
            self.assertEqual(
                self.entry.max_concurrent() * self.cursor.max_output_bytes(), 16000
            )


if __name__ == "__main__":
    unittest.main()
