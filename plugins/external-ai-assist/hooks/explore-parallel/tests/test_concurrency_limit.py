"""同時起動数の上限と注入バイト数の上限 (0.11.0)。

Claude は 1 メッセージで複数の Explore を並列起動するのが通常で、0.10.0 までは
その数だけ無条件に cursor が同時に走り、注入量も Explore の本数に比例して増えていた。

- 起動数: `state.live_analyzer_count()` (生きている pid ファイルの数) が
  `__main__.max_concurrent()` に達していたら起動しない
- 直列化: 数え上げ〜起動を `state.launch_gate()` の flock で排他にする
  (PreToolUse hook 自体が同時に走るため)。**CLI の検出はロックの外**で済ませる
- 注入量: 1 結果あたり `cursor.max_output_bytes()` で頭打ち。**同時に走れる本数 ×
  これ**が「1 度に注入されうる量」の上限で、**1 ターンの合計はこれでは縛られない**
  (枠は 1 本終わるごとに空くので、逐次なら完了した本数分だけ積まれる)
- 予算: pre は同期 hook なので、GC と検出が `hooks.json` の timeout を共有する

cursor 本体は起動しない (PATH 先頭の偽 cursor と、枠を埋めるための `sleep`)。
"""
import fcntl
import json
import os
import signal
import subprocess
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload

from _common import cursorcli

ENV_MAX_CONCURRENT = "EXTERNAL_AI_EXPLORE_MAX_CONCURRENT"
ENV_MAX_RESULT_BYTES = "EXTERNAL_AI_EXPLORE_MAX_RESULT_BYTES"
_HOOKS_JSON = _testutil._PKG_DIR.parent / "hooks.json"


