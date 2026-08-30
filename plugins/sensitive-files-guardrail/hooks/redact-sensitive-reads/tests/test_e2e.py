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

    def test_read_many_key_dotenv_keeps_closing_tag_and_note(self):
        """0.26.0: 32KB 未満だが鍵数が多い ``.env`` で ``</DATA>`` と末尾 note が
        生き残ること。

        ``test_read_large_file_keyonly`` は 32KB 超 (``redact_large_file`` /
        keyonly scan) 経路の回帰。こちらは **inline (``format_dotenv``) 経路**
        の回帰で、90 key 程度の ``.env`` で ``</DATA>`` 閉じタグと「実値は
        無い」の末尾 note が key 行の途中で失われていた実測 (0.26.0 以前) の
        直接の再現先。
        """
        p = Path(self.tmp) / ".env"
        p.write_text(
            "".join(
                f"KEY_{i:03d}=value_that_is_reasonably_long_{i:03d}\n"
                for i in range(90)
            )
        )
        self.assertLess(p.stat().st_size, 32 * 1024)
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
        self.assertLessEqual(len(reason.encode("utf-8")), 3 * 1024)
        self.assertIn("</DATA>", reason)
        self.assertIn("real values are not in context", reason)
        self.assertRegex(reason, r"\.\.\. \(\d+ more lines\)")
        # 値は漏れない
        self.assertNotIn("value_that_is_reasonably_long_0\n", reason)

    def test_bash_cat_many_key_env_keeps_closing_tag_note_and_exclude_hint(self):
        """0.26.0: Bash ``cat`` 経路でも ``</DATA>`` / note / 除外案内が両立する。

        除外案内自体は 0.23.0 で ``_join_with_exclude_hint`` が保護済みだが、
        埋め込まれた ``<DATA>`` ブロック自身の閉じタグと末尾 note は保護
        対象外で、盲目 byte cut で key 行の途中から失われていた (90 key の
        ``.env`` で実測)。
        """
        p = Path(self.tmp) / ".env"
        p.write_text(
            "".join(
                f"KEY_{i:03d}=value_that_is_reasonably_long_{i:03d}\n"
                for i in range(90)
            )
        )
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode("utf-8")), 3 * 1024)
        self.assertIn("</DATA>", reason)
        self.assertIn("real values are not in context", reason)
        self.assertIn("patterns.local.txt", reason)
        self.assertIn("保護そのもの", reason)
        self.assertNotIn("value_that_is_reasonably_long_0\n", reason)

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


# ---- 両 hook の推奨コマンドが Bash hook を通過する (0.19.0) ------------

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
    固定する (0.19.0)。

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


