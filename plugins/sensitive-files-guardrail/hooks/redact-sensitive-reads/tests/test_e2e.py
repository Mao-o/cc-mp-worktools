"""__main__.py の stdin→stdout 経由 E2E テスト。

subprocess ではなく main() を直接呼び、stdin/stdout を差し替える。
`__main__` は unittest runner 自身と名前衝突するため importlib でファイル直読み。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"
# Stop hook (check-sensitive-files) のエントリ。両 hook の reason が推奨する
# コマンドの整合を取るテストで実行する。
_STOP_ENTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "check-sensitive-files"
    / "__main__.py"
)
_spec = importlib.util.spec_from_file_location("redact_entry", _ENTRY_PATH)
assert _spec is not None and _spec.loader is not None
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


def _run_main(envelope: dict, argv: list[str]) -> dict:
    """main() を in-process で呼び、stdout JSON を dict にして返す。"""
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    try:
        sys.stdin = io.StringIO(json.dumps(envelope))
        sys.stdout = io.StringIO()
        rc = entry.main(argv)
        out = sys.stdout.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
    assert rc == 0, f"main() returned {rc}"
    if not out.strip():
        return {}
    return json.loads(out)


class TestE2EReadHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _env_path(self):
        p = Path(self.tmp) / ".env"
        p.write_text(
            "DATABASE_URL=postgresql://u:p@h/d\n"
            "JWT_SECRET=eyJ...\n"
            "DEBUG=true\n"
        )
        return p

    def test_read_dotenv_deny(self):
        self._env_path()
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "bypassPermissions",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("format: dotenv", reason)
        self.assertIn("DATABASE_URL", reason)
        # 値は出ない
        self.assertNotIn("postgresql", reason)

    def test_read_non_sensitive_allow(self):
        p = Path(self.tmp) / "README.md"
        p.write_text("# hi")
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(result, {})

    def test_read_example_excluded(self):
        p = Path(self.tmp) / ".env.example"
        p.write_text("FOO=bar\n")
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env.example"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(result, {})

    def test_read_symlink_ask_non_bypass(self):
        target = Path(self.tmp) / "real.env"
        target.write_text("FOO=bar\n")
        link = Path(self.tmp) / ".env"
        os.symlink(target, link)
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "ask"
        )

    def test_read_symlink_deny_under_bypass(self):
        target = Path(self.tmp) / "real.env"
        target.write_text("FOO=bar\n")
        link = Path(self.tmp) / ".env"
        os.symlink(target, link)
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "bypassPermissions",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_read_fifo_ask_or_deny(self):
        fifo = Path(self.tmp) / ".env"
        os.mkfifo(fifo)
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "ask"
        )

    def test_read_missing_file_allow(self):
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(result, {})

    def test_read_large_file_keyonly(self):
        p = Path(self.tmp) / ".env"
        lines = [f"KEY_{i}=value_{i}\n" for i in range(2000)]
        p.write_text("".join(lines))
        # size check
        self.assertGreater(p.stat().st_size, 32 * 1024)
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": ".env"},
            "cwd": self.tmp,
            "permission_mode": "bypassPermissions",
        }
        result = _run_main(envelope, ["--tool", "read"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("keys-only scan", reason)
        # 値は漏れない
        self.assertNotIn("value_0", reason)

    def test_bash_cat_env_denies(self):
        """Bash handler は ``cat .env`` を deny 固定 (0.2.0 で ask_or_deny → deny に変更)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_bash_cat_env_bypass_denies(self):
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "bypassPermissions",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_bash_echo_allows(self):
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_cat_env_denies(self):
        """auto モードでも機密確定 match は deny (0.3.2)。"""
        self._env_path()
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_bash_auto_glob_dotenv_star_denies(self):
        """auto モードでも glob 候補列挙で deny (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env*", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_bash_auto_star_log_allows(self):
        """`*.log` は既定 rules と交差しないため auto/default 共に allow (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat *.log", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_opaque_wrapper_allows(self):
        """auto モードでは opaque wrapper (`bash -c`) を allow に倒す (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "bash -c 'date'", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_env_prefix_dotenv_allows(self):
        """0.8.0: env-assignment prefix は opaque first token として ``ask_or_allow``。
        auto mode では allow (= 空 dict) に倒す。0.3.2〜0.7.x の prefix normalize
        撤廃。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "FOO=1 cat .env", "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_abs_env_basename_allows(self):
        """0.8.0: ``/usr/bin/env`` のような任意 path exec は opaque first token →
        ``ask_or_allow``。auto mode で allow に倒る。0.3.2〜0.7.x の透過剥がしは撤廃。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "/usr/bin/env FOO=1 cat .env",
                "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_abs_cat_basename_allows(self):
        """basename=cat は透過対象外 → opaque → auto で allow (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "/bin/cat .env", "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_input_redirect_allows(self):
        """0.7.0: ``<`` を含む command は hard-stop と同じ ``ask_or_allow``。

        autonomous mode (auto / bypassPermissions) では allow (= 空 dict) に倒す。
        target 抽出 + literal/glob 一致での deny 固定は 0.7.0 で撤廃。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat < .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_bash_auto_heredoc_allows(self):
        """heredoc は target 抽出されず opaque → auto で allow (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "cat <<EOF\nhello\nEOF", "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(result, {})

    def test_edit_dotenv_denies(self):
        """Edit handler は既存 .env を deny 固定 (0.2.0)。"""
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        envelope = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(Path(self.tmp) / ".env"),
                "old_string": "a",
                "new_string": "b",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "edit"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_write_new_dotenv_denies(self):
        """Write handler は新規 .env を deny 固定 (0.2.0)。"""
        envelope = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(Path(self.tmp) / ".env"),
                "content": "FOO=bar\n",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "write"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_write_template_allows(self):
        envelope = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(Path(self.tmp) / ".env.example"),
                "content": "FOO=placeholder\n",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "write"])
        self.assertEqual(result, {})

    def test_invalid_stdin_json(self):
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        try:
            sys.stdin = io.StringIO("{not json")
            sys.stdout = io.StringIO()
            rc = entry.main(["--tool", "read"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )


# ---- 両 hook の推奨コマンドが Bash hook を通過する (0.19.0, snw.3) ------------

_REMEDY_CMD_WORDS = frozenset({"git", "chmod", "chown", "chgrp", "touch"})
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _remedy_commands(text: str) -> list[str]:
    """reason 文中の backtick スニペットのうちコマンド形 (``git`` / ``chmod`` /
    ``chown`` / ``chgrp`` / ``touch`` で始まるもの) を抽出する。Stop hook の
    ``<path>`` プレースホルダは ``.env`` に置換する。"""
    cmds: list[str] = []
    for snippet in _BACKTICK_RE.findall(text):
        words = snippet.split()
        if words and words[0] in _REMEDY_CMD_WORDS:
            cmds.append(snippet.replace("<path>", ".env"))
    return cmds


def _load_stop_entry():
    spec = importlib.util.spec_from_file_location(
        "check_entry_for_e2e", _STOP_ENTRY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class TestE2ERecommendedRemediesPassBashHook(unittest.TestCase):
    """両 hook の reason が推奨する次善策コマンドが Bash hook を通過することを
    固定する (0.19.0, bd_092a232e-snw.3)。

    0.18.0 までは Stop hook と ``_bash_deny_history`` が ``git rm --cached <path>``
    を案内しながら Bash hook 自身がそれを deny していた (自己矛盾)。reason から
    backtick コマンドを機械抽出して Bash hook に通すことで、文面と allow 境界の
    乖離を再発時に検知する。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        home = Path(self.tmp) / "home"
        home.mkdir()
        # patterns.local.txt / stop-ack state を実 HOME から隔離
        self._env = mock.patch.dict(
            os.environ,
            {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")},
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        (self.repo / ".env").write_text("KEY=value\n")

    def _bash(self, cmd: str, mode: str = "default") -> dict:
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "test"},
            "cwd": str(self.repo),
            "permission_mode": mode,
        }
        return _run_main(envelope, ["--tool", "bash"])

    def _assert_passes(self, cmd: str, origin: str) -> None:
        for mode in ("default", "auto"):
            result = self._bash(cmd, mode)
            self.assertEqual(
                result, {},
                msg=f"{origin}: {cmd!r} [{mode}] -> {result}",
            )

    def test_documented_remedies_pass(self):
        # README / docs が次善策として挙げる形
        for cmd in (
            "git rm --cached .env",
            "chmod 600 .env",
            "touch .env",
            "chown user .env",
        ):
            self._assert_passes(cmd, origin="docs")

    def test_bash_history_deny_reasons_recommend_only_passing_commands(self):
        for cmd in (
            "git show HEAD:.env",
            "git add .env",
            "git rm .env",
            "git mv .env old.env",
            "git restore .env",
        ):
            result = self._bash(cmd)
            self.assertEqual(
                result["hookSpecificOutput"]["permissionDecision"], "deny",
                msg=cmd,
            )
            reason = result["hookSpecificOutput"]["permissionDecisionReason"]
            recommended = _remedy_commands(reason)
            self.assertIn(
                "git rm --cached .env", recommended,
                msg=f"{cmd!r}: no untrack remedy in reason:\n{reason}",
            )
            for rec in recommended:
                self._assert_passes(rec, origin=f"bash reason of {cmd!r}")

    def test_stop_hook_block_reason_recommends_only_passing_commands(self):
        _git(["init", "--initial-branch=main"], str(self.repo))
        _git(["config", "user.name", "test"], str(self.repo))
        _git(["config", "user.email", "test@example.com"], str(self.repo))
        _git(["config", "commit.gpgsign", "false"], str(self.repo))
        _git(["add", ".env"], str(self.repo))
        _git(["commit", "-m", "add env"], str(self.repo))

        stop = _load_stop_entry()
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(json.dumps(
                {"cwd": str(self.repo), "session_id": "e2e-session"}
            ))
            sys.stdout = io.StringIO()
            rc = stop.main()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        reason = payload["reason"]

        recommended = _remedy_commands(reason)
        self.assertIn("git rm --cached .env", recommended)
        for rec in recommended:
            self._assert_passes(rec, origin="stop reason")
        # 恒久除外レシピは read 側 hint と同じヘッダー
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        self.assertIn("!.env", reason)
        self.assertNotIn(str(self.repo), reason)


if __name__ == "__main__":
    unittest.main()
