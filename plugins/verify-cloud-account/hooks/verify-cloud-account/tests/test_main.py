"""`__main__.py` の stdin → stdout E2E スモーク。

クラウド CLI は起動しない (未設定 project の deny は verify 前に決まり、readonly /
非対象コマンドは検証しない)。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

_PKG_DIR = Path(__file__).resolve().parent.parent


def _load_entry_module():
    """`__main__.py` を `__main__` 以外の名前でロードする (import 名の衝突回避)。

    `python3 <pkg_dir>` (subprocess E2E) 以外に、内部エラー時の fail-open を
    プロセスを跨がず直接ユニットテストするために使う。
    """
    spec = importlib.util.spec_from_file_location(
        "verify_cloud_account_entry", _PKG_DIR / "__main__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMainEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = Path(self.tmp) / "project"
        self.project.mkdir()
        self.cache_tmp = Path(self.tmp) / "cache"
        self.cache_tmp.mkdir()
        # 子プロセスは実環境の env を引き継ぐため、`$HOME` (グローバル既定の
        # accounts.local.json) / CLI 設定 dir / モード env をここで隔離する。
        # 開発者が自分用のグローバル既定や VERIFY_CLOUD_ACCOUNT_MODE を持って
        # いると「未設定なら deny」のスモークが通らなくなる。
        #
        # `start_isolation()` は使えない (この `os.environ` ではなく**子プロセスの**
        # env を組むため) が、除去規則は `_testutil.sanitized_env()` で共有する。
        self.fake_home = Path(self.tmp) / "home"
        self.fake_home.mkdir()
        self.env = _testutil.sanitized_env(
            {
                **os.environ,
                **_testutil.cli_config_env(
                    Path(self.tmp) / "cliconfig", self.fake_home
                ),
                "TMPDIR": str(self.cache_tmp),
                "CLAUDE_PROJECT_DIR": str(self.project),
            }
        )

    def _run(self, command: str) -> subprocess.CompletedProcess:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.project),
        }
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def test_unconfigured_write_emits_deny_json(self):
        res = self._run("gh pr create")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("未設定", out["permissionDecisionReason"])

    def test_mode_off_env_is_silent_end_to_end(self):
        """escape hatch の実プロセススモーク: off なら JSON を出さない (= 通す)。"""
        res = subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh pr create"},
                    "cwd": str(self.project),
                }
            ),
            capture_output=True,
            text=True,
            env={**self.env, "VERIFY_CLOUD_ACCOUNT_MODE": "off"},
            timeout=30,
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_readonly_login_is_silent_and_invalidates(self):
        res = self._run("gh auth login --skip-ssh-key")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertTrue(
            (self.cache_tmp / "cc-mp-verify-cloud-account" / "github.epoch").is_file()
        )

    def test_non_target_command_is_silent(self):
        res = self._run("git status")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_debug_trace_with_closed_stderr_still_emits_deny_on_stdout(self):
        """マージ前レビューの指摘: 実プロセスで stderr の読み手が居ない
        (broken pipe) 状態で `VERIFY_CLOUD_ACCOUNT_DEBUG=1` の trace 出力が
        失敗しても、計算済みの deny が stdout にそのまま出ることを固定する。
        修正前は trace の print() が BrokenPipeError を投げて dispatch() の
        外まで伝播し、fail-open 経路の 2 段目の stderr 書き込みも同じ理由で
        失敗して stdout に何も出ないまま子プロセスが終了していた。"""
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "cwd": str(self.project),
        }
        r, w = os.pipe()
        os.close(r)  # 読み手不在にする = 子プロセスの stderr write は EPIPE
        env = {**self.env, "VERIFY_CLOUD_ACCOUNT_DEBUG": "1"}
        try:
            res = subprocess.run(
                [sys.executable, str(_PKG_DIR)],
                input=json.dumps(payload),
                stdout=subprocess.PIPE,
                stderr=w,
                text=True,
                env=env,
                timeout=30,
            )
        finally:
            os.close(w)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")

    def test_invalid_json_is_silent(self):
        res = subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input="{not json",
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    # --- 読めないファイルで検証が飛ばない (v0.18.0) -----------------------------------
    #
    # 期待値ファイル / `.firebaserc` / 成功 cache の読み込みで UnicodeDecodeError /
    # RecursionError が dispatch() の外まで抜けると、最終防波堤が「内部エラーのため検証を
    # スキップ」(additionalContext だけ。実行は止めない) にしていた。実プロセスで、その経路が
    # 残っていないことを確かめる。PATH は空のディレクトリにしてクラウド CLI を起動しない
    # (firebase の照合は `--project` の解決か、CLI が無いことで決まる)。

    _DEEP = "[" * 100000 + "]" * 100000
    _ACCOUNTS = ".claude/verify-cloud-account/accounts.local.json"

    def _write(self, rel: str, data) -> None:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")

    def _run_without_cli(self, command: str, mode: str = "enforce"):
        empty_bin = Path(self.tmp) / "empty-bin"
        empty_bin.mkdir(exist_ok=True)
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.project),
        }
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**self.env, "PATH": str(empty_bin), "VERIFY_CLOUD_ACCOUNT_MODE": mode},
            timeout=60,
        )

    def _hook_outcome(self, res) -> tuple[str, str]:
        """(判定, 本文)。deny / warn (additionalContext だけ) / allow (出力なし)。"""
        self.assertEqual(res.returncode, 0, res.stderr)
        if not res.stdout.strip():
            return "allow", ""
        out = json.loads(res.stdout)["hookSpecificOutput"]
        if "permissionDecision" in out:
            return out["permissionDecision"], out.get("permissionDecisionReason", "")
        return "warn", out.get("additionalContext", "")

    def test_unreadable_accounts_file_is_handled_like_malformed_json(self):
        """UTF-8 でない・入れ子が深い・桁の多すぎる整数のある期待値ファイルは、不正な JSON と
        同じ判定 (mode は env だけで決める。`"$mode"` は読めない)。旧版は検証をスキップしていた
        (実測は旧パスの `.claude/accounts.json` に 0xFF を 1 バイト入れただけ)。

        桁の多すぎる整数は Python の上限 (3.11 以降の既定は 4,300 桁) で `json.loads` が
        ValueError (JSONDecodeError ではない) を投げる (マージ前レビューの指摘)。上限の無い
        Python (3.9.6 など) では読めるファイルなので、その case は上限があるときだけ流す。"""
        not_utf8 = b'{"firebase": "right-project\xff"}'
        cases = {
            "malformed JSON (control)": (self._ACCOUNTS, "{not json", "JSON が不正です"),
            "not UTF-8": (self._ACCOUNTS, not_utf8, "読めません (UnicodeDecodeError)"),
            "not UTF-8, legacy path": (
                ".claude/accounts.json", not_utf8, "読めません (UnicodeDecodeError)"
            ),
            "too deep to parse": (
                self._ACCOUNTS,
                '{"firebase": "right-project", "pad": ' + self._DEEP + "}",
                "読めません (RecursionError)",
            ),
            '"$mode": "off" in a file too deep to parse': (
                self._ACCOUNTS,
                '{"$mode": "off", "firebase": "right-project", "pad": ' + self._DEEP + "}",
                "読めません (RecursionError)",
            ),
            "parsed, but deeper than the limit": (
                self._ACCOUNTS,
                '{"firebase": {"a": "right-project", "pad": ' + "[" * 40 + "]" * 40 + "}}",
                "読めません (入れ子が 32 段より深い)",
            ),
        }
        if 0 < getattr(sys, "get_int_max_str_digits", lambda: 0)() < 5000:
            cases["too many digits"] = (
                self._ACCOUNTS,
                '{"firebase": "right-project", "pad": ' + "1" * 5000 + "}",
                "読めません (ValueError)",
            )
        for name, (rel, data, marker) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.project / ".claude", ignore_errors=True)
                self._write(rel, data)
                for mode, want in (("enforce", "deny"), ("warn", "warn")):
                    decision, text = self._hook_outcome(
                        self._run_without_cli("firebase deploy --project prod", mode)
                    )
                    self.assertEqual(decision, want, text)
                    self.assertIn(marker, text)
                    self.assertNotIn("内部エラー", text)

    def test_unconfirmed_firebaserc_is_denied_not_skipped(self):
        """入れ子が深すぎて読めない `.firebaserc` は、`--project` の解決でも、CLI が無いときの
        ローカル設定の解決でも deny (旧版は RecursionError で検証をスキップしていた)。"""
        self._write(self._ACCOUNTS, json.dumps({"firebase": "right-project"}))
        self._write("firebase.json", "{}")
        self._write(
            ".firebaserc",
            '{"projects": {"default": "right-project", "right-project": "wrong-project"},'
            ' "pad": ' + self._DEEP + "}",
        )
        for command, marker in (
            ("firebase deploy --project right-project", "--project の行き先を確かめられません"),
            ("firebase deploy", "firebase コマンドが見つかりません"),
        ):
            with self.subTest(command=command):
                decision, text = self._hook_outcome(self._run_without_cli(command))
                self.assertEqual(decision, "deny", text)
                self.assertIn(marker, text)

    def test_unreadable_cache_state_is_reverified_not_skipped(self):
        """読めない成功 cache の entry / epoch ファイルは cache miss / epoch 0 として検証を
        続ける (旧版は UnicodeDecodeError / RecursionError で検証をスキップしていた)。"""
        self._write(self._ACCOUNTS, json.dumps({"firebase": "right-project"}))
        # `.firebaserc` が無いので --project right-project の行き先は right-project (allow)。
        command = "firebase deploy --project right-project"
        cache_dir = self.cache_tmp / "cc-mp-verify-cloud-account"
        cases = {
            "cache entry not UTF-8": ("firebase-*.json", b'{"success": true, "x": "\xff"}'),
            "cache entry too deep": ("firebase-*.json", self._DEEP.encode()),
            "epoch file too deep": ("firebase.epoch", self._DEEP.encode()),
        }
        for name, (pattern, payload) in cases.items():
            with self.subTest(name):
                shutil.rmtree(cache_dir, ignore_errors=True)
                # 1 回目の allow が成功 cache を書く。
                self.assertEqual(self._hook_outcome(self._run_without_cli(command)), ("allow", ""))
                if "*" in pattern:
                    targets = list(cache_dir.glob(pattern))
                    self.assertTrue(targets, "成功 cache が書かれていない")
                else:
                    targets = [cache_dir / pattern]
                for path in targets:
                    path.write_bytes(payload)
                self.assertEqual(self._hook_outcome(self._run_without_cli(command)), ("allow", ""))


class TestMainInternalErrorFailOpen(unittest.TestCase):
    """内部バックログ: dispatch() の未捕捉例外は exit 1 の無音 fail-open ではなく、
    additionalContext の warn として明示し、stderr にも同じ理由を出す。"""

    def test_dispatch_exception_emits_warn_json_and_stderr(self):
        module = _load_entry_module()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "cwd": "/tmp",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(module, "dispatch", side_effect=RuntimeError("boom")):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr):
                module.main()
        out = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        self.assertIn("内部エラーのため検証をスキップしました", out["additionalContext"])
        self.assertIn("RuntimeError", out["additionalContext"])
        self.assertIn("boom", out["additionalContext"])
        # permissionDecision (deny) は含まない = 実行を阻止しない (fail-open)。
        self.assertNotIn("permissionDecision", out)
        self.assertIn("内部エラーのため検証をスキップしました", stderr.getvalue())
        self.assertIn("RuntimeError", stderr.getvalue())

    def test_recovery_path_stderr_failure_still_emits_stdout_json(self):
        """マージ前レビューの指摘: 回復経路 (fail-open) 自身の stderr 書き込みが
        失敗 (OSError/BrokenPipeError) しても、stdout への判定 JSON 出力は
        必ず行われる。修正前は print() が例外を投げて main() 全体が異常終了し、
        stdout に何も出ないまま終了していた。"""

        class _BrokenStderr:
            def write(self, s):
                raise OSError("stderr is closed")

            def flush(self):
                pass

        module = _load_entry_module()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr create"},
            "cwd": "/tmp",
        }
        stdout = io.StringIO()
        with mock.patch.object(module, "dispatch", side_effect=RuntimeError("boom")):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", _BrokenStderr()):
                module.main()
        out = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        self.assertIn("内部エラーのため検証をスキップしました", out["additionalContext"])
        self.assertNotIn("permissionDecision", out)

    def test_no_exception_does_not_write_stderr(self):
        """正常系 (DEBUG 無効) では stderr に何も書かない (回帰防止)。"""
        module = _load_entry_module()
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "cwd": "/tmp",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(module, "dispatch", return_value=None):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr):
                module.main()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
