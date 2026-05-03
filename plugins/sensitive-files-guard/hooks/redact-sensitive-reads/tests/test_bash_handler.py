"""Bash handler の判定テスト (0.3.2: 誤爆ガード緩和版)。

主要変更:
- opaque wrapper / hard-stop / shell keyword / 任意 path exec / 残留 metachar /
  shlex 失敗 → ``ask_or_allow`` (default=ask, auto/bypass=allow)
- env prefix / ``env`` (option 無し) / ``command`` (option 無し) / ``builtin`` /
  ``nohup`` の前置きは剥がして再判定。確定 match なら deny 固定 (全 mode)
- glob operand → ``_glob_operand_is_sensitive`` (既定 rules への候補列挙)
  - ``cat .env*`` ``cat .e[n]v`` ``cat .en?`` 等 → deny 固定
  - ``cat *.log`` 等 → allow
- ``< target`` 形式の入力リダイレクトは target 抽出して先に operand scan
- ``patterns.txt`` 読込失敗 → 全 mode で ``make_deny`` 固定
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

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
        self.assertIsNone(_decision(r))

    def test_ls_allowed(self):
        r = handle(_make_envelope("ls -la", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_env_example_allowed(self):
        # .env.example はテンプレート除外なので allow
        r = handle(_make_envelope("cat .env.example", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_regular_file_allowed(self):
        r = handle(_make_envelope("cat README.md", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_with_options_non_sensitive(self):
        r = handle(_make_envelope("head -n 5 README.md", self.tmp))
        self.assertIsNone(_decision(r))

    def test_empty_command(self):
        r = handle(_make_envelope("", self.tmp))
        self.assertIsNone(_decision(r))

    def test_unknown_command_allow(self):
        r = handle(_make_envelope("npm test", self.tmp))
        self.assertIsNone(_decision(r))


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
    """hard-stop metachar (`$`, ``(``, `{`, バッククォート) は default=ask / auto/bypass=allow。

    ``<`` だけは target 抽出が走り target 一致なら deny 固定 (TestInputRedirectDeny)。
    """

    def test_variable_expansion_default(self):
        r = handle(_make_envelope('cat "$X"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_variable_expansion_auto(self):
        r = handle(_make_envelope('cat "$X"', self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_variable_expansion_bypass(self):
        r = handle(_make_envelope('cat "$X"', self.tmp, mode="bypassPermissions"))
        self.assertEqual(r, {})

    def test_command_substitution_default(self):
        r = handle(_make_envelope("cat $(echo .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_substitution_auto(self):
        r = handle(_make_envelope("cat $(echo .env)", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_backtick_default(self):
        r = handle(_make_envelope("cat `echo .env`", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_backtick_bypass(self):
        r = handle(_make_envelope("cat `echo .env`", self.tmp, mode="bypassPermissions"))
        self.assertEqual(r, {})

    def test_heredoc_default(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_heredoc_auto(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_subshell_group_default(self):
        r = handle(_make_envelope("(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_subshell_group_auto(self):
        # (cat .env) は ( hard-stop。target 抽出は < がないので走らず ask_or_allow。
        # auto/bypass では allow に倒る (機密 .env が中にあっても!)
        r = handle(_make_envelope("(cat .env)", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_brace_group_default(self):
        r = handle(_make_envelope("{ cat .env; }", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestInputRedirectDeny(BaseBash):
    """``< target`` 形式の入力リダイレクトは target 抽出して機密一致で deny 固定。"""

    def test_redirect_in_default(self):
        # `< .env cat` (引数順序は逆) → target ".env" 抽出 → deny
        r = handle(_make_envelope("< .env cat", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_redirect_in_auto(self):
        r = handle(_make_envelope("< .env cat", self.tmp, mode="auto"))
        self.assertEqual(_decision(r), "deny")

    def test_cat_lt_dotenv_bypass(self):
        r = handle(_make_envelope("cat < .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")


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
        self.assertIn("glob", reason)

    def test_input_redirect_includes_target(self):
        r = handle(_make_envelope("cat < .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")
        reason = _reason(r)
        self.assertIn(".env", reason)
        self.assertIn("`!.env`", reason)
        self.assertIn("リダイレクト", reason)


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
        self.assertEqual(r, {})

    def test_if_then_body_default(self):
        r = handle(_make_envelope("if true; then cat .env; fi", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_if_then_body_bypass(self):
        r = handle(_make_envelope(
            "if true; then cat .env; fi", self.tmp, mode="bypassPermissions",
        ))
        self.assertEqual(r, {})

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
        self.assertEqual(r, {})

    def test_eval_bypass(self):
        r = handle(_make_envelope("eval cat .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(r, {})

    def test_time_default(self):
        # 0.3.2 で time は _SHELL_KEYWORDS から _OPAQUE_WRAPPERS に移動
        r = handle(_make_envelope("time cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_time_auto(self):
        r = handle(_make_envelope("time cat .env", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_bang_default(self):
        r = handle(_make_envelope("! cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bang_auto(self):
        r = handle(_make_envelope("! cat .env", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_exec_default(self):
        r = handle(_make_envelope("exec cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_exec_with_options_auto(self):
        r = handle(_make_envelope("exec -a name cat .env", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_python_c_default(self):
        r = handle(_make_envelope("python3 -c 'print(1)'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_python_c_auto(self):
        r = handle(_make_envelope("python3 -c 'print(1)'", self.tmp, mode="auto"))
        self.assertEqual(r, {})


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
        self.assertIsNone(_decision(r))

    def test_unknown_command_no_sensitive_operand_allow(self):
        r = handle(_make_envelope("make build", self.tmp))
        self.assertIsNone(_decision(r))

    def test_git_commit_message_allow(self):
        r = handle(_make_envelope("git commit -m 'update docs'", self.tmp))
        self.assertIsNone(_decision(r))


class TestWrapperBypass(BaseBash):
    """wrapper 経由 (timeout/nohup/nice/stdbuf/busybox) でも operand .env が機密一致で deny。"""

    def test_timeout_cat(self):
        r = handle(_make_envelope("timeout 1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_nohup_cat(self):
        # nohup は 0.3.2 で transparent prefix → 剥がして cat .env で deny 確定
        r = handle(_make_envelope("nohup cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_nice_cat(self):
        r = handle(_make_envelope("nice cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_stdbuf_cat(self):
        r = handle(_make_envelope("stdbuf -o0 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_busybox_cat(self):
        r = handle(_make_envelope("busybox cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestPrefixStrippingDeny(BaseBash):
    """env prefix / env / command / builtin / nohup の前置きを剥がして確定 match で deny (0.3.2)。"""

    def test_env_prefix_cat_default(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_env_prefix_cat_auto(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp, mode="auto"))
        self.assertEqual(_decision(r), "deny")

    def test_env_prefix_cat_bypass(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(_decision(r), "deny")

    def test_multi_env_prefix(self):
        r = handle(_make_envelope("FOO=1 BAR=2 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_env_command_cat(self):
        r = handle(_make_envelope("env cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_env_command_with_assignment(self):
        r = handle(_make_envelope("env FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_command_wrapper_cat(self):
        r = handle(_make_envelope("command cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_builtin_wrapper_cat(self):
        r = handle(_make_envelope("builtin cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_nohup_chain_with_command(self):
        r = handle(_make_envelope("nohup command cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_command_chain_with_env(self):
        r = handle(_make_envelope("command env FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_abs_env_with_assignment(self):
        # /usr/bin/env FOO=1 cat .env: basename=env → 透過 → deny
        r = handle(_make_envelope("/usr/bin/env FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_abs_command_wrapper(self):
        # /bin/command cat .env: basename=command → 透過 → deny
        r = handle(_make_envelope("/bin/command cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestPrefixStrippingOpaque(BaseBash):
    """env / command にオプションがあると opaque (default=ask / auto/bypass=allow)。"""

    def test_env_dash_i_default(self):
        r = handle(_make_envelope("env -i cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_dash_i_auto(self):
        r = handle(_make_envelope("env -i cat .env", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_env_dash_u_default(self):
        r = handle(_make_envelope("env -u HOME cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_double_dash_default(self):
        r = handle(_make_envelope("env -- cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_double_dash_auto(self):
        r = handle(_make_envelope("env -- cat .env", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_command_dash_p_default(self):
        r = handle(_make_envelope("command -p cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_double_dash_default(self):
        r = handle(_make_envelope("command -- cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_double_dash_bypass(self):
        r = handle(_make_envelope("command -- cat .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(r, {})


class TestGlobMatch(BaseBash):
    """operand glob (`*`/`?`/`[`) は ``_glob_operand_is_sensitive`` 経由 (0.3.2)。

    既定 rules と交差すれば全 mode で deny 固定、交差しなければ allow。
    """

    def test_dotenv_star_deny(self):
        r = handle(_make_envelope("cat .env*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_dotenv_dot_star_deny(self):
        r = handle(_make_envelope("cat .env.*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_star_envrc_deny(self):
        r = handle(_make_envelope("cat *.envrc", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_id_rsa_star_deny(self):
        r = handle(_make_envelope("cat id_rsa*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_id_star_deny(self):
        r = handle(_make_envelope("cat id_*", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_star_key_deny(self):
        r = handle(_make_envelope("cat *.key", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cred_star_json_deny(self):
        r = handle(_make_envelope("cat cred*.json", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_question_glob_deny(self):
        # `cat .en?` → 候補 `.env` が include 決着 → deny
        r = handle(_make_envelope("cat .en?", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_inner_char_class_deny(self):
        # `grep SECRET .e[n]v` → 候補 `.env` が include 決着 → deny
        r = handle(_make_envelope("grep SECRET .e[n]v", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_char_class_deny(self):
        # `cat [.]env` → 候補 `.env` が include 決着 → deny
        r = handle(_make_envelope("cat [.]env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_star_log_allow(self):
        # `*.log` は既定 rules と交差しないため allow (False positive 抑制)
        r = handle(_make_envelope("cat *.log", self.tmp))
        self.assertIsNone(_decision(r))

    def test_dotenv_example_literal_allow(self):
        r = handle(_make_envelope("cat .env.example", self.tmp))
        self.assertIsNone(_decision(r))

    def test_dotenv_example_star_allow(self):
        # 全候補が exclude 決着 → allow
        r = handle(_make_envelope("cat .env.example*", self.tmp))
        self.assertIsNone(_decision(r))

    def test_dotenv_sample_literal_allow(self):
        r = handle(_make_envelope("cat .env.sample", self.tmp))
        self.assertIsNone(_decision(r))


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
        self.assertIsNone(_decision(r))

    def test_curl_max_time_allow(self):
        r = handle(_make_envelope(
            "curl --max-time=30 https://example.com", self.tmp,
        ))
        self.assertIsNone(_decision(r))


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
        self.assertIsNone(_decision(r))

    def test_rm_flag_group_allow(self):
        r = handle(_make_envelope("rm -rf target", self.tmp))
        self.assertIsNone(_decision(r))

    def test_grep_short_non_sensitive_allow(self):
        r = handle(_make_envelope("grep -i pattern README.md", self.tmp))
        self.assertIsNone(_decision(r))


class TestQuotedFdTarget(BaseBash):
    """quote された ``'&2'`` を fd duplication と誤認しない (Codex P2, 0.3.1)。"""

    def test_quoted_amp_target_not_stripped_default(self):
        r = handle(_make_envelope("echo foo > '&2'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_quoted_amp_target_not_stripped_auto(self):
        # 0.3.2: residual metachar も auto/bypass で allow に倒る
        r = handle(_make_envelope("echo foo > '&2'", self.tmp, mode="auto"))
        self.assertEqual(r, {})

    def test_unquoted_single_token_fd_dup_stripped(self):
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertIsNone(_decision(r))

    def test_stderr_dup_stripped(self):
        r = handle(_make_envelope("echo foo >&2", self.tmp))
        self.assertIsNone(_decision(r))

    def test_dev_null_two_token_still_stripped(self):
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertIsNone(_decision(r))


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
        self.assertIsNone(_decision(r))

    def test_pipe_unknown_commands(self):
        r = handle(_make_envelope("ls -la | head -n 5", self.tmp))
        self.assertIsNone(_decision(r))

    def test_semicolon_unknown_commands(self):
        r = handle(_make_envelope("pwd; date; whoami", self.tmp))
        self.assertIsNone(_decision(r))

    def test_newline_unknown_commands(self):
        r = handle(_make_envelope("pwd\ndate\nwhoami", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_non_sensitive_with_stderr_discard(self):
        r = handle(_make_envelope("cat README.md 2>/dev/null", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_non_sensitive_with_all_discard(self):
        r = handle(_make_envelope("cat README.md &>/dev/null", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_non_sensitive_with_space_redirect(self):
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_non_sensitive_with_stderr_dup(self):
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertIsNone(_decision(r))


class TestRedirectToNonNull(BaseBash):
    """/dev/null 以外への ``>`` リダイレクトは default=ask / auto/bypass=allow (0.3.2)。"""

    def test_redirect_to_file_default(self):
        r = handle(_make_envelope("echo foo > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_redirect_to_file_auto(self):
        r = handle(_make_envelope("echo foo > out.txt", self.tmp, mode="auto"))
        self.assertEqual(r, {})

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
        self.assertEqual(r, {})

    def test_relative_path_exec(self):
        r = handle(_make_envelope("./myscript", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_relative_path_auto(self):
        r = handle(_make_envelope("./myscript", self.tmp, mode="auto"))
        self.assertEqual(r, {})

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
        self.assertEqual(r, {})

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
        self.assertEqual(r, {})

    def test_sudo_wrapper(self):
        r = handle(_make_envelope("sudo cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sudo_wrapper_bypass(self):
        r = handle(_make_envelope("sudo cat .env", self.tmp, mode="bypassPermissions"))
        self.assertEqual(r, {})


class TestShlexFailure(BaseBash):
    def test_unbalanced_quote_default(self):
        r = handle(_make_envelope("cat '.env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_unbalanced_quote_auto(self):
        r = handle(_make_envelope("cat '.env", self.tmp, mode="auto"))
        self.assertEqual(r, {})


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


if __name__ == "__main__":
    unittest.main()
