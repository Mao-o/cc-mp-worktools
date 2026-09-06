"""孤児 analyzer の停止 (process group / PID 再利用ガード) と残骸の TTL GC。

0.9.1 までは `post()` が `os.kill(pid, SIGTERM)` でグループリーダーだけを止めており、
cursor-agent が生成した孫プロセスが走り続けていた。また post が来ない経路
(Agent ツールの失敗・中断・セッション終了・`async` hook の teardown kill) では
pid / 結果ファイルが無期限に残っていた (内部バックログ)。

いずれも `cursor` 本体は起動せず、PATH 先頭の偽 cursor (bash script) で検証する。
"""
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class OrphanTestCase(HookTestCase):
    """孫プロセス / 無関係プロセスを確実に回収する tearDown を足した基底クラス。"""

    def setUp(self) -> None:
        super().setUp()
        self._extra_pids: list[int] = []
        self._procs: list[subprocess.Popen] = []
        self._grace = mock.patch.object(self.cursor, "KILL_GRACE_SEC", 1.0)
        self._grace.start()

    def tearDown(self) -> None:
        """起動した孫・無関係プロセスを 1 つ残らず回収する (`sleep` の孤児を作らない)。"""
        for pid in self._extra_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for proc in self._procs:
            try:
                proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                pass
        for pid in self._extra_pids:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        self._grace.stop()
        super().tearDown()

    def spawn_unrelated(self) -> subprocess.Popen:
        """analyzer ではない生存プロセス (自前の process group) を起動して返す。"""
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._procs.append(proc)
        self._extra_pids.append(proc.pid)
        return proc

    def assert_unharmed(self, proc: subprocess.Popen, msg: str) -> None:
        """`proc` に signal が届いていないこと。

        **`os.kill(pid, 0)` では判定できない**。この犠牲プロセスは test プロセスの
        直接の子なので、SIGTERM を受けても誰も `wait` しないうちは zombie として残り、
        `os.kill(pid, 0)` は成功し続ける。「撃たれたのに生きている」と読めてしまい、
        ガードを外す mutation を素通りさせる (実際に mutation で空振りを観測した)。
        親である test プロセス自身が `poll()` すれば reap して終了を検出できる。
        """
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        self.assertIsNone(proc.poll(), msg)

    def fake_cursor_with_grandchild(self) -> str:
        """孫 `sleep 30` に stdout を継承させたまま待つ偽 cursor。孫 pid の記録先を返す。"""
        gc_pid_file = os.path.join(self.tmpdir, "cursor-grandchild.pid")
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                "sleep 30 &\n"
                f"echo $! > {gc_pid_file}\n"
                "wait\n"
            )
        os.chmod(path, 0o755)
        return gc_pid_file

    def fake_cursor_with_stubborn_grandchild(self) -> str:
        """SIGTERM を**無視する**孫を持つ偽 cursor。孫 pid の記録先を返す。

        `terminate()` の SIGTERM → 猶予 → SIGKILL のエスカレーションを確かめる用。
        cursor-agent (node) が TERM を受けても終了処理から抜けられない状況の代役。

        孫は **SIG_IGN を設定し終えてから** pid ファイルを書く。`sleep &` + `echo $!` だと
        ハンドラ設置前に SIGTERM が届きうるので、既定動作で死んだのか SIGKILL で死んだのか
        区別が付かず、エスカレーションを外す mutation を素通りさせる。
        """
        gc_pid_file = os.path.join(self.tmpdir, "cursor-stubborn.pid")
        script = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({gc_pid_file!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} &\n"
                "wait\n"
            )
        os.chmod(path, 0o755)
        return gc_pid_file

    def read_grandchild(self, gc_pid_file: str) -> int:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with open(gc_pid_file, encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    pid = int(text)
                    self._extra_pids.append(pid)
                    return pid
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        self.fail("偽 cursor の孫 pid が記録されなかった")


class TestGroupKill(OrphanTestCase):
    """停止は process group ごと (リーダーだけを止めて孫を取り残さない)。"""

    def test_post_timeout_kills_the_whole_process_group(self):
        gc_pid_file = self.fake_cursor_with_grandchild()

        with mock.patch.object(self.cursor, "TIMEOUT_SEC", 0.2), mock.patch.object(
            self.cursor, "POLL_INTERVAL_SEC", 0.05
        ):
            self.run_hook("pre", explore_payload("tu-group"))
            _, pid_file = self.state.paths(self.cursor.NAME, "tu-group")
            leader = int(pid_file.read_text().strip())
            self._children.append(leader)
            grandchild = self.read_grandchild(gc_pid_file)
            self.assertTrue(_alive(grandchild), "偽 cursor の孫が起動していない")

            self.run_hook("post", explore_payload("tu-group"))

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.05)
        self.assertFalse(
            _alive(grandchild),
            "孫プロセスが取り残されている (リーダーだけ SIGTERM している)",
        )

    def test_a_group_that_ignores_sigterm_is_escalated_to_sigkill(self):
        """SIGTERM で止まらない相手には SIGKILL まで上げる (猶予後)。

        猶予だけ待って諦めると、TERM を握るタイプの analyzer が丸ごと生き残る
        (課金・CPU のリークは post が来た経路でも起きる)。
        """
        gc_pid_file = self.fake_cursor_with_stubborn_grandchild()

        with mock.patch.object(self.cursor, "TIMEOUT_SEC", 0.2), mock.patch.object(
            self.cursor, "POLL_INTERVAL_SEC", 0.05
        ):
            self.run_hook("pre", explore_payload("tu-stubborn"))
            _, pid_file = self.state.paths(self.cursor.NAME, "tu-stubborn")
            self._children.append(int(pid_file.read_text().strip()))
            stubborn = self.read_grandchild(gc_pid_file)
            self.assertTrue(_alive(stubborn), "SIGTERM を無視する孫が起動していない")

            self.run_hook("post", explore_payload("tu-stubborn"))

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(stubborn):
            time.sleep(0.05)
        self.assertFalse(
            _alive(stubborn),
            "SIGTERM を無視する相手に SIGKILL までエスカレートしていない",
        )


