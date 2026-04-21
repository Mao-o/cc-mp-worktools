"""__main__.py の stdin→stdout 経由 E2E テスト。

subprocess ではなく main() を直接呼び、stdin/stdout を差し替える。
`__main__` は unittest runner 自身と名前衝突するため importlib でファイル直読み。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"
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

    def test_bash_auto_env_prefix_dotenv_denies(self):
        """env prefix を剥がした後の確定 match は auto でも deny (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "FOO=1 cat .env", "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

    def test_bash_auto_abs_env_basename_denies(self):
        """/usr/bin/env basename=env で透過 → cat .env で deny (0.3.2)。"""
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
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

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

    def test_bash_auto_input_redirect_denies(self):
        """`< target` の target が機密 → auto でも deny (0.3.2)。"""
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat < .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

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

    def test_multiedit_dotenv_denies(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        envelope = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(Path(self.tmp) / ".env"),
                "edits": [{"old_string": "a", "new_string": "b"}],
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "multiedit"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny",
        )

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


if __name__ == "__main__":
    unittest.main()