class TestE2EKeyonlyKeepsKeyNames(unittest.TestCase):
    """0.26.0 隔離内レビュー P1-1 の 3 経路回帰 (Read / Bash / Edit)。

    >32KB のファイルは ``redaction.engine.redact_large_file`` →
    ``format_keyonly`` に降りる。0.26.0 で予算内折り畳みを Read / Bash に
    配線した際、``format_keyonly`` が全鍵名を **1 行**に並べていたため
    「行ごと落ちて鍵名 0 個」になっていた (0.25.0 の盲目 cut では数十個
    見えていた = 退行)。3 経路とも鍵名が残ることを固定する。

    値は 1 文字も出さない (これが崩れたら redaction そのものの破綻)。
    """

    _KEY_COUNT = 200
    _VALUE = "sk_live_" + "v" * 297

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        pad = "X" * 26
        self.keys = [
            f"SERVICE_API_KEY_{pad}_{i:03d}" for i in range(self._KEY_COUNT)
        ]
        self.path = Path(self.tmp) / ".env.longkeys"
        self.path.write_text(
            "".join(f"{k}={self._VALUE}\n" for k in self.keys)
        )
        # keys-only scan (>32KB) 経路に入ることを前提にした fixture
        self.assertGreater(self.path.stat().st_size, 32 * 1024)

    def _assert_keys_survive(self, reason: str, minimum: int):
        self.assertLessEqual(len(reason.encode("utf-8")), 3 * 1024)
        self.assertIn("keys-only scan", reason)
        self.assertNotIn("sk_live_", reason)
        visible = sum(1 for k in self.keys if k in reason)
        self.assertGreaterEqual(
            visible,
            minimum,
            f"鍵名が {visible} 個しか残っていない"
            f" (reason {len(reason.encode('utf-8'))} byte)",
        )

    def _reason(self, envelope: dict, argv: list[str]) -> str:
        result = _run_main(envelope, argv)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_read_keeps_key_names(self):
        reason = self._reason({
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.path)},
            "cwd": self.tmp,
            "permission_mode": "bypassPermissions",
        }, ["--tool", "read"])
        self._assert_keys_survive(reason, 20)

    def test_bash_cat_keeps_key_names(self):
        reason = self._reason({
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env.longkeys"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }, ["--tool", "bash"])
        self._assert_keys_survive(reason, 10)
        # 除外案内は従来どおり全文残る (0.23.0 の保護を壊していない)
        self.assertIn("patterns.local.txt", reason)

    def test_edit_overwrite_keeps_key_names(self):
        reason = self._reason({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(self.path),
                "old_string": "x=1",
                "new_string": "y=2",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }, ["--tool", "edit"])
        self._assert_keys_survive(reason, 10)


class TestAsciiStdoutEncoding(unittest.TestCase):
    """外部レビュー R2 P2-A 回帰: stdout が非 UTF-8 でも判定を必ず出す。

    ``_emit`` は ``sys.stdout.write`` を使っていたため、``PYTHONIOENCODING=ascii``
    のように stdout が非 UTF-8 で hook が起動されると、日本語を含む deny reason
    (``M.bash_deny`` など、この hook の主要文面はほぼ全て日本語) で
    ``UnicodeEncodeError`` が送出された。``_emit`` の ``except`` は
    ``(BrokenPipeError, OSError)`` しか捕まえず ``UnicodeEncodeError`` は
    ``ValueError`` 系なので素通りし、**exit 1 / stdout 0 byte** になる (実測)。
    PreToolUse hook がこうなると判定が届かず **tool 呼出がそのまま通る**
    (fail-open) — deny したかった Bash / Read / Edit が実行されてしまう。

    このクラスが唯一の防波堤である点に注意: 他の E2E は ``sys.stdout`` を
    ``StringIO`` に差し替えるため ``encoding`` も ``buffer`` も持たず、この失敗
    モードを**構造的に再現できない** (修正後はフォールバック経路を通る)。
    子プロセスは ``_testutil`` の bootstrap を継承しないので、``HOME`` と
    ``SFG_LOG_PATH`` を明示して実 HOME / 実ログを汚さない。
    """

    ASCII_ENV = {"PYTHONIOENCODING": "ascii", "LC_ALL": "C"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (Path(self.tmp) / ".env").write_text("SECRET=1\n")
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()

    def _run(self, tool: str, envelope: dict, env_extra: dict):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["SFG_LOG_PATH"] = str(self.home / "redact-hook.log")
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(_ENTRY_PATH), "--tool", tool],
            input=json.dumps(envelope).encode("utf-8"),
            capture_output=True,
            env=env,
        )

    def _bash_envelope(self):
        return {
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }

    def test_bash_deny_survives_ascii_stdout(self):
        proc = self._run("bash", self._bash_envelope(), self.ASCII_ENV)
        self.assertEqual(
            proc.returncode, 0, msg=proc.stderr.decode("utf-8", "replace")
        )
        self.assertTrue(proc.stdout, msg="stdout が空 = deny が届かず fail-open")
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        # 日本語の reason が欠落せずそのまま届いていること
        self.assertIn(
            "機密", payload["hookSpecificOutput"]["permissionDecisionReason"]
        )

    def test_ascii_and_utf8_stdout_are_byte_identical(self):
        """encoding が変わっても出力が同一 = 情報が落ちていないこと。"""
        utf8 = self._run("bash", self._bash_envelope(), {"PYTHONIOENCODING": "utf-8"})
        ascii_ = self._run("bash", self._bash_envelope(), self.ASCII_ENV)
        self.assertEqual(utf8.returncode, 0)
        self.assertEqual(utf8.stdout, ascii_.stdout)

    def test_edit_deny_survives_ascii_stdout(self):
        """Bash 以外の handler も同じ経路 (``_emit``) で落ちていたこと。

        Edit / Write の deny reason は冒頭の note と 2 本の suggestion が日本語
        なので、Bash と同様に修正前は exit 1 になる (実測)。

        **開示**: Read の deny reason は現状ほぼ英語 (``<DATA>`` サニタイズ結果 +
        英文 note) のため、同じ環境でも修正前から exit 0 で通ってしまい回帰
        テストにならない。したがって非 Bash 経路の代表として Edit を使う。
        """
        envelope = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(Path(self.tmp) / ".env"),
                "old_string": "SECRET=1",
                "new_string": "SECRET=2",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        proc = self._run("edit", envelope, self.ASCII_ENV)
        self.assertEqual(
            proc.returncode, 0, msg=proc.stderr.decode("utf-8", "replace")
        )
        self.assertTrue(proc.stdout, msg="stdout が空 = deny が届かず fail-open")
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn(
            "機密", payload["hookSpecificOutput"]["permissionDecisionReason"]
        )


if __name__ == "__main__":
    unittest.main()