class TestPidReuseGuard(OrphanTestCase):
    """pid ファイルが指す先が analyzer でなければ signal を送らない。

    pid ファイルは TTL 超過まで残りうるので、その間に pid が別プロセスへ再利用される
    ことがある。**判定できないときは送らない側に倒す** — 無関係なプロセスを撃つ事故の
    ほうが、cursor を 1 つ取り残すより重い。
    """

    def test_post_does_not_signal_a_reused_pid(self):
        victim = self.spawn_unrelated()
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-reuse")
        pid_file.write_text(str(victim.pid))
        result_file.write_text("stale")

        with mock.patch.object(self.cursor, "TIMEOUT_SEC", 0.2), mock.patch.object(
            self.cursor, "POLL_INTERVAL_SEC", 0.05
        ):
            self.cursor.post("tu-reuse")

        self.assert_unharmed(victim, "analyzer ではない pid に signal を送っている")
        self.assertFalse(pid_file.exists(), "pid ファイルは掃除されるべき")

    def test_reap_orphan_does_not_signal_a_reused_pid(self):
        victim = self.spawn_unrelated()
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-reuse-gc")
        pid_file.write_text(str(victim.pid))

        self.cursor.reap_orphan(pid_file)

        self.assert_unharmed(
            victim, "GC が analyzer ではない pid に signal を送っている"
        )

    def test_terminate_reports_that_it_did_not_signal(self):
        victim = self.spawn_unrelated()

        sent = self.cursor.terminate(victim.pid)

        self.assertFalse(sent, "同一性を確認できない pid で True を返している")
        self.assert_unharmed(victim, "戻り値は False なのに signal を送っている")


