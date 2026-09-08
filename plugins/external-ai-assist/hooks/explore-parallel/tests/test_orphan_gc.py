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

from _common import subproc  # noqa: E402  (_testutil の sys.path 挿入後に import する)


def _alive(pid: int) -> bool:
    """pid が「まだ走っている」か。**zombie は死んだ扱い**。

    `os.kill(pid, 0)` だけでは足りない。PID 1 が孤児を reap しないコンテナでは、kill に
    成功した孫がそのまま zombie として残り `os.kill(pid, 0)` が成功し続けるため、停止でき
    ているのに「生存」と報告して group 停止系のテストが待ち時間ののちに落ちる。
    `_common.subproc.pid_is_zombie` (timeout テストが同じ理由で使っている) で除外する。

    判定不能 (`None`。/proc も ps も使えない) は `os.kill` の結果に従う = 生存側に倒す。
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return subproc.pid_is_zombie(pid) is not True


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


class TestAliveHelper(OrphanTestCase):
    """テストヘルパー `_alive` の契約: **zombie は死んだ扱い**。

    PID 1 が孤児を reap しないコンテナでは、kill に成功した孫が zombie として残り
    `os.kill(pid, 0)` が成功し続ける。ヘルパーがそれを「生存」と報告すると、group 停止が
    正しく効いているのに待ち時間ののちに落ちる (下の停止系テストが偽陽性で失敗する)。
    """

    def test_a_zombie_is_not_reported_as_alive(self):
        proc = subprocess.Popen(
            ["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._procs.append(proc)  # tearDown の wait で reap する
        if subproc.pid_is_zombie(proc.pid) is None:
            self.skipTest("zombie を判定できない環境 (/proc も ps も使えない)")

        # 親 (= このテストプロセス) が wait しない限り zombie のまま残る。
        # `proc.poll()` は reap してしまうので触らない。
        proc.kill()
        deadline = time.monotonic() + 3
        while (
            time.monotonic() < deadline and subproc.pid_is_zombie(proc.pid) is not True
        ):
            time.sleep(0.02)
        self.assertIs(
            subproc.pid_is_zombie(proc.pid),
            True,
            "zombie を作れていない (フィクスチャが成立していない)",
        )
        try:
            os.kill(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            self.fail("zombie に os.kill(pid, 0) が失敗した (前提が成立していない)")

        self.assertFalse(_alive(proc.pid), "zombie を生存と報告している")

        proc.wait(timeout=3)


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

        sent = self.cursor.terminate(victim.pid, time.time())

        self.assertFalse(sent, "同一性を確認できない pid で True を返している")
        self.assert_unharmed(victim, "戻り値は False なのに signal を送っている")


class TestStartTimeGuard(OrphanTestCase):
    """署名が一致しても、pid ファイルより後に起動したプロセスには signal を送らない。

    署名 (`agent --trust --print --mode plan`) は本 plugin の review 系 hook が起動する
    cursor とも一致する。TTL 超過まで残った pid ファイルの pid がそれらに再利用されて
    いると、署名照合だけでは素通りして無関係なレビューを `killpg` で撃つ。`pre` は Popen
    直後に pid ファイルを書くので、自分の analyzer なら開始時刻は必ず mtime 以前になる。

    2 つのテストは **同じフィクスチャで pid ファイルの mtime だけが違う**。開始時刻の
    照合を外すと negative 側だけが落ちる (署名照合の副作用ではないことが分かる)。
    """

    def _launch_analyzer(self, tool_use_id: str) -> tuple[int, int, object]:
        """偽 cursor を `pre` 経由で起動し、(leader pid, 孫 pid, pid ファイル) を返す。

        孫の生死で判定する。leader は test プロセスの直接の子なので、SIGTERM を受けても
        誰も `wait` しないうちは zombie として残り `os.kill(pid, 0)` が成功し続ける
        (「撃たれたのに生きている」と読めてしまう)。孫は leader の子なので、leader が
        死ねば init に引き取られて確実に reap される。孫は leader と同じ process group に
        居るので、`killpg` が飛べば必ず巻き込まれる。
        """
        gc_pid_file = self.fake_cursor_with_grandchild()
        self.run_hook("pre", explore_payload(tool_use_id))
        _, pid_file = self.state.paths(self.cursor.NAME, tool_use_id)
        leader = int(pid_file.read_text().strip())
        self._children.append(leader)
        grandchild = self.read_grandchild(gc_pid_file)
        self.assertTrue(_alive(grandchild), "偽 cursor の孫が起動していない")
        return leader, grandchild, pid_file

    def test_terminate_skips_a_process_started_after_the_pid_file(self):
        """再利用された pid の形: プロセスの開始時刻が pid ファイルの mtime より後。"""
        leader, grandchild, pid_file = self._launch_analyzer("tu-start-after")
        # pid ファイルだけを過去へずらす = 「この pid は 15 分前に記録された。いま走って
        # いるのはその後で起動した別プロセス」という再利用の状況。
        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))

        sent = self.cursor.terminate(leader, self.cursor._started_at(pid_file))

        self.assertFalse(sent, "pid ファイルより後に起動したプロセスに送っている")
        time.sleep(self.cursor.KILL_GRACE_SEC + 0.3)
        self.assertTrue(
            _alive(grandchild), "送らないと報告したのに process group を撃っている"
        )

    def test_terminate_signals_an_analyzer_started_before_the_pid_file(self):
        """正常経路: 開始時刻が mtime 以前なら今までどおり process group ごと停止する。"""
        leader, grandchild, pid_file = self._launch_analyzer("tu-start-before")

        sent = self.cursor.terminate(leader, self.cursor._started_at(pid_file))

        self.assertTrue(sent, "自分が起動した analyzer なのに停止をあきらめている")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "孫プロセスが取り残されている")

    def test_terminate_skips_when_the_pid_file_mtime_is_unavailable(self):
        """mtime が取れない (pid ファイルが消えている) ときも送らない側。"""
        leader, grandchild, pid_file = self._launch_analyzer("tu-start-nomtime")
        pid_file.unlink()

        sent = self.cursor.terminate(leader, self.cursor._started_at(pid_file))

        self.assertFalse(sent, "起動時刻が不明なのに signal を送っている")
        time.sleep(self.cursor.KILL_GRACE_SEC + 0.3)
        self.assertTrue(_alive(grandchild), "起動時刻が不明なのに撃っている")


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

    def test_gc_stops_at_the_budget_and_leaves_the_rest_for_next_time(self):
        # 予算 0 なら 1 件も処理せず、予算があれば全件処理する。この対で
        # 予算打ち切りの break が実在することを固定する (main 側レビューの指摘)。
        for i in range(3):
            r, p = self.state.paths(self.cursor.NAME, f"tu-budget-{i}")
            r.write_text("x")
            p.write_text("999999")
            past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
            os.utime(p, (past, past))
            os.utime(r, (past, past))
        with mock.patch.object(self.state, "GC_BUDGET_SEC", 0.0):
            self.assertEqual(self.entry.gc_orphans(), 0, "予算 0 でも掃除している")
        self.assertEqual(self.entry.gc_orphans(), 3)

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
        """**TTL を縮めて待つ**。pid ファイルの mtime を過去へずらしてはいけない。

        本番の孤児は「pid ファイルの mtime と同時刻に起動して TTL を超えて生き残った」
        プロセス。mtime だけを過去へずらすと、走っているのは mtime より後に起動した
        プロセスということになり、`cursor` の PID 同一性判定 (再利用ガード) から見て
        別プロセスと区別が付かない — 再利用の状況を作って「停止できること」を主張する
        フィクスチャになってしまう。TTL 側を縮めれば時系列は本番と同じまま短縮できる。
        """
        gc_pid_file = self.fake_cursor_with_grandchild()
        self.run_hook("pre", explore_payload("tu-gc-live"))
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-gc-live")
        leader = int(pid_file.read_text().strip())
        self._children.append(leader)
        grandchild = self.read_grandchild(gc_pid_file)

        ttl = 0.2
        with mock.patch.object(self.state, "ORPHAN_TTL_SEC", ttl):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.state.stale_entries():
                time.sleep(ttl / 2)
            self.assertEqual(self.entry.gc_orphans(), 1)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (_alive(leader) or _alive(grandchild)):
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "孤児の孫プロセスが残っている")
        self.assertFalse(result_file.exists())
        self.assertFalse(pid_file.exists())

    def test_gc_keeps_the_records_when_the_stop_is_unconfirmed(self):
        """停止を確認できなかった孤児は pid / 結果ファイルを残し、次回の GC が再試行する。

        pid ファイルは**その孤児を追える唯一の記録**。`ps` が一時的に使えない・cmdline が
        切り詰められた等で同一性を確認できなかったときに無条件で消すと、以後どの GC も
        再試行できず、ハングした cursor が走り続けて課金され続ける。

        フィクスチャは「署名の一致しない生存プロセスが pid ファイルに記録されている」形
        (= `terminate()` が送らない側に倒れる形) で、`reap_orphan` の
        `REAP_UNCONFIRMED` を実際に通す。
        """
        victim = self.spawn_unrelated()
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-unconfirmed")
        result_file.write_text("孤児がまだ書いている途中")
        pid_file.write_text(str(victim.pid))
        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))
        os.utime(result_file, (past, past))

        with mock.patch.object(
            self.cursor, "reap_orphan", wraps=self.cursor.reap_orphan
        ) as reap:
            self.assertEqual(
                self.entry.gc_orphans(), 0, "停止を確認できていないのに掃除を数えている"
            )
            self.assertTrue(
                pid_file.exists(), "停止を確認できていないのに pid 記録を消している"
            )
            self.assertTrue(
                result_file.exists(), "pid 記録を残しながら結果ファイルだけ消している"
            )

            self.assertEqual(self.entry.gc_orphans(), 0)
            self.assertEqual(reap.call_count, 2, "次回の GC が再試行していない")

        self.assert_unharmed(victim, "同一性を確認できない pid に signal を送っている")

    def test_gc_keeps_the_records_when_reaping_raises(self):
        """`reap_orphan` が例外で落ちた場合も未確定扱い (記録を残す)。"""
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-reap-raises")
        result_file.write_text("x")
        pid_file.write_text("999999")
        past = time.time() - (self.state.ORPHAN_TTL_SEC + 60)
        os.utime(pid_file, (past, past))
        os.utime(result_file, (past, past))

        with mock.patch.object(
            self.cursor, "reap_orphan", side_effect=OSError("ps が使えない")
        ):
            self.assertEqual(self.entry.gc_orphans(), 0)

        self.assertTrue(pid_file.exists(), "停止に失敗した孤児の pid 記録を消している")
        self.assertTrue(result_file.exists())
        self.assertEqual(self.entry.gc_orphans(), 1, "次回の GC が掃除できていない")

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
