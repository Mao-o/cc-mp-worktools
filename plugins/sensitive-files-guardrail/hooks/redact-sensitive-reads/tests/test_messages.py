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

import os
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from _shared.patterns import (
    PROJECT_SECTION_HEADER_HINT,
    exclude_recipe_lines,
)
from core import messages as M
from core import output


class TestExcludeHintBasename(unittest.TestCase):
    """``_exclude_hint`` が basename を実展開していることの保証。"""

    def test_exclude_hint_with_basename(self):
        out = M._exclude_hint(".env")
        self.assertIn("`!.env`", out)
        self.assertIn("patterns.local.txt", out)
        self.assertNotIn("<basename>", out)

    def test_exclude_hint_guides_project_section_by_default(self):
        """0.19.0 (bd_092a232e-snw.23): 既定で [project:] セクション配下への追記を
        案内し、ヘッダー無し (全プロジェクト共通) は明示的な選択にする。
        絶対パスは reason に出さない (環境変数名で示す)。"""
        out = M._exclude_hint(".env")
        self.assertIn("`[project:$CLAUDE_PROJECT_DIR]`", out)
        self.assertIn("全プロジェクト共通", out)
        self.assertIn("ヘッダー無し", out)
        self.assertIn("承認なしに", out)
        self.assertNotIn(str(Path.home()), out)
        self.assertNotIn(os.getcwd(), out)

    def test_exclude_hint_without_basename_also_guides_project_section(self):
        out = M._exclude_hint("")
        self.assertIn("`[project:$CLAUDE_PROJECT_DIR]`", out)
        self.assertIn("<basename>", out)

    def test_exclude_hint_shares_header_with_shared_recipe(self):
        # Stop hook の block reason と同じ定数 (_shared.patterns) を使う
        out = M._exclude_hint(".env")
        self.assertIn(PROJECT_SECTION_HEADER_HINT, out)
        self.assertEqual(
            exclude_recipe_lines([".env", ".env", "", "ca.pem"]),
            [PROJECT_SECTION_HEADER_HINT, "!.env", "!ca.pem"],
        )

    def test_exclude_recipe_lines_caps_long_lists(self):
        lines = exclude_recipe_lines([f"k{i}.pem" for i in range(25)], limit=20)
        self.assertEqual(len(lines), 22)
        self.assertEqual(lines[1], "!k0.pem")
        self.assertEqual(lines[-1], "... (5 more)")

    def test_exclude_recipe_lines_is_linear_for_large_inputs(self):
        # 重複除去が list membership の二次計算だと 5 万件で Stop の 15s timeout
        # を超えうる (Codex R5 P2-2)。10 万件 (5 万 unique × 2) で 1 秒未満
        import time

        names = [f"k{i}.pem" for i in range(50_000)] * 2
        started = time.perf_counter()
        lines = exclude_recipe_lines(names, limit=20)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(lines), 22)
        self.assertEqual(lines[1], "!k0.pem")
        self.assertEqual(lines[-1], "... (49980 more)")
        self.assertLess(elapsed, 1.0)

    def test_exclude_hint_without_basename(self):
        out = M._exclude_hint("")
        # basename が無いケースは plain プレースホルダを出す
        self.assertIn("<basename>", out)

    def test_exclude_hint_strips_backtick(self):
        # backtick が name に混じっていても markdown を壊さない
        out = M._exclude_hint(".env`evil")
        self.assertNotIn(".env`evil", out)
        self.assertIn(".envevil", out)


class TestGitSubcommandOf(unittest.TestCase):
    """``_git_subcommand_of`` (0.19.0): history builder の subcommand 推定。"""

    def test_simple(self):
        self.assertEqual(
            M._git_subcommand_of("git show HEAD:.env", "HEAD:.env"), "show"
        )

    def test_skips_global_options(self):
        self.assertEqual(
            M._git_subcommand_of(
                "git -C /repo -c k=v --git-dir=/x --no-pager diff .env", ".env"
            ),
            "diff",
        )

    def test_prefers_window_containing_operand(self):
        self.assertEqual(
            M._git_subcommand_of("git log && git add .env", ".env"), "add"
        )

    def test_falls_back_to_first_git_without_operand_match(self):
        self.assertEqual(
            M._git_subcommand_of("git log && git status", ".env"), "log"
        )

    def test_opt_equals_value_operand_matches_window(self):
        # ``--pathspec-from-file=.env`` 由来の operand でも後段の git を選ぶ
        self.assertEqual(
            M._git_subcommand_of(
                "git log && git rm --cached --pathspec-from-file=.env", ".env"
            ),
            "rm",
        )

    def test_empty_command(self):
        self.assertEqual(M._git_subcommand_of("", ".env"), "")

    def test_unparseable_command_does_not_raise(self):
        # shlex 失敗 → 空白 split fallback。operate set に入らないので閲覧文面
        sub = M._git_subcommand_of("git 'unterminated .env", ".env")
        self.assertNotIn(sub, M._GIT_OPERATE_SUBCOMMANDS)


