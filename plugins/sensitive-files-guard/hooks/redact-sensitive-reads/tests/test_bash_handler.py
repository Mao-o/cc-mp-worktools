"""Bash handler の判定テスト (0.8.0: prefix normalize / glob 候補列挙 撤廃)。

主要挙動:
- opaque wrapper / hard-stop / shell keyword / 任意 path exec / 残留 metachar /
  shlex 失敗 → ``ask_or_allow`` (default=ask, auto/bypass=allow)
- env-assignment prefix (``FOO=1``) / ``env`` / ``command`` / ``builtin`` /
  ``nohup`` / 任意 path 実行 (``/usr/bin/env``, ``/bin/cat``) は **opaque** 扱い
  で ``ask_or_allow``。0.3.2〜0.7.x の prefix normalize は 0.8.0 で撤廃
- glob operand → ``_glob_operand_is_dotenv_match`` で ``.env`` / ``.envrc``
  literal に fnmatch するときだけ deny 固定。それ以外の glob は ``ask_or_allow``
  (0.3.2〜0.7.x の既定 rules 候補列挙は 0.8.0 で撤廃)
- ``<`` 入力リダイレクト系は hard-stop で ``ask_or_allow`` (0.7.0)
- ``patterns.txt`` 読込失敗 → 全 mode で ``make_deny`` 固定
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from handlers.bash_handler import handle


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


def _reason(resp: dict) -> str:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecisionReason") or ""


class BaseBash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # XDG / HOME を隔離
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

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAllow(BaseBash):
    def test_echo_allowed(self):
        r = handle(_make_envelope("echo foo", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_ls_allowed(self):
        r = handle(_make_envelope("ls -la", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_env_example_allowed(self):
        # .env.example はテンプレート除外なので allow
        r = handle(_make_envelope("cat .env.example", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_regular_file_allowed(self):
        r = handle(_make_envelope("cat README.md", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_with_options_non_sensitive(self):
        r = handle(_make_envelope("head -n 5 README.md", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_empty_command(self):
        r = handle(_make_envelope("", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_unknown_command_allow(self):
        r = handle(_make_envelope("npm test", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestDenyFixed(BaseBash):
    """機密 path への単純読み取りアクセスは全 mode で ``deny`` 固定。"""

    def test_cat_dotenv(self):
        r = handle(_make_envelope("cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_dotenv_bypass(self):
        r = handle(_make_envelope("cat .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")

    def test_cat_dotenv_auto(self):
        r = handle(_make_envelope("cat .env", self.tmp, mode="auto"))
        self.assertEqual(_decision(r), "deny")

    def test_source_dotenv(self):
        r = handle(_make_envelope("source .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_dot_dotenv(self):
        r = handle(_make_envelope(". .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_head_with_options_dotenv(self):
        r = handle(_make_envelope("head -n 1 .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_tail_dotenv(self):
        r = handle(_make_envelope("tail -f .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_less_dotenv(self):
        r = handle(_make_envelope("less .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_subdir_dotenv(self):
        r = handle(_make_envelope("cat sub/.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_private_key(self):
        r = handle(_make_envelope("cat id_rsa", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_ddash_then_path(self):
        r = handle(_make_envelope("cat -- .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestHardStopLenient(BaseBash):
    """hard-stop metachar (`$`, ``(``, `{`, ``<``, バッククォート) は default=ask /
    auto/bypass=allow。0.7.0 で ``<`` 入力リダイレクトの target 抽出を撤廃し、
    全 hard-stop が ``ask_or_allow`` 一本に統合された。
    """

    def test_variable_expansion_default(self):
        r = handle(_make_envelope('cat "$X"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_variable_expansion_auto(self):
        r = handle(_make_envelope('cat "$X"', self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_variable_expansion_bypass(self):
        r = handle(_make_envelope('cat "$X"', self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))

    def test_command_substitution_default(self):
        r = handle(_make_envelope("cat $(echo .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_substitution_auto(self):
        r = handle(_make_envelope("cat $(echo .env)", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_backtick_default(self):
        r = handle(_make_envelope("cat `echo .env`", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_backtick_bypass(self):
        r = handle(_make_envelope("cat `echo .env`", self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))

    def test_heredoc_default(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_heredoc_auto(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_subshell_group_default(self):
        r = handle(_make_envelope("(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_subshell_group_auto(self):
        # (cat .env) は ( hard-stop。target 抽出は < がないので走らず ask_or_allow。
        # auto/bypass では allow に倒る (機密 .env が中にあっても!)
        r = handle(_make_envelope("(cat .env)", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_brace_group_default(self):
        r = handle(_make_envelope("{ cat .env; }", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestInputRedirectAskOrAllow(BaseBash):
    """0.7.0: ``<`` 入力リダイレクトは hard-stop と同じ ``ask_or_allow`` に格下げ。

    0.3.4〜0.6.x で行っていた target 抽出 + literal/glob 一致での deny 固定は、
    思想 1 (うっかり露出予防が目的、敵対的防御は非目的) に反するため撤廃。
    default mode で ask、autonomous (auto / bypassPermissions) で allow に倒す。
    """

    def test_redirect_in_default(self):
        # `< .env cat` (引数順序は逆) → hard-stop で ask
        r = handle(_make_envelope("< .env cat", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_redirect_in_auto(self):
        # autonomous モードでは allow (= None)
        r = handle(_make_envelope("< .env cat", self.tmp, mode="auto"))
        self.assertIsNone(_decision(r))

    def test_cat_lt_dotenv_bypass(self):
        # bypassPermissions でも allow に倒る (hard-stop は ask_or_allow)
        r = handle(_make_envelope("cat < .env", self.tmp, mode="bypassPermissions"))
        self.assertIsNone(_decision(r))


class TestDenyReasonContent(BaseBash):
    """deny reason に operand 名 / basename 案内が **実展開で** 含まれること (H1 / H3)。

    builder の単体テストは ``test_messages.py`` で行う。ここでは handler の
    呼び出し経路で reason に正しく繋がっているか (regression 防止) を確認する。
    """

    def test_literal_match_includes_operand_and_basename(self):
        # H1: literal の deny reason に operand `.env` が出る
        r = handle(_make_envelope("cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")
        reason = _reason(r)
        self.assertIn("cat", reason)
        self.assertIn(".env", reason)
        # H3: `!.env` がコピペ可能な形で埋まる
        self.assertIn("`!.env`", reason)

    def test_literal_match_with_subdir_uses_basename(self):
        # operand がパス込みでも `!<basename>` には basename だけが入る
        r = handle(_make_envelope("cat sub/.env", self.tmp))
        self.assertEqual(_decision(r), "deny")
        reason = _reason(r)
        self.assertIn("sub/.env", reason)
        self.assertIn("`!.env`", reason)

    def test_glob_match_includes_glob_operand(self):
        r = handle(_make_envelope("cat .env*", self.tmp))
        self.assertEqual(_decision(r), "deny")
        reason = _reason(r)
        self.assertIn(".env*", reason)
        self.assertIn("`!.env*`", reason)


class TestBackslashQuoteSplit(BaseBash):
    """ダブルクォート内の偶数個バックスラッシュを正しく扱う (Codex P1 対応, 0.3.1)。"""

    def test_even_backslash_two_closes_quote(self):
        r = handle(_make_envelope(r'echo "\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_even_backslash_four_closes_quote(self):
        r = handle(_make_envelope(r'echo "\\\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_odd_backslash_three_keeps_quote(self):
        # 3 個 = 奇数 → 閉じクォートがエスケープされる。shlex が落ちて ask_or_allow
        r = handle(_make_envelope(r'echo "\\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_quoted_and_operator_with_outer_semicolon(self):
        r = handle(_make_envelope(r'echo "a && b"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_single_quote_unchanged(self):
        r = handle(_make_envelope("echo 'a && b'; cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestShellKeywordLenient(BaseBash):
    """シェル制御構文 (if/for/do/coproc 等) は default=ask / auto/bypass=allow (0.3.2)。"""

    def test_for_loop_body_default(self):
        r = handle(_make_envelope("for i in 1; do cat .env; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_for_loop_body_auto(self):
        r = handle(_make_envelope(
            "for i in 1; do cat .env; done", self.tmp, mode="auto",
        ))
        self.assertTrue(output.is_allow(r))

    def test_if_then_body_default(self):
        r = handle(_make_envelope("if true; then cat .env; fi", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_if_then_body_bypass(self):
        r = handle(_make_envelope(
            "if true; then cat .env; fi", self.tmp, mode="bypassPermissions",
        ))
        self.assertTrue(output.is_allow(r))

    def test_while_test(self):
        r = handle(_make_envelope("while cat .env; do pwd; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_until_test(self):
        r = handle(_make_envelope("until cat .env; do true; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_select_body(self):
        r = handle(_make_envelope("select x in a; do cat .env; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_coproc(self):
        r = handle(_make_envelope("coproc cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestOpaqueWrapperLenient(BaseBash):
    """opaque wrapper (eval/python/sudo/awk/sed/time/!/exec) は default=ask / auto/bypass=allow。"""

    def test_eval_default(self):
        r = handle(_make_envelope("eval cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_eval_auto(self):
        r = handle(_make_envelope("eval cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_eval_bypass(self):
        r = handle(_make_envelope("eval cat .env", self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))

    def test_time_default(self):
        # 0.3.2 で time は _SHELL_KEYWORDS から _OPAQUE_WRAPPERS に移動
        r = handle(_make_envelope("time cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_time_auto(self):
        r = handle(_make_envelope("time cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_bang_default(self):
        r = handle(_make_envelope("! cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bang_auto(self):
        r = handle(_make_envelope("! cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_exec_default(self):
        r = handle(_make_envelope("exec cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_exec_with_options_auto(self):
        r = handle(_make_envelope("exec -a name cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_python_c_default(self):
        r = handle(_make_envelope("python3 -c 'print(1)'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_python_c_auto(self):
        r = handle(_make_envelope("python3 -c 'print(1)'", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))


class TestUnknownCommandOperand(BaseBash):
    """未知コマンドでも operand が機密 path なら deny 固定 (0.3.1, 維持)。"""

    def test_grep_sensitive_path(self):
        r = handle(_make_envelope("grep SECRET .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_grep_sensitive_path_piped(self):
        r = handle(_make_envelope("grep SECRET .env | head -n 1", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_base64_sensitive(self):
        r = handle(_make_envelope("base64 .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_xxd_sensitive(self):
        r = handle(_make_envelope("xxd .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_od_sensitive(self):
        r = handle(_make_envelope("od -An -tx1 .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_hexdump_sensitive(self):
        r = handle(_make_envelope("hexdump -C .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_git_diff_no_index_sensitive(self):
        r = handle(_make_envelope(
            "git diff --no-index /dev/null .env", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")

    def test_cp_sensitive(self):
        r = handle(_make_envelope("cp .env backup", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_mv_sensitive(self):
        r = handle(_make_envelope("mv .env .env.old", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_grep_non_sensitive_allow(self):
        r = handle(_make_envelope("grep foo README.md", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_unknown_command_no_sensitive_operand_allow(self):
        r = handle(_make_envelope("make build", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_git_commit_message_allow(self):
        r = handle(_make_envelope("git commit -m 'update docs'", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestWrapperBypass(BaseBash):
    """wrapper 経由 (timeout/nice/stdbuf/busybox) でも operand .env が機密一致で deny。

    0.8.0 で ``nohup`` は ``_OPAQUE_WRAPPERS`` に統合 (透過プレフィクス撤廃) のため
    ``ask_or_allow`` に倒れる。timeout/nice/stdbuf/busybox は通常コマンド扱いのため
    operand scan で第二トークン以降の機密 path に一致して deny 固定 (維持)。
    """

    def test_timeout_cat(self):
        r = handle(_make_envelope("timeout 1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_nohup_cat_default(self):
        # 0.8.0 で nohup は opaque wrapper (透過プレフィクス撤廃) → ask
        r = handle(_make_envelope("nohup cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_nohup_cat_auto(self):
        r = handle(_make_envelope("nohup cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_nice_cat(self):
        r = handle(_make_envelope("nice cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_stdbuf_cat(self):
        r = handle(_make_envelope("stdbuf -o0 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_busybox_cat(self):
        r = handle(_make_envelope("busybox cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestOpaquePrefixAskOrAllow(BaseBash):
    """0.8.0: env-assignment / env / command / builtin / nohup / 任意 path exec を含む
    第一トークンは opaque 扱いで ``ask_or_allow`` (default=ask, auto/bypass=allow)。

    0.3.2〜0.7.x で行っていた prefix normalize (``FOO=1 cat .env`` を
    ``cat .env`` と解釈して deny) は 0.8.0 で撤廃。これらは「うっかり書く形」
    ではないため思想 1 (うっかり露出予防、敵対的防御は非目的) と整合しない。
    """

    def test_env_prefix_cat_default(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_prefix_cat_auto(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_env_prefix_cat_bypass(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))

    def test_multi_env_prefix(self):
        r = handle(_make_envelope("FOO=1 BAR=2 cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_command_cat_default(self):
        r = handle(_make_envelope("env cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_command_with_assignment_auto(self):
        r = handle(_make_envelope("env FOO=1 cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_command_wrapper_cat_default(self):
        r = handle(_make_envelope("command cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_builtin_wrapper_cat_default(self):
        r = handle(_make_envelope("builtin cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_nohup_chain_with_command(self):
        r = handle(_make_envelope("nohup command cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_chain_with_env_bypass(self):
        r = handle(_make_envelope(
            "command env FOO=1 cat .env", self.tmp, mode="bypassPermissions",
        ))
        self.assertTrue(output.is_allow(r))

    def test_abs_env_with_assignment_default(self):
        # /usr/bin/env: 任意 path exec → opaque → ask
        r = handle(_make_envelope("/usr/bin/env FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_abs_command_wrapper_auto(self):
        r = handle(_make_envelope("/bin/command cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))


class TestPrefixWithOptionsOpaque(BaseBash):
    """env / command にオプションがあるケースも opaque (default=ask / auto/bypass=allow)。

    0.7.x ではオプション付きの env/command のみ opaque だったが、0.8.0 では
    オプション有無に関わらず env/command/builtin/nohup を全て opaque にした
    (TestOpaquePrefixAskOrAllow と統合)。本クラスは「オプション有り」の
    regression 担保用。
    """

    def test_env_dash_i_default(self):
        r = handle(_make_envelope("env -i cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_dash_i_auto(self):
        r = handle(_make_envelope("env -i cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_env_dash_u_default(self):
        r = handle(_make_envelope("env -u HOME cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_double_dash_default(self):
        r = handle(_make_envelope("env -- cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_double_dash_auto(self):
        r = handle(_make_envelope("env -- cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_command_dash_p_default(self):
        r = handle(_make_envelope("command -p cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_double_dash_default(self):
        r = handle(_make_envelope("command -- cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_double_dash_bypass(self):
        r = handle(_make_envelope("command -- cat .env", self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))


class TestGlobDotenvDeny(BaseBash):
    """0.8.0: operand glob が dotenv literal stem (``.env`` / ``.envrc``) に
    fnmatch するときだけ deny 固定 (うっかり頻出ケース)。

    判定は ``_glob_operand_is_dotenv_match`` (operand_lexer.py)。0.3.2〜0.7.x の
    既定 rules 候補列挙 (``cat *.key`` / ``cat id_rsa*`` / ``cat cred*.json``
    も deny する経路) は 0.8.0 で撤廃。
    """

    def test_dotenv_star_deny(self):
        # fnmatchcase(".env", ".env*") = True → deny
        r = handle(_make_envelope("cat .env*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_star_envrc_deny(self):
        # fnmatchcase(".envrc", "*.envrc") = True → deny
        r = handle(_make_envelope("cat *.envrc", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_envrc_star_deny(self):
        r = handle(_make_envelope("cat .envrc*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_question_glob_deny(self):
        # fnmatchcase(".env", ".en?") = True → deny
        r = handle(_make_envelope("cat .en?", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_inner_char_class_deny(self):
        # fnmatchcase(".env", ".e[n]v") = True → deny
        r = handle(_make_envelope("grep SECRET .e[n]v", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_char_class_deny(self):
        # fnmatchcase(".env", "[.]env") = True → deny
        r = handle(_make_envelope("cat [.]env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestGlobUncertainAskOrAllow(BaseBash):
    """0.8.0: dotenv literal stem に fnmatch しない glob は ``ask_or_allow``
    (default=ask, auto/bypass=allow)。0.3.2〜0.7.x で deny / allow に倒していた
    既定 rules 交差判定は 0.8.0 で撤廃 (``id_rsa*`` / ``*.key`` / ``cred*.json``
    / ``*.log`` / ``.env.*`` / ``.env.example*`` を ``ask_or_allow`` に格下げ)。
    """

    def test_dotenv_dot_star_default(self):
        # fnmatchcase(".env", ".env.*") = False (".env." 以降が必要) → ask
        r = handle(_make_envelope("cat .env.*", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_dotenv_dot_star_auto(self):
        r = handle(_make_envelope("cat .env.*", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_id_rsa_star_default(self):
        r = handle(_make_envelope("cat id_rsa*", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_id_rsa_star_auto(self):
        r = handle(_make_envelope("cat id_rsa*", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_id_star_default(self):
        r = handle(_make_envelope("cat id_*", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_star_key_default(self):
        r = handle(_make_envelope("cat *.key", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_cred_star_json_default(self):
        r = handle(_make_envelope("cat cred*.json", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_cred_star_json_bypass(self):
        r = handle(_make_envelope(
            "cat cred*.json", self.tmp, mode="bypassPermissions",
        ))
        self.assertTrue(output.is_allow(r))

    def test_star_log_default(self):
        # 0.7.x までは allow だったが 0.8.0 で ask_or_allow に統一 (rules 交差
        # 判定撤廃の副作用)。default で ask、auto/bypass で allow に倒る。
        r = handle(_make_envelope("cat *.log", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_star_log_auto(self):
        r = handle(_make_envelope("cat *.log", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_dotenv_example_star_default(self):
        # fnmatchcase(".env", ".env.example*") = False → ask
        r = handle(_make_envelope("cat .env.example*", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_dotenv_example_star_auto(self):
        r = handle(_make_envelope("cat .env.example*", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))


class TestGlobLiteralExcludeAllow(BaseBash):
    """literal (glob 文字を含まない) operand は従来通り ``_operand_is_sensitive`` で
    判定。``.env.example`` は ``!*.example`` の last-match-wins で False → allow 維持。
    """

    def test_dotenv_example_literal_allow(self):
        r = handle(_make_envelope("cat .env.example", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_dotenv_sample_literal_allow(self):
        r = handle(_make_envelope("cat .env.sample", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestOptEqualsValue(BaseBash):
    """``--opt=value`` / ``-o=value`` 形式の option-arg から value 側を拾う (0.3.1)。"""

    def test_grep_file_equals_sensitive(self):
        r = handle(_make_envelope(
            "grep --file=.env foo README.md && true", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")

    def test_gpg_keyring_equals_sensitive(self):
        r = handle(_make_envelope(
            "gpg --keyring=.env --export", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")

    def test_short_opt_equals_sensitive(self):
        r = handle(_make_envelope("cmd -o=.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_non_sensitive_opt_value_allow(self):
        r = handle(_make_envelope(
            "grep --color=auto foo README.md", self.tmp,
        ))
        self.assertTrue(output.is_allow(r))

    def test_curl_max_time_allow(self):
        r = handle(_make_envelope(
            "curl --max-time=30 https://example.com", self.tmp,
        ))
        self.assertTrue(output.is_allow(r))


class TestAttachedShortOption(BaseBash):
    """``-X<value>`` 短形連結の operand から basename を拾う (0.3.1)。"""

    def test_grep_attached_file_sensitive(self):
        r = handle(_make_envelope("grep -f.env foo README.md", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_grep_attached_file_in_compound(self):
        r = handle(_make_envelope(
            "grep -f.env foo README.md && true", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")

    def test_grep_multi_flag_attached_sensitive(self):
        r = handle(_make_envelope(
            "grep -vn .env.local README.md", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")

    def test_ls_flag_group_allow(self):
        r = handle(_make_envelope("ls -la", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_rm_flag_group_allow(self):
        r = handle(_make_envelope("rm -rf target", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_grep_short_non_sensitive_allow(self):
        r = handle(_make_envelope("grep -i pattern README.md", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestQuotedFdTarget(BaseBash):
    """quote された ``'&2'`` を fd duplication と誤認しない (Codex P2, 0.3.1)。"""

    def test_quoted_amp_target_not_stripped_default(self):
        r = handle(_make_envelope("echo foo > '&2'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_quoted_amp_target_not_stripped_auto(self):
        # 0.3.2: residual metachar も auto/bypass で allow に倒る
        r = handle(_make_envelope("echo foo > '&2'", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_unquoted_single_token_fd_dup_stripped(self):
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_stderr_dup_stripped(self):
        r = handle(_make_envelope("echo foo >&2", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_dev_null_two_token_still_stripped(self):
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestUriVcsPathspec(BaseBash):
    """URI / VCS pathspec / rsync 経由の機密 path 検出 (0.3.1, 維持)。"""

    def test_git_show_pathspec(self):
        r = handle(_make_envelope("git show HEAD:.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_git_cat_file_pathspec(self):
        r = handle(_make_envelope("git cat-file -p :.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_curl_file_uri(self):
        r = handle(_make_envelope("curl file://.env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_rsync_style_remote_path(self):
        r = handle(_make_envelope("cp user@host:/etc/.env /tmp", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestCompoundDeny(BaseBash):
    """複合コマンド (&&/||/;/|/\\n) のいずれかのセグメントが機密一致 → deny (0.3.0)。"""

    def test_pipe_left_sensitive(self):
        r = handle(_make_envelope("cat .env | head", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_and_left_sensitive(self):
        r = handle(_make_envelope("cat .env && pwd", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_or_right_sensitive(self):
        r = handle(_make_envelope("false || cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_semicolon_left_sensitive(self):
        r = handle(_make_envelope("cat .env; pwd", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_newline_right_sensitive(self):
        r = handle(_make_envelope("pwd\ncat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_compound_with_redirect_sensitive(self):
        r = handle(_make_envelope("pwd && cat .env 2>/dev/null", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestCompoundAllow(BaseBash):
    """複合コマンドでも全セグメントが非機密 / 未知コマンドなら allow (0.3.0)。"""

    def test_git_status_and_log_with_null_redirect(self):
        cmd = (
            "git -C /tmp/x status && "
            "git -C /tmp/x log --oneline -5 2>/dev/null || true"
        )
        r = handle(_make_envelope(cmd, self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_pipe_unknown_commands(self):
        r = handle(_make_envelope("ls -la | head -n 5", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_semicolon_unknown_commands(self):
        r = handle(_make_envelope("pwd; date; whoami", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_newline_unknown_commands(self):
        r = handle(_make_envelope("pwd\ndate\nwhoami", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_non_sensitive_with_stderr_discard(self):
        r = handle(_make_envelope("cat README.md 2>/dev/null", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_non_sensitive_with_all_discard(self):
        r = handle(_make_envelope("cat README.md &>/dev/null", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_non_sensitive_with_space_redirect(self):
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_non_sensitive_with_stderr_dup(self):
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertTrue(output.is_allow(r))


class TestRedirectToNonNull(BaseBash):
    """/dev/null 以外への ``>`` リダイレクトは default=ask / auto/bypass=allow (0.3.2)。"""

    def test_redirect_to_file_default(self):
        r = handle(_make_envelope("echo foo > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_redirect_to_file_auto(self):
        r = handle(_make_envelope("echo foo > out.txt", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_append_redirect_default(self):
        r = handle(_make_envelope("echo foo >> out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestArbitraryPathExec(BaseBash):
    """basename が透過対象でない絶対/相対パス実行は opaque (default=ask / auto/bypass=allow)。"""

    def test_absolute_path_default(self):
        r = handle(_make_envelope("/bin/cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_absolute_path_auto(self):
        r = handle(_make_envelope("/bin/cat .env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_relative_path_exec(self):
        r = handle(_make_envelope("./myscript", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_relative_path_auto(self):
        r = handle(_make_envelope("./myscript", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_dotdot_exec(self):
        r = handle(_make_envelope("../bin/cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestBashShellWrapper(BaseBash):
    """bash/sh/zsh -c 系は opaque (default=ask / auto/bypass=allow)。"""

    def test_bash_c_default(self):
        r = handle(_make_envelope('bash -c "cat .env"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bash_c_auto(self):
        r = handle(_make_envelope('bash -c "cat .env"', self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_bash_lc(self):
        r = handle(_make_envelope("bash -lc 'cat .env'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sh_c(self):
        r = handle(_make_envelope('sh -c "cat .env"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_xargs_a(self):
        r = handle(_make_envelope("xargs -a .env cat", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_xargs_a_auto(self):
        r = handle(_make_envelope("xargs -a .env cat", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))

    def test_sudo_wrapper(self):
        r = handle(_make_envelope("sudo cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sudo_wrapper_bypass(self):
        r = handle(_make_envelope("sudo cat .env", self.tmp, mode="bypassPermissions"))
        self.assertTrue(output.is_allow(r))


class TestShlexFailure(BaseBash):
    def test_unbalanced_quote_default(self):
        r = handle(_make_envelope("cat '.env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_unbalanced_quote_auto(self):
        r = handle(_make_envelope("cat '.env", self.tmp, mode="auto"))
        self.assertTrue(output.is_allow(r))


class TestConfirmedMatchAcrossModes(BaseBash):
    """機密 path への確定 match は全 mode (auto/bypass を含む) で deny を維持する。"""

    def test_all_modes_deny(self):
        cmds = ("cat .env", "grep X .env", "base64 .env", "git show HEAD:.env")
        modes = (
            "default", "auto", "bypassPermissions", "acceptEdits", "dontAsk", "plan",
        )
        for cmd in cmds:
            for mode in modes:
                r = handle(_make_envelope(cmd, self.tmp, mode=mode))
                self.assertEqual(
                    _decision(r), "deny",
                    msg=f"{cmd} with mode={mode} should deny but got {_decision(r)!r}",
                )


class TestSegmentHardStopReevaluate(BaseBash):
    """0.11.0 (F1): hard-stop は segment 単位で再評価される。

    0.10.0 までは command 全体に hard-stop char (``$``, バッククォート, ``(``,
    ``)``, ``{``, ``}``, ``<``, ``\\r``) が 1 つでもあると ``ask_or_allow`` に
    倒していたため、``cat .env | sed 's/(=)/X/'`` のような複合で sed segment の
    ``(`` が原因で全体 ask になり、autonomous で素通りしていた。0.11.0 では
    全体 early return を撤廃し、segment ごとに ``_has_hard_stop`` を再判定する。

    思想 1 (うっかり露出予防、敵対的防御は非目的) との整合: 攻撃シナリオ
    ``cat <(echo \\(\\)) < .env`` は全 segment が hard-stop となるため挙動不変。
    """

    # --- 核心: ユーザー報告ケース ---
    def test_user_reported_compound_with_sed_redact_paren_default(self):
        cmd = (
            "ls src/lib/lore/enishi/ 2>/dev/null && echo '---' && "
            "ls src/lib/lore/maturity/ 2>/dev/null && echo '---' && "
            "cat .env.local 2>/dev/null | sed -E 's/(=).*/\\1***REDACTED***/' | head -20"
        )
        r = handle(_make_envelope(cmd, self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_user_reported_compound_with_sed_redact_paren_auto(self):
        cmd = (
            "ls src/lib/lore/enishi/ 2>/dev/null && echo '---' && "
            "ls src/lib/lore/maturity/ 2>/dev/null && echo '---' && "
            "cat .env.local 2>/dev/null | sed -E 's/(=).*/\\1***REDACTED***/' | head -20"
        )
        r = handle(_make_envelope(cmd, self.tmp, mode="auto"))
        self.assertEqual(_decision(r), "deny")

    def test_user_reported_compound_with_sed_redact_paren_bypass(self):
        cmd = (
            "ls src/lib/lore/enishi/ 2>/dev/null && echo '---' && "
            "ls src/lib/lore/maturity/ 2>/dev/null && echo '---' && "
            "cat .env.local 2>/dev/null | sed -E 's/(=).*/\\1***REDACTED***/' | head -20"
        )
        r = handle(_make_envelope(cmd, self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")

    # --- 互換性: 「seg1 で deny 確定」を hard-stop が阻まない ---
    def test_dotenv_seg1_then_dollar_seg2_deny(self):
        # cat .env (literal match) を seg1、echo $HOME (hard-stop) を seg2
        # 0.10.0 では全体 hard-stop で ask、0.11.0 では seg1 で deny
        r = handle(_make_envelope("cat .env && echo $HOME", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_pipe_grep_paren_pattern_still_deny_seg1(self):
        # cat .env | grep '(=)' — 0.10.0: seg2 の `(` で全体 ask
        # 0.11.0: seg1 で deny 確定 (短絡)
        r = handle(_make_envelope("cat .env | grep '(=)'", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_or_chain_dotenv_seg1_short_circuit(self):
        # ls .env || cat $X || echo done — 0.10.0: 全体 hard-stop で ask
        # 0.11.0: seg1 (ls .env) literal match → deny 確定
        r = handle(_make_envelope("ls .env || cat $X || echo done", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_hard_stop_seg1_then_deny_seg2(self):
        # cat $X | ls .env | head — 0.10.0: 全体 hard-stop で ask
        # 0.11.0: seg1 hard-stop pending_ask → seg2 (ls .env) で deny 確定
        r = handle(_make_envelope("cat $X | ls .env | head", self.tmp))
        self.assertEqual(_decision(r), "deny")

    # --- 互換性: 既存挙動の継続 (regression 保護) ---
    def test_subshell_group_dotenv_still_ask_default(self):
        # (cat .env) — 1 segment 全体 hard-stop → pending_ask
        r = handle(_make_envelope("(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_substitution_in_quoted_string_still_ask(self):
        # echo "secret=$(cat .env)" — 1 segment 全体 hard-stop → pending_ask
        r = handle(_make_envelope('echo "secret=$(cat .env)"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_attack_scenario_input_redirect_dotenv_still_ask(self):
        # cat <(echo \(\)) < .env — 全 segment hard-stop で挙動不変
        # (process sub `<(...)`、入力 redirect `<` がそれぞれ hard-stop)
        r = handle(_make_envelope("cat <(echo \\(\\)) < .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_attack_scenario_input_redirect_dotenv_auto_allow(self):
        r = handle(_make_envelope(
            "cat <(echo \\(\\)) < .env", self.tmp, mode="auto",
        ))
        self.assertTrue(output.is_allow(r))

    def test_all_segments_hard_stop_pending_ask(self):
        # cat $X || cat $Y — 全 segment hard-stop → pending_ask 1 個 → ask
        r = handle(_make_envelope("cat $X || cat $Y", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sed_paren_with_dotenv_arg_still_ask(self):
        # sed 's/(=)/X/' .env — 1 segment 全体 hard-stop (`(`) → pending_ask
        # (sed は opaque wrapper だが hard-stop が先に発火)
        r = handle(_make_envelope("sed 's/(=)/X/' .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    # --- reason 文確認 (E3 dispatch との整合) ---
    def test_deny_reason_includes_first_token_and_minimal_info(self):
        # tmpdir に .env.local を実体作成して、reason に minimal info が
        # 埋まることを確認 (E3/E4 dispatch との整合)
        env_path = os.path.join(self.tmp, ".env.local")
        with open(env_path, "w") as f:
            f.write(
                "DATABASE_URL=postgresql://u:p@h/d\n"
                "JWT_SECRET=eyJabcdefghijklmnop\n"
            )
        cmd = (
            "ls . 2>/dev/null && echo '---' && "
            "cat .env.local 2>/dev/null | "
            "sed -E 's/(=).*/\\1***REDACTED***/' | head -20"
        )
        r = handle(_make_envelope(cmd, self.tmp))
        self.assertEqual(_decision(r), "deny")
        reason = _reason(r)
        self.assertIn("first_token: cat", reason)
        self.assertIn(".env.local", reason)
        self.assertIn("DATABASE_URL", reason)


class TestSafeReadAllowlist(BaseBash):
    """0.12.0: ``_SAFE_READ_FIRST_TOKENS`` (副作用なしの read-only allow-list) に
    該当する first_token は ``_segment_has_residual_metachar`` の ask 経路を
    スキップして operand scan に直行する。

    `grep foo > /tmp/out` `ls > listing.txt` のような調査用ワンライナーを
    ask に倒さないため (ログ実測で ask 発火の 約 80% が residual_metachar 起因)。
    機密 redirect target / hard-stop / opaque wrapper / allow-list 外の
    first_token は依然 ask / deny を維持する。
    """

    def test_grep_with_output_redirect_allow(self):
        # 0.11.x: residual `>` で ask、0.12.0: grep allow-list で allow
        r = handle(_make_envelope("grep foo README.md > /tmp/out", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_grep_with_append_redirect_allow(self):
        r = handle(_make_envelope("grep foo README.md >> /tmp/out", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_ls_with_output_redirect_allow(self):
        r = handle(_make_envelope("ls -la > /tmp/listing.txt", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_cat_with_output_redirect_allow(self):
        r = handle(_make_envelope("cat README.md > /tmp/out", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_head_with_output_redirect_allow(self):
        r = handle(_make_envelope("head -n 5 README.md > /tmp/x", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_wc_with_output_redirect_allow(self):
        r = handle(_make_envelope("wc -l README.md > /tmp/count", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_grep_redirect_to_sensitive_still_deny(self):
        # `>` 先が機密パスでも operand scan で deny される (safety net)
        r = handle(_make_envelope("grep foo file.txt > .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_grep_sensitive_operand_still_deny(self):
        # allow-list 対象でも operand が機密なら依然 deny 固定
        r = handle(_make_envelope("grep SECRET .env > out.txt", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_grep_input_redirect_kept_ask(self):
        # `<` 入力リダイレクトは hard-stop で ask 維持 (allow-list でも)
        r = handle(_make_envelope("grep foo < .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_grep_command_substitution_kept_ask(self):
        # `$()` hard-stop は allow-list でも ask 維持 (shell 展開漏洩リスク)
        r = handle(_make_envelope("grep foo $(find . -name x)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_awk_not_in_allowlist_still_ask(self):
        # awk は副作用持つ可能性 (`print > "/p"`, `-i`) のため allow-list 外。
        # opaque wrapper として ask 維持。
        r = handle(_make_envelope("awk '{print}' README.md > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sed_not_in_allowlist_still_ask(self):
        # sed は `-i` で in-place 書換できるため allow-list 外。
        r = handle(_make_envelope("sed 's/foo/bar/' README.md > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_find_not_in_allowlist_still_ask(self):
        # find は `-delete` / `-exec` で副作用持ちうるため allow-list 外。
        # `>` を含むので residual metachar の ask に倒れる。
        r = handle(_make_envelope("find . -name '*.py' > files.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_echo_not_in_allowlist_still_ask(self):
        # echo は stdout 出力のみで「見る・数える」とは異なる。allow-list 外。
        r = handle(_make_envelope("echo foo > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_grep_pipe_pure_read_allow(self):
        # pipe (`|`) は segment 分割なので各 segment は metachar 無し。
        # 両 segment が allow-list、operand 非機密 → allow。
        r = handle(_make_envelope("grep foo file.txt | head -n 5", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_grep_redirect_default_mode_allow(self):
        # default mode でも allow (autonomous でなくても許可される)
        r = handle(_make_envelope(
            "grep foo README.md > /tmp/out", self.tmp, mode="default",
        ))
        self.assertTrue(output.is_allow(r))

    def test_grep_background_ampersand_allow(self):
        # `&` background は residual metachar `&` を含む。allow-list で skip。
        r = handle(_make_envelope("grep foo file.txt &", self.tmp))
        self.assertTrue(output.is_allow(r))

    def test_all_safe_read_tokens_with_redirect_allow(self):
        # 主要 allow-list メンバーが redirect 含みで allow になることを確認
        cmds = [
            "ls -la > /tmp/x",
            "cat README.md > /tmp/x",
            "head -5 README.md > /tmp/x",
            "tail -5 README.md > /tmp/x",
            "wc -l README.md > /tmp/x",
            "grep foo README.md > /tmp/x",
            "file README.md > /tmp/x",
            "stat README.md > /tmp/x",
        ]
        for cmd in cmds:
            r = handle(_make_envelope(cmd, self.tmp))
            self.assertTrue(
                output.is_allow(r),
                msg=f"{cmd!r} should allow but got {_decision(r)!r}",
            )

    def test_safe_read_with_compound_pipe_grep_to_wc_allow(self):
        # `grep foo file | wc -l > /tmp/count` のような調査ワンライナー
        r = handle(_make_envelope(
            "grep foo README.md | wc -l > /tmp/count", self.tmp,
        ))
        self.assertTrue(output.is_allow(r))

    def test_safe_read_with_sensitive_in_compound_still_deny(self):
        # 複合で 1 segment が機密一致なら依然 deny 確定 (allow-list は他 segment
        # の ask を allow に倒すだけで、deny 判定は変えない)
        r = handle(_make_envelope(
            "grep foo README.md > /tmp/out && cat .env", self.tmp,
        ))
        self.assertEqual(_decision(r), "deny")


if __name__ == "__main__":
    unittest.main()
