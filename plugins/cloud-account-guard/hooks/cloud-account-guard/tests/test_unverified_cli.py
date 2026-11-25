"""検証の対象外のクラウド CLI の知らせ (v0.31.0。`core/unverified.py`)。

対象外の CLI (wrangler など) が Bash のコマンドの位置に現れたら、allow のまま `additionalContext` で
1 回だけ知らせる。判定 (allow / deny) は変えない。同じセッションの同じエージェント (メインは hook input の
`agent_id` 無し、サブエージェントは `agent_id` ごと) では CLI ごとに 1 回。deny には足さず、知らせたことも
記録しない。
"""
from __future__ import annotations

import contextlib
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

from core import cache, dispatcher, mode, unverified  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

_PKG_DIR = Path(__file__).resolve().parent.parent

_ISOLATION = None
_ISOLATION_ROOT = None

# 知らせの本文に必ず入る句 (`unverified.notice`)。deny の本文に混ざっていないことの確認にも使う。
_MARK = "検証の対象外の CLI です"


def setUpModule():
    """実環境の `$HOME` / CLI 設定 / モード env を読ませない (test_dispatcher と同じ)。"""
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        (self.project / ".claude" / "cloud-account-guard").mkdir(parents=True)
        self.cache_tmp = self.tmp / "cache"
        self.cache_tmp.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project), "TMPDIR": str(self.cache_tmp)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data: dict) -> None:
        path = self.project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        path.write_text(json.dumps(data), encoding="utf-8")

    def _dispatch(self, command: str, session="s1", agent=None):
        # 知らせの処理から例外が漏れる変異も、ERROR ではなく assertion の失敗に数える。
        try:
            return dispatch(command, str(self.project), session_id=session, agent_id=agent)
        except Exception as e:  # noqa: BLE001
            self.fail(f"dispatch が例外を投げた: {type(e).__name__}: {e}")

    def _verdict_without_notice(self, command: str):
        """知らせを足さない (対象外の CLI を検出しない) ときの判定の結果。"""
        with mock.patch.object(unverified, "detect", return_value=[]):
            return dispatch(command, str(self.project), session_id="baseline")

    def assert_notice(self, result, names: list[str]) -> str:
        """result が「allow のまま知らせる」形で、知らせの名前が names (この順) であること。"""
        self.assertIsNotNone(result, "知らせが出ていない")
        self.assertIsInstance(result, dict)
        self.assertIn("hookSpecificOutput", result)
        out = result["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out, "知らせが判定 (deny / ask など) を変えた")
        self.assertEqual(out.get("hookEventName"), "PreToolUse")
        self.assertIn("additionalContext", out)
        context = out["additionalContext"]
        self.assertTrue(context.endswith(unverified.notice(names)), context)
        return context


class TestOncePerSession(_Base):
    def test_wrangler_deploy_notifies_once(self):
        self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])
        self.assertIsNone(self._dispatch("wrangler deploy"))

    def test_npx_wrangler_deploy_notifies_once(self):
        self.assert_notice(self._dispatch("npx wrangler deploy"), ["wrangler"])
        self.assertIsNone(self._dispatch("npx wrangler deploy"))
        # 素の形も同じ CLI として数える (2 回目は出ない)。
        self.assertIsNone(self._dispatch("wrangler deploy"))

    def test_npx_pinned_version_counts_as_the_same_cli(self):
        """`npx wrangler@3 deploy` (版の固定) も wrangler として知らせる。"""
        self.assert_notice(self._dispatch("npx wrangler@3 deploy"), ["wrangler"])
        self.assertIsNone(self._dispatch("npx wrangler@latest deploy"))

    def test_other_session_is_notified_again(self):
        self.assert_notice(self._dispatch("wrangler deploy", session="s1"), ["wrangler"])
        self.assert_notice(self._dispatch("wrangler deploy", session="s2"), ["wrangler"])
        self.assertIsNone(self._dispatch("wrangler deploy", session="s1"))

    def test_each_cli_is_notified_once_per_session(self):
        self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])
        self.assert_notice(self._dispatch("cf push app"), ["cf"])
        self.assertIsNone(self._dispatch("wrangler deploy && cf push app"))

    def test_two_clis_in_one_command_share_one_notice(self):
        self.assert_notice(
            self._dispatch("wrangler deploy && cf push app"), ["wrangler", "cf"]
        )
        self.assertIsNone(self._dispatch("cf push app"))

    def test_main_and_subagents_do_not_silence_each_other(self):
        """1 回の単位は「セッション × エージェント × CLI」。

        サブエージェントは同じ session_id を持ち、知らせはツールを呼んだエージェントにだけ届く。先に
        サブエージェントが使っても、メイン (agent_id 無し) にはメインの 1 回が出る。同じエージェントの
        2 回目は出ない。
        """
        self.assert_notice(self._dispatch("wrangler deploy", agent="agent-a1"), ["wrangler"])
        self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])
        self.assert_notice(self._dispatch("wrangler deploy", agent="agent-b2"), ["wrangler"])
        self.assertIsNone(self._dispatch("wrangler deploy"))
        self.assertIsNone(self._dispatch("wrangler deploy", agent="agent-a1"))
        self.assertIsNone(self._dispatch("npx wrangler deploy", agent="agent-b2"))
        # 別のセッションでは、同じ agent_id でも改めて出す。
        self.assert_notice(
            self._dispatch("wrangler deploy", session="s2", agent="agent-a1"), ["wrangler"]
        )

    def test_agent_id_that_is_not_a_string_counts_as_the_main(self):
        """agent_id は文字列のときだけ使う (無い・空・文字列でないものはメインとして数える)。"""
        self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])
        for agent in ("", 123, ["x"]):
            with self.subTest(agent=agent):
                self.assertIsNone(self._dispatch("wrangler deploy", agent=agent))

    def test_cli_only_in_an_alternative_reading_is_notified(self):
        """既定の読みでは引用符の中だが、ANSI-C の読み (bash / zsh が実際に走らせる) でだけ候補になる形。"""
        self.assert_notice(self._dispatch("echo $'\\'' ; wrangler deploy"), ["wrangler"])

    def test_record_is_a_private_file_named_by_a_digest(self):
        session = "sess/../with-slash"
        self.assert_notice(self._dispatch("wrangler deploy", session=session), ["wrangler"])
        base = cache.state_dir()
        self.assertIsNotNone(base)
        flags = sorted(p.name for p in base.iterdir() if p.name.startswith("unverified-cli-"))
        self.assertEqual(len(flags), 1, flags)
        self.assertNotIn("with-slash", flags[0])
        if hasattr(os, "geteuid"):
            self.assertEqual(os.stat(base / flags[0]).st_mode & 0o777, 0o600)