def _pre_hook_timeout() -> float:
    """`hooks.json` の PreToolUse(Agent) の timeout (秒)。予算の突合に使う。"""
    hooks = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    for entry in hooks["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Agent":
            return float(entry["hooks"][0]["timeout"])
    raise AssertionError("hooks.json に PreToolUse(Agent) の登録が無い")


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

    # -- 走り続ける偽 cursor (枠を塞いだまま注入対象を作る) -----------------

    def long_running_cursor(self, output: str) -> None:
        """結果を書いてから走り続ける偽 cursor を PATH 先頭に置く。

        `fake_cursor()` は即終了するので枠がすぐ空く。「同時に走れる本数」を測るには
        走り続ける必要がある。**`exec` しない** (0.10.0 の note と同じ理由: argv を
        保たないと PID 同一性ガードから見て無関係なプロセスになる)。
        """
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                f"printf '%s' {json.dumps(output)}\n"
                "sleep 30 &\nwait\n"
            )
        os.chmod(path, 0o755)

    def wait_for_output(self, tool_use_id: str, timeout: float = 5.0) -> None:
        """偽 cursor が結果を書き出すまで待つ。

        `pre` は Popen した時点で返る = 結果ファイルはまだ空なので、待たずに停止すると
        「注入されうる量」を測るつもりで空の結果を測ることになる。
        """
        result_file, _ = self.state.paths(self.cursor.NAME, tool_use_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result_file.is_file() and result_file.stat().st_size > 0:
                return
            time.sleep(0.02)
        self.fail(f"偽 cursor ({tool_use_id}) が結果を書き出さなかった")

    def kill_and_reap(self, tool_use_id: str) -> None:
        """起動済みの偽 cursor を group ごと止めて reap する (post を待たせないため)。"""
        _, pid_file = self.state.paths(self.cursor.NAME, tool_use_id)
        pid = int(pid_file.read_text().strip())
        self._children.append(pid)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped == pid:
                break
            time.sleep(0.02)
        else:
            self.fail(f"偽 cursor (pid {pid}) を回収できなかった")
        if pid in self._children:
            self._children.remove(pid)

    # -- 注入本文 ----------------------------------------------------------

    def injected(self, output: str) -> str:
        """post の stdout から `additionalContext` のヘッダを除いた本文。"""
        body = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(body.startswith(self.cursor._CONTEXT_HEADER))
        return body[len(self.cursor._CONTEXT_HEADER) :]

    def strip_truncation_note(self, data: str, limit: int) -> str:
        """切詰マーカーが末尾に付いていることを確認して取り除く。"""
        note = self.cursor._TRUNCATION_NOTE.format(
            limit=limit, env=ENV_MAX_RESULT_BYTES
        )
        self.assertTrue(
            data.endswith(note), f"切詰マーカーが付いていない: {data[-80:]!r}"
        )
        return data[: -len(note)].rstrip("\n")


class TestConcurrencyCap(ConcurrencyTestCase):
    def test_second_launch_is_blocked_at_the_cap(self):
        argv_file = self.fake_cursor()
        self.occupy_slot("tu-running")

        with mock.patch.dict(os.environ, {ENV_MAX_CONCURRENT: "1"}):
            output = self.run_hook("pre", explore_payload("tu-blocked"))

        self.assertFalse(
            self.launched("tu-blocked"), "上限に達しているのに 2 本目が起動した"
        )
        self.assertFalse(os.path.exists(argv_file), "上限に達しているのに cursor が走った")
        self.assertIn("同時起動上限", self.last_stderr)
        # 落としたことは利用者に届く (stderr は exit 0 の hook では debug log 止まり)。
        # 先に生文字列で見る — 空出力のまま `json.loads` に渡すと failure ではなく
        # error になり、「テストが走っていない」と区別が付かなくなる
        # (本文は `json.dump` の既定で \\uXXXX に escape されるので ASCII 側で見る)。
        self.assertIn(
            "systemMessage", output, "上限で落としたことが通知されていない"
        )
        self.assertIn("同時起動上限", json.loads(output)["systemMessage"])

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
                output = self.run_hook("pre", explore_payload("tu-contended"))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        self.assertFalse(
            self.launched("tu-contended"), "ロックを取れていないのに起動した"
        )
        self.assertFalse(os.path.exists(argv_file))
        self.assertIn("競合", self.last_stderr)
        self.assertEqual(
            output, "", "一時的なロック競合まで systemMessage で通知している (雑音)"
        )

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

        payload = self.strip_truncation_note(self.injected(output), 100)
        self.assertEqual(len(payload), 100, "環境変数で指定した上限を超えて注入している")

    def test_truncation_drops_the_tail_and_says_so(self):
        """切るのは**末尾**なので、印が無いと親 Claude は「調査はここで終わった」と読む。

        マーカーには上限と上書き用の env 名を入れる (小さく設定した利用者が気付ける)。
        """
        result_file, _ = self.state.paths(self.cursor.NAME, "tu-cut")
        result_file.write_bytes(b"HEAD-MARKER" + b"y" * 5000 + b"TAIL-MARKER")

        with mock.patch.dict(os.environ, {ENV_MAX_RESULT_BYTES: "100"}):
            output = self.run_hook("post", explore_payload("tu-cut"))

        data = self.injected(output)
        self.assertIn("HEAD-MARKER", data)
        self.assertNotIn("TAIL-MARKER", data, "末尾ではなく先頭を切っている")
        self.assertIn("100", data)
        self.assertIn(ENV_MAX_RESULT_BYTES, data, "上書き用の env 名が案内されていない")

    def test_result_within_the_cap_is_not_marked(self):
        """切り詰めていない結果に余計な 1 行を足さない。"""
        result_file, _ = self.state.paths(self.cursor.NAME, "tu-small")
        result_file.write_bytes(b"y" * 50)

        with mock.patch.dict(os.environ, {ENV_MAX_RESULT_BYTES: "100"}):
            output = self.run_hook("post", explore_payload("tu-small"))

        self.assertEqual(self.injected(output), "y" * 50)

    def test_invalid_value_falls_back_to_default(self):
        for value in ("banana", "0", "-1", "1.5"):
            with mock.patch.dict(os.environ, {ENV_MAX_RESULT_BYTES: value}):
                self.assertEqual(
                    self.cursor.max_output_bytes(),
                    self.cursor.MAX_OUTPUT_BYTES,
                    f"{value!r} を上限として受理してしまった",
                )

    def test_concurrent_total_is_bounded_by_the_cap(self):
        """**同時に走れる本数** × 1 結果あたりの上限 = 1 度に注入されうる量の頭打ち。

        5 本の Explore を、起動済みの analyzer が走り続けている状態で連続して pre に
        通す。起動できるのは上限 (2) 本だけで、結果を持てるのも起動できた 2 本だけ
        なので、注入の合計も 2 × 上限に収まる。

        以前の `test_turn_total_is_bounded_by_the_cap` は
        `max_concurrent() * max_output_bytes() == 16000` という **2 定数の掛け算**で、
        実装のどこを壊しても落ちなかった (マージ前レビューの指摘)。ここでは実際に
        hook を走らせて起動本数と注入バイト数を測る。
        """
        ids = [f"tu-par-{i}" for i in range(5)]
        self.long_running_cursor("z" * 5000)

        with mock.patch.dict(
            os.environ, {ENV_MAX_CONCURRENT: "2", ENV_MAX_RESULT_BYTES: "100"}
        ):
            for tool_use_id in ids:
                self.run_hook("pre", explore_payload(tool_use_id))
            live = [tool_use_id for tool_use_id in ids if self.launched(tool_use_id)]
            self.assertEqual(
                live, ids[:2], "同時に走る本数が上限 (2) を超えている / 足りない"
            )

            for tool_use_id in live:
                self.wait_for_output(tool_use_id)
                self.kill_and_reap(tool_use_id)

            injected = []
            for tool_use_id in ids:
                output = self.run_hook("post", explore_payload(tool_use_id))
                if output:
                    injected.append(
                        self.strip_truncation_note(self.injected(output), 100)
                    )

        self.assertEqual(len(injected), 2, "起動できていない Explore にも結果が付いた")
        self.assertEqual(
            sum(len(payload) for payload in injected),
            200,
            "同時に注入されうる量が 2 × 100 バイトを超えている",
        )

    def test_sequential_completions_accumulate_beyond_the_cap(self):
        """**1 ターンの注入合計は上限では縛られない** (マージ前レビューの指摘)。

        `post()` は停止を確認した時点で pid ファイルを消す = 枠が空くので、同じターンの
        中で起動と完了を繰り返せば、完了した本数だけ注入が積まれる。文書側の表現を
        「同時に走る本数 × 1 結果あたりの上限」に狭めたのはこの事実に合わせたもので、
        「1 ターンの注入合計 = 同時起動数 × 上限」は成立しない。
        """
        self.fake_cursor(output="z" * 300)
        total = 0
        with mock.patch.dict(
            os.environ, {ENV_MAX_CONCURRENT: "2", ENV_MAX_RESULT_BYTES: "100"}
        ):
            for index in range(5):
                tool_use_id = f"tu-seq-{index}"
                self.run_hook("pre", explore_payload(tool_use_id))
                self.reap_cursor(tool_use_id)
                output = self.run_hook("post", explore_payload(tool_use_id))
                total += len(self.strip_truncation_note(self.injected(output), 100))

        self.assertEqual(total, 500, "逐次に完了した本数分だけ積まれていない")
        self.assertGreater(
            total, 2 * 100, "同時起動上限がターン合計を縛っているように見える"
        )


class TestPreBudget(ConcurrencyTestCase):
    """pre は同期 hook (`hooks.json` の timeout 5 秒)。GC + 検出 + ロック待ちの予算。

    0.11.0 で `is_available()` が最大 `cursorcli.PROBE_BUDGET_SEC` 秒の probe になり、
    しかも `launch_gate()` の**内側**で呼ばれていた (マージ前レビューの指摘)。
    GC 2 秒 + 検出 3 秒 + ロック待ち 0.5 秒 = 5.5 秒 > timeout 5 秒で hook 自体が
    kill され、現在の analyzer を起動できないまま次回も同じ残骸で同じところに嵌まる
    (0.10.0 で GC について一度直したのと同型)。加えて probe がロックの内側だと、
    cold cache では先頭の 1 本が probe する間に他の pre が待ちきれず、上限に届かない。
    """

    def setUp(self) -> None:
        super().setUp()
        self.probe_log = os.path.join(self.tmpdir, "probes")

    @contextmanager
    def detecting(self):
        """実際に検出を走らせる (テスト基底が固定している `ENV_COMMAND` を外す)。"""
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(cursorcli.ENV_COMMAND, None)
            cursorcli.reset()
            try:
                yield
            finally:
                cursorcli.reset()

    def hanging_candidate(self, name: str = "cursor-agent") -> None:
        """`--version` に応答しない候補を PATH 先頭に置く。

        **probe 記録の有無で判定するテストには使わない**: probe は timeout で group ごと
        kill されるため、bash の cold start が timeout より遅いと記録が残らない
        (経過時間で主張するテスト専用)。
        """
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n" "sleep 30 &\nwait\n")
        os.chmod(path, 0o755)

    def responding_candidate(self, name: str = "cursor-agent") -> None:
        """`--version` に即応答する候補 (呼ばれるたびに probe 記録へ 1 行足す)。

        probe が応答を待って返る = 記録の書き込みが終わっていることが保証されるので、
        「検出が走ったか」を記録の有無で決定論的に測れる。
        """
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                f"echo {name} >> {json.dumps(self.probe_log)}\n"
                "printf 'cursor-agent 2026.09.01\\n'\n"
            )
        os.chmod(path, 0o755)

    def probes(self) -> list[str]:
        try:
            with open(self.probe_log, encoding="utf-8") as f:
                return f.read().split()
        except OSError:
            return []

    def test_pre_budget_fits_the_hook_timeout(self):
        """`PRE_BUDGET_SEC` (GC と検出が共有) + ロック待ちが hooks.json の timeout 内。"""
        timeout = _pre_hook_timeout()
        self.assertLess(
            self.entry.PRE_BUDGET_SEC + self.state.LAUNCH_GATE_WAIT_SEC,
            timeout,
            "GC + 検出 + 起動枠の待ちが pre の hook timeout に収まらない",
        )
        self.assertGreater(
            self.state.GC_BUDGET_SEC
            + cursorcli.PROBE_BUDGET_SEC
            + self.state.LAUNCH_GATE_WAIT_SEC,
            timeout,
            "GC と検出がそれぞれ上限まで使っても timeout 内に収まるなら、"
            "予算を共有する必要がなくなっているので設計を見直す",
        )

    def test_detection_runs_before_taking_the_launch_gate(self):
        """検出はロックの外で走る (ロックが取れない回でも probe まで到達している)。

        probe をロックの内側に戻すと、ロックを取れなかった pre は検出を一切せずに
        返る = この probe 記録が空になる。
        """
        self.responding_candidate()
        self.state.BASE_DIR.mkdir(parents=True, exist_ok=True)
        holder = os.open(
            str(self.state.BASE_DIR / "launch.lock"), os.O_RDWR | os.O_CREAT, 0o600
        )
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with self.detecting(), mock.patch.object(
                self.state, "LAUNCH_GATE_WAIT_SEC", 0.05
            ):
                self.run_hook("pre", explore_payload("tu-gated"))
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        self.assertEqual(
            self.probes(),
            ["cursor-agent"],
            "ロックの内側で検出している (競合した回は probe に到達しない)",
        )
        self.assertFalse(self.launched("tu-gated"), "ロックを取れていないのに起動した")

    def test_detection_is_capped_by_the_remaining_pre_budget(self):
        """検出は `PRE_BUDGET_SEC` の締切で打ち切られる (probe 自前の上限より短ければ)。

        `__main__` が `deadline` を渡さないと、応答しない候補に
        `cursorcli.PROBE_BUDGET_SEC` 秒をまるごと使って hook timeout を食い潰す。
        """
        self.hanging_candidate()
        with self.detecting(), mock.patch.object(
            self.entry, "PRE_BUDGET_SEC", 0.5
        ), mock.patch.object(cursorcli, "PROBE_TIMEOUT_SEC", 10.0), mock.patch.object(
            cursorcli, "PROBE_BUDGET_SEC", 10.0
        ):
            started = time.monotonic()
            self.run_hook("pre", explore_payload("tu-budget"))
            elapsed = time.monotonic() - started
            if self.launched("tu-budget"):
                self.kill_and_reap("tu-budget")

        self.assertLess(
            elapsed, 3.0, "pre の予算が検出に効いていない (hook timeout を食い潰す)"
        )


if __name__ == "__main__":
    unittest.main()
