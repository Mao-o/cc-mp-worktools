"""Edit / Write handler (Step 6) のテスト。

新規 / 既存を問わず機密パターン一致なら ``ask_or_deny``。
テンプレ除外 (``.env.example`` 等) は既定 patterns の ``!*.example`` で allow。
親 dir が symlink / special / missing なら fail-closed。
MultiEdit は CLI 非搭載のため 0.6.0 で test を撤去。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from handlers.edit_handler import handle


def _env_shell_isolate(tmp: str):
    """HOME / XDG を tmpdir に隔離して patterns.local.txt を汚染しない。"""
    home = os.path.join(tmp, "home")
    xdg = os.path.join(tmp, "xdg")
    os.makedirs(home, exist_ok=True)
    os.makedirs(xdg, exist_ok=True)
    return mock.patch.dict(
        os.environ, {"HOME": home, "XDG_CONFIG_HOME": xdg},
    )


def _make_envelope(tool: str, file_path: str, cwd: str, mode: str = "default") -> dict:
    tool_input: dict = {"file_path": file_path}
    if tool == "Edit":
        tool_input.update({"old_string": "a", "new_string": "b"})
    elif tool == "Write":
        tool_input.update({"content": "x"})
    return {
        "tool_name": tool,
        "tool_input": tool_input,
        "cwd": cwd,
        "permission_mode": mode,
    }


def _decision(resp: dict) -> str | None:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


class BaseEdit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._env_patcher = _env_shell_isolate(self.tmp)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestEditExistingFile(BaseEdit):
    """既存 .env を Edit → **deny 固定** (bypass 関係なし)。

    0.2.0 で ask_or_deny → make_deny 固定に変更。実機観測でうっかり承認されて
    既存値を喪失する事例があったため。
    """

    def test_deny_non_bypass(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / ".env"), self.tmp),
            tool_label="Edit",
        )
        self.assertEqual(_decision(r), "deny")

    def test_deny_bypass(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / ".env"), self.tmp,
                           mode="bypassPermissions"),
            tool_label="Edit",
        )
        self.assertEqual(_decision(r), "deny")


class TestWriteNewFile(BaseEdit):
    """新規 .env 作成 → **deny 固定** (新規でも機密扱い)。"""

    def test_new_dotenv_denies(self):
        r = handle(
            _make_envelope("Write", str(Path(self.tmp) / ".env"), self.tmp),
            tool_label="Write",
        )
        self.assertEqual(_decision(r), "deny")

    def test_new_dotenv_bypass_denies(self):
        r = handle(
            _make_envelope("Write", str(Path(self.tmp) / ".env"), self.tmp,
                           mode="bypassPermissions"),
            tool_label="Write",
        )
        self.assertEqual(_decision(r), "deny")


class TestTemplateFileAllowed(BaseEdit):
    """``.env.example`` / ``.env.template`` は既定 !*.example で exclude。"""

    def test_example_new_file_allowed(self):
        r = handle(
            _make_envelope("Write", str(Path(self.tmp) / ".env.example"), self.tmp),
            tool_label="Write",
        )
        self.assertTrue(output.is_allow(r))

    def test_template_existing_edit_allowed(self):
        (Path(self.tmp) / "config.template").write_text("x\n")
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / "config.template"), self.tmp),
            tool_label="Edit",
        )
        self.assertTrue(output.is_allow(r))


class TestNonSensitiveAllowed(BaseEdit):
    def test_readme_allow(self):
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / "README.md"), self.tmp),
            tool_label="Edit",
        )
        self.assertTrue(output.is_allow(r))


class TestParentDirectoryChecks(BaseEdit):
    """親ディレクトリが symlink / special / missing → fail-closed (ask)。"""

    def test_parent_is_symlink(self):
        real = Path(self.tmp) / "real"
        real.mkdir()
        link = Path(self.tmp) / "link"
        os.symlink(real, link)
        r = handle(
            _make_envelope("Write", str(link / ".env"), self.tmp),
            tool_label="Write",
        )
        self.assertEqual(_decision(r), "ask")

    def test_parent_missing(self):
        r = handle(
            _make_envelope(
                "Write", str(Path(self.tmp) / "nope-dir" / ".env"), self.tmp
            ),
            tool_label="Write",
        )
        self.assertEqual(_decision(r), "ask")


class TestSymlinkTargetDenies(BaseEdit):
    """path 自体が symlink → **deny 固定** (書き込み先が意図せず外を向く可能性)。"""

    def test_path_is_symlink(self):
        target = Path(self.tmp) / "real.env"
        target.write_text("FOO=bar\n")
        link = Path(self.tmp) / ".env"
        os.symlink(target, link)
        r = handle(
            _make_envelope("Edit", str(link), self.tmp),
            tool_label="Edit",
        )
        self.assertEqual(_decision(r), "deny")


class TestEmptyOrInvalidInput(BaseEdit):
    def test_no_file_path(self):
        envelope = {
            "tool_name": "Edit",
            "tool_input": {},
            "cwd": self.tmp,
            "permission_mode": "default",
        }
        r = handle(envelope, tool_label="Edit")
        self.assertTrue(output.is_allow(r))


class TestDotenvParseFailureLogged(BaseEdit):
    """L2 (0.4.3): _extract_dotenv_keys が parse 例外を分類してログに残すこと。

    bare except を狭めた結果、ValueError / UnicodeDecodeError / AttributeError
    / TypeError は ``dotenv_parse_failed`` として log_info に記録される。
    silent fallback (空リスト返す) の挙動は維持。
    """

    def test_value_error_logged(self):
        from handlers import edit_handler
        from core import logging as L

        with mock.patch.object(
            edit_handler, "redact_dotenv",
            side_effect=ValueError("simulated"),
        ):
            with mock.patch.object(L, "log_info") as mock_log:
                envelope = _make_envelope(
                    "Write", str(Path(self.tmp) / ".env"), self.tmp,
                )
                envelope["tool_input"]["content"] = "FOO=bar\n"
                result = edit_handler._extract_dotenv_keys(
                    envelope, "Write", ".env",
                )
                self.assertEqual(result, [])
                mock_log.assert_any_call(
                    "dotenv_parse_failed", "ValueError",
                )

    def test_attribute_error_logged(self):
        from handlers import edit_handler
        from core import logging as L

        with mock.patch.object(
            edit_handler, "redact_dotenv",
            side_effect=AttributeError("simulated"),
        ):
            with mock.patch.object(L, "log_info") as mock_log:
                envelope = _make_envelope(
                    "Write", str(Path(self.tmp) / ".env"), self.tmp,
                )
                envelope["tool_input"]["content"] = "FOO=bar\n"
                result = edit_handler._extract_dotenv_keys(
                    envelope, "Write", ".env",
                )
                self.assertEqual(result, [])
                mock_log.assert_any_call(
                    "dotenv_parse_failed", "AttributeError",
                )

    def test_unexpected_exception_propagates(self):
        # KeyboardInterrupt / SystemExit など想定外は握りつぶさない
        from handlers import edit_handler

        with mock.patch.object(
            edit_handler, "redact_dotenv",
            side_effect=KeyboardInterrupt(),
        ):
            envelope = _make_envelope(
                "Write", str(Path(self.tmp) / ".env"), self.tmp,
            )
            envelope["tool_input"]["content"] = "FOO=bar\n"
            with self.assertRaises(KeyboardInterrupt):
                edit_handler._extract_dotenv_keys(
                    envelope, "Write", ".env",
                )


def _reason(resp: dict) -> str:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecisionReason", "")


class TestDenyReasonSuggestions(BaseEdit):
    """0.2.0: deny reason に dotenv 追加キー名を埋め込む。"""

    def test_write_content_keys_in_reason(self):
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = (
            "DATABASE_URL=postgresql://x\n"
            "JWT_SECRET=abc\n"
            "DEBUG=true\n"
        )
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # 抽出されたキーが reason に含まれる
        self.assertIn("DATABASE_URL", reason)
        self.assertIn("JWT_SECRET", reason)
        self.assertIn("DEBUG", reason)
        # 案内文も入る
        self.assertIn(".env.example", reason)
        self.assertIn("patterns.local.txt", reason)
        # 値は漏れない
        self.assertNotIn("postgresql", reason)
        self.assertNotIn("abc", reason)
        self.assertNotIn("true", reason)

    def test_edit_new_string_keys_in_reason(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["old_string"] = "FOO=bar"
        envelope["tool_input"]["new_string"] = "FOO=baz\nNEW_KEY=123\n"
        r = handle(envelope, tool_label="Edit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("FOO", reason)
        self.assertIn("NEW_KEY", reason)
        # 値は漏れない
        self.assertNotIn("baz", reason)
        self.assertNotIn("123", reason)

    def test_non_dotenv_has_no_key_suggestion(self):
        """非 dotenv 基準 (credentials.json 等) は keys 案内を埋めない。"""
        envelope = _make_envelope(
            "Write",
            str(Path(self.tmp) / "credentials.json"),
            self.tmp,
        )
        envelope["tool_input"]["content"] = '{"api": "secret"}'
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # credentials.json は dotenv 非該当。keys 案内は出ない
        self.assertNotIn(".env.example", reason)
        # block の基本メッセージは出る
        self.assertIn("patterns.local.txt", reason)
        # 値は漏れない
        self.assertNotIn("secret", reason)

    def test_envrc_extracts_keys(self):
        """Step 3: .envrc も dotenv 扱いなのでキー抽出される。"""
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".envrc"), self.tmp,
        )
        envelope["tool_input"]["content"] = "export AWS_ACCESS_KEY=x\nexport AWS_REGION=y\n"
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("AWS_ACCESS_KEY", reason)
        self.assertIn("AWS_REGION", reason)

    def test_empty_content_no_keys(self):
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = ""
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # 空 content → 追加キー無し。基本メッセージのみ
        self.assertNotIn(".env.example", reason)

    def test_key_count_cap_with_overflow(self):
        """A-2: 30 キー超は先頭 30 + ``... (N more)`` で切り詰める。"""
        # 35 キーの .env を生成
        lines = [f"KEY_{i}=v\n" for i in range(35)]
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "".join(lines)
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # 先頭 30 は含まれる
        self.assertIn("KEY_0=", reason)
        self.assertIn("KEY_29=", reason)
        # 31 以降は省略される
        self.assertNotIn("KEY_31=", reason)
        self.assertNotIn("KEY_34=", reason)
        # overflow マーカー
        self.assertIn("... (5 more)", reason)


class TestDenyReasonBasename(BaseEdit):
    """H3: deny reason の ``!<basename>`` 案内に **実 basename を展開**して
    埋め込むこと (LLM がコピペで patterns.local.txt に追記できる形)。

    既存 ``TestDenyReasonSuggestions`` は dotenv キー名抽出に focus している。
    こちらは basename 展開という別軸の保証。
    """

    def test_dotenv_write_embeds_basename(self):
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "FOO=1\n"
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # H3: `!<basename>` プレースホルダではなく、実 basename が埋まる
        self.assertIn("`!.env`", reason)
        self.assertNotIn("`!<basename>`", reason)

    def test_credentials_json_embeds_basename(self):
        envelope = _make_envelope(
            "Write",
            str(Path(self.tmp) / "credentials.json"),
            self.tmp,
        )
        envelope["tool_input"]["content"] = '{"k":"v"}'
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("`!credentials.json`", reason)

    def test_subdir_dotenv_embeds_basename_only(self):
        sub = Path(self.tmp) / "deep" / "nested"
        sub.mkdir(parents=True)
        envelope = _make_envelope(
            "Write", str(sub / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "FOO=1\n"
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # フルパスは `!...` の中に入らない
        self.assertNotIn("`!" + str(sub / ".env") + "`", reason)
        # basename だけが入る
        self.assertIn("`!.env`", reason)


class TestEditDenyKindBranches(BaseEdit):
    """E6 (0.20.0): ``classify`` の結果ごとに deny 文面が分岐すること。

    **判定は 4 分岐すべて deny 固定のまま**で、変わるのは reason 文字列だけ
    (verdict 不変は ``TestEditExistingFile`` / ``TestWriteNewFile`` /
    ``TestSymlinkTargetDenies`` が引き続き担保する)。

    ここでは「この文面はこう出ると主張している」内容を実文字列で固定する。
    docstring や CHANGELOG に書いた案内が実際に出ていることの機械的な裏付け。
    """

    EXISTING = (
        "DATABASE_URL=postgresql://user:pw@host/db\n"
        "API_TOKEN=sk-abcdefghijklmnopqrst\n"
        "EMPTY_ONE=\n"
    )

    def _write_existing(self, name: str = ".env", body: str | None = None):
        p = Path(self.tmp) / name
        p.write_text(self.EXISTING if body is None else body)
        return p

    # -- new (missing) ---------------------------------------------------

    def test_new_branch_note_says_new_file(self):
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("新規作成しようとしたため block しました", reason)
        self.assertNotIn("上書きしようとしたため", reason)

    def test_new_branch_guides_env_example_with_empty_values(self):
        """REVIEW_TASKS E6 の「同じキーで .env.example を作り空値にする」案内。"""
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Write"))
        self.assertIn(
            "同じキー名で `.env.example` を作成し、値は空にしてください。",
            reason,
        )
        self.assertIn("1Password CLI", reason)

    def test_new_branch_has_no_existing_minimal_info(self):
        """新規作成には既存ファイルが無いので minimal info セクションを出さない。"""
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Write"))
        self.assertNotIn("上書き対象の既存ファイル", reason)
        self.assertNotIn("<DATA", reason)
        self.assertNotIn("minimal info", reason)

    # -- overwrite (regular) ---------------------------------------------

    def test_overwrite_branch_note_says_overwrite(self):
        self._write_existing()
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Edit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("既存の機密ファイル", reason)
        self.assertIn("上書きしようとしたため block しました", reason)
        self.assertNotIn("新規作成しようとしたため", reason)

    def test_overwrite_branch_embeds_existing_key_names(self):
        """既存 key 名を Read 同等 minimal info として返す (E6 の本体)。"""
        self._write_existing()
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn(
            "minimal info (Read 同等 / 上書き対象の既存ファイル):", reason,
        )
        # <DATA> 包装が閉じタグまで揃っていること (外殻破壊防御の前提)
        self.assertIn('<DATA untrusted="true"', reason)
        self.assertIn("</DATA>", reason)
        # 既存キー名は出る
        self.assertIn("DATABASE_URL", reason)
        self.assertIn("API_TOKEN", reason)
        self.assertIn("EMPTY_ONE", reason)
        # 追加予定キーとは別セクションで並ぶ
        self.assertIn("suggested_keys:", reason)
        self.assertIn("  NEW_KEY=", reason)

    def test_overwrite_branch_does_not_leak_existing_values(self):
        """minimal info は鍵名・型・length・status まで。実値は出さない。"""
        self._write_existing()
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertNotIn("postgresql://user", reason)
        self.assertNotIn("user:pw@host", reason)
        self.assertNotIn("abcdefghijklmnopqrst", reason)
        # 型 / status / length の要約は出る (Read handler と同じ粒度)
        self.assertIn("<type=url>", reason)
        self.assertIn("<empty>", reason)
        self.assertIn("length=", reason)

    def test_overwrite_dotenv_suggests_dotenv_cli_merge(self):
        self._write_existing()
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("dotenv-cli", reason)
        self.assertIn("merge", reason)

    def test_overwrite_non_dotenv_suggests_patch_not_dotenv_cli(self):
        """非 dotenv (credentials.json) に dotenv-cli を勧めない。"""
        self._write_existing(
            "credentials.json", '{"client_id": "abc", "token": "xyz"}',
        )
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / "credentials.json"), self.tmp,
        )
        envelope["tool_input"]["content"] = '{"a": "b"}'
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertNotIn("dotenv-cli", reason)
        self.assertIn("差分適用 (patch)", reason)
        # 非 dotenv でも既存の鍵名構造は minimal info で返る
        self.assertIn("client_id", reason)
        self.assertNotIn("xyz", reason)

    # -- symlink / special -----------------------------------------------

    def test_symlink_branch_asks_to_confirm_target(self):
        target = Path(self.tmp) / "real.env"
        target.write_text("FOO=bar\n")
        link = Path(self.tmp) / ".env"
        os.symlink(target, link)
        envelope = _make_envelope("Edit", str(link), self.tmp)
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Edit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("symlink 経由で", reason)
        self.assertIn("symlink 先が意図した参照か確認してください", reason)
        self.assertIn("symlink を維持する運用を推奨", reason)
        # 実体 path は reason に出さない (絶対パス非開示方針)
        self.assertNotIn(str(target), reason)
        self.assertNotIn(self.tmp, reason)

    def test_special_branch_names_the_file_kind(self):
        fifo = Path(self.tmp) / ".env"
        os.mkfifo(fifo)
        envelope = _make_envelope("Write", str(fifo), self.tmp)
        envelope["tool_input"]["content"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("非通常ファイル (FIFO / socket / device)", reason)
        self.assertIn("通常ファイルを対象にするか", reason)
        # symlink / overwrite の文面が混ざらない
        self.assertNotIn("上書き対象の既存ファイル", reason)
        self.assertNotIn("symlink 先が意図した参照か", reason)


class TestOverwriteMinimalInfoFailure(BaseEdit):
    """E6: 既存ファイルの minimal info 取得に失敗しても **verdict は動かない**。

    0.16.0 の silent degradation 対策と同じ方針で、取れなかったことと next
    action を明示する。
    """

    def _run(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        return handle(envelope, tool_label="Edit")

    def test_render_failure_reports_unavailable_with_next_action(self):
        from handlers import edit_handler

        with mock.patch.object(
            edit_handler, "render_for_bash",
            return_value=(None, None, "open_failed", ""),
        ):
            r = self._run()
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn(
            "minimal info: unavailable (既存ファイルの読み取り / 解析に失敗)",
            reason,
        )
        self.assertIn("Read tool に", reason)
        # 除外案内は従来どおり残る
        self.assertIn("patterns.local.txt", reason)

    def test_render_exception_still_denies(self):
        """``render_for_bash`` が例外を投げても deny のまま (verdict 不変)。"""
        from handlers import edit_handler
        from core import logging as L

        with mock.patch.object(
            edit_handler, "render_for_bash",
            side_effect=RuntimeError("simulated"),
        ):
            with mock.patch.object(L, "log_error") as mock_log:
                r = self._run()
        self.assertEqual(_decision(r), "deny")
        self.assertIn("minimal info: unavailable", _reason(r))
        mock_log.assert_any_call(
            "edit_existing_render_failed", "RuntimeError",
        )


class TestOverwriteReasonByteBudget(BaseEdit):
    """E6: minimal info を足しても ``MAX_REASON_BYTES`` を超えないこと。

    ``edit_deny`` は minimal info 以外を先に組んでから残り byte を計算し、
    その範囲に収まる行数だけ載せる。したがって **E6 の追加によって末尾の
    除外案内が truncate で失われることはない**。
    """

    def _overwrite_reason(self, existing_keys: int, new_keys: int) -> str:
        body = "".join(
            f"EXISTING_KEY_{i}=value{i}\n" for i in range(existing_keys)
        )
        (Path(self.tmp) / ".env").write_text(body)
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "".join(
            f"ADDED_KEY_{i}=v\n" for i in range(new_keys)
        )
        r = handle(envelope, tool_label="Write")
        self.assertEqual(_decision(r), "deny")
        return _reason(r)

    def test_huge_existing_file_fits_budget_and_keeps_hint(self):
        reason = self._overwrite_reason(existing_keys=300, new_keys=1)
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES,
        )
        # 予算内に畳んだ痕跡と、壊れていない <DATA> 包装
        self.assertRegex(reason, r"  \.\.\. \(\d+ more lines\)")
        self.assertIn("</DATA>", reason)
        # 末尾の除外案内が生き残る = output._truncate は発火していない
        self.assertIn("patterns.local.txt", reason)
        self.assertIn("`!.env`", reason)
        self.assertNotIn(output.TRUNCATE_MARKER.strip(), reason)

    def test_huge_existing_plus_many_new_keys_fits_budget(self):
        reason = self._overwrite_reason(existing_keys=300, new_keys=35)
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES,
        )
        # 追加キー側の切り詰め (30 件 + "... (5 more)") は従来どおり
        self.assertIn("  ADDED_KEY_29=", reason)
        self.assertIn("... (5 more)", reason)
        self.assertIn("patterns.local.txt", reason)
        self.assertNotIn(output.TRUNCATE_MARKER.strip(), reason)

    def test_suggested_keys_alone_over_budget_falls_back_to_truncate(self):
        """予算を ``suggested_keys`` だけで使い切る極端な入力の実際の挙動。

        この場合 E6 の minimal info セクションは **丸ごと省略** され
        (載せる余地が無いため)、残りは E6 以前と同じく
        ``core.output._truncate`` が最終防御として切る。E6 が状況を悪化させて
        いないこと (= 省略するだけで、他の行を押し出さないこと) を固定する。
        """
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        long_name = "K" * 200  # sanitize_key で 131 文字に丸められる
        envelope = _make_envelope(
            "Write", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["content"] = "".join(
            f"{long_name}{i}=v\n" for i in range(30)
        )
        r = handle(envelope, tool_label="Write")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES,
        )
        # minimal info セクションは載る余地が無いので出ない
        self.assertNotIn("上書き対象の既存ファイル", reason)
        # 予算切れは output._truncate が引き取る (E6 以前と同じ挙動)
        self.assertIn(output.TRUNCATE_MARKER.strip(), reason)


if __name__ == "__main__":
    unittest.main()
