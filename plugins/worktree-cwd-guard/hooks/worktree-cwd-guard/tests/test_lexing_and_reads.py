"""comment・行の継続・here-document の字句と、別 checkout を指す読み取りの git の判定。

comment / 行の継続の読みは bash 3.2 と `zsh -f` で実測した結果に合わせている (どちらも同じ読み)。
"""
from __future__ import annotations

import unittest

import test_guard
from test_guard import bash, decision, evaluate


class CommentAndContinuationTest(test_guard.GuardBase):
    def assertDenied(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def assertAllowed(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_trailing_comment_does_not_join_the_next_line(self):
        m = self.main
        self.assertDenied((
            f'git status  # 状態を確認\ngit -C "{m}" checkout -b x',
            f'cd "{m}"  # main へ移る\ngit checkout -b x',
            f'git status;# c\ngit -C "{m}" commit -m x',
            f'git status &&# c\ngit -C "{m}" commit -m x',
            f'git status\t# c\ngit -C "{m}" reset --hard',
            # comment の中の `\` は行を継続しない
            f'git status  # c \\\ngit -C "{m}" reset --hard',
        ))

    def test_hash_after_each_word_break_starts_a_comment(self):
        # comment の中の `'` を引用符として読むと、次の行の git が閉じない引用符に飲まれる
        m = self.main
        self.assertDenied(tuple(
            f'{head}# don\'t\ngit -C "{m}" reset --hard'
            for head in ("git status ", "git status\t", "git status;", "git status&", "git status&&",
                         "git status|", "(git status)", "(", "# x\n", "")
        ))

    def test_line_continuation_joins_the_command(self):
        m = self.main
        self.assertDenied((
            f'git -C "{m}" \\\n  checkout -b x',
            f'cd "{m}" && git \\\n  commit -m msg',
            f'git \\\n-C "{m}" reset --hard',
            # 継続は単語の始まりかどうかを変えない (空白 + 継続の後ろの `#` は comment)
            f'git -C "{m}" reset --hard \\\n# don\'t',
            # 継続の後ろの `#` は前の単語の続きなら comment ではない
            f'git -C "{m}" reset\\\n#x --hard; git -C "{m}" stash',
        ))
        # 二重引用符の中の継続もつながる (`-C` の値が main を指す)
        self.assertDenied((f'git -C "{str(m)[:3]}\\\n{str(m)[3:]}" reset --hard',))

    def test_hash_inside_a_word_or_quotes_is_not_a_comment(self):
        m = self.main
        self.assertDenied((
            f'git checkout feature#1 && git -C "{m}" reset --hard',
            f'git commit -m "fix #1" && git -C "{m}" reset --hard',
            f"git log --grep '#1' && git -C \"{m}\" reset --hard",
            f'echo \\#x && git -C "{m}" reset --hard',
            f'echo a\\ #b && git -C "{m}" reset --hard',
            f'echo x\r#y && git -C "{m}" reset --hard',
        ))
        self.assertAllowed((
            "git checkout feature#1",
            'git commit -m "a # b"',
            f"git commit -m 'see {m} # later'",
        ))

    def test_comment_hides_only_the_rest_of_its_line(self):
        m = self.main
        self.assertAllowed((
            f'git status  # git -C "{m}" reset --hard',
            f'# cd "{m}"\ngit checkout -b x',
            f'git -C "{m}" log  # then reset\ngit status',
        ))

    def test_single_quotes_keep_backslash_newline(self):
        m = self.main
        # 単一引用符の中の `\` + 改行は文字どおり。引用符は `\` の直後で閉じる
        self.assertDenied((f"echo 'a\\\n' && cd \"{m}\" && git stash",))
        self.assertDenied((f"echo 'a\\' # c\ngit -C \"{m}\" reset --hard",))

    def test_ansi_c_quotes_and_unfinished_input(self):
        m = self.main
        self.assertDenied((
            # `$'...'` の中の `\'` は引用符を閉じない
            f"echo $'it\\'s' && git -C \"{m}\" reset --hard",
            f"echo $'it\\'s' # c\ngit -C \"{m}\" reset --hard",
            # 閉じない引用符より前の行は bash が実行する
            f"git -C \"{m}\" reset --hard\necho 'oops",
            f"git -C \"{m}\" reset --hard\necho $'oops",
            # 行の区切りは引用符の外の改行だけ (複数行の引用符の中の改行で切らない)
            f"echo \"a\nb\"; git -C \"{m}\" reset --hard\necho 'oops",
            # 閉じない引用符の中 (`"$(` の中を含む) の改行では切らない (切ると開いた引用符が残る)
            f"git -C \"{m}\" reset --hard\necho 'oops\nmore",
            f"git -C \"{m}\" reset --hard\necho \"$(true\nmore",
            # 末尾の `\` は shell が捨てる
            f'git -C "{m}" reset --hard \\',
        ))
        # 閉じない引用符を含む行そのものはどちらのシェルも実行しない
        self.assertAllowed((f"git -C \"{m}\" reset --hard 'oops", f"echo 'x\ny' && git status 'z"))

    def test_command_substitution_and_backticks(self):
        m = self.main
        # 二重引用符の中の `$(cat <<'EOF' ...)` の本文 (引用符や git を含む) は読まない
        body = f'fix: "quoted" don\'t\ncd "{m}" && git reset --hard\n'
        odd = f'fix: 5" screen\ncd "{m}" && git reset --hard\n'  # 本文の `"` が奇数個
        self.assertAllowed((
            f"git commit -m \"$(cat <<'EOF'\n{body}EOF\n)\"",
            f"git commit -m \"$(cat <<'EOF'\n{odd}EOF\n)\"",
        ))
        self.assertDenied((
            f"git -C \"{m}\" commit -m \"$(cat <<'EOF'\n{body}EOF\n)\"",
            f"git commit -m \"$(cat <<'EOF'\n{body}EOF\n)\" && git -C \"{m}\" stash",
            # `$(...)` の閉じは単語の途中。直後の `#` は comment ではない
            f'echo $(pwd)#x && git -C "{m}" reset --hard',
            f'cat <(pwd)#x && git -C "{m}" reset --hard',
            f'tee >(cat)#x && git -C "{m}" reset --hard',
            # backtick の中の comment は閉じの手前まで
            f'echo `echo a #b` && git -C "{m}" reset --hard',
            # 同じ行で閉じるコマンド置換の中の here-document は、外の行を本文にしない
            f'echo $(cat <<E) && git status\ngit -C "{m}" reset --hard',
        ))
        # 二重引用符の中の `$(...)` は行き先が静的に決まらないものとして扱う (中身は読まない)
        out = evaluate(bash('cd "$(git rev-parse --show-toplevel)" && git reset --hard', self.a))
        self.assertEqual(decision(out), "context")

    def test_heredoc_operator_in_comment_or_quotes_is_not_a_heredoc(self):
        m = self.main
        self.assertDenied((
            f'git status  # a<<E\ngit -C "{m}" reset --hard\nE',
            f'echo "<<E"\ngit -C "{m}" reset --hard\nE',
            f"echo '<<E'\ngit -C \"{m}\" reset --hard\nE",
        ))
        # 本物の here-document の本文は読まない (行末の comment があっても、継続があっても)
        self.assertAllowed((
            f'cat > x.sh <<E  # note\ngit -C "{m}" reset --hard\nE',
            f'cat <<E \\\n> x.sh\ngit -C "{m}" reset --hard\nE\ngit status',
        ))
        self.assertDenied((
            f'cat <<E\nbody\nE\ngit -C "{m}" reset --hard  # done',
            # `<<-` の終端は行頭の tab を外して比べる
            f'cat <<-E\n\tbody\n\tE\ngit -C "{m}" reset --hard',
        ))


class HeredocDelimiterFormsTest(test_guard.GuardBase):
    """識別子以外の区切り語 (`\\EOF` / 空白を含む / 数字始まり) の here-document。

    区切り語を読み損ねると本文をコードとして読み、本文の `'` や `(` が閉じないまま残って、
    その行から後ろの git をすべて捨てていた (zsh では後ろの git が実行される)。
    """

    def test_commit_message_heredoc_with_unusual_delimiter(self):
        m = self.main
        for op, d in (("<<\\EOF", "EOF"), ("<<'END MSG'", "END MSG"), ("<<'2EOF'", "2EOF")):
            for body in ("fix: don't x", "msg (with paren"):
                for cmd in (
                    f'git -C "{m}" commit -m "$(cat {op}\n{body}\n{d}\n)"',
                    f'git commit -m "$(cat {op}\n{body}\n{d}\n)" && git -C "{m}" reset --hard',
                    f'cat > n.md {op}\n{body}\n{d}\ngit -C "{m}" reset --hard',
                ):
                    with self.subTest(cmd=cmd):
                        self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_quoted_delimiter_is_compared_after_quote_removal(self):
        m = self.main
        for op, d in (('<<"EOF"', "EOF"), ("<<-'EOF'", "\tEOF"), ('<<E"O"F', "EOF"), ("<<2EOF", "2EOF"),
                      ("<<'END MSG'", "END MSG")):
            with self.subTest(op=op):
                # 本文の git は読まず、終端の次の行は読む
                self.assertIsNone(evaluate(bash(f'cat > x.sh {op}\ngit -C "{m}" reset --hard\n{d}\ngit status',
                                                self.a)))
                self.assertEqual(decision(evaluate(bash(f'cat {op}\nbody\n{d}\ngit -C "{m}" reset --hard',
                                                        self.a))), "deny")

    def test_delimiter_with_dollar_is_compared_literally(self):
        # bash / zsh は区切り語の `$x` / `${x}` を展開せずに終端行と比べ、`$'E'` は E と読む
        # (どちらのシェルでも終端の次の git が実行される)
        m = self.main
        for op, d in (("<<E$x", "E$x"), ("<<E${x}", "E${x}"), ("<<$'E'", "E"), ('<<"E\\"F"', 'E"F')):
            with self.subTest(op=op):
                cmd = f"cat > n.md {op}\nfix: don't x\n{d}\ngit -C \"{m}\" reset --hard"
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def test_outer_body_starts_after_the_command_substitution(self):
        # 外の here-document の本文は、同じ深さの改行から始まる。コマンド置換の中の改行からは始まらない
        # (bash / zsh とも、`; git` は実行され、本文は閉じの後の改行から)
        m = self.main
        for sub in ('"$(cat <<\\E\nfix: don\'t x\nE\n)"', '"$(cat <<\'E F\'\nin\nE F\n)"', '"$(echo a\necho b)"',
                    "$(echo a\necho b)", "`echo a\necho b`", "<(echo a\necho b)"):
            with self.subTest(sub=sub):
                cmd = f'cat <<\\E {sub} ; git -C "{m}" reset --hard\nouter\nE'
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # 閉じの後の改行から始まる本文の中の git は読まない
        self.assertIsNone(evaluate(bash(
            f'cat <<\\E "$(echo a\necho b)" ; git status\ngit -C "{m}" reset --hard\nE', self.a)))
        # サブシェルの `(` の中の改行では始まる
        self.assertEqual(decision(evaluate(bash(f'cat <<E ; (echo a\nE\ngit -C "{m}" reset --hard)', self.a))),
                         "deny")
        self.assertIsNone(evaluate(bash(f'cat <<E ; (echo a\ngit -C "{m}" reset --hard)\nE', self.a)))
        # 算術の `((` の中の改行では始まらない (bash / zsh とも `; git` を実行する)
        for arith in ("((1\n+2))", "echo $((1\n+2))"):
            with self.subTest(arith=arith):
                cmd = f'cat <<E ; {arith} ; git -C "{m}" reset --hard\nbody\nE'
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")
        # 単語に続けた `(...)` は zsh の glob の group で、閉じの後の `#` は comment ではない。zsh は引用符を次の行まで
        # 読み、here-document の本文はその後ろから始まる (`; git` が実行される。bash は構文エラーで何も実行しない)
        self.assertEqual(decision(evaluate(bash(
            f"cat <<'E F' x(a)#' ; b\n' ; git -C \"{m}\" reset --hard", self.a))), "deny")
        # その group の中の改行でも本文は始まらない (zsh は閉じの後の `; git` を実行する)
        self.assertEqual(decision(evaluate(bash(
            f'cat <<E x(a\nb) ; git -C "{m}" reset --hard\nbody\nE', self.a))), "deny")
        # 算術の閉じの直後の `#` は comment (サブシェルの閉じと同じ)
        self.assertEqual(decision(evaluate(bash(f"((1))#'\ngit -C \"{m}\" reset --hard", self.a))), "deny")

    def test_delimiter_with_command_substitution_is_not_cut_short(self):
        # `$(` や backtick を含む区切り語は here-document と読まない。手前の `E` だけを区切り語にすると、
        # shell が終端と見る `E$(echo x)` の行の後ろの git を本文として飛ばす
        m = self.main
        # zsh は区切り語に続けた `(...)` も区切り語に含める (`<<E(x)` の終端行は `E(x)`)
        for w in ("E$(echo x)", "E`echo x`", "E(x)", "E((1))#"):
            with self.subTest(w=w):
                cmd = f'cat <<{w}\nbody\n{w}\ngit -C "{m}" reset --hard\nE'
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")


class ReadOnlyStashAndApplyTest(test_guard.GuardBase):
    def test_reading_stash_and_apply_in_other_checkout_is_allowed(self):
        m = self.main
        for cmd in (
            f'git -C "{m}" stash list',
            f'git -C "{m}" stash show -p',
            f'git -C "{m}" stash show stash@{{1}}',
            f'git -C "{m}" stash create',
            f'cd "{m}" && git stash list',
            f'git -C "{m}" apply --check x.diff',
            f'git -C "{m}" apply --stat x.diff',
            f'git -C "{m}" apply --numstat x.diff',
            f'git -C "{m}" apply --summary x.diff',
            f'git -C "{m}" apply --check --index x.diff',
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_writing_stash_and_apply_in_other_checkout_is_denied(self):
        m = self.main
        for cmd in (
            f'git -C "{m}" stash',
            f'git -C "{m}" stash push -m x',
            f'git -C "{m}" stash pop',
            f'git -C "{m}" stash apply',
            f'git -C "{m}" stash drop',
            f'git -C "{m}" stash -u',
            # subcommand は最初の引数だけ。`-m list` は message が list の push
            f'git -C "{m}" stash -m list',
            f'git -C "{m}" stash -q list',
            f'git -C "{m}" apply x.diff',
            f'git -C "{m}" apply --index x.diff',
            f'git -C "{m}" apply --check --apply x.diff',
            f'git -C "{m}" apply --stat --app x.diff',
            f'git -C "{m}" apply --numstat --ap x.diff',
            # `--` の後ろは option ではない
            f'git -C "{m}" apply -- --check',
        ):
            with self.subTest(cmd=cmd):
                out = evaluate(bash(cmd, self.a))
                self.assertEqual(decision(out), "deny")


if __name__ == "__main__":
    unittest.main()
