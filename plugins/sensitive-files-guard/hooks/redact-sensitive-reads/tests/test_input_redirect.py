"""``< target`` 入力リダイレクトの target 抽出と handle() 経由判定 (0.3.4)。

0.3.4 で character-level quote-aware parser に移行。空白なし / fd 前置き /
quote 付きを網羅し、heredoc (``<<``), herestring (``<<<``), fd dup (``<&N``),
process substitution (``<(...)``) は parser 内で明示的にスキップする。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from handlers.bash.redirects import _scan_input_redirect_targets_with_form
from handlers.bash_handler import _extract_input_redirect_targets, handle


def _make_envelope(cmd: str, cwd: str, mode: str = "default") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd, "description": "test"},
        "cwd": cwd,
        "permission_mode": mode,
    }


def _decision(resp: dict) -> str | None:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


class TestExtractInputRedirectTargets(unittest.TestCase):
    """character-level parser の単体テスト (0.3.4)。"""

    def test_simple_target(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat < .env"),
            [".env"],
        )

    def test_target_with_extension(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat < .env.local"),
            [".env.local"],
        )

    def test_no_space_inline_target(self):
        # 0.3.4: character-level parser で `cat<target` を拾う
        self.assertEqual(
            _extract_input_redirect_targets("cat<.env"), [".env"]
        )

    def test_heredoc_excluded(self):
        self.assertEqual(_extract_input_redirect_targets("cat << EOF"), [])
        self.assertEqual(_extract_input_redirect_targets("cat <<EOF"), [])

    def test_process_sub_excluded(self):
        # 0.3.4: `<(` を parser が明示的にスキップする
        self.assertEqual(
            _extract_input_redirect_targets("cat <(cat .env)"),
            [],
        )

    def test_fd_dup_excluded(self):
        self.assertEqual(_extract_input_redirect_targets("cat <&2"), [])

    def test_digit_fd_prefix_extracts_target(self):
        # 0.3.4: fd 前置き `0<`/`N<` も target 抽出対象
        self.assertEqual(
            _extract_input_redirect_targets("cat 0< .env"), [".env"]
        )

    def test_multiple_targets(self):
        self.assertEqual(
            _extract_input_redirect_targets(
                "cat < .env && cat < .env.local"
            ),
            [".env", ".env.local"],
        )


class TestExtractInputRedirectTargetsInline(unittest.TestCase):
    """0.3.4: 空白なし / fd 前置き / quote 付き inline の境界検証。"""

    def test_inline_no_fd_no_space(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat<.env"), [".env"]
        )

    def test_inline_double_quoted(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat<".env"'), [".env"]
        )

    def test_inline_single_quoted(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat<'.env'"), [".env"]
        )

    def test_inline_quoted_space_name(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat<"a file.env"'),
            ["a file.env"],
        )

    def test_fd_zero_space(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat 0< .env"), [".env"]
        )

    def test_fd_zero_inline(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat 0<.env"), [".env"]
        )

    def test_fd_nonzero_space(self):
        # `1<` は意味論上 stdout-as-input で runtime error だが syntax は有効。
        # 静的解析では target を拾う (fail-closed)。
        self.assertEqual(
            _extract_input_redirect_targets("cat 1< .env"), [".env"]
        )

    def test_fd_nonzero_inline(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat 2<.env"), [".env"]
        )

    def test_fd_multi_digit(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat 10<.env"), [".env"]
        )

    def test_mixed_fd_and_quote(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat 0<".env"'), [".env"]
        )


class TestExtractInputRedirectTargetsQuoteAware(unittest.TestCase):
    """0.3.4: 空白 + quote 付きの網羅。"""

    def test_double_quoted_dotenv_with_space(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env"'), [".env"]
        )

    def test_single_quoted_dotenv_with_space(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat < '.env'"), [".env"]
        )

    def test_double_quoted_space_name_with_space(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < "a file.env"'),
            ["a file.env"],
        )

    def test_double_quoted_glob_target(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env*"'), [".env*"]
        )

    def test_mixed_quoted_and_bare(self):
        self.assertEqual(
            _extract_input_redirect_targets(
                'cat < README.md && cat<"a file.env"'
            ),
            ["README.md", "a file.env"],
        )

    def test_escaped_space_in_bare(self):
        # backslash escape で space を含む bare target
        self.assertEqual(
            _extract_input_redirect_targets("cat < a\\ file.env"),
            ["a file.env"],
        )


class TestExtractInputRedirectTargetsExclusion(unittest.TestCase):
    """0.3.4: heredoc / herestring / fd dup / process sub の除外境界。"""

    def test_herestring_excluded(self):
        # `<<<` は herestring (literal 渡し、file read ではない)
        self.assertEqual(
            _extract_input_redirect_targets("cat <<< '.env'"), []
        )

    def test_process_sub_inline_no_space(self):
        # `<(` 直後の内部コマンドの `.env` を拾わない (depth tracking)
        self.assertEqual(
            _extract_input_redirect_targets("cat <(cat .env)"), []
        )

    def test_process_sub_with_space_outside(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat <(cat .env) README.md"),
            [],
        )

    def test_process_sub_nested_paren(self):
        # 内部に nested `(...)` があっても depth tracking で正しく閉じる
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo (x))"), []
        )

    def test_process_sub_followed_by_redirect(self):
        # process sub 終了後に `<` があれば target 抽出
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo x) < .env"),
            [".env"],
        )

    def test_fd_dup_excluded(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat <&2"), []
        )

    def test_heredoc_body_literal(self):
        # heredoc body 内に `< .env` が含まれても 0.3.3 と同等挙動で拾う
        # (false-positive 側 deny に倒す。body 解析は範囲外)。
        self.assertEqual(
            _extract_input_redirect_targets("cat <<EOF\ncat < .env\nEOF"),
            [".env"],
        )


class TestExtractInputRedirectTargetsConcatWord(unittest.TestCase):
    """0.3.4 R1 fix: quote + bare が連結した 1 つの word を正しく抽出する。

    Codex review 指摘: closing quote で即 return すると
    ``cat < ".env".example`` の suffix ``.example`` を落として ``.env`` だけを
    抽出してしまう。POSIX sh では quote + bare は 1 word なので、word boundary
    まで読み続ける必要がある。
    """

    def test_quoted_prefix_bare_suffix(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env".example'),
            [".env.example"],
        )

    def test_quoted_prefix_bare_suffix_local(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env".local'),
            [".env.local"],
        )

    def test_inline_quoted_concat(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat<".env".local'),
            [".env.local"],
        )

    def test_bare_quoted_bare(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < a"b"c'),
            ["abc"],
        )

    def test_quoted_then_glob_suffix(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env"*'),
            [".env*"],
        )

    def test_multi_quote_sections(self):
        # single + double + bare の混合
        self.assertEqual(
            _extract_input_redirect_targets("cat < 'a'\"b\"c"),
            ["abc"],
        )


class TestExtractInputRedirectTargetsComment(unittest.TestCase):
    """0.3.4 R2 fix: quote 外のシェルコメント (`#` ... 改行) 内の redirect を無視。

    Codex review 指摘: 新 character parser が ``echo ok #cat<.env`` のような
    コメント内の ``<`` を target として抽出し、false-positive deny を誘発する。
    Bash の仕様通り ``#`` が word start 位置にあれば改行まで skip する。
    """

    def test_comment_hides_inline_redirect(self):
        self.assertEqual(
            _extract_input_redirect_targets("echo ok #cat<.env"), []
        )

    def test_comment_hides_spaced_redirect(self):
        self.assertEqual(
            _extract_input_redirect_targets("echo ok # < .env"), []
        )

    def test_line_start_comment(self):
        self.assertEqual(
            _extract_input_redirect_targets("#cat < .env"), []
        )

    def test_double_quoted_hash_not_comment(self):
        # double quote 内の `#` はコメント開始ではない、後段の redirect は抽出
        self.assertEqual(
            _extract_input_redirect_targets('echo "a#b" < .env'),
            [".env"],
        )

    def test_single_quoted_hash_not_comment(self):
        self.assertEqual(
            _extract_input_redirect_targets("echo 'a#b' < .env"),
            [".env"],
        )

    def test_inline_hash_in_word_not_comment(self):
        # word 内部の `#` は通常文字扱い (comment ではない)
        self.assertEqual(
            _extract_input_redirect_targets("echo abc#def < .env"),
            [".env"],
        )

    def test_comment_ends_at_newline_then_redirect(self):
        # 改行で comment 終了後の redirect は抽出される
        self.assertEqual(
            _extract_input_redirect_targets("echo ok # <x\ncat < .env"),
            [".env"],
        )


class TestExtractInputRedirectTargetsProcessSubEscapedParen(unittest.TestCase):
    """0.3.4 R3 fix: process sub `<(...)` 内のエスケープ括弧を depth から除外。

    Codex review 指摘: `cat <(echo \\() < .env` のような escape された `(` `)` が
    depth tracking を狂わせ、後続の `< .env` を取りこぼす。auto/plan モードでは
    `ask_or_allow` が allow に倒るため、機密ファイルの bypass (security
    regression) を誘発する。quote 外 backslash escape は depth 計算から除外する。
    """

    def test_escaped_open_paren_in_proc_sub(self):
        # process sub 内の `\(` は depth に影響しない → 後続の `< .env` を抽出
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo \\() < .env"),
            [".env"],
        )

    def test_escaped_close_paren_in_proc_sub(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo \\)) < .env"),
            [".env"],
        )

    def test_escaped_both_parens_in_proc_sub(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo \\(\\)) < .env"),
            [".env"],
        )

    def test_escaped_paren_no_following_redirect(self):
        # process sub だけで終わる場合は target なし (内部の `.env` は拾わない)
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo \\()"),
            [],
        )

    def test_proc_sub_unaffected_when_no_escape(self):
        # 既存の正常 process sub は影響なし (regression check)
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo x) < .env"),
            [".env"],
        )


class TestExtractInputRedirectTargetsDoubleQuoteEscape(unittest.TestCase):
    """0.3.4 R4 fix: POSIX sh の double-quote escape semantics に準拠する。

    Codex review 指摘: double quote 内の backslash escape が任意文字を unescape
    していたため、``cat < ".\\env"`` が ``.env`` として解釈され誤って deny される
    挙動があった。POSIX sh では double quote 内 ``\\X`` は X が ``$`` ``\\``
    ``\"`` ``\\\\`` ``\\n`` のいずれかのときのみ X を取り込み、それ以外は ``\\``
    も literal として保持する。
    """

    def test_literal_backslash_letter(self):
        # `\e` は literal `\e`
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".\\env"'),
            [".\\env"],
        )

    def test_literal_backslash_star(self):
        # `\*` は literal `\*` (glob 展開対象にならない)
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env\\*"'),
            [".env\\*"],
        )

    def test_escape_dollar_sign(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env\\$"'),
            [".env$"],
        )

    def test_escape_backtick(self):
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env\\`"'),
            [".env`"],
        )

    def test_escape_double_quote(self):
        # `\"` は escape された `"` (closing quote ではない)
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env\\""'),
            ['.env"'],
        )

    def test_escape_backslash(self):
        # `\\` は literal `\`
        self.assertEqual(
            _extract_input_redirect_targets('cat < ".env\\\\"'),
            [".env\\"],
        )


class TestExtractInputRedirectTargetsProcessSubWordBoundary(unittest.TestCase):
    """0.3.4 R5 fix: process sub `<(...)` 終了直後を word boundary 扱いしない。

    Codex review 指摘: `<(` ブランチ終了時に ``at_word_start`` を更新せず、
    続く `#` をシェルコメントと誤認して ``< .env`` を取りこぼす security
    regression。``<(...)`` は bash では 1 つの word なので、直後は word
    boundary ではなく、`#` を comment として扱わない。
    """

    def test_no_space_hash_after_proc_sub_keeps_redirect(self):
        # `cat <(echo x)#bar < .env` の `<(echo x)#bar` は bash では 1 word。
        # `#` は word 内部 → comment ではない → 後続の `< .env` を抽出する。
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo x)#bar < .env"),
            [".env"],
        )

    def test_inline_hash_after_proc_sub_with_redirect(self):
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo x)# < .env"),
            [".env"],
        )

    def test_space_hash_after_proc_sub_is_comment(self):
        # 空白を挟んだ `#` は comment (regression check)
        self.assertEqual(
            _extract_input_redirect_targets("cat <(echo x) #bar < .env"),
            [],
        )


class TestExtractInputRedirectTargetsConditionalArith(unittest.TestCase):
    """0.3.4 R6 fix: ``[[ ... ]]`` / ``(( ... ))`` 内の ``<`` は比較演算子。

    Codex review 指摘: character-level parser が ``[[ "$x"<.env ]]`` の inline
    ``<.env`` を redirect target として抽出して deny に倒っていた。bash では
    ``[[`` 内の ``<`` は文字列比較、``((`` 内の ``<`` は算術比較で、どちらも
    redirect ではない。これらを閉じ ``]]`` / ``))`` までスキップして target を
    拾わないようにする。
    """

    def test_double_bracket_inline_lt(self):
        self.assertEqual(
            _extract_input_redirect_targets('[[ "$x"<.env ]]'), []
        )

    def test_double_bracket_spaced_lt(self):
        self.assertEqual(
            _extract_input_redirect_targets('[[ "$x" < .env ]]'), []
        )

    def test_arith_inline_lt(self):
        self.assertEqual(
            _extract_input_redirect_targets("(( a<5 ))"), []
        )

    def test_arith_nested_paren(self):
        # `((` 内に nested `(` があっても depth tracking で正しく閉じる
        self.assertEqual(
            _extract_input_redirect_targets("(( (a < b) < c ))"), []
        )

    def test_double_bracket_then_real_redirect(self):
        # `[[ ]]` の後にある本物の `< .env` は抽出する
        self.assertEqual(
            _extract_input_redirect_targets('[[ "$x"<y ]] && cat < .env'),
            [".env"],
        )

    def test_real_redirect_then_double_bracket(self):
        # 本物の `< .env` の後に `[[ ]]` がある場合も target 抽出
        self.assertEqual(
            _extract_input_redirect_targets('cat < .env && [[ "$x"<y ]]'),
            [".env"],
        )

    def test_double_bracket_with_quoted_close(self):
        # `[[ ]]` 内 quote 中の `]]` は閉じとみなさず、その後の本物 redirect を抽出
        self.assertEqual(
            _extract_input_redirect_targets('[[ "a]]b" == c ]] < .env'),
            [".env"],
        )

    def test_single_bracket_is_normal_word(self):
        # `[` 単独 (test コマンド) は通常 word 扱い → 後続 redirect 抽出
        self.assertEqual(
            _extract_input_redirect_targets("[ a ] < .env"),
            [".env"],
        )

    def test_double_bracket_no_space_is_normal_word(self):
        # R7: `[[foo` は通常 word (bash の `[[` 予約語ではない) → 後続 redirect 抽出
        self.assertEqual(
            _extract_input_redirect_targets("tee [[foo < .env"),
            [".env"],
        )

    def test_double_bracket_no_space_multiword(self):
        self.assertEqual(
            _extract_input_redirect_targets("tee [[bar baz < .env"),
            [".env"],
        )

    def test_double_bracket_paired_no_space_is_word(self):
        # `[[a]]` は通常 word (predicate ではない) → 後続 redirect 抽出
        self.assertEqual(
            _extract_input_redirect_targets("cmd [[a]] < .env"),
            [".env"],
        )

    def test_double_bracket_with_tab_after_is_keyword(self):
        # tab も word boundary 扱い → `[[` 予約語 → 内部 `<` skip
        self.assertEqual(
            _extract_input_redirect_targets('[[\t"$x"<.env ]]'), []
        )

    def test_double_bracket_at_argument_position_not_keyword(self):
        # R8: `tee [[ ... ]]` の `[[` は引数位置 (command word ではない) なので
        # 予約語ではない。内部の `< .env` は本物の redirect として抽出する。
        self.assertEqual(
            _extract_input_redirect_targets('tee [[ "$x" < .env ]]'),
            [".env"],
        )

    def test_double_bracket_after_command_word_not_keyword(self):
        # `echo` の引数位置の `[[` も同様
        self.assertEqual(
            _extract_input_redirect_targets('echo [[ "$x"<.env ]]'),
            [".env"],
        )

    def test_double_bracket_after_segment_separator_is_keyword(self):
        # `;` 直後 (command 位置) の `[[` は予約語 → 内部の `<` を skip
        self.assertEqual(
            _extract_input_redirect_targets(
                'cat < .env; [[ "$x"<.env ]]'
            ),
            [".env"],  # 最初の `< .env` のみ抽出、後者の `[[` 内は skip
        )

    def test_double_bracket_after_pipe_is_keyword(self):
        # `|` 直後 (command 位置) の `[[` も予約語
        self.assertEqual(
            _extract_input_redirect_targets(
                'echo y | [[ "$x"<.env ]]'
            ),
            [],
        )


class _BaseHandle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        os.makedirs(self.xdg)
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestHandleInputRedirect(_BaseHandle):
    """handle() 経由の挙動。target 一致なら deny 固定、それ以外は ask_or_allow。"""

    def test_cat_lt_dotenv_deny(self):
        r = handle(_make_envelope("cat < .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_lt_dotenv_local_deny(self):
        r = handle(_make_envelope("cat < .env.local", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_lt_readme_default_ask(self):
        # target 非機密 → 後段の hard_stop ask_or_allow に倒れる
        r = handle(_make_envelope("cat < README.md", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_cat_lt_readme_auto_allow(self):
        r = handle(_make_envelope("cat < README.md", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_heredoc_default_ask(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_heredoc_auto_allow(self):
        r = handle(
            _make_envelope("cat <<EOF\nhello\nEOF", self.tmp, mode="auto")
        )
        self.assertEqual(r, {})

    def test_process_sub_default_ask(self):
        # `<(` は process sub。`(` も別の hard-stop として効くが、いずれにせよ ask
        r = handle(_make_envelope("cat <(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_dotenv_glob_target_deny(self):
        r = handle(_make_envelope("cat < .env*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_dotenv_example_target_default_ask(self):
        # target literal は exclude 決着 → 機密判定 False。後段 ask_or_allow に倒れる
        r = handle(_make_envelope("cat < .env.example", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bypass_dotenv_lt_deny(self):
        r = handle(_make_envelope("cat < .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")


class TestHandleInputRedirectExpanded(_BaseHandle):
    """0.3.4: 全有効 redirect 構文で deny / ask_or_allow が正しく倒れるか。"""

    def test_inline_no_space_dotenv_deny(self):
        r = handle(_make_envelope("cat<.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_inline_quoted_dotenv_deny(self):
        r = handle(_make_envelope('cat<".env"', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_fd_zero_space_dotenv_deny(self):
        r = handle(_make_envelope("cat 0< .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_fd_zero_inline_dotenv_deny(self):
        r = handle(_make_envelope("cat 0<.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_fd_nonzero_dotenv_deny(self):
        r = handle(_make_envelope("cat 1< .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_quoted_space_dotenv_deny(self):
        r = handle(_make_envelope('cat < ".env"', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_quoted_single_dotenv_deny(self):
        r = handle(_make_envelope("cat < '.env'", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_quoted_glob_dotenv_deny(self):
        r = handle(_make_envelope('cat < ".env*"', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_quoted_space_nonmatch_ask(self):
        # basename `a file.env` は rule 非 match → ask_or_allow
        r = handle(_make_envelope('cat < "a file.env"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_herestring_opaque_default_ask(self):
        # herestring は除外 → target 空 → 後段 hard_stop ask_or_allow
        r = handle(_make_envelope("cat <<< '.env'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_process_sub_opaque_default_ask(self):
        r = handle(_make_envelope("cat <(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestHandleCharLevelFixes(_BaseHandle):
    """0.3.4 Codex review R1/R2 fix: handle() 経由の挙動確認。"""

    def test_concat_quoted_env_example_not_deny(self):
        # `.env.example` は exclude 決着 → rule 非 match → hard_stop ask_or_allow
        r = handle(_make_envelope('cat < ".env".example', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_concat_quoted_env_local_deny(self):
        # `.env.local` は `.env.*` include → deny (target 抽出成功で rule 一致)
        r = handle(_make_envelope('cat < ".env".local', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_comment_hides_inline_redirect_default_ask(self):
        # `<` を含むので hard_stop 経路だが target 空 → ask_or_allow (default=ask)
        r = handle(_make_envelope("echo ok #cat<.env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_comment_hides_inline_redirect_auto_allow(self):
        r = handle(
            _make_envelope("echo ok #cat<.env", self.tmp, mode="auto")
        )
        self.assertEqual(r, {})

    def test_proc_sub_escaped_paren_does_not_bypass_default(self):
        # R3: escape された `\(` で depth tracking が壊れ、後続の `< .env` を
        # 取りこぼすと auto モードで bypass。修正後は target 抽出成功で deny。
        r = handle(_make_envelope("cat <(echo \\() < .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_proc_sub_escaped_paren_does_not_bypass_auto(self):
        # auto モードでも bypass しない (機密 leak の最重要回帰)
        r = handle(
            _make_envelope("cat <(echo \\() < .env", self.tmp, mode="auto")
        )
        self.assertEqual(_decision(r), "deny")

    def test_dq_literal_backslash_does_not_match_dotenv(self):
        # R4: `".\env"` は literal `.\env` (rule 非 match) → ask_or_allow
        # 修正前は `.env` と誤解釈されて deny になっていた (false-positive 回避)
        r = handle(_make_envelope('cat < ".\\env"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_proc_sub_inline_hash_does_not_bypass_default(self):
        # R5: `cat <(echo x)#bar < .env` の `#bar` を comment 扱いすると
        # 後続の `< .env` を取りこぼす。bash では `#` が word 内なので
        # comment ではなく target は抽出される → deny。
        r = handle(
            _make_envelope("cat <(echo x)#bar < .env", self.tmp)
        )
        self.assertEqual(_decision(r), "deny")

    def test_proc_sub_inline_hash_does_not_bypass_auto(self):
        # auto モードでも bypass しない (機密 leak 防止の最重要回帰)
        r = handle(
            _make_envelope(
                "cat <(echo x)#bar < .env", self.tmp, mode="auto"
            )
        )
        self.assertEqual(_decision(r), "deny")

    def test_double_bracket_compare_default_ask(self):
        # R6: `[[ "$x"<.env ]]` の `<` は文字列比較。redirect ではないので
        # target 抽出 0 件 → hard_stop fallback の ask_or_allow (default=ask)。
        r = handle(_make_envelope('[[ "$x"<.env ]]', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_double_bracket_compare_auto_allow(self):
        # auto モードでも allow (元の regex 挙動と整合、機密 deny 暴発防止)
        r = handle(
            _make_envelope('[[ "$x"<.env ]]', self.tmp, mode="auto")
        )
        self.assertEqual(r, {})

    def test_double_bracket_no_space_does_not_bypass_default(self):
        # R7: `tee [[foo < .env` の `[[foo` は通常 word なので `< .env` 抽出 → deny
        r = handle(
            _make_envelope("tee [[foo < .env", self.tmp)
        )
        self.assertEqual(_decision(r), "deny")

    def test_double_bracket_no_space_does_not_bypass_auto(self):
        # auto モードでも bypass しない (機密 leak 防止の最重要回帰)
        r = handle(
            _make_envelope(
                "tee [[foo < .env", self.tmp, mode="auto"
            )
        )
        self.assertEqual(_decision(r), "deny")

    def test_double_bracket_at_argument_position_does_not_bypass_default(self):
        # R8: `tee [[ "$x" < .env ]]` の `[[` は引数位置 → 通常 word →
        # `< .env` 抽出 → deny。
        r = handle(
            _make_envelope('tee [[ "$x" < .env ]]', self.tmp)
        )
        self.assertEqual(_decision(r), "deny")

    def test_double_bracket_at_argument_position_does_not_bypass_auto(self):
        # auto モードでも bypass しない
        r = handle(
            _make_envelope(
                'tee [[ "$x" < .env ]]', self.tmp, mode="auto"
            )
        )
        self.assertEqual(_decision(r), "deny")


class TestExtractInputRedirectTargetsWithForm(unittest.TestCase):
    """0.5.0 / M5: form 付き parser ``_scan_input_redirect_targets_with_form``。

    各 target に ``RedirectForm`` (``bare`` / ``fd_prefixed`` / ``no_space``
    / ``quoted``) が付与されること。優先順位は
    ``fd_prefixed`` > ``no_space`` > ``quoted`` > ``bare``。
    """

    def test_bare_with_space(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat < .env"),
            [(".env", "bare")],
        )

    def test_no_space_inline(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat<.env"),
            [(".env", "no_space")],
        )

    def test_fd_prefixed_with_space(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat 0< .env"),
            [(".env", "fd_prefixed")],
        )

    def test_fd_prefixed_inline(self):
        # fd_prefixed > no_space (fd 前置きは空白なしでも優先)
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat 0<.env"),
            [(".env", "fd_prefixed")],
        )

    def test_fd_prefixed_multi_digit(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat 10<.env"),
            [(".env", "fd_prefixed")],
        )

    def test_fd_prefixed_nonzero(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat 2<.env"),
            [(".env", "fd_prefixed")],
        )

    def test_fd_prefixed_at_command_start(self):
        # 行頭の `0<` (前に空白なし) も fd_prefixed
        self.assertEqual(
            _scan_input_redirect_targets_with_form("0<.env"),
            [(".env", "fd_prefixed")],
        )

    def test_quoted_double_with_space(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form('cat < ".env"'),
            [(".env", "quoted")],
        )

    def test_quoted_single_with_space(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat < '.env'"),
            [(".env", "quoted")],
        )

    def test_quoted_no_space(self):
        # quoted > no_space (target 冒頭が quote なら quoted を優先)
        self.assertEqual(
            _scan_input_redirect_targets_with_form('cat<".env"'),
            [(".env", "quoted")],
        )

    def test_fd_prefixed_beats_quoted(self):
        # fd_prefixed > quoted (fd 前置きが quote より優先)
        self.assertEqual(
            _scan_input_redirect_targets_with_form('cat 0<".env"'),
            [(".env", "fd_prefixed")],
        )

    def test_fd_prefixed_with_space_and_quote(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form('cat 0< ".env"'),
            [(".env", "fd_prefixed")],
        )

    def test_word_internal_digit_is_not_fd_prefix(self):
        # `abc0<` の `0` は word 内部 → fd prefix 扱いしない (no_space)。
        # bash は abc0 を引数として、`<` を独立した redirect として扱うが、
        # form 分類軸としては word boundary 起点でない数字は fd_prefixed と
        # 区別する設計。
        self.assertEqual(
            _scan_input_redirect_targets_with_form("echo abc0<.env"),
            [(".env", "no_space")],
        )

    def test_multiple_targets_with_different_forms(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form(
                'cat < .env && cat<".env.local"'
            ),
            [(".env", "bare"), (".env.local", "quoted")],
        )

    def test_quoted_concat_word(self):
        # `< ".env".example` は word 連結。冒頭 quote なので quoted。
        self.assertEqual(
            _scan_input_redirect_targets_with_form('cat < ".env".example'),
            [(".env.example", "quoted")],
        )

    def test_empty_command(self):
        self.assertEqual(_scan_input_redirect_targets_with_form(""), [])

    def test_no_redirect(self):
        self.assertEqual(
            _scan_input_redirect_targets_with_form("cat .env"), []
        )

    def test_excluded_constructs_yield_empty(self):
        # heredoc / herestring / fd dup / process sub は除外 → 空リスト
        for cmd in (
            "cat << EOF",
            "cat <<< '.env'",
            "cat <&2",
            "cat <(cat .env)",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    _scan_input_redirect_targets_with_form(cmd), []
                )

    def test_segment_separator_resets_command_position(self):
        # `;` 直後の `0<` も command 位置 → fd_prefixed
        self.assertEqual(
            _scan_input_redirect_targets_with_form("date; 0<.env"),
            [(".env", "fd_prefixed")],
        )


class TestExtractInputRedirectTargetsCharsThinWrapper(unittest.TestCase):
    """0.5.0 / M5: ``_scan_input_redirect_targets_chars`` (thin wrapper) は
    既存戻り値型 ``list[str]`` を維持する (74 件の戻り値型 assert テストの
    後方互換)。
    """

    def test_thin_wrapper_returns_list_of_strings(self):
        from handlers.bash.redirects import _scan_input_redirect_targets_chars
        result = _scan_input_redirect_targets_chars("cat < .env && cat<'.env.local'")
        # 戻り値の各要素は str (tuple ではない)
        self.assertEqual(result, [".env", ".env.local"])
        for x in result:
            self.assertIsInstance(x, str)


if __name__ == "__main__":
    unittest.main()
