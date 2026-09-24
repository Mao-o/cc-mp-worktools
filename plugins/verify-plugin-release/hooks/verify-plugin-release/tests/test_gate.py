"""gate.run_gate の統合テスト (使い捨て git repo 上で実行する)。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401
from _testutil import FAILING_TEST, add_plugin, bump, commit_all, make_marketplace, sh, write

import gate
from config import Config
from runner import Deadline

def statuses(rep: gate.Report) -> dict[str, str]:
    return {r.check: r.status for r in rep.results}


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

    def test_test_command_config(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        st = statuses(self.gate(Config(fetch=False, test_command=False)))
        self.assertEqual(st["tests[alpha]"], gate.SKIP)
        st = statuses(self.gate(Config(fetch=False, test_command=["git", "definitely-not-a-command"])))
        self.assertEqual(st["tests[alpha]"], gate.FAIL)

    def test_uncommitted_changes_warn(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        write(self.root, "plugins/alpha/extra.py", "x = 1\n")
        rep = self.gate()
        self.assertEqual(statuses(rep)["uncommitted"], gate.WARN)
        self.assertFalse(rep.failed)

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

    def test_validate_warning_is_warn_unless_strict(self):
        self.branch()
        self.change_alpha()
        commit_all(self.root, "alpha")
        fake = self.root.parent / "fake_claude.py"
        fake.write_text("print('⚠ Found 1 warning')\nprint('passed with warnings')\n", encoding="utf-8")
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
