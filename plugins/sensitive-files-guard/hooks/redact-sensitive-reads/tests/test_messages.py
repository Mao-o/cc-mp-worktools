"""core/messages.py の builder 単体テスト (M1 / H1 / H3)。

各 builder が:
- 必須の情報 (operand / basename) を文中に含めること
- ``!<basename>`` 案内に **実 basename を展開**して埋め込むこと
- dotenv 系の代替案 (``.env.example``) や extra_note を所定の位置に置くこと
を保証する。

文言の細部 (動詞ルール) は M2 / M4 で再調整するため、ここでは情報伝達の
本質要件のみ検証する。
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from core import messages as M


class TestExcludeHintBasename(unittest.TestCase):
    """``_exclude_hint`` が basename を実展開していることの保証。"""

    def test_exclude_hint_with_basename(self):
        out = M._exclude_hint(".env")
        self.assertIn("`!.env`", out)
        self.assertIn("patterns.local.txt", out)
        self.assertNotIn("<basename>", out)

    def test_exclude_hint_without_basename(self):
        out = M._exclude_hint("")
        # basename が無いケースは plain プレースホルダを出す
        self.assertIn("<basename>", out)

    def test_exclude_hint_strips_backtick(self):
        # backtick が name に混じっていても markdown を壊さない
        out = M._exclude_hint(".env`evil")
        self.assertNotIn(".env`evil", out)
        self.assertIn(".envevil", out)


class TestBashDenyLiteral(unittest.TestCase):
    def test_basic(self):
        msg = M.bash_deny(first_token="cat", operand=".env", kind="literal")
        # 必須情報
        self.assertIn("cat", msg)
        self.assertIn(".env", msg)
        # H3: basename 展開
        self.assertIn("`!.env`", msg)
        # 種別の表現
        self.assertIn("operand", msg)

    def test_path_operand_basename_extraction(self):
        msg = M.bash_deny(
            first_token="head", operand="/abs/path/to/.env", kind="literal"
        )
        self.assertIn("/abs/path/to/.env", msg)
        # basename のみ案内に出る
        self.assertIn("`!.env`", msg)
        # フル path はそのまま `!...` には埋めない
        self.assertNotIn("`!/abs/path/to/.env`", msg)


class TestBashDenyGlob(unittest.TestCase):
    def test_basic(self):
        msg = M.bash_deny(first_token="cat", operand="*.env*", kind="glob")
        self.assertIn("cat", msg)
        self.assertIn("*.env*", msg)
        self.assertIn("`!*.env*`", msg)
        self.assertIn("glob", msg)


class TestBashDenyInputRedirect(unittest.TestCase):
    def test_literal(self):
        msg = M.bash_deny(
            first_token="", operand=".env", kind="input_redirect"
        )
        self.assertIn(".env", msg)
        self.assertIn("`!.env`", msg)
        self.assertIn("リダイレクト", msg)

    def test_glob(self):
        msg = M.bash_deny(
            first_token="", operand=".env*", kind="input_redirect_glob"
        )
        self.assertIn(".env*", msg)
        self.assertIn("`!.env*`", msg)
        self.assertIn("リダイレクト", msg)


class TestBashDenyInputRedirectForm(unittest.TestCase):
    """0.5.0 / M5: ``bash_deny`` に ``form`` 引数を渡すと SFG_DENY body に
    ``form: <値>`` 行が追加される。

    form 値の優先順位 (fd_prefixed > no_space > quoted > bare) は parser 側
    (``_classify_redirect_form`` / ``test_input_redirect.py``) で確認済み。
    本クラスは builder が caller の form 引数を素直に body に反映するかだけを
    単独で確認する (各 form 値ごと 1 ケース)。
    """

    def test_input_redirect_with_bare_form(self):
        msg = M.bash_deny(
            first_token="", operand=".env", kind="input_redirect", form="bare"
        )
        self.assertIn("form: bare", msg)

    def test_input_redirect_with_fd_prefixed_form(self):
        msg = M.bash_deny(
            first_token="",
            operand=".env",
            kind="input_redirect",
            form="fd_prefixed",
        )
        self.assertIn("form: fd_prefixed", msg)

    def test_input_redirect_with_no_space_form(self):
        msg = M.bash_deny(
            first_token="",
            operand=".env",
            kind="input_redirect",
            form="no_space",
        )
        self.assertIn("form: no_space", msg)

    def test_input_redirect_with_quoted_form(self):
        msg = M.bash_deny(
            first_token="",
            operand=".env",
            kind="input_redirect",
            form="quoted",
        )
        self.assertIn("form: quoted", msg)

    def test_input_redirect_glob_with_form(self):
        msg = M.bash_deny(
            first_token="",
            operand=".env*",
            kind="input_redirect_glob",
            form="bare",
        )
        self.assertIn("form: bare", msg)

    def test_form_omitted_no_form_line(self):
        # form 引数省略 (default None) → body に form 行を出さない
        msg = M.bash_deny(
            first_token="", operand=".env", kind="input_redirect"
        )
        self.assertNotIn("form:", msg)

    def test_literal_kind_default_no_form_line(self):
        # literal / glob kind では caller (bash_handler) は form を渡さない設計
        msg = M.bash_deny(
            first_token="cat", operand=".env", kind="literal"
        )
        self.assertNotIn("form:", msg)

    def test_form_line_position_before_suggestion(self):
        # form 行は suggestion 行より前に出る (body 行順序の保証)
        msg = M.bash_deny(
            first_token="",
            operand=".env",
            kind="input_redirect",
            form="quoted",
        )
        form_pos = msg.find("form:")
        suggestion_pos = msg.find("suggestion:")
        self.assertGreater(form_pos, 0)
        self.assertGreater(suggestion_pos, 0)
        self.assertLess(form_pos, suggestion_pos)


class TestEditDeny(unittest.TestCase):
    def test_minimal_no_keys(self):
        msg = M.edit_deny("Edit", ".env", new_keys=None)
        self.assertIn("Edit", msg)
        self.assertIn(".env", msg)
        self.assertIn("`!.env`", msg)
        # block と書く方針 (M2 で再検討)
        self.assertIn("block", msg)

    def test_with_dotenv_keys(self):
        msg = M.edit_deny(
            "Write",
            ".env",
            new_keys=["DATABASE_URL", "JWT_SECRET", "DEBUG"],
        )
        self.assertIn("Write", msg)
        # キー名がそれぞれ別行で出る
        self.assertIn("DATABASE_URL=", msg)
        self.assertIn("JWT_SECRET=", msg)
        self.assertIn("DEBUG=", msg)
        # 代替案として .env.example 案内
        self.assertIn(".env.example", msg)
        # basename 展開
        self.assertIn("`!.env`", msg)

    def test_with_extra_note_no_keys(self):
        msg = M.edit_deny(
            "Edit", ".env", new_keys=None, extra_note="NOTE: symlink でした。"
        )
        self.assertIn("symlink", msg)
        self.assertIn(".env", msg)

    def test_with_extra_note_and_keys(self):
        msg = M.edit_deny(
            "MultiEdit",
            ".env",
            new_keys=["FOO"],
            extra_note="NOTE: 特殊ファイルでした。",
        )
        self.assertIn("FOO=", msg)
        self.assertIn("特殊ファイル", msg)

    def test_truncation_marker_for_many_keys(self):
        keys = [f"KEY_{i}" for i in range(40)]
        msg = M.edit_deny("Edit", ".env", new_keys=keys, max_suggested_keys=30)
        self.assertIn("KEY_0=", msg)
        self.assertIn("KEY_29=", msg)
        # 30 個以上は切り詰め
        self.assertNotIn("KEY_30=", msg)
        self.assertIn("(10 more)", msg)


class TestPolicyUnavailable(unittest.TestCase):
    """M3: patterns.txt 読込失敗時の reason 文。"""

    def test_deny_severity_for_bash(self):
        msg = M.policy_unavailable("deny")
        self.assertIn("patterns.txt", msg)
        self.assertIn("Bash", msg)
        # H2: 動詞 "block しました" を採用
        self.assertIn("block しました", msg)
        # LLM が取れる action として「設定を確認」を含む
        self.assertIn("設定を確認", msg)
        # 「管理者に連絡してください」は LLM が取れない指示なので削除済み
        self.assertNotIn("管理者", msg)

    def test_pause_severity_default(self):
        msg = M.policy_unavailable("pause")
        self.assertIn("patterns.txt", msg)
        # H2: 動詞 "再試行してください" (ask_or_deny 系の next action)
        self.assertIn("再試行", msg)
        self.assertNotIn("管理者", msg)

    def test_pause_with_tool_label(self):
        msg = M.policy_unavailable("pause", tool_label="Edit")
        self.assertTrue(msg.startswith("Edit:"))


class TestReadAsk(unittest.TestCase):
    """M2: Read handler の judgement-pause reason 文。"""

    def test_symlink(self):
        msg = M.read_ask("symlink")
        self.assertIn("symlink", msg)
        # 「続行しますか？」(人間 UI 語) は使わない
        self.assertNotIn("続行しますか", msg)
        # LLM が取れる next action
        self.assertIn("再試行", msg)

    def test_special(self):
        msg = M.read_ask("special")
        self.assertIn("FIFO", msg)
        self.assertNotIn("続行しますか", msg)
        self.assertIn("再試行", msg)

    def test_io_error(self):
        msg = M.read_ask("io_error")
        self.assertIn("権限", msg)
        self.assertIn("再試行", msg)

    def test_normalize_failed(self):
        msg = M.read_ask("normalize_failed")
        self.assertIn("正規化", msg)
        self.assertIn("再試行", msg)

    def test_redaction_failed(self):
        msg = M.read_ask("redaction_failed")
        self.assertIn("redaction", msg)

    def test_open_failed(self):
        msg = M.read_ask("open_failed")
        self.assertIn("symlink race", msg)
        self.assertIn("再試行", msg)


class TestEditPause(unittest.TestCase):
    """Edit/Write/MultiEdit の judgement-pause reason 文。"""

    def test_normalize_failed_default_label(self):
        msg = M.edit_pause("normalize_failed")
        self.assertTrue(msg.startswith("Edit/Write:"))
        self.assertIn("正規化", msg)
        self.assertIn("再試行", msg)

    def test_io_error_with_label(self):
        msg = M.edit_pause("io_error", tool_label="MultiEdit")
        self.assertTrue(msg.startswith("MultiEdit:"))
        self.assertIn("権限", msg)

    def test_parent_not_directory(self):
        msg = M.edit_pause("parent_not_directory", tool_label="Edit")
        self.assertIn("親ディレクトリ", msg)
        # H2: ask_or_deny 系の next action
        self.assertIn("再試行", msg)


class TestBashLenient(unittest.TestCase):
    """H2: Bash の静的解析不能ケース (ask_or_allow) の reason 文。"""

    LENIENT_SUFFIX = "判定不能のため確認を挟みます"

    def test_hard_stop(self):
        msg = M.bash_lenient("hard_stop")
        self.assertIn("動的展開", msg)
        # H2: 共通 suffix
        self.assertIn(self.LENIENT_SUFFIX, msg)
        # autonomous モードで通過する旨を文中で明示
        self.assertIn("auto", msg)
        self.assertIn("plan", msg)
        self.assertIn("bypass", msg)

    def test_opaque_prefix(self):
        msg = M.bash_lenient("opaque_prefix")
        self.assertIn("wrapper", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_residual_metachar(self):
        msg = M.bash_lenient("residual_metachar")
        self.assertIn("metachar", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_shell_keyword_with_detail(self):
        msg = M.bash_lenient("shell_keyword", detail="if")
        self.assertIn("予約語", msg)
        self.assertIn("(if)", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_shell_keyword_without_detail(self):
        # detail 省略でも壊れない
        msg = M.bash_lenient("shell_keyword")
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_tokenize_failed(self):
        msg = M.bash_lenient("tokenize_failed")
        self.assertIn("tokenize", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_normalize_failed(self):
        msg = M.bash_lenient("normalize_failed")
        self.assertIn("正規化", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)


class TestHookErrorMessages(unittest.TestCase):
    """__main__ wrapper 系の reason 文。LLM が取れる action を明示する。"""

    def test_hook_invocation_error(self):
        msg = M.hook_invocation_error()
        self.assertIn("settings.json", msg)
        # 旧文言「管理者に連絡してください」は LLM が取れる action ではない
        self.assertNotIn("管理者", msg)

    def test_stdin_parse_failed(self):
        msg = M.stdin_parse_failed()
        self.assertIn("hook", msg)
        self.assertIn("envelope", msg)
        # 「安全側で deny します」のような不要な揺れ表現を含めない
        self.assertNotIn("安全側で deny", msg)

    def test_unsupported_platform(self):
        msg = M.unsupported_platform()
        self.assertIn("UNIX", msg)
        self.assertIn("README", msg)

    def test_handler_internal_error_with_type(self):
        msg = M.handler_internal_error("bash", "ValueError")
        self.assertIn("bash", msg)
        self.assertIn("ValueError", msg)
        # ログファイルへの導線を明示
        self.assertIn("redact-hook.log", msg)

    def test_handler_internal_error_without_type(self):
        msg = M.handler_internal_error("read")
        self.assertIn("read", msg)
        self.assertIn("redact-hook.log", msg)


class TestSfgDenyEnvelope(unittest.TestCase):
    """M4: deny 系 reason は ``<SFG_DENY>`` 構造化包装で出ること。

    - 外殻の ``tool=`` ``reason=`` ``guard="sfg-v1"`` 属性
    - 開きと閉じタグの完整性
    - body の各行が key: value 形式で並ぶこと
    - 外殻破壊耐性 (body に ``</SFG_DENY>`` が混入しても外殻が壊れない)
    """

    def _assert_envelope(self, msg: str, tool: str, reason: str) -> None:
        """共通: 外殻属性と閉じタグを検証。"""
        self.assertTrue(
            msg.startswith(
                f'<SFG_DENY tool="{tool}" reason="{reason}" guard="sfg-v1">\n'
            ),
            msg=f"opening tag mismatch in: {msg!r}",
        )
        self.assertTrue(
            msg.rstrip().endswith("</SFG_DENY>"),
            msg=f"closing tag missing in: {msg!r}",
        )

    def test_bash_deny_literal_envelope(self):
        msg = M.bash_deny(first_token="cat", operand=".env", kind="literal")
        self._assert_envelope(msg, "Bash", "literal")
        self.assertIn("note:", msg)
        self.assertIn("matched_operand: .env", msg)
        self.assertIn("first_token: cat", msg)
        self.assertIn("suggestion:", msg)
        self.assertIn("`!.env`", msg)

    def test_bash_deny_glob_envelope(self):
        msg = M.bash_deny(first_token="cat", operand="*.env*", kind="glob")
        self._assert_envelope(msg, "Bash", "glob")
        self.assertIn("matched_operand: *.env*", msg)
        self.assertIn("first_token: cat", msg)

    def test_bash_deny_input_redirect_envelope(self):
        msg = M.bash_deny(first_token="", operand=".env", kind="input_redirect")
        self._assert_envelope(msg, "Bash", "input_redirect")
        self.assertIn("matched_operand: .env", msg)
        # first_token が空のときは body に出さない
        self.assertNotIn("first_token:", msg)

    def test_bash_deny_input_redirect_glob_envelope(self):
        msg = M.bash_deny(
            first_token="", operand=".env*", kind="input_redirect_glob"
        )
        self._assert_envelope(msg, "Bash", "input_redirect_glob")

    def test_edit_deny_default_kind_envelope(self):
        msg = M.edit_deny("Edit", ".env")
        self._assert_envelope(msg, "Edit", "sensitive_path")
        self.assertIn("basename: .env", msg)
        self.assertIn("suggestion:", msg)

    def test_edit_deny_symlink_kind_envelope(self):
        msg = M.edit_deny(
            "Edit", ".env",
            extra_note="NOTE: symlink 経由だったため",
            kind="sensitive_path_symlink",
        )
        self._assert_envelope(msg, "Edit", "sensitive_path_symlink")
        self.assertIn("extra_note:", msg)

    def test_edit_deny_special_kind_envelope(self):
        msg = M.edit_deny(
            "Write", ".env",
            extra_note="NOTE: 非通常ファイル",
            kind="sensitive_path_special",
        )
        self._assert_envelope(msg, "Write", "sensitive_path_special")

    def test_edit_deny_with_keys_envelope(self):
        msg = M.edit_deny("MultiEdit", ".env", new_keys=["A", "B"])
        self._assert_envelope(msg, "MultiEdit", "sensitive_path")
        self.assertIn("suggested_keys:", msg)
        self.assertIn("  A=", msg)
        self.assertIn("  B=", msg)
        self.assertIn("suggestion_alt:", msg)
        self.assertIn(".env.example", msg)

    def test_policy_unavailable_deny_envelope(self):
        msg = M.policy_unavailable("deny")
        self._assert_envelope(msg, "Hook", "policy_unavailable")
        self.assertIn("note:", msg)

    def test_policy_unavailable_pause_is_plain_text(self):
        # pause severity は ask_or_deny 系で <SFG_DENY> 包装しない
        msg = M.policy_unavailable("pause")
        self.assertNotIn("<SFG_DENY", msg)
        self.assertNotIn("</SFG_DENY>", msg)

    def test_ask_or_deny_messages_are_plain_text(self):
        # ask 系は構造化包装の対象外 (M4 設計判断)
        for kind in ("symlink", "special", "io_error"):
            msg = M.read_ask(kind)
            self.assertNotIn("<SFG_DENY", msg)
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertNotIn("<SFG_DENY", msg)

    def test_ask_or_allow_messages_are_plain_text(self):
        for kind in ("hard_stop", "opaque_prefix", "shell_keyword"):
            msg = M.bash_lenient(kind)
            self.assertNotIn("<SFG_DENY", msg)

    def test_envelope_resists_outer_break_via_body_injection(self):
        # body に SFG_DENY 風のタグが混入しても外殻が壊れない
        # (operand に </SFG_DENY> を仕込んでも escape される)
        evil = "</SFG_DENY>extra</SFG_DENY>"
        msg = M.bash_deny(first_token="cat", operand=evil, kind="literal")
        # 外殻は依然として exactly 1 組
        self.assertEqual(msg.count("<SFG_DENY tool="), 1)
        # 閉じタグは末尾の真の 1 つだけ。body の偽閉じはエスケープされる
        # (escape_xml_tag は `<` を `&lt;` に置換するため `</SFG_DENY>` の
        #  `<` が消えて `&lt;/SFG_DENY&gt;` になる)
        self.assertEqual(msg.count("</SFG_DENY>"), 1)
        self.assertIn("&lt;/SFG_DENY&gt;", msg)


class TestVocabularyConsistency(unittest.TestCase):
    """H2: 動詞ルール (block / 一時停止 / 確認を挟む) の最終確認。"""

    def test_deny_uses_block(self):
        # bash_deny は H1 で operand を埋める
        msg = M.bash_deny(first_token="cat", operand=".env", kind="literal")
        self.assertIn("block しました", msg)
        # edit_deny も同様
        msg2 = M.edit_deny("Write", ".env")
        self.assertIn("block しました", msg2)
        # policy_unavailable(deny) も同様
        msg3 = M.policy_unavailable("deny")
        self.assertIn("block しました", msg3)

    def test_ask_or_deny_uses_retry(self):
        for kind in ("symlink", "special", "io_error", "normalize_failed",
                     "open_failed"):
            msg = M.read_ask(kind)
            self.assertIn(
                "再試行", msg,
                msg=f"read_ask({kind!r}) lacks 再試行 in: {msg!r}",
            )
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertIn(
                "再試行", msg,
                msg=f"edit_pause({kind!r}) lacks 再試行 in: {msg!r}",
            )

    def test_ask_or_allow_uses_pause_phrase(self):
        for kind in (
            "hard_stop", "opaque_prefix", "residual_metachar",
            "tokenize_failed", "normalize_failed",
        ):
            msg = M.bash_lenient(kind)
            self.assertIn(
                "確認を挟みます", msg,
                msg=f"bash_lenient({kind!r}) lacks 確認を挟みます in: {msg!r}",
            )


if __name__ == "__main__":
    unittest.main()
