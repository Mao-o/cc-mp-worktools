"""Bash handler (Step 5) の判定テスト。

0.3.0 からセグメント分割 (``&&`` ``||`` ``;`` ``|`` ``\\n``) と安全リダイレクト
(``>/dev/null`` ``2>&1`` 等) の剥離に対応。

- allow: ``echo foo``, ``cat .env.example``, ``ls -la``, ``git status && git log``,
  ``cat README.md 2>/dev/null``
- deny 固定 (機密検出): ``cat .env``, ``cat .env && pwd``, ``false || cat .env``
- ask (fail-closed): hard-stop metachars (``$`` ``<`` ``(`` ``{`` 等), 絶対パス,
  env prefix, shell wrapper, xargs 等
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
        # npm, git, make 等の未知コマンドは allow (副作用なし想定)
        r = handle(_make_envelope("npm test", self.tmp))
        self.assertIsNone(_decision(r))


class TestDenyFixed(BaseBash):
    """機密 path への単純読み取りアクセスは ``deny`` 固定 (bypass 関係なし)。

    0.2.0 で ``ask_or_deny`` → ``make_deny`` 固定に変更。実機観測で ask が
    うっかり承認される事例があったため。
    """

    def test_cat_dotenv(self):
        r = handle(_make_envelope("cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_cat_dotenv_bypass(self):
        r = handle(_make_envelope("cat .env", self.tmp, mode="bypassPermissions"))
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


class TestFailClosedHardStop(BaseBash):
    """動的評価 / 入力リダイレクト / グループ化は fail-closed (ask)。

    0.3.0 以降、``&&`` ``||`` ``;`` ``|`` ``\\n`` は segment split で扱うため
    hard-stop ではない。``$`` ``<`` ``(`` ``{`` バッククォートのみ hard-stop。
    """

    def test_variable_expansion(self):
        r = handle(_make_envelope('cat "$X"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_substitution(self):
        r = handle(_make_envelope("cat $(echo .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_backtick(self):
        r = handle(_make_envelope("cat `echo .env`", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_redirect_in(self):
        r = handle(_make_envelope("< .env cat", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_heredoc(self):
        r = handle(_make_envelope("cat <<EOF\nhello\nEOF", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_subshell_group(self):
        r = handle(_make_envelope("(cat .env)", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_brace_group(self):
        r = handle(_make_envelope("{ cat .env; }", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestBackslashQuoteSplit(BaseBash):
    """ダブルクォート内の偶数個バックスラッシュを正しく扱う (Codex P1 対応)。

    Bash 仕様: ``"`` の直前の連続バックスラッシュが偶数 → 閉じクォート、
    奇数 → エスケープされた ``"``。直前 1 文字だけで判定すると
    ``echo "\\\\"; cat .env`` で分割を失敗し後続 ``cat .env`` を検出できない。
    """

    def test_even_backslash_two_closes_quote(self):
        # echo "\\"; cat .env — \\ は literal \, 閉じクォートが効いて ; で分割される
        r = handle(_make_envelope(r'echo "\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_even_backslash_four_closes_quote(self):
        r = handle(_make_envelope(r'echo "\\\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_odd_backslash_three_keeps_quote(self):
        # 3 個 = 奇数 → 閉じクォートがエスケープされる。splitter は 1 セグメントのまま。
        # shlex が closing quotation 不在で落ちて ask_or_deny (fail-closed)
        r = handle(_make_envelope(r'echo "\\\"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_quoted_and_operator_with_outer_semicolon(self):
        # クォート内の && は保存、外側 ; で分割して cat .env を検出
        r = handle(_make_envelope(r'echo "a && b"; cat .env', self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_single_quote_unchanged(self):
        # Bash: シングルクォート内にエスケープなし。動作変更なし確認
        r = handle(_make_envelope("echo 'a && b'; cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestShellKeywordBypass(BaseBash):
    """シェル制御構文を絡めた機密 path 読み出し bypass を塞ぐ。

    segment split (``;`` / ``\\n``) を挟むと ``do cat .env`` のような制御構文
    本体セグメントが未知コマンド扱いで allow される regression があった
    (Codex P1 指摘)。first token が予約語なら fail-closed する。
    """

    def test_for_loop_body(self):
        r = handle(_make_envelope("for i in 1; do cat .env; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_if_then_body(self):
        r = handle(_make_envelope("if true; then cat .env; fi", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_while_test(self):
        r = handle(_make_envelope("while cat .env; do pwd; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_until_test(self):
        r = handle(_make_envelope("until cat .env; do true; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_select_body(self):
        r = handle(_make_envelope("select x in a; do cat .env; done", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_time_prefix(self):
        r = handle(_make_envelope("time cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bang_negation(self):
        r = handle(_make_envelope("! cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_eval_wrapper(self):
        r = handle(_make_envelope("eval cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_coproc(self):
        r = handle(_make_envelope("coproc cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestUnknownCommandOperand(BaseBash):
    """未知コマンドでも operand が機密 path なら deny 固定 (Codex P1 指摘対応)。

    0.3.0 時点では ``grep SECRET .env`` のような unknown command + sensitive
    path が unknown → allow で素通りしていた。0.3.1 で unified operand scan に
    変更し、コマンド名に関わらず非 option トークンを全て機密判定する。
    """

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

    # ergonomics: 非機密 operand は unknown command でも allow 維持
    def test_grep_non_sensitive_allow(self):
        r = handle(_make_envelope("grep foo README.md", self.tmp))
        self.assertIsNone(_decision(r))

    def test_unknown_command_no_sensitive_operand_allow(self):
        r = handle(_make_envelope("make build", self.tmp))
        self.assertIsNone(_decision(r))

    def test_git_commit_message_allow(self):
        # commit message の文字列は基本的に機密 basename と一致しない
        r = handle(_make_envelope("git commit -m 'update docs'", self.tmp))
        self.assertIsNone(_decision(r))


class TestWrapperBypass(BaseBash):
    """``_SHELL_WRAPPERS`` 未列挙の launcher / wrapper 経由の bypass を塞ぐ。

    0.3.0 時点では ``timeout 1 cat .env`` ``nohup cat .env`` ``busybox cat
    .env`` が unknown → allow で素通りしていた。0.3.1 の unified operand scan
    は operand 側の ``.env`` を拾って deny する (コマンド名は判断しない)。
    """

    def test_timeout_cat(self):
        r = handle(_make_envelope("timeout 1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "deny")

    def test_nohup_cat(self):
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


class TestGlobBypass(BaseBash):
    """operand に glob 文字 (``*`` ``?`` ``[``) が含まれる場合は fail-closed。

    ``cat .env*`` のように shell 展開で ``.env`` に解決しうる glob を
    literal basename 判定で見落としていた bypass (Codex P1) を塞ぐ。
    """

    def test_star_glob(self):
        r = handle(_make_envelope("cat .env*", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_char_class_glob(self):
        r = handle(_make_envelope("cat [.]env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_inner_char_class(self):
        r = handle(_make_envelope("grep SECRET .e[n]v", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_question_glob(self):
        r = handle(_make_envelope("cat .en?", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_non_sensitive_glob_also_ask(self):
        # 非機密向けの glob も ergonomics 上は allow したいが、展開結果を静的に
        # 判定できないため現実装は保守的に ask。ユーザーが許したい場合は明示 path に。
        r = handle(_make_envelope("cat *.log", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestOptEqualsValue(BaseBash):
    """``--opt=value`` / ``-o=value`` 形式の option-arg から value 側を拾う。

    0.3.1 初版では ``_find_path_candidates`` が ``-`` 始まりのトークンを丸ごと
    捨てていたため ``grep --file=.env foo README.md && true`` のような option
    value に機密 path を埋め込む bypass があった (Codex P1 指摘)。
    """

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

    # 無害な --opt=value は allow を維持
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


class TestQuotedFdTarget(BaseBash):
    """quote された ``'&2'`` 等を fd duplication と誤認しない (Codex P2 指摘)。

    shlex posix 後 ``>`` + ``&N`` の 2 トークン形式は ``echo foo > '&2'``
    (literal `&2` ファイルへの書込み) と区別できない。安全側で剥がさず、
    後段の residual metachar で ask に倒す。単一トークン ``2>&1`` ``>&2``
    は従来どおり ``_SAFE_REDIRECT_RE`` で剥離する。
    """

    def test_quoted_amp_target_not_stripped(self):
        r = handle(_make_envelope("echo foo > '&2'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_unquoted_single_token_fd_dup_stripped(self):
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertIsNone(_decision(r))

    def test_stderr_dup_stripped(self):
        r = handle(_make_envelope("echo foo >&2", self.tmp))
        self.assertIsNone(_decision(r))

    def test_dev_null_two_token_still_stripped(self):
        # 2 トークン形式でも /dev/null 系は引き続き剥離
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertIsNone(_decision(r))


class TestUriVcsPathspec(BaseBash):
    """URI / VCS pathspec / rsync ``user@host:path`` 経由の機密 path 検出。

    ``git show HEAD:.env`` ``curl file://.env`` のような非 path 形式も、コロン
    分割 + basename 判定で機密一致を検出する (Codex P1 指摘対応)。
    """

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
    """複合コマンド (&&/||/;/|/\\n) のいずれかのセグメントが機密一致 → deny。"""

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
        # 右辺で安全リダイレクト剥離しても .env 参照は残る
        r = handle(_make_envelope("pwd && cat .env 2>/dev/null", self.tmp))
        self.assertEqual(_decision(r), "deny")


class TestCompoundAllow(BaseBash):
    """複合コマンドでも全セグメントが非機密 / 未知コマンドなら allow。"""

    def test_git_status_and_log_with_null_redirect(self):
        # 実運用で頻出する複合コマンド (このリリースの主動機)
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
        # 空白区切りの > /dev/null も剥がす
        r = handle(_make_envelope("cat README.md > /dev/null", self.tmp))
        self.assertIsNone(_decision(r))

    def test_cat_non_sensitive_with_stderr_dup(self):
        # 2>&1 (fd 複製) も剥がす
        r = handle(_make_envelope("cat README.md 2>&1", self.tmp))
        self.assertIsNone(_decision(r))


class TestRedirectToNonNullAsk(BaseBash):
    """/dev/null 以外への ``>`` リダイレクトは剥がさず fail-closed する。"""

    def test_redirect_to_file(self):
        r = handle(_make_envelope("echo foo > out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_append_redirect(self):
        r = handle(_make_envelope("echo foo >> out.txt", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestFailClosedExec(BaseBash):
    """絶対/相対パス実行・env prefix・shell wrapper は fail-closed。"""

    def test_absolute_path_exec(self):
        r = handle(_make_envelope("/bin/cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_relative_path_exec(self):
        r = handle(_make_envelope("./myscript", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_dotdot_exec(self):
        r = handle(_make_envelope("../bin/cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_prefix_cat(self):
        r = handle(_make_envelope("FOO=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_prefix_source(self):
        r = handle(_make_envelope("FOO=1 source .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_bash_c(self):
        r = handle(_make_envelope('bash -c "cat .env"', self.tmp))
        # bash -c は shell_wrapper → 先頭トークン `bash` で fail-closed
        # (ただし `"cat .env"` 内は metachar 判定されないため、shell_wrapper 判定が効く)
        self.assertEqual(_decision(r), "ask")

    def test_bash_lc(self):
        r = handle(_make_envelope("bash -lc 'cat .env'", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sh_c(self):
        r = handle(_make_envelope('sh -c "cat .env"', self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_env_wrapper(self):
        r = handle(_make_envelope("env X=1 cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_xargs_a(self):
        r = handle(_make_envelope("xargs -a .env cat", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_command_wrapper(self):
        r = handle(_make_envelope("command cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")

    def test_sudo_wrapper(self):
        r = handle(_make_envelope("sudo cat .env", self.tmp))
        self.assertEqual(_decision(r), "ask")


class TestShlexFailure(BaseBash):
    def test_unbalanced_quote(self):
        r = handle(_make_envelope("cat '.env", self.tmp))
        # metachar 判定で既に落ちない ($ や & は無い) → shlex.split 失敗 → ask
        self.assertEqual(_decision(r), "ask")


if __name__ == "__main__":
    unittest.main()
