"""gate.run_gate の統合テスト (使い捨て git repo 上で実行する)。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _testutil
from _testutil import FAILING_TEST, add_plugin, bump, commit_all, make_marketplace, sh, write

import gate
from config import Config
from runner import Deadline

def statuses(rep: gate.Report) -> dict[str, str]:
    return {r.check: r.status for r in rep.results}


def _running(pid: int) -> bool:
    """pid がまだ動いているか。zombie (終了済みで回収待ち) は止まったものとみなす。

    孤児の回収は PID 1 の仕事で、コンテナによっては回収が遅れて zombie が残る。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    stat = r.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class GateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_marketplace(Path(self._tmp.name) / "repo", ["alpha", "beta"])
        self.cfg = Config(fetch=False)

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, cfg: Config | None = None, base: str | None = None) -> gate.Report:
        return gate.run_gate(self.root, cfg or self.cfg, Deadline(60), base_hint=base, which=lambda n: None)

    def branch(self, name: str = "feat") -> None:
        sh(self.root, "switch", "-q", "-c", name)

    def change_alpha(self, version: str | None = "0.2.0", changelog: bool = True) -> None:
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('changed')\n")
        if version:
            bump(self.root, "alpha", version)
        if changelog:
            write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")

    def test_complete_release_passes(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        rep = self.gate()
        st = statuses(rep)
        self.assertFalse(rep.failed, rep.results)
        self.assertEqual(rep.base, "main")
        self.assertEqual(st["version[alpha]"], gate.PASS)
        self.assertEqual(st["changelog[alpha]"], gate.PASS)
        self.assertEqual(st["tests[alpha]"], gate.PASS)
        self.assertEqual(st["validate[alpha]"], gate.SKIP)
        self.assertEqual(st["merge"], gate.PASS)
        self.assertNotIn("version[beta]", st)

    def test_on_base_branch_fails(self):
        self.change_alpha()
        commit_all(self.root, "alpha on main")
        rep = self.gate()
        # main 上では base...HEAD が空になるので diff も FAIL
        self.assertEqual(statuses(rep)["branch"], gate.FAIL)
        self.assertEqual(statuses(rep)["diff"], gate.FAIL)

    def test_missing_bump_and_changelog_fail(self):
        self.branch()
        self.change_alpha(version=None, changelog=False)
        commit_all(self.root, "alpha")
        st = statuses(self.gate())
        self.assertEqual(st["version[alpha]"], gate.FAIL)
        self.assertEqual(st["changelog[alpha]"], gate.FAIL)

    def test_version_order(self):
        cases = [
            ("0.0.9", gate.FAIL),  # 下げ
            ("0.1.1", gate.PASS),
            ("1.0.0-rc.1", gate.PASS),
            ("0.1.0-rc.1", gate.FAIL),  # 同じ番号の pre-release は正式版より古い
            ("next", gate.WARN),  # semver でなければ比較しない
        ]
        self.branch()
        for version, want in cases:
            with self.subTest(version=version):
                sh(self.root, "reset", "-q", "--hard", "main")
                self.change_alpha(version=version)
                commit_all(self.root, f"alpha {version}")
                self.assertEqual(statuses(self.gate())["version[alpha]"], want)

    def test_compare_versions(self):
        self.assertLess(gate._compare_versions("1.0.0-alpha", "1.0.0-alpha.1"), 0)
        self.assertLess(gate._compare_versions("1.0.0-alpha.1", "1.0.0-beta"), 0)
        self.assertLess(gate._compare_versions("1.0.0-rc.1", "1.0.0"), 0)
        self.assertLess(gate._compare_versions("0.9.9", "0.10.0"), 0)
        self.assertEqual(gate._compare_versions("1.0.0+build.1", "1.0.0"), 0)
        self.assertIsNone(gate._compare_versions("1.0", "1.0.1"))

    def test_doc_only_change_skips_release_checks(self):
        self.branch()
        write(self.root, "plugins/alpha/README.md", "# alpha\n")
        commit_all(self.root, "docs")
        rep = self.gate()
        st = statuses(rep)
        self.assertFalse(rep.failed, rep.results)
        self.assertEqual(st["release[alpha]"], gate.SKIP)

    def test_failing_suite_fails(self):
        self.branch()
        self.change_alpha()
        write(self.root, "plugins/alpha/hooks/alpha/tests/test_x.py", FAILING_TEST)
        commit_all(self.root, "alpha")
        self.assertEqual(statuses(self.gate())["tests[alpha]"], gate.FAIL)

    def test_parallel_suites_attribute_results_to_their_plugin(self):
        # 2 plugin を同時に変更し、片方だけ落ちる。並列実行でも結果を取り違えない
        self.branch()
        self.change_alpha()
        write(self.root, "plugins/beta/hooks/beta/__main__.py", "print('changed')\n")
        write(self.root, "plugins/beta/hooks/beta/tests/test_x.py", FAILING_TEST)
        bump(self.root, "beta", "0.2.0")
        write(self.root, "plugins/beta/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        commit_all(self.root, "alpha ok, beta broken")
        st = statuses(self.gate())
        self.assertEqual(st["tests[alpha]"], gate.PASS)
        self.assertEqual(st["tests[beta]"], gate.FAIL)

    def test_failed_job_stops_the_others_without_waiting(self):
        # 1 本が起動に失敗したら、長く走る別の suite を待たずに例外を上げる
        # (待つと hook の timeout を超え、Claude Code がコマンドを通してしまう)
        import time

        from layout import Plugin

        slow = gate._TestPlan(Plugin("slow", "plugins/slow", True), jobs=[([sys.executable, "-c", "import time; time.sleep(30)"], self.root)])
        broken = gate._TestPlan(Plugin("broken", "plugins/broken", True), jobs=[(["definitely-missing-binary-vpr"], self.root)], custom=True)
        started = time.monotonic()
        with self.assertRaises(OSError):
            gate._run_tests(self.root, [slow, broken], Deadline(60), gate.Report())
        self.assertLess(time.monotonic() - started, 10)

    @unittest.skipIf(os.name == "nt", "生存確認に os.kill(pid, 0) を使う (Windows では終了させてしまう)")
    def test_cancel_also_stops_grandchildren(self):
        # suite が起動した子プロセス (make test の下の python など) も止める。
        # 直下だけ止めると孫が残り、checkout を触り続けて次の実行と干渉する
        import threading
        import time

        pidfile = self.root / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        stop = threading.Event()
        t = threading.Thread(target=gate._run_job, args=([sys.executable, "-c", script], self.root, Deadline(60), stop))
        t.start()
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.1)
        pid = int(pidfile.read_text())
        stop.set()
        t.join(10)
        self.assertFalse(t.is_alive())
        for _ in range(50):
            if not _running(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("孫プロセスが残っている")

    def test_test_command_config(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        st = statuses(self.gate(Config(fetch=False, test_command=False)))
        self.assertEqual(st["tests[alpha]"], gate.SKIP)
        st = statuses(self.gate(Config(fetch=False, test_command=["git", "definitely-not-a-command"])))
        self.assertEqual(st["tests[alpha]"], gate.FAIL)

    def test_uncommitted_changes_outside_checked_plugins_warn(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        write(self.root, "plugins/beta/extra.py", "x = 1\n")
        rep = self.gate()
        self.assertEqual(statuses(rep)["uncommitted-other"], gate.WARN)
        self.assertFalse(rep.failed)

    def test_dirty_fix_does_not_mask_committed_failure(self):
        # commit 済みのテストは落ちるが、未 commit の修正で作業ツリーでは通る状態
        self.branch()
        self.change_alpha()
        write(self.root, "plugins/alpha/hooks/alpha/tests/test_x.py", FAILING_TEST)
        commit_all(self.root, "alpha with failing test")
        write(self.root, "plugins/alpha/hooks/alpha/tests/test_x.py", _testutil.PASSING_TEST)
        rep = self.gate()
        self.assertEqual(statuses(rep)["uncommitted"], gate.FAIL)
        self.assertTrue(rep.failed)

    def test_removed_plugin_with_dangling_entry_fails(self):
        self.branch()
        sh(self.root, "rm", "-q", "-r", "plugins/beta")
        commit_all(self.root, "remove beta, keep entry")
        st = statuses(self.gate())
        self.assertEqual(st["removed[beta]"], gate.FAIL)
        self.assertEqual(st["validate[marketplace]"], gate.SKIP)

    def test_single_plugin_per_pr(self):
        self.branch()
        self.change_alpha()
        write(self.root, "README.md", "# root\n")
        commit_all(self.root, "alpha + root")
        self.assertNotIn("single-plugin", statuses(self.gate()))
        st = statuses(self.gate(Config(fetch=False, single_plugin_per_pr=True)))
        self.assertEqual(st["single-plugin"], gate.FAIL)

    def test_new_unlisted_plugin_warns(self):
        self.branch()
        add_plugin(self.root, "gamma")
        commit_all(self.root, "gamma")
        rep = self.gate()
        st = statuses(rep)
        self.assertEqual(st["version[gamma]"], gate.PASS)
        self.assertEqual(st["listed[gamma]"], gate.WARN)
        self.assertEqual(st["validate[marketplace]"], gate.SKIP)

    def test_merge_conflict_fails(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha on branch")
        sh(self.root, "switch", "-q", "main")
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('main side')\n")
        commit_all(self.root, "alpha on main")
        sh(self.root, "switch", "-q", "feat")
        self.assertEqual(statuses(self.gate())["merge"], gate.FAIL)

    def test_explicit_base(self):
        sh(self.root, "branch", "dev")
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        self.assertEqual(self.gate(base="dev").base, "dev")
        rep = self.gate(base="nope")
        self.assertEqual(statuses(rep)["base"], gate.FAIL)

    def test_gh_merge_base_config_is_honored(self):
        sh(self.root, "branch", "develop")
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        sh(self.root, "config", "branch.feat.gh-merge-base", "develop")
        self.assertEqual(self.gate().base, "develop")
        # --base の明示は設定より優先
        self.assertEqual(self.gate(base="main").base, "main")

    def test_validate_warning_is_warn_unless_strict(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        fake = self.root.parent / "fake_claude.py"
        fake.write_text("print('Found 1 warning')\nprint('passed with warnings')\n", encoding="utf-8")
        # claude の実物は使わず、warning を出す偽コマンドに差し替えて判定部分だけ確かめる
        rep = gate.Report()
        orig = gate.run

        def fake_run(args, cwd, dl, cap=None):
            return orig([sys.executable, str(fake)], cwd, dl, cap)

        gate.run = fake_run
        try:
            gate._check_validate(self.root, "plugins/alpha", "alpha", Config(), Deadline(30), rep, "claude")
            gate._check_validate(
                self.root, "plugins/alpha", "alpha2", Config(strict_validate=True), Deadline(30), rep, "claude"
            )
        finally:
            gate.run = orig
        st = statuses(rep)
        self.assertEqual(st["validate[alpha]"], gate.WARN)
        self.assertEqual(st["validate[alpha2]"], gate.FAIL)


if __name__ == "__main__":
    unittest.main()
