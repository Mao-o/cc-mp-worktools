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

    def test_overwrite_branch_note_says_rewrite(self):
        """note は tool 中立の「書き換え」。tool 差は suggestion 側で言う。

        「上書き」と書くと Edit (対象を絞った置換) では事実と違うため
        (PR #47 Codex P2)。
        """
        self._write_existing()
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Edit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn("既存の機密ファイル", reason)
        self.assertIn("書き換えようとしたため block しました", reason)
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

    # -- overwrite: 既存キーの値更新 vs 新規追加 (0.26.0) ------------------

    def test_overwrite_pure_value_update_is_not_labeled_as_addition(self):
        """既存 TOKEN の値だけを差し替える Edit は「追加予定」と言わない。

        ticket 再現そのもの: 既存 ``TOKEN=abcdefghij`` を ``TOKEN=zzz`` に
        Edit すると、旧実装は minimal info で「TOKEN は既存キー」と示しつつ
        suggested_keys / suggestion_alt では同じ TOKEN を「追加予定」と言う
        自己矛盾があった。
        """
        self._write_existing(".env", "TOKEN=abcdefghijklmnop\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "TOKEN=zzz\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        # 既存キーとして表示される (minimal info)
        self.assertIn("1. TOKEN", reason)
        # suggested_keys には出るが「追加予定」ではなく「更新」と説明する
        self.assertIn("suggested_keys:", reason)
        self.assertIn("  TOKEN=", reason)
        self.assertIn("既存キーの値の更新", reason)
        self.assertNotIn("追加予定のキー名", reason)

    def test_overwrite_envrc_value_update_is_not_labeled_as_addition(self):
        """ticket のもう 1 つの再現例 (.envrc の AWS_PROFILE 更新)。"""
        self._write_existing(".envrc", "AWS_PROFILE=old-profile\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".envrc"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "AWS_PROFILE=new-profile\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("既存キーの値の更新", reason)
        self.assertNotIn("追加予定のキー名", reason)

    def test_overwrite_new_key_addition_keeps_addition_wording(self):
        """既存キーと無関係な**新規**キー追加は従来どおり「追加予定」のまま。

        「更新」文面への切替が overwrite 全般に広がっていないことの対照。
        """
        self._write_existing(".env", "TOKEN=abcdefghijklmnop\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "BRAND_NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("追加予定のキー名", reason)
        self.assertNotIn("既存キーの値の更新", reason)

    def test_overwrite_mixed_new_and_existing_keeps_default_wording(self):
        """新規キーと既存キー更新が混在するときは「更新」と言い切らない。

        全件が既存キーのときだけ「更新」文面にする設計 (advisor 指摘の
        「上記が全部更新なのか一部だけなのか」を安全側で判定できないケース)。
        新規分については「追加予定」の説明が事実として正しいままなので、
        従来文面を維持する。
        """
        self._write_existing(".env", "TOKEN=abcdefghijklmnop\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "TOKEN=zzz\nBRAND_NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("追加予定のキー名", reason)
        self.assertNotIn("既存キーの値の更新", reason)

    def test_overwrite_render_failure_keeps_default_wording_not_update(self):
        """既存キー集合が取得できない (render 失敗) ときは「更新」と断定しない。

        判定できないことを「更新ではない」と誤って断定するより、従来の
        「追加予定」文面を維持する方が安全側 (誤って「更新なので追記不要」と
        言い切って本当に新規キーだった場合の見落としを避ける)。
        """
        self._write_existing(".env", "TOKEN=abcdefghijklmnop\n")
        envelope = _make_envelope(
            "Edit", str(Path(self.tmp) / ".env"), self.tmp,
        )
        envelope["tool_input"]["new_string"] = "TOKEN=zzz\n"
        from handlers import edit_handler

        with mock.patch.object(
            edit_handler, "render_for_bash",
            return_value=(None, None, "open_failed", ""),
        ):
            reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("追加予定のキー名", reason)
        self.assertNotIn("既存キーの値の更新", reason)

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


class TestOverwriteToolAxis(BaseEdit):
    """``overwrite`` の代替案は **tool 軸 × format 軸**で決まる (PR #47 Codex P2)。

    ``kind`` (書き込み先の状態) と tool (Edit / Write) は直交する。0.20.0 の
    初版は ``kind`` だけで文面を決めていたため、**Edit にも「ファイル全体の
    上書きは現在の値を失う」「patch にしろ」と書いて**いた。Edit は既に対象を
    絞った置換なので事実と違う。

    軸が 2 つになったので、**組み合わせを漏らさず**固定する。
    """

    # (tool, is_dotenv) → (必ず含む, 必ず含まない)
    CASES = {
        ("Write", True): (
            ["Write はファイル全体を置き換えるため、現在の値はすべて失われます。",
             "dotenv-cli の merge 機能"],
            ["Edit は対象を絞った置換", "差分適用 (patch)"],
        ),
        ("Write", False): (
            ["Write はファイル全体を置き換えるため、現在の値はすべて失われます。",
             "差分適用 (patch)"],
            ["Edit は対象を絞った置換", "dotenv-cli"],
        ),
        ("Edit", True): (
            ["Edit は対象を絞った置換なのでファイル全体は失われませんが、",
             "dotenv-cli の merge 機能"],
            ["ファイル全体を置き換えるため", "差分適用 (patch)"],
        ),
        ("Edit", False): (
            ["Edit は対象を絞った置換なのでファイル全体は失われませんが、",
             "差分適用 (patch)"],
            ["ファイル全体を置き換えるため", "dotenv-cli"],
        ),
    }

    def test_overwrite_suggestion_matrix(self):
        for (tool, is_dotenv), (must, must_not) in self.CASES.items():
            with self.subTest(tool=tool, dotenv=is_dotenv):
                name = ".env" if is_dotenv else "credentials.json"
                body = "FOO=bar\n" if is_dotenv else '{"a": "b"}'
                (Path(self.tmp) / name).write_text(body)
                envelope = _make_envelope(
                    tool, str(Path(self.tmp) / name), self.tmp,
                )
                if tool == "Edit":
                    envelope["tool_input"]["new_string"] = body
                else:
                    envelope["tool_input"]["content"] = body
                r = handle(envelope, tool_label=tool)
                reason = _reason(r)
                # 判定は tool にも format にも依らず deny 固定
                self.assertEqual(_decision(r), "deny")
                for s in must:
                    self.assertIn(s, reason)
                for s in must_not:
                    self.assertNotIn(s, reason)

    def test_unknown_tool_label_uses_neutral_clause(self):
        """``handle()`` 既定の ``"Edit/Write"`` では tool 中立の clause になる。"""
        from core import messages as M

        msg = M.edit_deny(
            "Edit/Write", ".env", kind="overwrite", is_dotenv=True,
        )
        self.assertIn("既存の機密ファイルへの書き込みは block 固定です。", msg)
        self.assertNotIn("Write はファイル全体を置き換える", msg)
        self.assertNotIn("Edit は対象を絞った置換", msg)

    def test_tool_axis_applies_only_to_overwrite(self):
        """``new`` / ``symlink`` / ``special`` は tool で文面が変わらない。

        書き換え方の違いが意味を持つのは既存ファイルがあるときだけなので、
        軸を増やすのは ``overwrite`` に限定している。
        """
        from core import messages as M

        for kind in ("new", "symlink", "special"):
            with self.subTest(kind=kind):
                a = M.edit_deny("Edit", ".env", kind=kind, is_dotenv=True)
                b = M.edit_deny("Write", ".env", kind=kind, is_dotenv=True)
                # tool_label が入る note 行以外は一致する
                self.assertEqual(
                    a.split("\n")[1:], b.split("\n")[1:],
                )


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
        """0.26.0: status 別のラベル + render 失敗専用の next action。

        旧文言「Read tool に絶対パスを渡してください (Read も block されますが
        同じ minimal info が返ります)」は 2 重に事実と違った: Read も同じ理由
        で失敗するので情報は増えず、しかも verdict は (bypass 以外) block では
        なく ask になる。新文言はどちらも主張しない。
        """
        from handlers import edit_handler

        with mock.patch.object(
            edit_handler, "render_for_bash",
            return_value=(None, None, "open_failed", ""),
        ):
            r = self._run()
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn(
            "minimal info: unavailable (安全な open に失敗した (権限 / symlink 検知))",
            reason,
        )
        self.assertIn("同じ理由で失敗し、情報は返りません", reason)
        self.assertIn("ファイルの権限・実体を確認してください", reason)
        # 旧文言の事実誤認 (block / 同じ minimal info が返る) を主張しない
        self.assertNotIn("Read tool に", reason)
        self.assertNotIn("block されますが", reason)
        # 除外案内は従来どおり残る
        self.assertIn("patterns.local.txt", reason)

    def test_render_failure_status_selects_matching_label(self):
        """0.26.0: status ごとに異なるラベルが出る (1 種類に潰れない)。"""
        from handlers import edit_handler

        cases = {
            "unresolved": "既存ファイルが見つからない",
            "not_regular": "通常ファイルではない",
            "stat_failed": "ファイル状態の確認に失敗した",
            "redact_failed": "内容の解析に失敗した",
            "normalize_failed": "パスの正規化に失敗した",
        }
        for status, expect in cases.items():
            with self.subTest(status=status):
                with mock.patch.object(
                    edit_handler, "render_for_bash",
                    return_value=(None, None, status, ""),
                ):
                    reason = _reason(self._run())
                self.assertIn(expect, reason)
                # render 失敗系は一律で新しい next action を使う
                self.assertIn("同じ理由で失敗し、情報は返りません", reason)

    def test_render_exception_still_denies(self):
        """``render_for_bash`` が例外を投げても deny のまま (verdict 不変)。

        status が取れない (例外) ケースは既知の kind に該当しないため、
        従来どおりの汎用ラベルにフォールバックしつつ、next action だけは
        0.26.0 の修正後 (render 失敗系) の文言になる。
        """
        from handlers import edit_handler
        from core import logging as L

        with mock.patch.object(
            edit_handler, "render_for_bash",
            side_effect=RuntimeError("simulated"),
        ):
            with mock.patch.object(L, "log_error") as mock_log:
                r = self._run()
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertIn(
            "minimal info: unavailable (既存ファイルの読み取り / 解析に失敗)",
            reason,
        )
        self.assertIn("同じ理由で失敗し、情報は返りません", reason)
        mock_log.assert_any_call(
            "edit_existing_render_failed", "RuntimeError",
        )

    def test_base_exception_propagates(self):
        """KeyboardInterrupt / SystemExit は握り潰さない。

        ``_extract_dotenv_keys`` 側の同名保証
        (``TestDotenvParseFailureLogged::test_unexpected_exception_propagates``)
        と対にする。``except Exception`` は verdict 不変のための捕捉であって、
        中断シグナルまで飲み込む意図ではない。
        """
        from handlers import edit_handler

        with mock.patch.object(
            edit_handler, "render_for_bash",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                edit_handler._render_existing(
                    Path(self.tmp) / ".env", self.tmp,
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
        self.assertRegex(reason, r"  \.\.\. \(\d+ more lines")
        # 畳んだ場合でも Read tool への誘導が付く (Read なら exclude
        # hint 等の固定行を持たないぶん、より多くの key 行を確認できる)
        self.assertIn("use Read tool with the absolute path", reason)
        self.assertIn("</DATA>", reason)
        # 折り畳んでも末尾の per-format note (「実値は無い」の免責事項)
        # を優先して保護する (0.26.0 以前は閉じタグだけ保護し note を失っていた)
        self.assertIn("real values are not in context", reason)
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

    def test_large_existing_file_keeps_data_wrapper_intact(self):
        """32KB 超の既存ファイル (``redact_large_file`` 経路) でも包装が壊れない。

        ``MAX_INLINE_BYTES`` を超えると ``_render_path`` は
        ``format_dotenv`` + ``build_reason`` ではなく ``redact_large_file``
        (streaming 鍵名スキャン) に切り替わる。``_fit_data_block`` の閉じタグ
        保持は最終行が ``</DATA>`` であることに依存しているので、**別 renderer
        でも同じ形で終わる**ことを固定する (崩れると閉じない ``<DATA>`` が
        大きいファイルのときだけ静かに出る)。
        """
        body = "".join(
            f"BIG_KEY_{i}=value_that_is_long_enough_{i}\n" for i in range(1500)
        )
        target = Path(self.tmp) / ".env"
        target.write_text(body)
        self.assertGreater(target.stat().st_size, 32 * 1024)

        envelope = _make_envelope("Edit", str(target), self.tmp)
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        r = handle(envelope, tool_label="Edit")
        reason = _reason(r)
        self.assertEqual(_decision(r), "deny")
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES,
        )
        # 開き / 閉じが 1 組で揃う (畳んでも閉じタグを落とさない)
        self.assertEqual(reason.count("<DATA"), 1)
        self.assertEqual(reason.count("</DATA>"), 1)
        # large 経路であることの確認 + 実値は出ない
        self.assertIn("keys-only scan", reason)
        self.assertIn("BIG_KEY_0", reason)
        self.assertNotIn("value_that_is_long_enough", reason)
        # 末尾は除外案内のまま
        self.assertTrue(reason.split("\n")[-1].startswith("suggestion:"))
        self.assertIn("patterns.local.txt", reason)

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


class TestOverwriteReadIsBounded(BaseEdit):
    """E6 が持ち込んだ「描画のための読み取り」に実効的な byte 上限があること。

    背景 (PR #47 Codex P1): 0.19.1 まで Edit/Write の deny 経路は ``lstat``
    しかせず、ファイルを読まなかった。E6 で ``regular`` のとき
    ``render_for_bash`` を呼ぶようになり、32KB 超では
    ``keyonly_scan.scan_stream`` に到達する。同関数は ``readline()`` で読んで
    いたため、**改行を含まない巨大レコード 1 本で公称 1MB の上限を突破**した
    (8MB の 1 行で実測 8MB 読み込み)。hook は 2 秒 timeout で outer timeout が
    fail-open しうるため、情報提供のための描画がガードレール自体を落としうる
    状態だった。

    時間そのものを assert すると flaky になるので、**読み取り byte 数を観測**
    する形にしている。
    """

    def _read_bytes_via_edit(self, body: bytes) -> tuple[str, int]:
        """Edit deny を 1 回起こし、(decision, scan_stream が読んだ byte 数)。"""
        from redaction import keyonly_scan

        target = Path(self.tmp) / ".env"
        target.write_bytes(body)
        observed: list[int] = []
        real_scan = keyonly_scan.scan_stream

        def _spy(f, **kwargs):
            keys, n = real_scan(f, **kwargs)
            observed.append(n)
            return keys, n

        envelope = _make_envelope("Edit", str(target), self.tmp)
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        with mock.patch.object(keyonly_scan, "scan_stream", _spy):
            # engine は from-import しているのでそちらも差し替える
            from redaction import engine
            with mock.patch.object(engine, "scan_stream", _spy):
                r = handle(envelope, tool_label="Edit")
        return _decision(r), (observed[0] if observed else 0)

    def test_record_without_newline_does_not_exceed_cap(self):
        """改行を 1 つも含まない 8MB のレコードでも 1MB を超えて読まない。"""
        from redaction.keyonly_scan import scan_stream  # noqa: F401

        decision, read_bytes = self._read_bytes_via_edit(
            b"PATHOLOGICAL_KEY=" + b"x" * (8 * 1024 * 1024),
        )
        # verdict は E6 以前と同じ deny 固定
        self.assertEqual(decision, "deny")
        self.assertGreater(read_bytes, 0, "描画経路に到達していない")
        self.assertLessEqual(read_bytes, 1024 * 1024)

    def test_normal_large_file_still_renders_within_cap(self):
        """32KB 超の正常なファイルは従来どおり鍵名が出て、上限内に収まる。

        読み取り byte 数そのものは chunk 境界 (``_CHUNK_BYTES``) で切り上がる
        ので厳密値は pin しない。「上限内であること」と「情報が落ちていない
        こと」を見る。
        """
        body = b"".join(
            b"BIG_KEY_%d=value_that_is_long_enough_%d\n" % (i, i)
            for i in range(1500)
        )
        self.assertGreater(len(body), 32 * 1024)
        decision, read_bytes = self._read_bytes_via_edit(body)
        self.assertEqual(decision, "deny")
        self.assertGreater(read_bytes, 0, "描画経路に到達していない")
        self.assertLessEqual(read_bytes, 1024 * 1024)

        # 鍵名は従来どおり返る (上限を入れて情報が消えていないこと)
        target = Path(self.tmp) / ".env"
        target.write_bytes(body)
        envelope = _make_envelope("Edit", str(target), self.tmp)
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"
        reason = _reason(handle(envelope, tool_label="Edit"))
        self.assertIn("keys-only scan", reason)
        self.assertIn("BIG_KEY_0", reason)
        self.assertNotIn("value_that_is_long_enough", reason)

    def test_verdict_unchanged_when_render_is_skipped_entirely(self):
        """描画を丸ごと落としても verdict は動かない (minimal info 非依存)。

        「予算を強制できないなら描画しない」に倒したときの安全性 = E6 を無効に
        したときと同じ、であることの確認。
        """
        from handlers import edit_handler

        target = Path(self.tmp) / ".env"
        target.write_bytes(b"K=" + b"y" * (4 * 1024 * 1024))
        envelope = _make_envelope("Edit", str(target), self.tmp)
        envelope["tool_input"]["new_string"] = "NEW_KEY=1\n"

        with_render = _decision(handle(envelope, tool_label="Edit"))
        with mock.patch.object(
            edit_handler, "_render_existing", return_value=("", None, ""),
        ):
            without_render = _decision(handle(envelope, tool_label="Edit"))
        self.assertEqual(with_render, "deny")
        self.assertEqual(with_render, without_render)


class TestScanStreamBound(unittest.TestCase):
    """``scan_stream`` の byte 上限を直接固定する (0.20.0 の修正本体)。

    Edit 経路だけでなく Read / Bash の deny 描画も同じ関数を通るため、
    ここが上限の単一の担保になる。
    """

    def _scan(self, data: bytes, max_bytes: int = 1024 * 1024):
        import io
        from redaction.keyonly_scan import scan_stream

        return scan_stream(io.BytesIO(data), max_bytes=max_bytes)

    def test_single_record_larger_than_cap_is_bounded(self):
        keys, n = self._scan(b"KEY_A=" + b"x" * (8 * 1024 * 1024))
        self.assertLessEqual(n, 1024 * 1024)
        # 行頭にある鍵名は、全体を読まなくても拾える
        self.assertEqual(keys, ["KEY_A"])

    def test_cap_is_never_exceeded_for_various_shapes(self):
        cases = {
            "no_newline_at_all": b"A=" + b"z" * 300_000,
            "one_huge_line_then_more": b"A=" + b"z" * 300_000 + b"\nB=2\n",
            "many_huge_lines": (b"X=1\n" + b"Y=" + b"z" * 200_000 + b"\n") * 5,
            "all_newlines": b"\n" * 200_000,
        }
        for name, data in cases.items():
            with self.subTest(shape=name):
                _keys, n = self._scan(data, max_bytes=64 * 1024)
                self.assertLessEqual(n, 64 * 1024)

    def test_final_line_without_newline_is_still_scanned(self):
        keys, _n = self._scan(b"A=1\nB=2\nC=3")
        self.assertEqual(keys, ["A", "B", "C"])

    def test_keys_after_a_huge_record_are_still_found(self):
        """巨大レコードを読み飛ばしても後続行の鍵名を拾えること。"""
        data = b"FIRST=1\n" + b"BIG=" + b"z" * 5000 + b"\n" + b"LAST=3\n"
        keys, n = self._scan(data)
        self.assertEqual(keys, ["FIRST", "BIG", "LAST"])
        self.assertEqual(n, len(data))

    def test_memory_does_not_scale_with_record_length(self):
        """レコード長を 8 倍にしても peak allocation がほぼ変わらないこと。"""
        import tracemalloc

        def peak_for(size: int) -> int:
            data = b"K=" + b"q" * size
            tracemalloc.start()
            self._scan(data)
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            return peak

        small = peak_for(512 * 1024)
        large = peak_for(4 * 1024 * 1024)
        # BytesIO 自体のコピー分があるので厳密比較はしない。レコード長に比例
        # (8 倍) していないことだけを見る。
        self.assertLess(large, small * 4)


if __name__ == "__main__":
    unittest.main()