class TestBashDeny(unittest.TestCase):
    """0.7.0: kind / form 引数を撤廃し、operand と first_token のみで build する。"""

    def test_literal_basic(self):
        msg = M.bash_deny(first_token="cat", operand=".env")
        # 必須情報
        self.assertIn("cat", msg)
        self.assertIn(".env", msg)
        # H3: basename 展開
        self.assertIn("`!.env`", msg)
        # 種別の表現
        self.assertIn("operand", msg)
        # 0.7.0: GUARDRAIL_DENY 構造化包装は plain text に戻したため出ない
        self.assertNotIn("<GUARDRAIL_DENY", msg)

    def test_path_operand_basename_extraction(self):
        msg = M.bash_deny(first_token="head", operand="/abs/path/to/.env")
        self.assertIn("/abs/path/to/.env", msg)
        # basename のみ案内に出る
        self.assertIn("`!.env`", msg)
        # フル path はそのまま `!...` には埋めない
        self.assertNotIn("`!/abs/path/to/.env`", msg)

    def test_glob_operand_uses_same_template(self):
        msg = M.bash_deny(first_token="cat", operand="*.env*")
        self.assertIn("cat", msg)
        self.assertIn("*.env*", msg)
        self.assertIn("`!*.env*`", msg)

    def test_first_token_omitted_no_first_token_line(self):
        # first_token を空で渡すと body に first_token 行を出さない
        msg = M.bash_deny(first_token="", operand=".env")
        self.assertIn(".env", msg)
        self.assertNotIn("first_token:", msg)


class TestEditDeny(unittest.TestCase):
    def test_minimal_no_keys(self):
        msg = M.edit_deny("Edit", ".env", new_keys=None, kind="new")
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
            kind="new",
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
            "Edit", ".env", new_keys=None, extra_note="NOTE: symlink でした。",
            kind="symlink",
        )
        self.assertIn("symlink", msg)
        self.assertIn(".env", msg)

    def test_with_extra_note_and_keys(self):
        msg = M.edit_deny(
            "Write",
            ".env",
            new_keys=["FOO"],
            extra_note="NOTE: 特殊ファイルでした。",
            kind="special",
        )
        self.assertIn("FOO=", msg)
        self.assertIn("特殊ファイル", msg)

    def test_truncation_marker_for_many_keys(self):
        keys = [f"KEY_{i}" for i in range(40)]
        msg = M.edit_deny(
            "Edit", ".env", new_keys=keys, kind="new", max_suggested_keys=30,
        )
        self.assertIn("KEY_0=", msg)
        self.assertIn("KEY_29=", msg)
        # 30 個以上は切り詰め
        self.assertNotIn("KEY_30=", msg)
        self.assertIn("(10 more)", msg)

    def test_overwrite_without_render_reports_unavailable(self):
        """``existing_render`` が空なら黙って省略せず unavailable + next action。"""
        msg = M.edit_deny("Edit", ".env", kind="overwrite")
        self.assertIn(
            "minimal info: unavailable (既存ファイルの読み取り / 解析に失敗)",
            msg,
        )
        self.assertIn("Read tool に", msg)

    def test_overwrite_many_lines_are_folded_not_dropped(self):
        """行数が多いだけなら入る分を載せ、残りを ``... (N more lines)`` に畳む。"""
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env"]
            + [f"  {i}. KEY_{i}  <type=str>  <set>  length=8"
               for i in range(400)]
            + ["</DATA>"]
        )
        msg = M.edit_deny(
            "Edit", ".env", kind="overwrite", existing_render=render,
        )
        self.assertLessEqual(len(msg.encode("utf-8")), output.MAX_REASON_BYTES)
        self.assertIn("上書き対象の既存ファイル", msg)
        self.assertIn("  0. KEY_0", msg)
        self.assertRegex(msg, r"  \.\.\. \(\d+ more lines\)")
        # 閉じタグは畳んでも残す (外殻破壊防御の前提)
        self.assertTrue(msg.split("\n")[-1].startswith("suggestion:"))
        self.assertIn("</DATA>", msg)

    def test_overwrite_render_too_large_reports_budget_reason(self):
        """内容行が 1 行も入らないときは理由を「予算」と明示して 2 行に降りる。

        ``<DATA>`` の header 3 行だけを載せても情報量ゼロの包装が残るだけなので、
        ``_fit_data_block`` は空リストを返し unavailable 分岐に降ろす。
        """
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: " + "x" * 4000, "file: .env",
             "  1. FOO  <type=str>", "</DATA>"]
        )
        msg = M.edit_deny(
            "Edit", ".env", kind="overwrite", existing_render=render,
        )
        self.assertIn(
            "minimal info: unavailable (reason の byte 予算に収まらないため省略)",
            msg,
        )
        self.assertNotIn("<DATA", msg)
        self.assertLessEqual(len(msg.encode("utf-8")), output.MAX_REASON_BYTES)

    def test_overwrite_budget_exhausted_by_keys_omits_section(self):
        """``suggested_keys`` で予算を使い切ると minimal info を丸ごと落とす。

        末尾の除外案内を残す方を優先する設計 (``_edit_existing_info_lines``)。
        """
        keys = ["K" * 120 + str(i) for i in range(30)]
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env", "  1. FOO  <type=str>", "</DATA>"]
        )
        msg = M.edit_deny(
            "Write", ".env", new_keys=keys, kind="overwrite",
            existing_render=render,
        )
        self.assertNotIn("上書き対象の既存ファイル", msg)
        self.assertNotIn("minimal info: unavailable", msg)

    def test_kind_is_required_keyword(self):
        """E6: ``kind`` を省略すると TypeError (誤った文面の silent 選択を防ぐ)。

        既定値を置くと「新規作成しようとしたため block」という **事実と違う説明**
        を返しうるため、呼び忘れを実行時に落とす設計にしている。
        """
        with self.assertRaises(TypeError):
            M.edit_deny("Edit", ".env")  # type: ignore[call-arg]


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
    """Edit/Write の judgement-pause reason 文。"""

    def test_normalize_failed_default_label(self):
        msg = M.edit_pause("normalize_failed")
        self.assertTrue(msg.startswith("Edit/Write:"))
        self.assertIn("正規化", msg)
        self.assertIn("再試行", msg)

    def test_io_error_with_label(self):
        msg = M.edit_pause("io_error", tool_label="Write")
        self.assertTrue(msg.startswith("Write:"))
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