class TestDetection(unittest.TestCase):
    def test_notice_text(self):
        text = unverified.notice(["wrangler", "cf"])
        self.assertTrue(
            text.startswith("[cloud-account-guard] wrangler / cf は検証の対象外の CLI です。"), text
        )
        self.assertIn("アカウントを照合しておらず", text)
        self.assertIn("同じセッションの同じエージェントでは CLI ごとに 1 回だけ", text)

    def test_names_in_first_appearance_order_without_duplicates(self):
        self.assertEqual(
            unverified.detect(["cf push", "wrangler deploy", "wrangler@3 tail", "cf"]),
            ["cf", "wrangler"],
        )

    def test_command_word_only(self):
        for candidate in (
            "npm install -D wrangler",
            "echo wrangler",
            "git commit -m 'wrangler deploy'",
            "wrangler-foo x",
            "cfg x",
            "xwrangler deploy",
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(unverified.detect([candidate]), [])


class TestNoNoticeForOtherCommands(_Base):
    def test_supported_clis_and_plain_commands_do_not_notify(self):
        self._write_accounts(
            {
                "github": "alice",
                "firebase": "proj-a",
                "aws": "123456789012",
                "gcloud": "proj-a",
                "kubectl": "ctx-a",
            }
        )
        commands = (
            "gh pr create",
            "gh auth status",
            "firebase deploy",
            "npx firebase-tools deploy",
            "aws s3 rm s3://b/k",
            "gcloud run deploy svc",
            "kubectl apply -f x.yaml",
            "git status",
            "npm install -D wrangler",
            "echo wrangler && git commit -m 'cf push'",
            "x=$(wrangler whoami) true",
        )
        with contextlib.ExitStack() as stack:
            for name in ("github", "firebase", "aws", "gcloud", "kubectl"):
                stack.enter_context(mock.patch(f"services.{name}.verify", return_value=None))
            for command in commands:
                with self.subTest(command=command):
                    self.assertIsNone(self._dispatch(command))

    def test_unverified_names_are_not_service_commands(self):
        """一覧の名前は検証の対象の CLI と重ならない (検証に対応したら一覧から外す)。"""
        for name in unverified.UNVERIFIED_CLIS:
            for candidate in (name, f"{name} deploy", f"{name}@1 deploy"):
                with self.subTest(candidate=candidate):
                    self.assertIsNone(dispatcher._match_service(candidate))
                    self.assertEqual(unverified.detect([candidate]), [name])


class TestVerdictIsKept(_Base):
    def test_deny_is_unchanged_and_not_recorded(self):
        """deny には知らせを足さず、記録もしない (次に通ったコマンドで知らせる)。"""
        command = "wrangler deploy && gh pr create"
        expected = self._verdict_without_notice(command)
        self.assertEqual(expected["hookSpecificOutput"]["permissionDecision"], "deny")
        result = self._dispatch(command)
        self.assertEqual(result, expected)
        self.assertNotIn(_MARK, json.dumps(result, ensure_ascii=False))
        self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])

    def test_query_warning_is_kept_and_the_notice_follows(self):
        command = "gh pr list && wrangler deploy"
        expected = self._verdict_without_notice(command)
        self.assertNotIn("permissionDecision", expected["hookSpecificOutput"])
        original = expected["hookSpecificOutput"]["additionalContext"]
        context = self.assert_notice(self._dispatch(command), ["wrangler"])
        self.assertEqual(context, original + "\n\n" + unverified.notice(["wrangler"]))

    def test_output_without_context_gets_the_notice_alone(self):
        """deny でも警告でもない出力 (今の判定は作らない形) には、知らせだけを入れる。"""
        result = dispatcher._with_unverified_notice(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}, ["wrangler"], None
        )
        self.assertEqual(
            result,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": unverified.notice(["wrangler"]),
                }
            },
        )

    def test_failure_in_the_notice_keeps_the_verdict(self):
        """知らせの処理の例外は握り、判定の結果をそのまま返す (内部エラーの fail-open に化けさせない)。"""
        for command in ("wrangler deploy", "gh pr list && wrangler deploy"):
            with self.subTest(command=command):
                expected = self._verdict_without_notice(command)
                with mock.patch.object(unverified, "claim", side_effect=RuntimeError("boom")):
                    self.assertEqual(self._dispatch(command), expected)

    def test_failure_in_the_detection_keeps_the_verdict(self):
        """検出 (`unverified.detect`) の例外も握り、判定 (deny を含む) をそのまま返す。

        検出は検証より前に、対象外の CLI を含まないコマンドでも走る。ここで例外が抜けると、__main__ の
        最終防波堤が deny を「内部エラーのため検証をスキップ」に置き換える (fail-open)。デバッグトレースの
        経路 (検出の結果を trace にも写す) でも同じ。
        """
        cases = (
            ("gh pr create", "deny"),
            ("wrangler deploy && gh pr create", "deny"),
            ("gh pr list && wrangler deploy", None),
        )
        for debug in ("0", "1"):
            for command, decision in cases:
                stderr = io.StringIO()
                with self.subTest(command=command, debug=debug), mock.patch.dict(
                    os.environ, {"CLOUD_ACCOUNT_GUARD_DEBUG": debug}
                ), contextlib.redirect_stderr(stderr):
                    expected = self._verdict_without_notice(command)
                    self.assertIsInstance(expected, dict)
                    self.assertIn("hookSpecificOutput", expected)
                    self.assertEqual(
                        expected["hookSpecificOutput"].get("permissionDecision"), decision
                    )
                    stderr.seek(0)
                    stderr.truncate()
                    with mock.patch.object(unverified, "detect", side_effect=RuntimeError("boom")):
                        self.assertEqual(self._dispatch(command), expected)
                    if debug == "1":
                        lines = [ln for ln in stderr.getvalue().splitlines() if ln.strip()]
                        self.assertEqual(len(lines), 1, lines)
                        self.assertEqual(json.loads(lines[0]).get("unverified"), [])


