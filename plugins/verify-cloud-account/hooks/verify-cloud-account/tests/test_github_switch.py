"""github の自動切替 (`plan_switch` / `apply_switch` / `parse_logged_in_accounts`)。

subprocess.run を mock する。実機の `gh auth switch` の挙動 (非対話・rc・所要時間) は
gh 2.101.0 で使い捨ての `GH_CONFIG_DIR` と架空の host を使って確認した
(実在 host の keyring 項目を巻き込まないため、実 gh を呼ぶテストはここに置かない)。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import auto_switch, budget  # noqa: E402
from services import github  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


def _run(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _status(accounts: dict[str, list[str]], active: dict[str, str]) -> str:
    """gh 2.40+ の `gh auth status` 形式 (非アクティブも列挙される)。"""
    lines = []
    for host, users in accounts.items():
        lines.append(host)
        for user in users:
            lines.append(f"  ✓ Logged in to {host} account {user} (keyring)")
            flag = "true" if active.get(host) == user else "false"
            lines.append(f"  - Active account: {flag}")
    return "\n".join(lines) + "\n"


class BudgetClean(unittest.TestCase):
    def setUp(self):
        budget.clear()
        self.addCleanup(budget.clear)


class TestParseLoggedInAccounts(unittest.TestCase):
    def test_includes_inactive_accounts(self):
        text = _status({"github.com": ["work", "Mao-o"]}, {"github.com": "work"})
        self.assertEqual(
            github.parse_logged_in_accounts(text), {"github.com": ["work", "Mao-o"]}
        )
        self.assertEqual(github.parse_active_accounts(text), {"github.com": "work"})

    def test_failed_login_is_not_a_switch_target(self):
        """トークン検証に失敗したアカウントへは切り替えても使えない。"""
        text = (
            "github.com\n"
            "  ✓ Logged in to github.com account work (keyring)\n"
            "  - Active account: true\n"
            "  X Failed to log in to github.com account Mao-o (keyring)\n"
            "  - Active account: false\n"
            "  - The token in keyring is invalid.\n"
        )
        self.assertEqual(github.parse_logged_in_accounts(text), {"github.com": ["work"]})

    def test_legacy_single_account_format(self):
        text = "github.com\n  ✓ Logged in to github.com as Mao-o (keyring)\n"
        self.assertEqual(github.parse_logged_in_accounts(text), {"github.com": ["Mao-o"]})

    def test_multiple_hosts(self):
        text = _status(
            {"github.com": ["a", "b"], "ghe.example.com": ["corp"]},
            {"github.com": "a", "ghe.example.com": "corp"},
        )
        self.assertEqual(
            github.parse_logged_in_accounts(text),
            {"github.com": ["a", "b"], "ghe.example.com": ["corp"]},
        )


class TestPlanSwitch(BudgetClean):
    def _plan(self, expected, status_text, env=None):
        with mock.patch("subprocess.run", return_value=_run(stdout=status_text)) as run:
            result = github.plan_switch(expected, "/p", env=env)
        return result, run

    def test_scalar_switches_to_logged_in_inactive_account(self):
        text = _status({"github.com": ["work", "Mao-o"]}, {"github.com": "work"})
        (steps, reason), run = self._plan("Mao-o", text)
        self.assertIsNone(reason)
        self.assertEqual(steps, [("github.com", "work", "Mao-o")])
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "status"])

    def test_scalar_already_active_needs_nothing(self):
        text = _status({"github.com": ["work", "Mao-o"]}, {"github.com": "Mao-o"})
        (steps, reason), _run_mock = self._plan("Mao-o", text)
        self.assertEqual((steps, reason), ([], None))

    def test_scalar_not_logged_in_is_refused(self):
        text = _status({"github.com": ["work"]}, {"github.com": "work"})
        (steps, reason), _run_mock = self._plan("Mao-o", text)
        self.assertIsNone(steps)
        self.assertIn("github.com の Mao-o", reason)
        self.assertIn("自動化しません", reason)

    def test_scalar_targets_github_com_when_ghe_is_listed_first(self):
        """照合先の決め方は verify() と同じ (`scalar_target_host`)。"""
        text = _status(
            {"ghe.example.com": ["corp"], "github.com": ["work", "Mao-o"]},
            {"ghe.example.com": "corp", "github.com": "work"},
        )
        (steps, reason), _run_mock = self._plan("Mao-o", text)
        self.assertIsNone(reason)
        self.assertEqual(steps, [("github.com", "work", "Mao-o")])

    def test_dict_switches_only_mismatched_hosts(self):
        text = _status(
            {"github.com": ["work", "Mao-o"], "ghe.example.com": ["corp"]},
            {"github.com": "work", "ghe.example.com": "corp"},
        )
        (steps, reason), _run_mock = self._plan(
            {"github.com": "Mao-o", "ghe.example.com": "corp"}, text
        )
        self.assertIsNone(reason)
        self.assertEqual(steps, [("github.com", "work", "Mao-o")])

    def test_dict_is_all_or_nothing(self):
        """1 つでも切り替えられない host があれば、切り替えられる host も切り替えない。"""
        text = _status(
            {"github.com": ["work", "Mao-o"], "ghe.example.com": ["someone"]},
            {"github.com": "work", "ghe.example.com": "someone"},
        )
        (steps, reason), _run_mock = self._plan(
            {"github.com": "Mao-o", "ghe.example.com": "corp"}, text
        )
        self.assertIsNone(steps)
        self.assertIn("ghe.example.com の corp", reason)
        self.assertIn("どの host も切り替えません", reason)

    def test_dict_host_not_logged_in_at_all(self):
        text = _status({"github.com": ["Mao-o"]}, {"github.com": "Mao-o"})
        (steps, reason), _run_mock = self._plan({"ghe.example.com": "corp"}, text)
        self.assertIsNone(steps)
        self.assertIn("ghe.example.com の corp", reason)

    def test_token_env_is_refused_without_running_gh(self):
        """トークン env があると hosts.yml を切り替えても実行アカウントは変わらない。"""
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            with self.subTest(env=name):
                (steps, reason), run = self._plan("Mao-o", "", env={name: "x"})
                self.assertIsNone(steps)
                self.assertIn("トークン", reason)
                self.assertFalse(run.called)

    def test_process_env_token_is_also_refused(self):
        with mock.patch.dict("os.environ", {"GH_TOKEN": "x"}):
            (steps, reason), run = self._plan("Mao-o", "")
        self.assertIsNone(steps)
        self.assertFalse(run.called)

    def test_gh_host_env_is_refused(self):
        (steps, reason), run = self._plan("Mao-o", "", env={"GH_HOST": "ghe.example.com"})
        self.assertIsNone(steps)
        self.assertIn("GH_HOST", reason)
        self.assertFalse(run.called)

    def test_cli_failure_is_refused(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            steps, reason = github.plan_switch("Mao-o", "/p")
        self.assertIsNone(steps)
        self.assertIn("gh コマンドが見つかりません", reason)

    def test_not_logged_in_anywhere(self):
        (steps, reason), _run_mock = self._plan("Mao-o", "You are not logged into any GitHub hosts.\n")
        self.assertIsNone(steps)
        self.assertTrue(reason)

    def test_invalid_expected_shape(self):
        steps, reason = github.plan_switch({}, "/p")
        self.assertIsNone(steps)
        self.assertIn("空", reason)

    def test_status_call_honours_budget(self):
        budget.start(3.0)
        text = _status({"github.com": ["work", "Mao-o"]}, {"github.com": "work"})
        (_steps, _reason), run = self._plan("Mao-o", text)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3.0)


class TestApplySwitch(BudgetClean):
    STEPS = [("github.com", "work", "Mao-o")]

    def test_runs_non_interactive_switch_with_explicit_host_and_user(self):
        with mock.patch("subprocess.run", return_value=_run(stderr="✓ Switched")) as run:
            done, err = github.apply_switch(self.STEPS, env={"PATH": "/usr/bin"})
        self.assertIsNone(err)
        self.assertEqual(done, self.STEPS)
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "auth", "switch", "--hostname", "github.com", "--user", "Mao-o"],
        )
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL, "対話プロンプトで待たない")
        self.assertEqual(kwargs.get("env"), {"PATH": "/usr/bin"}, "コマンドと同じ env で切り替えていない")

    def test_nonzero_exit_reports_first_line_of_stderr(self):
        with mock.patch(
            "subprocess.run",
            return_value=_run(stderr="not logged in to github.com account Mao-o\nmore", returncode=1),
        ):
            done, err = github.apply_switch(self.STEPS)
        self.assertEqual(done, [])
        self.assertIn("not logged in to github.com account Mao-o", err)
        self.assertNotIn("more", err)

    def test_cli_missing_and_timeout(self):
        for effect, fragment in (
            (FileNotFoundError, "見つかりません"),
            (PermissionError("denied"), "実行できません"),
            (subprocess.TimeoutExpired(cmd=[], timeout=1), "タイムアウト"),
        ):
            with self.subTest(effect=effect):
                with mock.patch("subprocess.run", side_effect=effect):
                    done, err = github.apply_switch(self.STEPS)
                self.assertEqual(done, [])
                self.assertIn(fragment, err)

    def test_stops_after_first_failure(self):
        steps = [("github.com", "a", "b"), ("ghe.example.com", "c", "d")]
        with mock.patch(
            "subprocess.run", side_effect=[_run(), _run(stderr="boom", returncode=1)]
        ) as run:
            done, err = github.apply_switch(steps)
        self.assertEqual(done, [steps[0]])
        self.assertIn("ghe.example.com", err)
        self.assertEqual(run.call_count, 2)

    def test_checks_budget_before_each_call(self):
        steps = [("github.com", "a", "b"), ("ghe.example.com", "c", "d")]

        def run(*_args, **_kwargs):
            budget.start(0.0)
            return _run()

        with mock.patch("subprocess.run", side_effect=run) as run_mock:
            done, err = github.apply_switch(steps)
        self.assertEqual(done, [steps[0]])
        self.assertIn("予算", err)
        self.assertEqual(run_mock.call_count, 1)

    def test_timeout_is_capped_by_budget(self):
        budget.start(3.0)
        with mock.patch("subprocess.run", return_value=_run()) as run:
            github.apply_switch(self.STEPS)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 3.0)
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], budget.MIN_CALL_TIMEOUT_SECONDS)


class TestDescribeSwitch(unittest.TestCase):
    def test_mentions_change_and_global_scope(self):
        text = github.describe_switch([("github.com", "work", "Mao-o")])
        self.assertIn("github.com: work → Mao-o", text)
        self.assertIn("全ターミナル・セッション", text)

    def test_host_without_previous_account(self):
        text = github.describe_switch([("ghe.example.com", None, "corp")])
        self.assertIn("(なし) → corp", text)


class TestGithubDeclaresAutoSwitchContract(unittest.TestCase):
    def test_supports(self):
        self.assertTrue(auto_switch.supports(github))
        self.assertTrue(callable(getattr(github, "describe_switch", None)))


if __name__ == "__main__":
    unittest.main()