class TestDenyPlainText(unittest.TestCase):
    """0.7.0: deny 系 reason は plain text 出力 (GUARDRAIL_DENY 構造化包装を撤廃)。

    各 deny builder の戻り値が:
    - ``<GUARDRAIL_DENY`` / ``</GUARDRAIL_DENY>`` を含まないこと
    - ``note:`` / ``matched_operand:`` / ``first_token:`` / ``basename:`` /
      ``suggested_keys:`` / ``extra_note:`` / ``suggestion:`` の各行を必要に応じて
      含むこと
    """

    def test_bash_deny_no_envelope(self):
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertNotIn("</GUARDRAIL_DENY>", msg)
        self.assertIn("note:", msg)
        self.assertIn("matched_operand: .env", msg)
        self.assertIn("first_token: cat", msg)
        self.assertIn("suggestion:", msg)
        self.assertIn("`!.env`", msg)

    def test_edit_deny_no_envelope(self):
        msg = M.edit_deny("Edit", ".env", kind="new")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("basename: .env", msg)
        self.assertIn("suggestion:", msg)

    def test_edit_deny_with_keys_no_envelope(self):
        msg = M.edit_deny("Write", ".env", new_keys=["A", "B"], kind="new")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("suggested_keys:", msg)
        self.assertIn("  A=", msg)
        self.assertIn("  B=", msg)
        self.assertIn("suggestion_alt:", msg)
        self.assertIn(".env.example", msg)

    def test_edit_deny_extra_note_lines(self):
        msg = M.edit_deny(
            "Edit", ".env",
            extra_note="NOTE: symlink 経由だったため",
            kind="symlink",
        )
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("extra_note:", msg)
        self.assertIn("symlink", msg)

    def test_policy_unavailable_deny_plain_text(self):
        msg = M.policy_unavailable("deny")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("patterns.txt", msg)

    def test_ask_messages_remain_plain_text(self):
        # 0.6.x 以前から ask 系は plain text。0.7.0 でも継続。
        for kind in ("symlink", "special", "io_error"):
            msg = M.read_ask(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("hard_stop", "opaque_prefix", "shell_keyword",
                     "program_dynamic"):
            msg = M.bash_lenient(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)


class TestVocabularyConsistency(unittest.TestCase):
    """H2: 動詞ルール (block / 一時停止 / 確認を挟む) の最終確認。"""

    def test_deny_uses_block(self):
        # bash_deny
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertIn("block しました", msg)
        # edit_deny — E6 の 4 分岐すべてが語彙ルールを守ること
        for kind in ("new", "overwrite", "symlink", "special"):
            with self.subTest(kind=kind):
                msg2 = M.edit_deny("Write", ".env", kind=kind)
                self.assertIn("block しました", msg2)
        # policy_unavailable(deny)
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
            "tokenize_failed", "normalize_failed", "program_dynamic",
        ):
            msg = M.bash_lenient(kind)
            self.assertIn(
                "確認を挟みます", msg,
                msg=f"bash_lenient({kind!r}) lacks 確認を挟みます in: {msg!r}",
            )


if __name__ == "__main__":
    unittest.main()
