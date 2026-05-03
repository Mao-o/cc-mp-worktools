"""Edit / Write / MultiEdit handler (Step 6) のテスト。

新規 / 既存を問わず機密パターン一致なら ``ask_or_deny``。
テンプレ除外 (``.env.example`` 等) は既定 patterns の ``!*.example`` で allow。
親 dir が symlink / special / missing なら fail-closed。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

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
    elif tool == "MultiEdit":
        tool_input.update({"edits": [{"old_string": "a", "new_string": "b"}]})
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
        self.assertIsNone(_decision(r))

    def test_template_existing_edit_allowed(self):
        (Path(self.tmp) / "config.template").write_text("x\n")
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / "config.template"), self.tmp),
            tool_label="Edit",
        )
        self.assertIsNone(_decision(r))


class TestNonSensitiveAllowed(BaseEdit):
    def test_readme_allow(self):
        r = handle(
            _make_envelope("Edit", str(Path(self.tmp) / "README.md"), self.tmp),
            tool_label="Edit",
        )
        self.assertIsNone(_decision(r))


class TestMultiEdit(BaseEdit):
    def test_multiedit_dotenv_denies(self):
        (Path(self.tmp) / ".env").write_text("FOO=bar\n")
        r = handle(
            _make_envelope("MultiEdit", str(Path(self.tmp) / ".env"), self.tmp),
            tool_label="MultiEdit",
        )
        self.assertEqual(_decision(r), "deny")


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
        self.assertIsNone(_decision(r))


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

    def test_multiedit_aggregates_keys(self):
        (Path(self.tmp) / ".env").write_text("A=1\n")
        envelope = _make_envelope(
            "MultiEdit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["edits"] = [
            {"old_string": "A=1", "new_string": "A=2\nB=3\n"},
            {"old_string": "B=3", "new_string": "B=4\nC=5\n"},
        ]
        r = handle(envelope, tool_label="MultiEdit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        # 全 edits から集約される (重複は dotenv parser がそのまま残す)
        self.assertIn("A", reason)
        self.assertIn("B", reason)
        self.assertIn("C", reason)

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


if __name__ == "__main__":
    unittest.main()
