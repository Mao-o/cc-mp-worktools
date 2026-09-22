"""__main__.py の stdin→stdout 経由 E2E テスト。

subprocess ではなく main() を直接呼び、stdin/stdout を差し替える。
`__main__` は unittest runner 自身と名前衝突するため importlib でファイル直読み。
"""
from __future__ import annotations

import contextlib
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


def _decision(result: dict) -> str | None:
    """``permissionDecision`` を取り出す (allow は ``None``)。

    ``result["hookSpecificOutput"]["permissionDecision"]`` を直接書くと、
    判定が allow (= ``{}``) に退行したとき ``KeyError`` になり、テストが
    **assertion failure ではなく error** で落ちる。error は「テストが走って
    いない」と見分けが付かず、mutation で床テストの有効性を測るときに
    空振りと区別できない (``docs/MAINTAINING.md`` のテスト規律)。
    """
    return (result.get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(result: dict) -> str:
    return (result.get("hookSpecificOutput") or {}).get(
        "permissionDecisionReason"
    ) or ""


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

    def _assert_lenient_allow(self, result: dict) -> None:
        """開示 note 付き lenient allow (autonomous mode) の実配線 assert。

        0.33.0: ``permissionDecision`` は出さないまま ``additionalContext`` に
        固定 1 文が載る。**判定は素の allow と同一**なので、判定側は
        ``is_allow`` / ``decision_of`` で、開示側は ``additionalContext`` で見る。
        command / path / 値が混ざっていないことも併せて固定する。

        note が載るのは「command に機密パターンらしい token を含む」ときだけ
        (``handlers.bash_handler._has_sensitive_looking_token``)。含まない形は
        ``_assert_lenient_allow_without_note`` を使う — verdict はどちらも同じ
        allow で、違うのは note の有無だけ。
        """
        from core import output

        self.assertTrue(output.is_allow(result), msg=repr(result))
        self.assertIsNone(output.decision_of(result), msg=repr(result))
        hook = result["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hook)
        self.assertEqual(
            hook["additionalContext"], output.LENIENT_ALLOW_CONTEXT,
        )
        self.assertNotIn(".env", hook["additionalContext"])

    def _assert_lenient_allow_without_note(self, result: dict) -> None:
        """note 無しの lenient allow の実配線 assert (0.33.0)。

        機密パターンらしい token を含まない command は、lenient allow に倒れても
        開示 note を載せない (素の allow = ``{}`` に戻る)。lenient allow は実測で
        全 Bash 呼出の 4 割強なので、全件に載せると note 自体がコンテキスト
        ノイズになるため絞っている。**verdict は note 付きの形と同じ allow**。
        """
        from core import output

        self.assertTrue(output.is_allow(result), msg=repr(result))
        self.assertIsNone(output.decision_of(result), msg=repr(result))
        self.assertEqual(result, {}, msg=repr(result))

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

    def test_read_suffix_dotenv_deny(self):
        """0.34.0: ``*.env`` (``production.env`` / ``local.env``) も既定で deny。

        0.33.x までは ``.env`` / ``.env.*`` / ``.envrc`` / ``*.envrc`` の 4 行
        しか無く、``*.envrc`` があるのに ``*.env`` が無いという非対称だった
        (``redaction/engine._detect_format`` は ``foo.env`` を dotenv として
        扱っており実装とも食い違っていた)。
        """
        for name in ("production.env", "local.env"):
            with self.subTest(name=name):
                p = Path(self.tmp) / name
                p.write_text("DATABASE_URL=postgresql://u:p@h/d\nDEBUG=true\n")
                envelope = {
                    "tool_name": "Read",
                    "tool_input": {"file_path": name},
                    "cwd": self.tmp,
                    "permission_mode": "default",
                }
                result = _run_main(envelope, ["--tool", "read"])
                self.assertEqual(_decision(result), "deny")
                reason = _reason(result)
                # ``_detect_format`` が dotenv として扱うので minimal info も dotenv 形
                self.assertIn("format: dotenv", reason)
                self.assertIn("DATABASE_URL", reason)
                self.assertNotIn("postgresql", reason)

    def test_read_suffix_dotenv_template_still_allowed(self):
        """``*.env`` を足しても既定の除外 (``!*.example`` 等) は先に勝つ。"""
        for name in ("foo.env.example", "foo.env.sample", "config.env.template"):
            with self.subTest(name=name):
                p = Path(self.tmp) / name
                p.write_text("FOO=bar\n")
                envelope = {
                    "tool_name": "Read",
                    "tool_input": {"file_path": name},
                    "cwd": self.tmp,
                    "permission_mode": "default",
                }
                self.assertEqual(_run_main(envelope, ["--tool", "read"]), {})

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

    def test_read_directory_ask_names_it_a_directory(self):
        """内部バックログ: ``.env`` がディレクトリの構成 (``python -m venv .env``
        等) は現実にある。従来は special (FIFO/socket/device) と誤表示して
        いた。verdict (ask/deny) 自体は special と同じ経路のまま変わらない。
        """
        envdir = Path(self.tmp) / ".env"
        envdir.mkdir()
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
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("ディレクトリ", reason)
        self.assertNotIn("FIFO", reason)

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
        """`*.log` は既定 rules と交差しないため auto/default 共に allow (0.3.2)。

        0.33.0: ``*.log`` は機密パターンらしい token ではないので開示 note も
        付かない (``cat *.key`` との対は ``test_bash_handler`` 側)。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat *.log", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self._assert_lenient_allow_without_note(result)

    def test_bash_auto_opaque_wrapper_allows(self):
        """auto モードでは opaque wrapper (`bash -c`) を allow に倒す (0.3.2)。

        0.33.0: payload に機密らしい token が無い (``date``) ので開示 note は
        付かない。``bash -c 'cat .env'`` との対は ``test_bash_handler`` 側。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": "bash -c 'date'", "description": "test"},
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self._assert_lenient_allow_without_note(result)

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
        self._assert_lenient_allow(result)

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
        self._assert_lenient_allow(result)

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
        self._assert_lenient_allow(result)

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
        self._assert_lenient_allow(result)

    def test_bash_auto_heredoc_allows(self):
        """heredoc は target 抽出されず opaque → auto で allow (0.3.2)。

        0.33.0: 本文に機密らしい token が無いので開示 note は付かない。
        """
        envelope = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "cat <<EOF\nhello\nEOF", "description": "test",
            },
            "cwd": self.tmp,
            "permission_mode": "auto",
        }
        result = _run_main(envelope, ["--tool", "bash"])
        self._assert_lenient_allow_without_note(result)

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

    def _run_raw_stdin(self, raw: str, argv: list[str] | None = None):
        """stdin に生文字列を流して ``main`` を走らせ ``(rc, stdout)`` を返す。"""
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        try:
            sys.stdin = io.StringIO(raw)
            sys.stdout = io.StringIO()
            rc = entry.main(argv or ["--tool", "read"])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
        return rc, out

    def test_invalid_stdin_json(self):
        rc, out = self._run_raw_stdin("{not json")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_empty_stdin_denies_for_every_tool(self):
        """0.32.0 (内部バックログ): 0 byte stdin は無音 allow ではなく deny。

        0.31.0 までは ``_read_envelope`` がここだけ ``{}`` を返し、各 handler が
        必須フィールド欠如で ``make_allow()`` に落ちていた (stderr もログも
        出ない **唯一の fail-open 分岐**)。全 tool の dispatch で deny になる
        ことを固定する。"""
        for tool in ("read", "bash", "edit", "write"):
            with self.subTest(tool=tool):
                rc, out = self._run_raw_stdin("", ["--tool", tool])
                self.assertEqual(rc, 0)
                # 退行 (allow に戻る) のとき KeyError で ERROR 扱いにならない
                # よう ``get`` で辿る — errors と failures を取り違えない
                hook = json.loads(out).get("hookSpecificOutput", {})
                self.assertEqual(
                    hook.get("permissionDecision"), "deny", msg=f"{tool}: {out!r}"
                )
                # 空でない reason が返る (無音にしない)
                self.assertTrue(hook.get("permissionDecisionReason"))

    def test_empty_stdin_logs_its_own_category(self):
        """``stdin_parse_failed`` と切り分けられるよう専用 category を出す。

        ハーネスが正常系で 0 byte stdin を送る (= 全 deny になる) 事態が
        起きたときにログから即座に判別できる必要がある。"""
        with mock.patch.object(entry.L, "log_error") as logged:
            self._run_raw_stdin("")
        self.assertEqual([c.args[0] for c in logged.call_args_list], ["stdin_empty"])
        with mock.patch.object(entry.L, "log_error") as logged:
            self._run_raw_stdin("{not json")
        self.assertEqual(
            [c.args[0] for c in logged.call_args_list], ["stdin_parse_failed"]
        )

    def test_blank_stdin_still_parse_failed(self):
        """空白のみ (``"   \\n"``) は 0 byte ではないので従来どおり parse 失敗。"""
        with mock.patch.object(entry.L, "log_error") as logged:
            rc, out = self._run_raw_stdin("   \n")
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(
            [c.args[0] for c in logged.call_args_list], ["stdin_parse_failed"]
        )


