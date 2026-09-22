"""Grep handler (0.34.0) の床テスト。

判定表は ``docs/MATRIX.md`` の「Grep handler」節と対。設計の根拠と
``tool_input`` の実形は ``handlers/grep_handler.py`` のモジュール docstring。

``glob`` は Bash operand と**同じ三態** (deny / ``ask_or_allow`` / allow)。
**``ask`` を作らないのはディレクトリ走査だけ** — ``path`` がディレクトリ /
未指定のときは allow に倒す。例外経路は ``ask_or_deny`` (``__main__`` の
catch-all と同じ向き)。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from handlers import grep_handler
from handlers.grep_handler import handle


def _envelope(tool_input: dict, cwd: str, mode: str = "default") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Grep",
        "tool_input": tool_input,
        "cwd": cwd,
        "permission_mode": mode,
    }


def _decision(resp: dict) -> str | None:
    return (resp.get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(resp: dict) -> str:
    return (resp.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


class BaseGrep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        os.makedirs(self.xdg)
        self._env_patcher = mock.patch.dict(
            os.environ, {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg}
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        (Path(self.tmp) / ".env").write_text("JWT_SECRET=dummy\n")
        (Path(self.tmp) / "README.md").write_text("# hi\n")
        (Path(self.tmp) / "sub").mkdir()
        (Path(self.tmp) / "sub" / ".env").write_text("JWT_SECRET=dummy\n")

    def _grep(self, tool_input: dict, mode: str = "default") -> dict:
        return handle(_envelope(tool_input, self.tmp, mode=mode))


class TestGrepPath(BaseGrep):
    def test_relative_sensitive_file_denies(self):
        for mode in ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions"):
            with self.subTest(mode=mode):
                r = self._grep({"pattern": "SECRET", "path": ".env"}, mode=mode)
                self.assertEqual(_decision(r), "deny")

    def test_absolute_sensitive_file_denies(self):
        r = self._grep(
            {"pattern": "SECRET", "path": os.path.join(self.tmp, "sub", ".env")}
        )
        self.assertEqual(_decision(r), "deny")

    def test_output_mode_does_not_change_the_verdict(self):
        # files_with_matches でもファイル名は漏れる (既存の非目的の範囲) が、
        # content と同じく deny にする。``output_mode`` は判定に使わない。
        for om in ("content", "files_with_matches", "count", None):
            with self.subTest(output_mode=om):
                ti = {"pattern": "SECRET", "path": ".env"}
                if om is not None:
                    ti["output_mode"] = om
                self.assertEqual(_decision(self._grep(ti)), "deny")

    def test_non_sensitive_file_allows(self):
        r = self._grep({"pattern": "hi", "path": "README.md"})
        self.assertTrue(output.is_allow(r))

    def test_directory_allows(self):
        r = self._grep({"pattern": "SECRET", "path": "sub"})
        self.assertTrue(output.is_allow(r))

    def test_sensitive_named_directory_allows(self):
        """``python -m venv .env`` 等で ``.env`` がディレクトリの構成。

        ディレクトリ走査で配下の行が返る経路は Bash の ``grep -r X .`` と
        同じ既知の限界として allow にする (ここで deny すると venv を置いた
        repo で全検索が止まる)。
        """
        d = Path(self.tmp) / "venv-style"
        (d / ".env").mkdir(parents=True)
        (d / ".env" / "note.txt").write_text("x\n")
        r = self._grep({"pattern": "x", "path": "venv-style/.env"})
        self.assertTrue(output.is_allow(r))

    def test_missing_path_allows(self):
        r = self._grep({"pattern": "SECRET", "path": "nope/.env"})
        self.assertTrue(output.is_allow(r))

    def test_no_path_and_no_glob_allows(self):
        r = self._grep({"pattern": "SECRET"})
        self.assertTrue(output.is_allow(r))

    def test_template_file_allows(self):
        (Path(self.tmp) / ".env.example").write_text("FOO=bar\n")
        r = self._grep({"pattern": "FOO", "path": ".env.example"})
        self.assertTrue(output.is_allow(r))

    def test_suffix_dotenv_denies(self):
        # 0.34.0 の ``*.env`` 追加が Grep でも効く。
        (Path(self.tmp) / "production.env").write_text("SECRET=x\n")
        r = self._grep({"pattern": "SECRET", "path": "production.env"})
        self.assertEqual(_decision(r), "deny")

    def test_symlink_matches_read_handler(self):
        target = Path(self.tmp) / "real.txt"
        target.write_text("JWT_SECRET=dummy\n")
        link = Path(self.tmp) / "link.env"
        os.symlink(target, link)
        self.assertEqual(_decision(self._grep({"pattern": "x", "path": "link.env"})), "ask")
        self.assertEqual(
            _decision(
                self._grep({"pattern": "x", "path": "link.env"}, mode="bypassPermissions")
            ),
            "deny",
        )

    def test_special_file_matches_read_handler(self):
        """機密名の FIFO は Read handler と同じ ``ask_or_deny`` (0.34.0 P3-7)。

        MATRIX の Grep 表に行があるのに床が無く、``cls == "special"`` 分岐を
        削っても 1 件も落ちなかったため追加した。
        """
        fifo = Path(self.tmp) / "a.secret"
        os.mkfifo(fifo)
        self.assertEqual(_decision(self._grep({"pattern": "x", "path": "a.secret"})), "ask")
        self.assertEqual(
            _decision(
                self._grep({"pattern": "x", "path": "a.secret"}, mode="bypassPermissions")
            ),
            "deny",
        )

    def test_lstat_error_matches_read_handler(self):
        """``lstat`` 失敗 (権限 / IO) も ``ask_or_deny`` (0.34.0 P3-7)。"""
        locked = Path(self.tmp) / "locked"
        locked.mkdir()
        (locked / "id_rsa").write_text("dummy\n")
        os.chmod(locked, 0o000)
        self.addCleanup(lambda: os.chmod(locked, 0o755))
        if os.geteuid() == 0:  # pragma: no cover — root では権限で弾けない
            self.skipTest("root では chmod 000 でも lstat が通る")
        self.assertEqual(
            _decision(self._grep({"pattern": "x", "path": "locked/id_rsa"})), "ask"
        )

    def test_path_parts_are_evaluated(self):
        """``path`` は Read と同じ ``parts=True`` で評価する (0.34.0 P3-7)。

        ``.env/bin/activate`` は末尾要素が非機密でも途中要素が機密名なので
        deny。Bash operand (``parts=False``) とは意図的に非対称 — Grep の
        ``path`` は Read の ``file_path`` と同じ「実在するパス」だから。
        """
        d = Path(self.tmp) / "venv" / ".env" / "bin"
        d.mkdir(parents=True)
        (d / "activate").write_text("x\n")
        self.assertEqual(
            _decision(self._grep({"pattern": "x", "path": "venv/.env/bin/activate"})),
            "deny",
        )

    def test_npmrc_content_gate_applies(self):
        # Read と同じ内容ゲートを通る (``is_sensitive`` ではなく Read handler の
        # ゲートが判定するのは値の露出経路だから) — Grep は中身を開かないので
        # ここでは **ゲートを通さず** 従来どおり deny する。判定表の非対称を
        # 明示的に固定しておく。
        (Path(self.tmp) / ".npmrc").write_text("engine-strict=true\n")
        r = self._grep({"pattern": "engine", "path": ".npmrc"})
        self.assertEqual(_decision(r), "deny")


class TestGrepGlob(BaseGrep):
    def test_literal_sensitive_glob_denies(self):
        for glob in (".env", ".npmrc", "id_rsa"):
            with self.subTest(glob=glob):
                r = self._grep({"pattern": "x", "glob": glob})
                self.assertEqual(_decision(r), "deny")

    def test_dotenv_expanding_glob_denies(self):
        # Bash operand と同じ規則 (``_glob_operand_is_dotenv_match``)。
        for glob in (".env*", "**/.env", "*/.env", ".en?"):
            with self.subTest(glob=glob):
                r = self._grep({"pattern": "x", "glob": glob})
                self.assertEqual(_decision(r), "deny")

    def test_non_sensitive_literal_glob_allows(self):
        # ワイルドカードを含まない literal だけが allow に落ちる。
        for glob in ("README.md", "src/main.py", "Makefile"):
            with self.subTest(glob=glob):
                r = self._grep({"pattern": "x", "glob": glob})
                self.assertTrue(output.is_allow(r))

    def test_wildcard_glob_is_ask_or_allow(self):
        """0.34.0 (マージ前レビュー P2-3): wildcard glob は Bash と同じ三態。

        0.34.0 の初版は「``ask`` は作らない」方針でこれらを全部 allow に
        していたが、同じ意図の Bash operand (``grep x *.pem``) が
        ``ask_or_allow`` なので **Grep の方が緩い**状態だった。Bash の
        ``glob_uncertain`` をそのまま再利用して揃える (ユーザー判定)。

        ``*.py`` / ``*.log`` のような無害な glob も ask 側に入る — Bash の
        positional operand と同じ扱いに倒したため (``--include='*.py'`` 形は
        Bash では allow だが、揃え先は positional 側)。
        """
        for glob in ("*.py", "**/*.ts", "src/**", "*.log", "*.pem", "id_rsa*", "?env"):
            with self.subTest(glob=glob):
                self.assertEqual(_decision(self._grep({"pattern": "x", "glob": glob})), "ask")
                for mode in ("auto", "bypassPermissions"):
                    self.assertTrue(
                        output.is_allow(
                            self._grep({"pattern": "x", "glob": glob}, mode=mode)
                        ),
                        msg=f"{glob} in {mode}",
                    )

    def test_star_dot_env_glob_is_ask_not_deny(self):
        """``*.env`` は Bash operand と同じく **deny にはならない**。

        glob の deny 判定は既定 rules への候補列挙ではなく dotenv literal
        stem (``.env`` / ``.envrc``) への展開可能性だけを見る (0.8.0 の縮約)。
        ``*.env`` は先頭ドットの ``.env`` には展開されず、``prod.env`` に
        展開されうることは patterns を見ないと分からない。0.34.0 のレビュー
        反映で allow → ask になった (deny にはしない)。docs/MATRIX.md の
        glob 行に同じ内容を書いてある。
        """
        self.assertEqual(_decision(self._grep({"pattern": "x", "glob": "*.env"})), "ask")
        self.assertTrue(
            output.is_allow(self._grep({"pattern": "x", "glob": "*.env"}, mode="auto"))
        )

    def test_glob_is_checked_even_when_path_is_a_directory(self):
        r = self._grep({"pattern": "x", "path": "sub", "glob": ".env"})
        self.assertEqual(_decision(r), "deny")

    def test_directory_path_with_wildcard_glob_is_ask_or_allow(self):
        r = self._grep({"pattern": "x", "path": "sub", "glob": "*.py"})
        self.assertEqual(_decision(r), "ask")

    def test_directory_path_without_glob_allows(self):
        """ディレクトリ走査だけは ``ask`` を作らない (既知の限界のまま)。"""
        r = self._grep({"pattern": "x", "path": "sub"})
        self.assertTrue(output.is_allow(r))

    def test_lenient_note_is_gated_like_bash(self):
        """lenient allow の開示 note は Bash と同じ絞りを通す (0.33.0 の契約)。

        ``core/output.py``: 「この関数を別 tool から呼ぶときは同じ絞りを通すか、
        頻度が問題にならないことを確認すること」。機密パターンらしい glob
        (``*.pem``) には載り、日常 glob (``*.py``) には載らない。
        """
        with_note = self._grep({"pattern": "x", "glob": "*.pem"}, mode="auto")
        self.assertTrue(output.is_allow(with_note))
        self.assertTrue(output.additional_context_of(with_note))
        without = self._grep({"pattern": "x", "glob": "*.py"}, mode="auto")
        self.assertTrue(output.is_allow(without))
        self.assertFalse(output.additional_context_of(without))


class TestGrepGlobBraces(BaseGrep):
    """ブレース展開 (0.34.0 のマージ前レビュー P2-2)。

    Claude Code の ``glob`` は ``"*.{ts,tsx}"`` を解釈する。Bash 側には同等の
    扱いが無い (``{`` は hard-stop として ask に倒れるだけ) ので、Grep 側に
    だけ「分岐ごとに判定して最も強い結論を採る」を足した。
    """

    def test_brace_branch_with_dotenv_denies(self):
        for glob in ("{.env,x}", "{.env,*.py}", "**/{.env,a}", "{.env*,src}"):
            with self.subTest(glob=glob):
                self.assertEqual(
                    _decision(self._grep({"pattern": "x", "glob": glob})), "deny"
                )

    def test_brace_branch_with_literal_sensitive_name_denies(self):
        self.assertEqual(
            _decision(self._grep({"pattern": "x", "glob": "{id_rsa,notes.txt}"})),
            "deny",
        )

    def test_brace_suffix_expansion_denies(self):
        # ``.en{v,x}`` → ``.env`` / ``.enx``。分岐の 1 つが dotenv stem。
        self.assertEqual(
            _decision(self._grep({"pattern": "x", "glob": ".en{v,x}"})), "deny"
        )

    def test_harmless_brace_glob_is_ask_or_allow(self):
        for glob in ("*.{ts,tsx}", "{src,lib}/**"):
            with self.subTest(glob=glob):
                self.assertEqual(
                    _decision(self._grep({"pattern": "x", "glob": glob})), "ask"
                )

    def test_literal_only_brace_glob_is_ask(self):
        """分岐が全部 literal 非機密でも ``{`` はワイルドカードなので ask。

        Bash は ``{`` を hard-stop として ask に倒す (``cat {a,b}`` = ask)
        ので、結論は Bash と揃う。
        """
        self.assertEqual(
            _decision(self._grep({"pattern": "x", "glob": "{a,b}"})), "ask"
        )

    def test_unterminated_or_oversized_brace_is_ask_not_allow(self):
        # 展開できない形は「判定できない wildcard」として ask 側に落とす。
        many = "{a,b}" * 9
        for glob in ("{.env,x", "a{b{c,d}", many):
            with self.subTest(glob=glob):
                self.assertEqual(
                    _decision(self._grep({"pattern": "x", "glob": glob})), "ask"
                )


class TestGrepDenyReason(BaseGrep):
    def test_path_reason_names_the_target_and_suggests_read(self):
        reason = _reason(self._grep({"pattern": "SECRET", "path": ".env"}))
        self.assertIn(".env", reason)
        self.assertIn("Read", reason)
        # 値も鍵名も出さない (Grep はファイルを開いていない)
        self.assertNotIn("JWT_SECRET", reason)
        # 恒久除外レシピは Bash / Edit と同じ案内が付く
        self.assertIn("patterns.local.txt", reason)
        self.assertIn("承認なしに自分で追加しないこと", reason)

    def test_glob_reason_names_the_glob(self):
        reason = _reason(self._grep({"pattern": "x", "glob": ".env*"}))
        self.assertIn(".env*", reason)

    def test_reason_fits_the_byte_budget(self):
        reason = _reason(
            self._grep({"pattern": "x", "path": ".env"})
        )
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES
        )


class TestGrepFailClosed(BaseGrep):
    def test_patterns_unavailable_is_ask_or_deny(self):
        with mock.patch(
            "handlers.grep_handler.load_patterns", side_effect=OSError("boom")
        ):
            self.assertEqual(
                _decision(self._grep({"pattern": "x", "path": ".env"})), "ask"
            )
            self.assertEqual(
                _decision(
                    self._grep(
                        {"pattern": "x", "path": ".env"}, mode="bypassPermissions"
                    )
                ),
                "deny",
            )

    def test_empty_rules_allow(self):
        with mock.patch("handlers.grep_handler.load_patterns", return_value=[]):
            self.assertTrue(
                output.is_allow(self._grep({"pattern": "x", "path": ".env"}))
            )

    def test_normalize_failure_is_ask_or_deny(self):
        with mock.patch(
            "handlers.grep_handler.normalize", side_effect=ValueError("nul")
        ):
            self.assertEqual(
                _decision(self._grep({"pattern": "x", "path": ".env"})), "ask"
            )

    def test_non_string_fields_are_ignored(self):
        for ti in (
            {"pattern": "x", "path": None},
            {"pattern": "x", "path": 3},
            {"pattern": "x", "glob": []},
            {"pattern": "x", "path": "", "glob": ""},
        ):
            with self.subTest(tool_input=ti):
                self.assertTrue(output.is_allow(self._grep(ti)))

    def test_missing_tool_input_allows(self):
        r = handle({"tool_name": "Grep", "cwd": self.tmp, "permission_mode": "default"})
        self.assertTrue(output.is_allow(r))

    def test_handler_is_reachable_from_module_namespace(self):
        # ``__main__._dispatch`` が ``handlers.grep_handler.handle`` を呼ぶ契約
        self.assertTrue(callable(grep_handler.handle))


if __name__ == "__main__":
    unittest.main()
