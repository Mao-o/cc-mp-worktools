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