class TestFallbacks(_Base):
    def test_without_session_id_notifies_every_time(self):
        for session in (None, "", 123):
            with self.subTest(session=session):
                for _ in range(2):
                    self.assert_notice(
                        self._dispatch("wrangler deploy", session=session), ["wrangler"]
                    )

    def test_unusable_state_dir_notifies_every_time(self):
        with mock.patch.object(cache, "state_dir", return_value=None):
            for _ in range(2):
                self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])

    def test_record_that_cannot_be_created_notifies_every_time(self):
        missing = self.tmp / "missing-dir" / "flag"
        with mock.patch.object(unverified, "_flag_path", return_value=missing):
            for _ in range(2):
                self.assert_notice(self._dispatch("wrangler deploy"), ["wrangler"])

    def test_notice_does_not_depend_on_the_mode(self):
        """検証モード (env の `CLOUD_ACCOUNT_GUARD_MODE` / accounts.local.json の `"$mode"`) によらず知らせる。

        `off` は検証を行わないが、知らせは出す。deny 相当を警告で伝える `warn` では、警告の後ろに足す。
        """
        command = "wrangler deploy && gh pr create"
        accounts = self.project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        notice = unverified.notice(["wrangler"])
        worlds = (
            ("env-off", {mode.ENV_VAR: mode.OFF}, None),
            ("env-warn", {mode.ENV_VAR: mode.WARN}, None),
            ("file-off", {}, mode.OFF),
            ("file-warn", {}, mode.WARN),
        )
        for label, env, file_mode in worlds:
            with self.subTest(mode=label), mock.patch.dict(os.environ, env):
                if file_mode is None:
                    accounts.unlink(missing_ok=True)
                else:
                    self._write_accounts({"$mode": file_mode})
                expected = self._verdict_without_notice(command)
                context = self.assert_notice(
                    self._dispatch(command, session=f"mode-{label}"), ["wrangler"]
                )
                if label.endswith("-off"):
                    # off が効いている (検証を行わず allow) ことを確かめてから、知らせだけが出ることを見る。
                    self.assertIsNone(expected)
                    self.assertEqual(context, notice)
                else:
                    self.assertIsInstance(expected, dict)
                    self.assertIn("hookSpecificOutput", expected)
                    original = expected["hookSpecificOutput"]
                    self.assertNotIn("permissionDecision", original)
                    self.assertIn("additionalContext", original)
                    self.assertTrue(original["additionalContext"].startswith(mode.WARN_HEADER))
                    self.assertEqual(context, original["additionalContext"] + "\n\n" + notice)


