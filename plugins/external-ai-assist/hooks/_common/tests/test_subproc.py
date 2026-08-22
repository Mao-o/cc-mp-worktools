"""外部 CLI 起動ヘルパー (process group 停止 / 残出力の読み捨て / 出力契約)。

zh5.15: `subprocess.run(capture_output=True, timeout=…)` は timeout 時に直接の子だけ
kill するため、stdout を継承した孫 (`sleep`) が取り残される。本ヘルパーは process group
ごと停止し、timeout 直後に返ることを偽 CLI で固定する。

偽 CLI は 4 型: 親子とも生存 / 親子とも TERM 無視 / 親が先に exit (zombie リーダー) /
親は TERM で死ぬが孫は TERM 無視 (SIGKILL 段でリーダーが zombie)。後ろ 2 つは macOS で
`getpgid(zombie)` が ESRCH になる経路の回帰テスト。
"""
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import _testutil
from _testutil import ensure_killed, hanging_cli, read_pid, wait_until_dead, write_script

from _common import subproc

# 偽 CLI が孫を起動して pid ファイルを書くまでの余裕を見て 1 秒。遅い CI でも
# timeout 前に pid ファイルが書かれるようにする (経過時間の上限は grace 込みで見る)。
TIMEOUT = 1.0
GRACE = 0.5


class SubprocTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._grace = mock.patch.object(subproc, "KILL_GRACE_SEC", GRACE)
        self._grace.start()
        self._grandchildren: list[int] = []

    def tearDown(self) -> None:
        for pid in self._grandchildren:
            ensure_killed(pid)
        self._grace.stop()
        self._tmp.cleanup()

    def _hanging(self, **kwargs) -> tuple[str, str]:
        pid_file = os.path.join(self.dir, "grandchild.pid")
        return hanging_cli(self.dir, pid_file=pid_file, **kwargs), pid_file

    def _run_until_timeout(self, cli: str, pid_file: str, **kwargs) -> tuple[float, int]:
        """run_captured を timeout させ、(経過秒, 孫 pid) を返す。戻り値 None を assert する。"""
        started = time.monotonic()
        result = subproc.run_captured([cli], timeout_sec=TIMEOUT, **kwargs)
        elapsed = time.monotonic() - started
        grandchild = read_pid(pid_file)
        self._grandchildren.append(grandchild)
        self.assertIsNone(result)
        return elapsed, grandchild


