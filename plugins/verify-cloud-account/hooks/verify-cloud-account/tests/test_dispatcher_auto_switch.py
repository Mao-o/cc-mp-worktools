"""dispatcher × 自動切替 (auto-switch) の結合テスト。

`verify()` を mock せず、状態を持つ偽の gh (`FakeGh`) で subprocess.run だけを
差し替える。検証 → 切替の計画 → 切替 → 再検証 が実際の service コードを通る。
ローカル設定の読取は隔離 (`_testutil.start_isolation`) で無効になっているので、
gh の現在値は常に偽の `gh auth status` から取られる。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import auto_switch, budget  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

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


class FakeGh:
    """`gh auth status` / `gh auth switch` を状態付きで模す `subprocess.run` の差し替え。"""

    def __init__(self, accounts: dict[str, list[str]], active: dict[str, str]):
        self.accounts = {host: list(users) for host, users in accounts.items()}
        self.active = dict(active)
        self.calls: list[list[str]] = []
        self.switch_error: str | None = None

    def switches(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "auth", "switch"]]

    def __call__(self, args, **_kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return SimpleNamespace(stdout=self._status(), stderr="", returncode=0)
        if args[:3] == ["gh", "auth", "switch"]:
            host = args[args.index("--hostname") + 1]
            user = args[args.index("--user") + 1]
            if self.switch_error:
                return SimpleNamespace(stdout="", stderr=self.switch_error, returncode=1)
            if user not in self.accounts.get(host, []):
                return SimpleNamespace(
                    stdout="", stderr=f"not logged in to {host} account {user}", returncode=1
                )
            self.active[host] = user
            return SimpleNamespace(
                stdout="", stderr=f"✓ Switched active account for {host} to {user}", returncode=0
            )
        raise AssertionError(f"想定外のコマンド: {args}")

    def _status(self) -> str:
        lines = []
        for host, users in self.accounts.items():
            lines.append(host)
            for user in users:
                lines.append(f"  ✓ Logged in to {host} account {user} (keyring)")
                flag = "true" if self.active.get(host) == user else "false"
                lines.append(f"  - Active account: {flag}")
        return "\n".join(lines) + "\n"


class AutoSwitchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project_dir = Path(self.tmp) / "project"
        self.new_dir = self.project_dir / ".claude" / "verify-cloud-account"
        self.new_dir.mkdir(parents=True)
        cache_tmp = Path(self.tmp) / "cache_tmp"
        cache_tmp.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project_dir), "TMPDIR": str(cache_tmp)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 既定: github.com に work (アクティブ) と Mao-o (非アクティブ) でログイン済み。
        self.gh = FakeGh({"github.com": ["work", "Mao-o"]}, {"github.com": "work"})

    def _write_accounts(self, data: dict):
        (self.new_dir / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    @contextlib.contextmanager
    def _enabled(self, value="github"):
        with mock.patch.dict(os.environ, {auto_switch.ENV_VAR: value}):
            yield

    def _dispatch(self, command: str):
        with mock.patch("subprocess.run", side_effect=self.gh):
            return dispatch(command, str(self.project_dir))

    def _deny_reason(self, result) -> str:
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out.get("permissionDecision"), "deny", out)
        return out["permissionDecisionReason"]

    def _context(self, result) -> str:
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out)
        return out["additionalContext"]


class TestOptIn(AutoSwitchBase):
    def test_disabled_by_default(self):
        """既定 (opt-in 無し) は従来どおり deny で、gh の状態に触らない。"""
        self._write_accounts({"github": "Mao-o"})
        reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("アカウント不一致", reason)
        self.assertEqual(self.gh.switches(), [])
        self.assertEqual(self.gh.active["github.com"], "work")

    def test_env_opt_in_switches_and_lets_the_write_through(self):
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            result = self._dispatch("gh pr create --fill")
        context = self._context(result)
        self.assertEqual(
            self.gh.switches(),
            [["gh", "auth", "switch", "--hostname", "github.com", "--user", "Mao-o"]],
        )
        self.assertEqual(self.gh.active["github.com"], "Mao-o")
        self.assertIn("自動切替", context)
        self.assertIn("github.com: work → Mao-o", context)
        self.assertIn("全ターミナル・セッション", context, "副作用の範囲を伝えていない")

    def test_file_opt_in(self):
        self._write_accounts({"github": "Mao-o", "$auto_switch": ["github"]})
        self._context(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.active["github.com"], "Mao-o")

    def test_env_off_overrides_file(self):
        self._write_accounts({"github": "Mao-o", "$auto_switch": ["github"]})
        with self._enabled("off"):
            self._deny_reason(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.switches(), [])

    def test_invalid_file_value_denies_with_note(self):
        self._write_accounts({"github": "Mao-o", "$auto_switch": True})
        reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn('"$auto_switch"', reason)
        self.assertEqual(self.gh.switches(), [])

    def test_matching_account_does_not_switch_or_notify(self):
        """negative control: 一致していれば従来どおり何も返さない。"""
        self.gh.active["github.com"] = "Mao-o"
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            self.assertIsNone(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.switches(), [])


class TestOnlyWhereItWouldStop(AutoSwitchBase):
    """切り替えるのは deny になる場面だけ (QUERY の警告 / warn モードでは切り替えない)。"""

    def test_query_mismatch_is_warned_not_switched(self):
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            context = self._context(self._dispatch("gh pr list"))
        self.assertIn("リモート read", context)
        self.assertEqual(self.gh.switches(), [])

    def test_query_is_switched_when_readonly_policy_denies(self):
        self._write_accounts({"github": "Mao-o", "$readonly": "deny"})
        with self._enabled():
            context = self._context(self._dispatch("gh pr list"))
        self.assertIn("自動切替", context)
        self.assertEqual(self.gh.active["github.com"], "Mao-o")

    def test_warn_mode_does_not_switch(self):
        self._write_accounts({"github": "Mao-o"})
        with self._enabled(), mock.patch.dict(os.environ, {"VERIFY_CLOUD_ACCOUNT_MODE": "warn"}):
            context = self._context(self._dispatch("gh pr create"))
        self.assertIn("warn モード", context)
        self.assertEqual(self.gh.switches(), [])

    def test_command_that_changes_account_state_itself_is_not_switched(self):
        """`gh auth refresh` (identity を変えない切替系) と連結された write。"""
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("gh auth refresh && gh pr create"))
        self.assertIn(auto_switch.NOTE_SWITCHING_HERE, reason)
        self.assertEqual(self.gh.switches(), [])

    def test_help_commands_are_not_switched(self):
        """`--help` 付きのコマンド (ヘルプの表示) のために、マシン全体の gh を切り替えない。

        `gh auth git-credential --help` などは READONLY の `gh --help` の形に当たらず、通常の
        検証に乗る。0.20.0 までは不一致で自動切替が走っていた (内部バックログ)。判定は変えず
        deny のまま、切り替えなかった理由を添える。"""
        self._write_accounts({"github": "Mao-o"})
        for command in (
            "gh auth git-credential --help",
            "gh auth --help",
            "gh pr create --help",
            "gh pr create --help=true",
            "gh pr create --help && gh issue create --help",
        ):
            with self.subTest(command=command):
                with self._enabled():
                    reason = self._deny_reason(self._dispatch(command))
                self.assertIn(auto_switch.NOTE_HELP, reason)
                self.assertIn("アカウント不一致", reason)
                self.assertEqual(self.gh.switches(), [])
                self.assertEqual(self.gh.active["github.com"], "work")

    def test_short_h_is_not_help_in_gh_auth(self):
        """`-h` はヘルプとして数えない (gh の `auth` / `api` では `--hostname`、`pr create` では
        `--head` の短い形で、実際の操作になる)。`-h` 付きのコマンドは従来どおり切り替える。
        `--help` の無いコマンドが同じ行にあるときも、そのために切り替える。"""
        for command in (
            "gh api -h github.com -X DELETE repos/o/r",
            "gh pr create -h feature",
            "gh pr create --title 'see --help' --fill",
            "gh issue create --help && gh pr create",
        ):
            with self.subTest(command=command):
                self.gh = FakeGh({"github.com": ["work", "Mao-o"]}, {"github.com": "work"})
                self._write_accounts({"github": "Mao-o"})
                with self._enabled():
                    self._context(self._dispatch(command))
                self.assertEqual(
                    self.gh.switches(),
                    [["gh", "auth", "switch", "--hostname", "github.com", "--user", "Mao-o"]],
                )

    def test_chained_switch_and_write_is_still_denied_first(self):
        """連結規則の deny は自動切替より前に決まる (切替後の状態は検証できない)。"""
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(
                self._dispatch("gh auth switch --user work && gh pr create")
            )
        self.assertIn("連結", reason)
        self.assertEqual(self.gh.switches(), [])


class TestCannotSwitch(AutoSwitchBase):
    def test_expected_account_not_logged_in(self):
        self.gh.accounts["github.com"] = ["work"]
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("自動切替 (auto-switch) は行いませんでした", reason)
        self.assertIn("github.com の Mao-o", reason)
        self.assertEqual(self.gh.switches(), [])
        # verify() の案内 (切替コマンドと単独実行の注記) は従来どおり残る。
        self.assertIn("gh auth switch --hostname github.com --user Mao-o", reason)
        self.assertIn("単独で実行してください", reason)

    def test_dict_all_or_nothing(self):
        self.gh = FakeGh(
            {"github.com": ["work", "Mao-o"], "ghe.example.com": ["someone"]},
            {"github.com": "work", "ghe.example.com": "someone"},
        )
        self._write_accounts({"github": {"github.com": "Mao-o", "ghe.example.com": "corp"}})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("どの host も切り替えません", reason)
        self.assertEqual(self.gh.switches(), [])
        self.assertEqual(self.gh.active["github.com"], "work", "一部の host だけ切り替えている")

    def test_dict_multi_host_switch(self):
        self.gh = FakeGh(
            {"github.com": ["work", "Mao-o"], "ghe.example.com": ["someone", "corp"]},
            {"github.com": "work", "ghe.example.com": "someone"},
        )
        self._write_accounts({"github": {"github.com": "Mao-o", "ghe.example.com": "corp"}})
        with self._enabled():
            context = self._context(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.active, {"github.com": "Mao-o", "ghe.example.com": "corp"})
        self.assertIn("ghe.example.com: someone → corp", context)

    def test_switch_failure_keeps_the_deny(self):
        self.gh.switch_error = "keyring: access denied"
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("自動切替 (auto-switch) を試みましたが", reason)
        self.assertIn("keyring: access denied", reason)
        self.assertEqual(self.gh.active["github.com"], "work")

    def test_token_env_prevents_switch(self):
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("GH_TOKEN=x gh pr create"))
        self.assertIn("トークン", reason)
        self.assertEqual(self.gh.switches(), [])

    def test_budget_exhausted_by_verification_skips_switch(self):
        """verify() で予算を使い切ったら、切替の CLI を起動しない。"""
        self._write_accounts({"github": "Mao-o"})
        real_gh = self.gh

        def gh_then_expire(args, **kwargs):
            result = real_gh(args, **kwargs)
            budget.start(0.0)
            return result

        with self._enabled(), mock.patch("subprocess.run", side_effect=gh_then_expire):
            result = dispatch("gh pr create", str(self.project_dir))
        reason = self._deny_reason(result)
        self.assertIn(auto_switch.NOTE_BUDGET, reason)
        self.assertEqual(real_gh.switches(), [])
        self.assertEqual(len(real_gh.calls), 1, "予算切れの後も CLI を起動している")


class TestConcurrentSessionGuard(AutoSwitchBase):
    def test_recent_switch_by_other_work_to_another_account_is_respected(self):
        auto_switch.record_switch(
            "github", [("github.com", "Mao-o", "work")], "/other/project"
        )
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("/other/project", reason)
        self.assertIn("ユーザーに確認", reason)
        self.assertEqual(self.gh.switches(), [])

    def test_recent_switch_to_the_same_account_does_not_block(self):
        auto_switch.record_switch("github", [("github.com", "work", "Mao-o")], "/worktree")
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            self._context(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.active["github.com"], "Mao-o")

    def test_old_switch_does_not_block(self):
        auto_switch.record_switch(
            "github", [("github.com", "Mao-o", "work")], "/other/project",
            now_ns=time.time_ns() - (auto_switch.GUARD_SEC + 5) * 1_000_000_000,
        )
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            self._context(self._dispatch("gh pr create"))
        self.assertEqual(self.gh.active["github.com"], "Mao-o")

    def test_ping_pong_between_two_projects_is_stopped_on_the_second_flip(self):
        """別アカウントを期待する 2 プロジェクトが交互に切り替えない。"""
        other = Path(self.tmp) / "other"
        (other / ".claude" / "verify-cloud-account").mkdir(parents=True)
        (other / ".claude" / "verify-cloud-account" / "accounts.local.json").write_text(
            json.dumps({"github": "work"}), encoding="utf-8"
        )
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            self._context(self._dispatch("gh pr create"))  # work → Mao-o
            with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(other)}), \
                    mock.patch("subprocess.run", side_effect=self.gh):
                result = dispatch("gh pr create", str(other))
        reason = self._deny_reason(result)
        self.assertIn(str(self.project_dir), reason)
        self.assertEqual(self.gh.active["github.com"], "Mao-o", "2 回目の切替で戻している")


class TestCacheAndReporting(AutoSwitchBase):
    def test_switch_invalidates_success_cache_before_switching(self):
        self._write_accounts({"github": "Mao-o"})
        events = []
        real_gh = self.gh

        def gh(args, **kwargs):
            if list(args)[:3] == ["gh", "auth", "switch"]:
                events.append("switch")
            return real_gh(args, **kwargs)

        with self._enabled(), \
                mock.patch("core.cache.invalidate", side_effect=lambda n: events.append(f"invalidate:{n}")), \
                mock.patch("subprocess.run", side_effect=gh):
            dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(events, ["invalidate:github", "switch"])

    def test_next_write_is_verified_against_the_switched_state(self):
        self._write_accounts({"github": "Mao-o"})
        with self._enabled():
            self._context(self._dispatch("gh pr create"))
            self.assertIsNone(self._dispatch("gh pr merge 1"), "切替後の状態で通っていない")
        self.assertEqual(len(self.gh.switches()), 1)

    def test_other_service_deny_still_reports_the_gh_switch(self):
        self._write_accounts({"github": "Mao-o", "aws": "123456789012"})
        with self._enabled(), mock.patch(
            "services.aws.verify", return_value="AWS アカウント不一致: 現在=999, 期待=123456789012"
        ):
            reason = self._deny_reason(
                self._dispatch("gh pr create && aws s3 rm s3://bucket/key")
            )
        self.assertIn("AWS アカウント不一致", reason)
        self.assertIn("github.com: work → Mao-o", reason, "gh を切り替えた事実が落ちている")
        self.assertNotIn("GitHub [github.com] アカウント不一致", reason)

    def test_debug_trace_records_the_switch(self):
        self._write_accounts({"github": "Mao-o"})
        stderr = io.StringIO()
        with self._enabled(), mock.patch.dict(os.environ, {"VERIFY_CLOUD_ACCOUNT_DEBUG": "1"}), \
                contextlib.redirect_stderr(stderr):
            self._dispatch("gh pr create")
        trace = json.loads(stderr.getvalue().strip().splitlines()[-1])
        self.assertEqual(
            trace["auto_switch"]["github"],
            {"resolved": True, "switched": [["github.com", "work", "Mao-o"]], "note": None},
        )

    def test_internal_error_in_auto_switch_keeps_the_deny(self):
        """自動切替の内部エラーで hook 全体を fail-open に落とさない。"""
        self._write_accounts({"github": "Mao-o"})
        with self._enabled(), mock.patch(
            "services.github.plan_switch", side_effect=RuntimeError("boom")
        ):
            reason = self._deny_reason(self._dispatch("gh pr create"))
        self.assertIn("内部エラー", reason)
        self.assertIn("アカウント不一致", reason)


if __name__ == "__main__":
    unittest.main()