class TestDebugTrace(_Base):
    def test_trace_records_the_names_and_the_notice_is_still_added(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"CLOUD_ACCOUNT_GUARD_DEBUG": "1"}), \
                contextlib.redirect_stderr(stderr):
            result = self._dispatch("npx wrangler deploy")
        self.assert_notice(result, ["wrangler"])
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        trace = json.loads(lines[0])
        self.assertEqual(trace.get("unverified"), ["wrangler"])


class TestMainEntry(unittest.TestCase):
    """`__main__.py` が hook input の session_id を渡すこと (実プロセス)。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.tmp / "cache").mkdir()
        (self.tmp / "home").mkdir()
        self.env = _testutil.sanitized_env(
            {
                **os.environ,
                **_testutil.cli_config_env(self.tmp / "cliconfig", self.tmp / "home"),
                "TMPDIR": str(self.tmp / "cache"),
                "CLAUDE_PROJECT_DIR": str(self.project),
            }
        )

    def _run(self, session_id: str, agent_id: str | None = None) -> subprocess.CompletedProcess:
        payload = {
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npx wrangler deploy"},
            "cwd": str(self.project),
        }
        if agent_id is not None:
            # サブエージェントの中で発火した hook input の形 (公式の hooks reference の共通フィールド)。
            payload["agent_id"] = agent_id
            payload["agent_type"] = "Explore"
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def test_notice_once_per_session_end_to_end(self):
        first = self._run("sess-e2e")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.strip(), "1 回目に知らせが出ていない")
        out = json.loads(first.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out)
        self.assertIn("wrangler", out.get("additionalContext", ""))
        second = self._run("sess-e2e")
        self.assertEqual((second.returncode, second.stdout), (0, ""), second.stderr)
        other = self._run("sess-other")
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertTrue(other.stdout.strip(), "別のセッションで知らせが出ていない")

    def _assert_notified(self, proc: subprocess.CompletedProcess, why: str) -> None:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), why)
        out = json.loads(proc.stdout)
        self.assertIn("hookSpecificOutput", out)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn(_MARK, out["hookSpecificOutput"].get("additionalContext", ""))

    def test_main_is_notified_after_a_subagent_was(self):
        """先にサブエージェント (hook input に agent_id がある) が使っても、メインには出る。

        サブエージェントは同じ session_id を持ち、知らせはツールを呼んだエージェントにだけ届く。
        同じ型 (agent_type) のサブエージェントどうしも agent_id で分ける。同じエージェントの 2 回目は出ない。
        """
        self._assert_notified(self._run("sess-sub", "agent-a1"), "サブエージェントの 1 回目に出ていない")
        self._assert_notified(self._run("sess-sub"), "サブエージェントの記録で、メインに出なかった")
        self._assert_notified(
            self._run("sess-sub", "agent-b2"), "同じ型の別のサブエージェントに出なかった"
        )
        for agent_id in (None, "agent-a1", "agent-b2"):
            with self.subTest(agent_id=agent_id):
                again = self._run("sess-sub", agent_id)
                self.assertEqual((again.returncode, again.stdout), (0, ""), again.stderr)


if __name__ == "__main__":
    unittest.main()