# ---- 両 hook の推奨コマンドが Bash hook を通過する (0.19.0) ------------

_REMEDY_CMD_WORDS = frozenset({
    "git", "chmod", "chown", "chgrp", "touch",
    # 0.33.0 (内部バックログ): 案内文が **シェルリダイレクトで追記する形**
    # (``echo '.env' >> .gitignore``) を載せると、residual metachar 判定で
    # ``ask_or_allow`` に落ち「block → 承認ダイアログ」の 2 段の摩擦になる。
    # これらを抽出語に含めることで、そういう形が reason に紛れ込んだ瞬間に
    # ``_assert_passes`` (= 全 mode で素通りすること) が落ちて気付ける。
    # 対処は Edit / Write ツールでの追記を案内すること (判定表は変えない)。
    "echo", "printf", "cat", "tee", "sed",
})
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
    """Stop hook のエントリを in-process で読み込む。

    ``check-sensitive-files/__main__.py`` は **本番経路として** 自ディレクトリを
    ``sys.path`` の先頭に挿入する (別プロセスで単独起動されるため、それ自体は
    正しい)。ただし in-process で ``exec_module`` するとその挿入が
    **このテストプロセス全体に残る** — 両 hook はどちらも ``tests`` パッケージを
    持つので、以降 ``tests.*`` が Stop 側に解決される。

    実害 (0.31.0 で修正、内部バックログ): ``tests/test_logging.py`` の並行
    ローテーションテストは ``multiprocessing`` の spawn 子プロセスを使い、子は
    target 関数を「モジュール名 + 関数名」で import し直す。``tests.test_logging``
    が Stop 側に解決されて ``ModuleNotFoundError`` になり、テストが flaky に見えて
    いた (``python3 -m unittest tests.test_e2e tests.test_logging`` で 100% 再現。
    ``unittest discover`` はモジュールを ``tests.`` 無しの top-level 名で import
    するため再現しない = 実行形態依存)。

    hook 本体の import 経路は変えず (本番挙動なので)、**呼出側で ``sys.path`` を
    元に戻す**。``exec_module`` 中に解決し終えた ``checker`` / ``stop_ack`` は
    ``sys.modules`` に残るので、戻した後も ``mod.main()`` は動く。
    """
    spec = importlib.util.spec_from_file_location(
        "check_entry_for_e2e", _STOP_ENTRY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved_path = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
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

        # 0.33.0 (内部バックログ): 追記操作は Edit ツールで行うよう案内する。
        # シェルリダイレクト形 (`>>`) は residual metachar 判定で ask に落ちる
        # ため、block 直後に承認ダイアログを挟む 2 段の摩擦になっていた。
        # 判定表は変えず、案内する実行手段を変えて解消した。
        self.assertIn("`.gitignore` に Edit で追記", reason)
        self.assertIn("patterns.local.txt` に次を Edit で追記", reason)
        # 案内文にシェルリダイレクトによる追記形を混ぜない
        for redirect_form in (">>", "> .gitignore", ">.gitignore"):
            self.assertNotIn(redirect_form, reason)


class TestE2ELogLevelSuppressesAllowPathInfo(unittest.TestCase):
    """0.32.0 (内部バックログ): ``SFG_LOG_LEVEL=WARNING`` で allow 経路の INFO が
    落ち、deny / ask 経路の診断は残ること (``main`` 経由の実配線)。

    ``redact-hook.log`` は Bash 呼出のたびに allow 経路でも INFO を書いており
    (実測 7.3MB / 12 万行)、0.27.0 のローテーションでは量が減らなかった。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.log = Path(self.tmp) / "redact-hook.log"
        p = mock.patch.object(entry.L, "LOG_PATH", self.log)
        p.start()
        self.addCleanup(p.stop)
        (Path(self.tmp) / ".env").write_text("SECRET_TOKEN=abcdef123456\n")

    def _run_bash(self, command: str, level: int) -> tuple[dict, str]:
        envelope = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        self.log.unlink(missing_ok=True)
        with mock.patch.object(entry.L, "LOG_LEVEL", level):
            result = _run_main(envelope, ["--tool", "bash"])
        return result, (self.log.read_text() if self.log.exists() else "")

    def test_allow_only_command_writes_nothing_at_warning(self):
        result, log = self._run_bash("ls -la", entry.L._LEVEL_WARNING)
        self.assertEqual(result, {})
        self.assertEqual(log, "", f"allow 経路の INFO が残っている: {log!r}")

    def test_allow_only_command_still_logs_at_default_level(self):
        """既定 (INFO) では従来どおり記録される = 既定の挙動は不変。"""
        result, log = self._run_bash("ls -la", entry.L._LEVEL_INFO)
        self.assertEqual(result, {})
        self.assertIn("bash_classify", log)

    def test_deny_command_keeps_diagnostics_at_warning(self):
        result, log = self._run_bash("cat .env", entry.L._LEVEL_WARNING)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("bash_classify", log)
        self.assertIn("match:cat", log)

    def test_ask_command_keeps_diagnostics_at_warning(self):
        result, log = self._run_bash("echo $HOME", entry.L._LEVEL_WARNING)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "ask"
        )
        self.assertIn("bash_classify", log)

    def test_later_deny_keeps_earlier_allow_diagnostics_at_warning(self):
        """同一コマンド内で後続 deny が先行 allow を上書きするケース。"""
        result, log = self._run_bash("ls -la && cat .env", entry.L._LEVEL_WARNING)
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("metadata_only_allow:ls", log)
        self.assertIn("match:cat", log)

    def test_repo_tier_record_survives_warning_level(self):
        """repo 同梱 tier の記録は leveling 対象外 (マージ前レビューの指摘)。

        repo 同梱 ``!`` 行で allow に倒れた呼出は最終判定が allow なので、
        既定の leveling では ``SFG_LOG_LEVEL=WARNING`` のときに
        「**repo の除外で保護が外れた**まさにその呼出」の記録だけが消えていた。
        この記録は README / docs が残存リスクの緩和策として公表しているもの
        なので、量を絞った利用者から静かに失われてはいけない。
        """
        tier = Path(self.tmp) / ".claude" / "sensitive-files-guardrail"
        tier.mkdir(parents=True)
        (tier / "patterns.txt").write_text("!.env\n")
        home = Path(self.tmp) / "home"  # user tier を実ホームから隔離する
        home.mkdir()
        with mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": self.tmp, "HOME": str(home)}
        ):
            result, log = self._run_bash("cat .env", entry.L._LEVEL_WARNING)
        self.assertEqual(result, {}, "repo tier の ! 行で allow に倒れる前提")
        self.assertIn("project_patterns_in_use", log)
        # 通常の INFO は従来どおり落ちる (leveling そのものを無効化していない)
        self.assertNotIn("bash_classify", log)

    def test_repo_tier_record_is_present_at_default_level_too(self):
        """既定 (INFO) でも当然残る = マークは「落とさない」方向にしか効かない。"""
        tier = Path(self.tmp) / ".claude" / "sensitive-files-guardrail"
        tier.mkdir(parents=True)
        (tier / "patterns.txt").write_text("!.env\n")
        home = Path(self.tmp) / "home"
        home.mkdir()
        with mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": self.tmp, "HOME": str(home)}
        ):
            _result, log = self._run_bash("cat .env", entry.L._LEVEL_INFO)
        self.assertIn("project_patterns_in_use", log)
        self.assertIn("bash_classify", log)

    def test_log_lines_carry_no_paths_or_values(self):
        """遅延化でログ規則 (path / 値 / basename を出さない) が崩れていない。"""
        _result, log = self._run_bash("cat .env", entry.L._LEVEL_INFO)
        self.assertNotIn(self.tmp, log)
        self.assertNotIn("abcdef123456", log)
        self.assertNotIn("SECRET_TOKEN", log)


class TestE2EDotenvInfoPathsKeepVerdictAndEnvelope(unittest.TestCase):
    """0.32.0 (内部バックログ): ``read_partial`` / ``search`` の折り畳み配線が
    **判定を変えていない**ことを 5 mode で固定する。

    reason builder は deny 確定後に文字列を組むだけなので判定に影響しない —
    ただし builder が例外を投げると ``__main__`` の catch-all が ``ask_or_deny``
    に倒し、**deny が ask に変わる**。実ファイル + 全 mode で通すことで、
    その経路が塞がっていることを確かめる。
    """

    _MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions")
    _KEY_COUNT = 300

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = Path(self.tmp) / ".env"
        self.path.write_text(
            "".join(
                f"KEY_{i:03d}=" + "v" * 40 + "\n" for i in range(self._KEY_COUNT)
            )
        )
        # inline 経路 (32KB 未満 = dotenv parse が走る) であること
        self.assertLess(self.path.stat().st_size, 32 * 1024)

    def _envelope(self, command: str, mode: str) -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": self.tmp,
            "permission_mode": mode,
        }

    def _assert_deny_and_intact(self, command: str) -> None:
        for mode in self._MODES:
            with self.subTest(mode=mode, command=command):
                result = _run_main(self._envelope(command, mode), ["--tool", "bash"])
                hook = result.get("hookSpecificOutput", {})
                self.assertEqual(hook.get("permissionDecision"), "deny", mode)
                reason = hook.get("permissionDecisionReason", "")
                self.assertLessEqual(len(reason.encode("utf-8")), 3 * 1024)
                self.assertNotIn("...[truncated]", reason)
                self.assertIn("</DATA>", reason)
                # 値は 1 文字も出さない
                self.assertNotIn("v" * 40, reason)

    def test_head_partial_read_denies_in_every_mode(self):
        self._assert_deny_and_intact("head -n 250 .env")

    def test_tail_partial_read_denies_in_every_mode(self):
        self._assert_deny_and_intact("tail -n 250 .env")

    def test_grep_search_denies_in_every_mode(self):
        # 実在する鍵名に一致するパターン (matched_pattern_keys 経路 = 明細行が
        # <DATA> に包まれる経路) を使う。存在しない名前だと
        # ``nomatch_pattern_keys`` だけの reason になり minimal info を持たない
        # (0.16.0 からの既存挙動)。
        self._assert_deny_and_intact("grep -E 'KEY_001|KEY_002' .env")

    def test_grep_nomatch_only_still_denies_in_every_mode(self):
        """鍵名に一致しないパターンは minimal info を持たないが判定は deny。"""
        for mode in self._MODES:
            with self.subTest(mode=mode):
                result = _run_main(
                    self._envelope("grep -E 'NOPE_KEY' .env", mode),
                    ["--tool", "bash"],
                )
                hook = result.get("hookSpecificOutput", {})
                self.assertEqual(hook.get("permissionDecision"), "deny", mode)


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


class TestPythonVersionDegradedWarning(unittest.TestCase):
    """3.11 未満で python_version_degraded を 1 回 log_info すること (内部バックログ)。

    TOML の構造付き minimal info は 3.11+ の ``tomllib`` が無いと opaque に
    劣化する (``redaction/tomllike.py``)。劣化そのものは既存の
    ``toml_unsupported`` fallback で reason に出るが、hook 起動側でもログに
    残してサイレント劣化にしない。
    """

    def test_logs_once_below_311(self):
        with mock.patch.object(entry.sys, "version_info", (3, 9, 6, "final", 0)):
            with mock.patch.object(entry.L, "log_info") as mock_log:
                entry._warn_if_python_degraded()
        mock_log.assert_called_once_with("python_version_degraded", "3.9")

    def test_no_log_at_or_above_311(self):
        with mock.patch.object(entry.sys, "version_info", (3, 11, 0, "final", 0)):
            with mock.patch.object(entry.L, "log_info") as mock_log:
                entry._warn_if_python_degraded()
        mock_log.assert_not_called()


class TestE2EGrepHandler(unittest.TestCase):
    """``--tool grep`` の実配線 (0.34.0)。

    判定そのものは ``tests/test_grep_handler.py`` が網羅する。ここで固定する
    のは ``__main__`` の dispatch (argparse の choices + ``_dispatch``) と
    ``hooks/hooks.json`` の matcher が揃っていること。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_grep_on_dotenv_denies(self):
        (Path(self.tmp) / ".env").write_text("JWT_SECRET=dummy\n")
        envelope = {
            "tool_name": "Grep",
            "tool_input": {
                "pattern": "SECRET",
                "path": ".env",
                "output_mode": "content",
            },
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        result = _run_main(envelope, ["--tool", "grep"])
        self.assertEqual(_decision(result), "deny")
        # 値も鍵名も出さない (Grep はファイルを開かない)
        self.assertNotIn("JWT_SECRET", _reason(result))

    def test_grep_on_source_tree_without_glob_allows(self):
        (Path(self.tmp) / "src").mkdir()
        (Path(self.tmp) / "src" / "a.py").write_text("x = 1\n")
        envelope = {
            "tool_name": "Grep",
            "tool_input": {"pattern": "x", "path": "src"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        self.assertEqual(_run_main(envelope, ["--tool", "grep"]), {})

    def test_grep_wildcard_glob_is_ask_or_allow(self):
        """0.34.0 (マージ前レビュー P2-3): wildcard glob は Bash と同じ三態。

        `grep x *.py` が Bash handler で ask になるのと揃える。autonomous
        では通す。
        """
        (Path(self.tmp) / "src").mkdir()
        (Path(self.tmp) / "src" / "a.py").write_text("x = 1\n")
        envelope = {
            "tool_name": "Grep",
            "tool_input": {"pattern": "x", "path": "src", "glob": "*.py"},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        from core import output

        self.assertEqual(_decision(_run_main(envelope, ["--tool", "grep"])), "ask")
        envelope["permission_mode"] = "auto"
        self.assertTrue(
            output.is_allow(_run_main(envelope, ["--tool", "grep"]))
        )

    def test_hooks_json_registers_the_grep_matcher(self):
        hooks_json = (
            Path(__file__).resolve().parent.parent.parent / "hooks.json"
        )
        with hooks_json.open() as f:
            config = json.load(f)
        entries = {
            entry["matcher"]: entry["hooks"][0]["command"]
            for entry in config["hooks"]["PreToolUse"]
        }
        self.assertIn("Grep", entries)
        self.assertIn("--tool grep", entries["Grep"])
        # Glob / NotebookEdit は対象外のまま (README の既知制限と対)
        self.assertNotIn("Glob", entries)
        self.assertNotIn("NotebookEdit", entries)


class TestSigalrmlessPlatformUsesNormalPipeline(unittest.TestCase):
    """0.34.0: ``signal.SIGALRM`` が無い環境でも通常のパイプラインを通る。

    0.33.x までは ``__main__`` 冒頭の ``_is_unsupported_platform`` が
    ``hasattr(signal, "SIGALRM")`` だけを見て、**機密と無関係な Read も含め
    全 tool 呼出を deny** していた (インストール即無効化級の体験)。根拠だった
    内部 soft-timeout は 0.6.0 で撤去済みで、hook 本体は SIGALRM を使わない。

    ここでは実際に ``signal.SIGALRM`` を一時的に取り除いて (Windows 相当の
    環境を模して) 通常判定が走ることを固定する。撤去前のコードではこの 2 件が
    どちらも deny になる (mutation で確認済み)。

    **Windows 実機の検証ではない** — 検証しているのは「SIGALRM の有無で判定が
    変わらない」ことだけ。Windows の実機検証は別チケット。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    @contextlib.contextmanager
    def _without_sigalrm(self):
        import signal

        missing = object()
        saved = getattr(signal, "SIGALRM", missing)
        if saved is not missing:
            delattr(signal, "SIGALRM")
        try:
            yield
        finally:
            if saved is not missing:
                signal.SIGALRM = saved

    def _read(self, name: str) -> dict:
        envelope = {
            "tool_name": "Read",
            "tool_input": {"file_path": name},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        with self._without_sigalrm():
            return _run_main(envelope, ["--tool", "read"])

    def test_non_sensitive_read_is_allowed(self):
        (Path(self.tmp) / "README.md").write_text("# hi\n")
        self.assertEqual(self._read("README.md"), {})

    def test_sensitive_read_is_still_denied(self):
        (Path(self.tmp) / ".env").write_text("JWT_SECRET=dummy\n")
        result = self._read(".env")
        self.assertEqual(_decision(result), "deny")
        self.assertIn("JWT_SECRET", _reason(result))

    def test_platform_gate_helper_is_gone(self):
        # 文面 (``M.unsupported_platform``) と同じく、gate 本体も残さない。
        self.assertFalse(hasattr(entry, "_is_unsupported_platform"))


if __name__ == "__main__":
    unittest.main()