class TestTimeoutKillsProcessGroup(SubprocTestCase):
    def test_grandchild_holding_stdout_is_killed_and_call_returns_promptly(self):
        cli, pid_file = self._hanging()
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0, "timeout 後に孫の EOF を待って止まっている")
        self.assertTrue(wait_until_dead(grandchild), "stdout を握った孫が取り残されている")

    def test_sigterm_ignored_escalates_to_sigkill(self):
        cli, pid_file = self._hanging(ignore_term=True)
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "SIGTERM を無視する孫が SIGKILL されていない")

    def test_leader_already_exited_but_grandchild_holds_stdout(self):
        """cursor-agent 本体が先に落ち、helper だけが pipe を握るケース (リーダーは zombie)。

        macOS では zombie に `getpgid` が ESRCH を返す。`send_signal` に落とすと内部の
        `poll()` が reap して signal が送られず孫が残るので、killpg を無条件に送る。
        """
        cli, pid_file = self._hanging(leader_exits=True)
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "zombie リーダーのグループに killpg が届いていない")

    def test_leader_dies_on_term_but_grandchild_ignores_term(self):
        """SIGTERM でリーダーだけ死に (zombie)、SIGKILL 段で孫に届くこと。"""
        cli, pid_file = self._hanging(grandchild_ignores_term=True)
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "SIGKILL 段で zombie リーダーのグループに届いていない")

    def test_member_without_pipe_when_leader_dies_on_term(self):
        """Codex P2: pipe を握らず TERM を無視するメンバーは、EOF が来ても猶予後に SIGKILL される。

        リーダーは SIGTERM で死ぬので pipe は TERM 直後に EOF になる。「drain 完了 = 停止」と
        見なすと `sleep >/dev/null` が残る。グループが空になるまで待って SIGKILL すること。
        """
        cli, pid_file = self._hanging(grandchild_detached_from_pipe=True)
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "pipe を握らない TERM 無視メンバーが取り残されている")

    def test_member_without_pipe_when_leader_ignores_term_too(self):
        """同上の SIGKILL 段: リーダーも TERM を無視して pipe を握り続ける型。"""
        cli, pid_file = self._hanging(ignore_term=True, grandchild_detached_from_pipe=True)
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "SIGKILL 段で pipe を握らないメンバーが取り残されている")

    def test_group_that_exits_on_term_returns_without_waiting_full_grace(self):
        """全員 TERM で死ぬ通常ケースでは猶予いっぱい待たない (probe で空を検知して即 return)。"""
        cli, pid_file = self._hanging()
        elapsed, grandchild = self._run_until_timeout(cli, pid_file)
        self.assertLess(elapsed, TIMEOUT + GRACE, "グループが空になった後も猶予を待っている")
        self.assertTrue(wait_until_dead(grandchild))

    def test_timeout_with_stdin_input(self):
        """codex 経路 (input_text あり) でも同じく timeout 直後に返る。"""
        cli, pid_file = self._hanging()
        elapsed, grandchild = self._run_until_timeout(cli, pid_file, input_text="plan body")
        self.assertLess(elapsed, TIMEOUT + 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild))

    def test_hook_process_group_is_never_signalled(self):
        """killpg は子のグループだけに届き、hook 自身 (このテストプロセス) は生き残る。"""
        cli, pid_file = self._hanging()
        proc = subprocess.Popen(
            [cli], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        self._grandchildren.append(read_pid(pid_file))
        self.assertNotEqual(os.getpgid(proc.pid), os.getpgid(0))
        self.assertEqual(os.getpgid(proc.pid), proc.pid)

        subproc.kill_process_group(proc, grace_sec=GRACE)

        self.assertIsNotNone(proc.returncode, "直接の子が回収されていない")
        self.assertTrue(wait_until_dead(self._grandchildren[-1]))

    def test_non_leader_child_falls_back_to_child_only_kill(self):
        """start_new_session 無しの Popen に対しては killpg しない (自分のグループを殺さない)。"""
        cli, pid_file = self._hanging()
        proc = subprocess.Popen([cli], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        grandchild = read_pid(pid_file)
        self._grandchildren.append(grandchild)
        self.assertEqual(os.getpgid(proc.pid), os.getpgid(0))

        subproc.kill_process_group(proc, grace_sec=GRACE)

        self.assertIsNotNone(proc.returncode)
        # 孫はこちらの process group に居るので意図的に殺さない (teardown で後始末)
        os.kill(grandchild, 0)

    def test_reaped_process_with_empty_group_is_never_signalled(self):
        """reap 済みでグループも空の proc には signal を送らない (pid 再利用の誤送信防止)。

        存在確認 (`killpg(pgid, 0)`) 以外の killpg / send_signal が呼ばれないこと。
        """
        cli = write_script(self.dir, "quick", "exit 0\n")
        proc = subprocess.Popen([cli], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        proc.wait(timeout=5)
        sent: list[int] = []

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError  # グループは消えている
            sent.append(sig)

        with mock.patch.object(os, "killpg", side_effect=fake_killpg), mock.patch.object(
            subprocess.Popen, "send_signal"
        ) as send_signal:
            subproc.kill_process_group(proc, grace_sec=GRACE, own_group=True)
        self.assertEqual(sent, [])
        send_signal.assert_not_called()


class TestNormalExitCleanup(SubprocTestCase):
    """正常終了した CLI が残した background メンバーも止める (timeout していなくても)。"""

    def test_detached_member_left_after_normal_exit_is_killed(self):
        cli, pid_file = self._hanging(leader_exits=True, grandchild_detached_from_pipe=True)
        started = time.monotonic()
        result = subproc.run_captured([cli], timeout_sec=5)
        elapsed = time.monotonic() - started
        grandchild = read_pid(pid_file)
        self._grandchildren.append(grandchild)

        self.assertIsNotNone(result, "正常終了の結果は返すこと (掃除は結果を捨てない)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "partial\n")
        self.assertLess(elapsed, 3 * GRACE + 2.0)
        self.assertTrue(wait_until_dead(grandchild), "正常終了後に残った TERM 無視メンバーが止まっていない")

    def test_normal_exit_without_leftovers_is_not_delayed(self):
        cli = write_script(self.dir, "ok", "printf 'hello'\n")
        started = time.monotonic()
        result = subproc.run_captured([cli], timeout_sec=5)
        elapsed = time.monotonic() - started
        self.assertEqual(result.stdout, "hello")
        self.assertLess(elapsed, GRACE, "残存メンバーが無いのに猶予を待っている")


class TestRunCapturedContract(SubprocTestCase):
    def test_normal_completion_returns_output(self):
        cli = write_script(self.dir, "ok", "printf 'hello'; printf 'err' >&2; exit 0\n")
        result = subproc.run_captured([cli], timeout_sec=5)
        self.assertIsNotNone(result)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.stderr, "err")

    def test_nonzero_exit_is_reported(self):
        cli = write_script(self.dir, "fail", "exit 3\n")
        result = subproc.run_captured([cli], timeout_sec=5)
        self.assertIsNotNone(result)
        self.assertEqual(result.returncode, 3)

    def test_stdin_input_is_delivered(self):
        cli = write_script(self.dir, "cat", "cat\n")
        result = subproc.run_captured([cli], timeout_sec=5, input_text="plan body\n")
        self.assertEqual(result.stdout, "plan body\n")

    def test_stdin_is_devnull_without_input(self):
        """hook 自身の stdin (payload の pipe) を子に継承させない。"""
        cli = write_script(self.dir, "readstdin", "if read -r line; then echo \"got:$line\"; else echo eof; fi\n")
        result = subproc.run_captured([cli], timeout_sec=5)
        self.assertEqual(result.stdout.strip(), "eof")

    def test_missing_binary_is_none(self):
        self.assertIsNone(
            subproc.run_captured([os.path.join(self.dir, "no-such-cli")], timeout_sec=5)
        )

    def test_invalid_utf8_does_not_raise(self):
        cli = write_script(self.dir, "bin", "printf '\\xff\\xfeok'\n")
        result = subproc.run_captured([cli], timeout_sec=5)
        self.assertIsNotNone(result)
        self.assertIn("ok", result.stdout)


class TestRunForOutput(SubprocTestCase):
    def test_strips_and_truncates(self):
        cli = write_script(self.dir, "out", "printf '  abcdef  \\n'\n")
        self.assertEqual(subproc.run_for_output([cli], timeout_sec=5), "abcdef")
        self.assertEqual(
            subproc.run_for_output([cli], timeout_sec=5, max_output_chars=3), "abc"
        )

    def test_empty_output_is_none(self):
        cli = write_script(self.dir, "empty", "printf '  \\n'\n")
        self.assertIsNone(subproc.run_for_output([cli], timeout_sec=5))

    def test_nonzero_exit_is_none_even_with_output(self):
        cli = write_script(self.dir, "fail", "printf 'partial'; exit 1\n")
        self.assertIsNone(subproc.run_for_output([cli], timeout_sec=5))

    def test_timeout_is_none(self):
        cli, pid_file = self._hanging()
        self.assertIsNone(subproc.run_for_output([cli], timeout_sec=TIMEOUT))
        self._grandchildren.append(read_pid(pid_file))

    def test_cli_available_uses_path(self):
        self.assertTrue(subproc.cli_available("sh"))
        self.assertFalse(subproc.cli_available("no-such-cli-for-external-ai-assist"))


if __name__ == "__main__":
    unittest.main()