class TestStaleEntries(HookTestCase):
    """`state.stale_entries` の TTL 判定 (pid ファイルの mtime = 起動時刻で測る)。"""

    def _age(self, path, seconds: float) -> None:
        past = time.time() - seconds
        os.utime(path, (past, past))

    def test_fresh_entries_are_not_stale(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-fresh")
        result_file.write_text("x")
        pid_file.write_text("1")

        self.assertEqual(self.state.stale_entries(), [])

    def test_entries_older_than_ttl_are_reported_once(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-old")
        result_file.write_text("x")
        pid_file.write_text("1")
        self._age(pid_file, self.state.ORPHAN_TTL_SEC + 60)
        self._age(result_file, self.state.ORPHAN_TTL_SEC + 60)

        entries = self.state.stale_entries()

        self.assertEqual(entries, [(self.cursor.NAME, result_file, pid_file)])

    def test_age_is_measured_on_the_pid_file_not_the_result_file(self):
        """走り続けている孤児ほど結果ファイルの mtime が新しくなる。

        結果ファイル基準にすると「止めたい対象ほど残る」逆転が起きるので、
        起動時刻 (pid ファイルの mtime) で測る。
        """
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-writing")
        pid_file.write_text("1")
        self._age(pid_file, self.state.ORPHAN_TTL_SEC + 60)
        result_file.write_text("まだ書いている")  # mtime は今

        self.assertEqual(
            self.state.stale_entries(), [(self.cursor.NAME, result_file, pid_file)]
        )

    def test_current_tool_use_id_is_excluded(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-current")
        result_file.write_text("x")
        pid_file.write_text("1")
        self._age(pid_file, self.state.ORPHAN_TTL_SEC + 60)

        self.assertEqual(
            self.state.stale_entries(exclude_tool_use_id="tu-current"),
            [],
            "実行中の tool_use_id を GC 対象にしている",
        )

    def test_result_only_leftover_falls_back_to_its_own_mtime(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-orphan-txt")
        result_file.write_text("x")
        self._age(result_file, self.state.ORPHAN_TTL_SEC + 60)

        self.assertEqual(
            self.state.stale_entries(), [(self.cursor.NAME, result_file, pid_file)]
        )

    def test_missing_base_dir_is_not_an_error(self):
        """まだ一度も analyzer を起動していないセッションでも GC 走査が落ちないこと。"""
        self.assertFalse(self.state.BASE_DIR.exists())

        self.assertEqual(self.state.stale_entries(), [])


class TestGcOrphans(OrphanTestCase):
    """`__main__.gc_orphans` が残骸を消し、走っている孤児を止めること。"""

    def test_gc_removes_stale_files(self):
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-gc")
        result_file.write_text("x")
        pid_file.write_text("999999")
        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))
        os.utime(result_file, (past, past))

        self.assertEqual(self.entry.gc_orphans(), 1)
        self.assertFalse(result_file.exists())
        self.assertFalse(pid_file.exists())

    def test_gc_terminates_a_still_running_orphan_group(self):
        gc_pid_file = self.fake_cursor_with_grandchild()
        self.run_hook("pre", explore_payload("tu-gc-live"))
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-gc-live")
        leader = int(pid_file.read_text().strip())
        self._children.append(leader)
        grandchild = self.read_grandchild(gc_pid_file)

        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))

        self.assertEqual(self.entry.gc_orphans(), 1)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (_alive(leader) or _alive(grandchild)):
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "孤児の孫プロセスが残っている")
        self.assertFalse(result_file.exists())
        self.assertFalse(pid_file.exists())

    def test_gc_runs_in_both_phases(self):
        """pre / post のどちらから入っても GC が走る (post が来ない経路の受け皿)。"""
        for phase, tool_use_id in (("pre", "tu-phase-pre"), ("post", "tu-phase-post")):
            with self.subTest(phase=phase):
                self.fake_cursor()
                result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-leftover")
                result_file.write_text("x")
                pid_file.write_text("999999")
                past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
                os.utime(pid_file, (past, past))
                os.utime(result_file, (past, past))

                self.run_hook(phase, explore_payload(tool_use_id))
                if phase == "pre":
                    self.reap_cursor(tool_use_id)

                self.assertFalse(
                    pid_file.exists(), f"{phase} フェーズで GC が走っていない"
                )

    def test_gc_is_skipped_for_non_explore_agents(self):
        """Explore 以外の Agent 呼び出しでは何もしない (既存の早期 return を壊さない)。"""
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-other")
        result_file.write_text("x")
        pid_file.write_text("999999")
        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))

        self.run_hook("pre", explore_payload("tu-x", subagent_type="general-purpose"))

        self.assertTrue(pid_file.exists(), "Explore 以外でも GC が走っている")


if __name__ == "__main__":
    unittest.main()
