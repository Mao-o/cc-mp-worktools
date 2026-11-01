"""heredoc の本文の読み飛ばしと、複数の cd の合成 (command.find_invocations)。

期待値は、PATH に偽の gh だけを置いた /bin/bash (3.2) と zsh -f で各コマンドを実際に走らせ、
どの `gh pr ...` が走ったかを記録したもの (両シェルで一致した形だけを載せている)。
`--base m<N>` / `gh pr ready m<N>` は、どの呼び出しが走ったかを見分ける目印。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _testutil  # noqa: F401

from command import find_invocations, unresolved_reason


def _marks(cmd: str) -> list[str]:
    return [i.base or i.target or i.kind for i in find_invocations(cmd)]


# (区切り語の書き方, 終わりの行)
_DELIMITER_FORMS = {
    "bare": ("<<EOF", "EOF"),
    "single quoted": ("<<'EOF'", "EOF"),
    "double quoted": ('<<"EOF"', "EOF"),
    "backslash": ("<<\\EOF", "EOF"),
    "space in delimiter": ("<<'END MSG'", "END MSG"),
    "leading digit": ("<<'2EOF'", "2EOF"),
    "dash strips tabs": ("<<-EOF", "\tEOF"),
    "dash quoted strips tabs": ("<<-'EOF'", "\t\tEOF"),
    "space after operator": ("<< EOF", "EOF"),
    "partly quoted": ('<<E"O"F', "EOF"),
    "escaped space": ("<<a\\ b", "a b"),
    "escaped quote in double quotes": ('<<"a\\"b"', 'a"b'),
    "empty delimiter": ("<<''", ""),
}

# 本文に入れる、シェルのコードとして読むと解析を壊す / 誤読させる行
_BODIES = {
    "odd single quote": "It's done",
    "two odd quotes": "I don't know if it's ok",
    "open paren": "fix (part 1",
    "hash": "# not a comment, it's text",
    "double quote": 'say "hi',
    "backslash at end": "trailing \\\\",
}


class HeredocBodyTest(unittest.TestCase):
    def assert_clean(self, cmd: str, want: list[str]) -> None:
        found = find_invocations(cmd)
        self.assertEqual([i.base or i.target or i.kind for i in found], want, cmd)
        self.assertTrue(all(i.parsed for i in found), cmd)
        self.assertIsNone(unresolved_reason(cmd, found), cmd)

    def test_ticket_forms(self):
        self.assert_clean("cat > b.md <<'EOF'\nIt's done\nEOF\ngh pr create --body-file b.md", ["create"])
        self.assert_clean("gh pr create --body-file - <<'EOF'\nI don't know\nEOF", ["create"])

    def test_every_delimiter_form_and_body(self):
        templates = {
            "file then create": ("cat > b.md {op}\n{body}\n{end}\ngh pr create --base m1 --body-file b.md", ["m1"]),
            "stdin create": ("gh pr create --base m1 --body-file - {op}\n{body}\n{end}", ["m1"]),
            "commit then create": ("git commit -F - {op}\n{body}\n{end}\ngh pr create --base m1 --fill", ["m1"]),
            "create then ready": (
                "gh pr create --draft --base m1 --body-file - {op}\n{body}\n{end}\ngh pr ready m2",
                ["m1", "m2"],
            ),
            "pipe on the operator line": ("cat {op} | gh pr create --base m1 --body-file -\n{body}\n{end}", ["m1"]),
            "two bodies": ("cat {op} {op}\n{body}\n{end}\n{body}\n{end}\ngh pr create --base m1", ["m1"]),
        }
        for form, (op, end) in _DELIMITER_FORMS.items():
            for body_label, body in _BODIES.items():
                for tlabel, (tmpl, want) in templates.items():
                    cmd = tmpl.format(op=op, body=body, end=end)
                    with self.subTest(form=form, body=body_label, template=tlabel):
                        self.assert_clean(cmd, want)

    def test_terminator_must_match_the_whole_line(self):
        # shell は mA を本文の一部と読み、mB だけを走らせる (bash / zsh 実測)
        cases = {
            "trailing space": "cat <<EOF\nx'\nEOF \ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "leading space": "cat <<EOF\nx'\n EOF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "dash with spaces": "cat <<-EOF\nx'\n  EOF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "dash with tab then space": "cat <<-EOF\nx'\n\t EOF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "carriage return": "cat <<EOF\nx'\nEOF\r\ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "unquoted: backslash-newline joins": (
                "cat <<EOF\nab\\\nEOF\ngh pr create --base mA\nEOF\ngh pr create --base mB"
            ),
            "inside $( ) the ) is not part of the line": "x=$(cat <<'EOF'\nIt's\nEOF)\ngh pr create --base mA",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assertNotIn("mA", _marks(cmd))
        # 安全網 (加工前の文字列で数える) は本文の行頭の gh pr create も数えるので止める側に倒れる
        self.assertIsNotNone(unresolved_reason(cases["trailing space"], find_invocations(cases["trailing space"])))

    def test_terminator_after_joined_or_unjoined_lines(self):
        cases = {
            # 引用の無い区切り語: `EO\`+改行+`F` はつないで EOF になり本文が終わる
            "unquoted: joined into the delimiter": (
                "cat <<EOF\nEO\\\nF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
                ["mA", "mB"],
            ),
            "unquoted: escaped backslash does not join": (
                "cat <<EOF\nab\\\\\nEOF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
                ["mA", "mB"],
            ),
        }
        # 引用した (一部でもエスケープ・引用した) 区切り語では `\`+改行をつながない
        for op in ("<<'EOF'", '<<"EOF"', "<<\\EOF", "<<E'O'F", '<<E"O"F', "<<E\\OF"):
            cases[f"quoted {op}: no join"] = (
                f"cat {op}\nab\\\nEOF\ngh pr create --base mA\nEOF\ngh pr create --base mB",
                ["mA", "mB"],
            )
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(_marks(cmd), want)

    def test_body_starts_after_the_operator_line(self):
        cases = {
            "command after ; on the operator line": (
                "cat <<EOF; gh pr create --base m1\nx'\nEOF\ngh pr ready m2",
                ["m1", "m2"],
            ),
            "comment on the operator line": ("cat <<EOF # it's\nbody'\nEOF\ngh pr create --base m1", ["m1"]),
            "quote spanning a line on the operator line": (
                "cat <<EOF; echo 'a\nb'\nbody'\nEOF\ngh pr create --base m1",
                ["m1"],
            ),
            "continuation on the operator line": ("cat <<EOF \\\n > f\nbody'\nEOF\ngh pr create --base m1", ["m1"]),
            "delimiter followed by ;": ("cat <<EOF;\nx'\nEOF\ngh pr create --base m1", ["m1"]),
            "two on one line": ("cat <<A; cat <<'B'\na'\nA\nb'\nB\ngh pr create --base m1", ["m1"]),
            "inside a function": ("f() { cat <<EOF\nx'\nEOF\n}; f\ngh pr create --base m1", ["m1"]),
            "inside case": ("case a in a) cat <<EOF\nx'\nEOF\n;; esac\ngh pr create --base m1", ["m1"]),
            "let shift is a heredoc": ("let x=1<<2\nx'\n2\ngh pr create --base m1", ["m1"]),
            "heredoc after closed arithmetic": ("echo $(( (1+2)<<1 ))\ncat <<'EOF'\nIt's\nEOF\ngh pr create --base m1", ["m1"]),
            "delimiter with $": ("cat <<$X\nx'\n$X\ngh pr create --base m1", ["m1"]),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assert_clean(cmd, want)

    def test_not_a_heredoc(self):
        cases = {
            "here-string": "cat <<< \"it's\"\ngh pr create --base m1",
            "arithmetic shift": "echo $((1<<2))\ngh pr create --base m1",
            "arithmetic command": "(( x = 1<<2 ))\ngh pr create --base m1",
            "nested parens in arithmetic": "echo $(( (1)+(2)<<1 ))\ngh pr create --base m1",
            "inside single quotes": "echo '<<EOF'\ngh pr create --base m1",
            "inside double quotes": 'echo "<<EOF"\ngh pr create --base m1',
            "inside a comment": "echo a # <<EOF\ngh pr create --base m1",
            "comment after a continuation": "echo a \\\n# <<EOF\ngh pr create --base m1",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assert_clean(cmd, ["m1"])
        # `${...}` の中の `<<` は展開の語。後ろの行は本文ではなくコマンドとして走る (bash / zsh 実測)
        for label, (cmd, want) in {
            "parameter expansion": ("echo ${x:-1<<2}\ngh pr create --base mA\n2\ngh pr create --base mB", ["mA", "mB"]),
            "nested parameter expansion": (
                "echo ${x:-${y:-a<<b}}\ngh pr create --base mA\nb\ngh pr create --base mB",
                ["mA", "mB"],
            ),
            # 展開が閉じた後の heredoc は heredoc
            "heredoc after a closed expansion": ("echo ${x:-{a}}; cat <<'EOF'\nIt's\nEOF\ngh pr create --base mB", ["mB"]),
            # 展開の中の改行は語の一部。本文は展開が閉じた行の次から始まる
            "newline inside an expansion": ("cat <<EOF ${x:-a\nb}\nbody'\nEOF\ngh pr create --base mB", ["mB"]),
            "delimiter line inside an expansion": ("cat <<EOF ${x:-a\nEOF\n}\nIt's\nEOF\ngh pr create --base mB", ["mB"]),
            "space after << inside an expansion": (
                "echo ${x:-1<<2 }\ngh pr create --base mA\n2\ngh pr create --base mB",
                ["mA", "mB"],
            ),
            # bash は最初の `}` で展開を閉じて `<<2}` を heredoc と読み (mB だけ走る)、zsh は構文エラー。
            # 波括弧を入れ子で数えて両方の行をコードとして読む (shell が走らせうる gh を見失わない側)
            "inner braces inside an expansion": (
                "echo ${x:-{a}1<<2}\ngh pr create --base mA\n2}\ngh pr create --base mB",
                ["mA", "mB"],
            ),
        }.items():
            with self.subTest(label):
                self.assertEqual(_marks(cmd), want)

    def test_heredoc_inside_a_command_substitution(self):
        # 本文が始まる前に置換が閉じる heredoc は本文を持たない (bash / zsh 実測: mA も走る)
        for cmd in (
            "echo $(cat <<2)\ngh pr create --base mA\n2\ngh pr create --base mB",
            "echo $(cat <<'EOF')\ngh pr create --base mA\nEOF\ngh pr create --base mB",
            "echo `cat <<2`\ngh pr create --base mA\n2\ngh pr create --base mB",
            "echo $( (cat <<2) )\ngh pr create --base mA\n2\ngh pr create --base mB",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(_marks(cmd), ["mA", "mB"])
        # サブシェル `( )` / グループ `{ }` の後では本文が続く (mA は本文)
        for cmd in (
            "(cat <<2)\ngh pr create --base mA\n2\ngh pr create --base mB",
            "{ cat <<2; }\ngh pr create --base mA\n2\ngh pr create --base mB",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(_marks(cmd), ["mB"])
        # 置換の中で本文が始まる heredoc は、本文の終わりが bash (閉じ括弧で切る) と zsh (区切り語の行まで)
        # で割れるので、従来どおり本文もコードとして読む (本文の ' は止める側に倒れる)
        for cmd in (
            "x=$(cat <<'EOF'\nIt's\nEOF\n)\ngh pr create --base m1",
            "x=`cat <<EOF\nIt's\n`\ngh pr create --base m1",
            # 置換の中のサブシェルの `)` で置換を閉じたと読まない
            "x=$( (true) ; cat <<'EOF'\nIt's\nEOF\n)\ngh pr create --base m1",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))
        self.assertEqual(_marks("x=$(cat <<EOF\nbody\n)\ngh pr create --base mB"), ["mB"])
        # そのときはコマンド全体を従来の読み方に戻す: 後ろの heredoc だけ読み飛ばすと、ずれた引用符の対応が
        # 偶然そろって「解析できる」になり、zsh が走らせる gh を見失う
        cmd = (">/dev/null gh pr create --base m1 $(cat <<-'EOF'\nx'\n\tEOF\n) <<'END MSG'\nab\\\nEND MSG\n"
               "gh pr create --base m2")
        self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))
        self.assertEqual(_marks("echo \\<<EOF\ngh pr create --base mA\nEOF\ngh pr create --base mB"), ["mA", "mB"])

    def test_safety_nets_still_read_the_raw_body(self):
        cases = {
            # 引用の無い区切り語の本文のコマンド置換は shell が実行する
            "substitution in an unquoted body": "cat <<EOF\n$(gh pr create --base m1)\nEOF",
            "backtick in an unquoted body": "cat <<EOF\n`gh pr create`\nEOF",
            # 終わりの行が無い (本文がコマンドの最後まで続く)
            "unterminated": "cat <<EOF\nx'\ngh pr create",
            "nested shell in a body": "cat <<'EOF'\nbash -c 'gh pr create'\nEOF",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_quoted_body_text_is_not_counted_by_the_safety_net(self):
        # 区切り語に引用がある本文は shell が展開しない文字列。PR の本文で使い方を説明しても止めない
        # (0.3.3 の初版は、安全網が加工前の本文の行頭の gh pr create を数えて止めていた)
        bodies = (
            "gh pr create --fill",
            "$(gh pr create --base x)",
            "`gh pr ready 3`",
            "run: gh pr create; gh pr ready",
        )
        for op, end in (("<<'EOF'", "EOF"), ('<<"EOF"', "EOF"), ("<<\\EOF", "EOF"), ("<<'END MSG'", "END MSG"),
                        ("<<-'EOF'", "\tEOF"), ("<<E'O'F", "EOF")):
            for body in bodies:
                cmd = f"cat > b.md {op}\nIt's\n{body}\n{end}\ngh pr create --base m1 --body-file b.md"
                with self.subTest(op=op, body=body):
                    self.assert_clean(cmd, ["m1"])
        self.assert_clean("gh pr create --base m1 --body-file - <<'EOF'\ngh pr create --fill\nEOF", ["m1"])

    def test_quoted_body_is_counted_when_something_may_run_it(self):
        # 本文を標準入力から読んで実行しうるコマンド (sh / bash / eval / source / .) があれば本文も数える
        for cmd in (
            "bash <<'EOF'\ngh pr create\nEOF",
            "sudo sh -s <<'EOF'\ngh pr create\nEOF",
            "/bin/zsh <<'EOF'\ngh pr ready 3\nEOF",
            "ssh host <<'EOF'\ngh pr create\nEOF",
            "source /dev/stdin <<'EOF'\ngh pr create\nEOF",
            ". /dev/stdin <<'EOF'\ngh pr create\nEOF",
            "cat <<'EOF' | bash\ngh pr create\nEOF",
            # 引用の無い区切り語の本文は従来どおり加工前を数える
            "cat <<EOF\ngh pr create --fill\nEOF\ngh pr create --base m1",
            # 終わりの行が無い本文は、区切り語の読み違いで shell と本文の範囲がずれていることがあるので数える
            "cat <<'EOF'\nIt's\ngh pr create --base m1",
            "cat <<'EOF' ${x:-a\nEOF\n}\ngh pr create --fill\nEO",
            # 置換の中で本文が始まり全体を従来の読み方に戻したときは、その前の本文も数える
            "cat > b.md <<'EOF'\n`gh pr ready 3`\nEOF\nx=$(cat <<'E2'\nplain\nE2\n)\ngh pr create --base m1",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))
        # ファイル名の .sh や、実行位置にない . は本文を読むコマンドではない
        for cmd in (
            "cat > run.sh <<'EOF'\ngh pr create --fill\nEOF\ngh pr create --base m1",
            "git add . && cat > b.md <<'EOF'\ngh pr create\nEOF\ngh pr create --base m1",
            "cd .. && cat > b.md <<'EOF'\ngh pr create\nEOF\ngh pr create --base m1",
        ):
            with self.subTest(cmd=cmd):
                self.assert_clean(cmd, ["m1"])

    def test_safety_net_reads_words_split_by_a_continuation(self):
        # `\`+改行でつながる gh / pr は、継続行をつないでから「gh pr を含まない」を判定する (bash / zsh は走らせる)
        for cmd in ("g\\\nh pr create --base m1", "gh p\\\nr create --base m1"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_redirect_runs_do_not_take_exponential_time(self):
        # 実行位置の前置きの繰り返しは、1 つのリダイレクトの読み方が 1 通りでないと、一致しないときの
        # 後戻りが個数に対して指数時間になる (0.3.3 の初版は 24 個で 5 秒)。hook が時間切れになると
        # コマンドは素通しになるので、別プロセスで時間を測る
        script = (
            "import sys, time\n"
            "from command import find_invocations, unresolved_reason\n"
            "worst = 0.0\n"
            "for tok in ('2>&1 ', '>>build.log ', \">'f' \", '>\"$f\" ', '<<- x ', '>& f '):\n"
            "    cmd = 'true; ' + tok * 40 + 'make gh pr'\n"
            "    t = time.perf_counter()\n"
            "    unresolved_reason(cmd, find_invocations(cmd))\n"
            "    worst = max(worst, time.perf_counter() - t)\n"
            "print(worst)\n"
        )
        pkg = Path(__file__).resolve().parent.parent
        try:
            r = subprocess.run([sys.executable, "-c", script], cwd=pkg, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("リダイレクトの繰り返しで 20 秒を超えた")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(float(r.stdout), 1.0)

    def test_safety_net_reads_redirect_operators_as_the_shell_does(self):
        # shell の演算子どおりに読む: `>- x` の `-` はファイル名、`<<- gh` の gh は区切り語で、どちらも
        # 後ろの語はコマンド語 (bash / zsh 実測: gh は走らない)。数えすぎると止める理由が事実と合わない
        for cmd in (">- echo gh pr create", "<<- gh pr create\ngh\n"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(unresolved_reason(cmd, find_invocations(cmd)))
        for cmd in ("2>- gh pr create --base m1", "<&- gh pr create --base m1", "2>& 1 gh pr create --base m1",
                    "&>> f gh pr create --base m1", "<<-x gh pr create --base m1\nx"):
            with self.subTest(cmd=cmd):
                self.assert_clean(cmd, ["m1"])

    def test_redirect_before_the_command_word(self):
        # 本文の ' で止めていた頃は隠れていた: コマンド語の前のリダイレクトで gh を見失わない (bash / zsh 実測)
        cases = {
            "heredoc": ("<<'EOF' gh pr create --base m1 --body-file -\nIt's\nEOF", ["m1"]),
            "two heredocs": ("V=1 <<-EOF <<-'EOF' gh pr create --base m1\nx'\n\tEOF\ny'\n\tEOF", ["m1"]),
            "stderr": ("2>/dev/null gh pr create --base m1", ["m1"]),
            "dup": ("2>&1 gh pr ready m1", ["m1"]),
            "here-string": ("<<<x gh pr create --base m1", ["m1"]),
            "both streams": ("&>/dev/null gh pr create --base m1", ["m1"]),
            # shlex が区切りとリダイレクトを 1 token にまとめる形 (旧版は止めずに通していた)
            "newline then redirect": ("true\n>/dev/null gh pr create --base m1", ["m1"]),
            "; then redirect": ("true;>/dev/null gh pr create --base m1", ["m1"]),
            "&& then redirect": ("true&&>/dev/null gh pr create --base m1", ["m1"]),
            "newline then &>": ("true\n&>/dev/null gh pr create --base m1", ["m1"]),
            "newline then heredoc": ("true\n<<'END MSG' gh pr create --base m1\nIt's\nEND MSG", ["m1"]),
            "pipe then here-string": ("true|<<<x gh pr create --base m1", ["m1"]),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assert_clean(cmd, want)
        inv = find_invocations(">/dev/null cd plugins && gh pr create")[0]
        self.assertEqual(inv.cd_steps, ("plugins",))

    def test_safety_net_counts_redirect_prefixed_invocations(self):
        # 構文解析が見つけるリダイレクト付きの gh を安全網も数える。数えないと、関数定義の中の gh
        # (構文解析は見失い、安全網は数える) と数が合って止まらなくなる
        for prefix in (">/dev/null ", "2>&1 ", "&>f ", "<<<x ", "<<'END MSG' "):
            cmd = f"{prefix}gh pr create --base m1; f() {{ gh pr create; }}"
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_cd_in_a_body_is_not_followed(self):
        inv = find_invocations("cat <<'EOF'\ncd ../out\nEOF\ngh pr create")[0]
        self.assertIsNone(inv.cd)

    def test_unreadable_delimiter_keeps_the_old_reading(self):
        # 区切り語の引用符が閉じていなければ heredoc として扱わない (本文をコードとして読む = 止める側)
        cmd = "cat <<'EOF\nbody\ngh pr create"
        found = find_invocations(cmd)
        self.assertIsNotNone(unresolved_reason(cmd, found))
        # 区切り語の引用符の中の改行: 区切り語として扱わず、従来どおりに読む
        self.assertEqual(_marks("cat <<'EOF\nIt'\ngh pr create --base m1"), ["m1"])


class CdCompositionTest(unittest.TestCase):
    def first(self, cmd):
        found = find_invocations(cmd)
        self.assertTrue(found, cmd)
        return found[0]

    def test_ticket_reproduction(self):
        inv = self.first("cd plugins/x && make test && cd ../.. && gh pr create --fill")
        self.assertEqual(inv.cd_steps, ("plugins/x", "../.."))
        self.assertEqual(inv.cd, "plugins/x/../..")
        self.assertIsNone(inv.cd_unresolved)

    def test_single_cd_is_unchanged(self):
        cases = {
            "relative": ("cd plugins/x && gh pr create", "plugins/x"),
            "absolute": ("cd /abs/repo && gh pr create", "/abs/repo"),
            "home": ("cd ~/repo && gh pr create", "~/repo"),
            "subshell": ("(cd x)&& gh pr create", "x"),
            "env wrapper": ("env cd x; gh pr create", "x"),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                inv = self.first(cmd)
                self.assertEqual((inv.cd, inv.cd_steps, inv.cd_unresolved), (want, (want,), None))

    def test_absolute_replaces(self):
        cases = {
            "slash": ("cd a && cd /abs && gh pr create", ("/abs",)),
            "tilde": ("cd a && cd ~/x && gh pr create", ("~/x",)),
            "after unresolvable": ('cd "$X" && cd /abs && gh pr create', ("/abs",)),
            "after subshell": ("(cd a) && cd /abs && gh pr create", ("/abs",)),
            "root then relative": ("cd / && cd tmp && gh pr create", ("/", "tmp")),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                inv = self.first(cmd)
                self.assertEqual((inv.cd_steps, inv.cd_unresolved), (want, None))
        self.assertEqual(self.first("cd / && cd tmp && gh pr create").cd, "/tmp")

    def test_effective_forms_compose(self):
        # どれも shell の cwd を変える (bash / zsh 実測。`command cd` は bash のみ。zsh では gh が走らない)
        for prefix in ("", "time ", "command ", "A=1 ", "! "):
            cmd = f"cd a && {prefix}cd b && gh pr create"
            with self.subTest(prefix=prefix):
                inv = self.first(cmd)
                self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("a", "b"), None))
        for cmd in (
            "cd a && { cd b; } && gh pr create",
            "cd a && if cd b; then gh pr create; fi",
            "cd a; cd b || exit; gh pr create",
            "cd a\ncd b\ngh pr create",
            # 区切りの直後のリダイレクト: `&&>` は `&&` + `>`、改行 + `&>` は改行 + `&>` (どちらも `&` ではない)
            "cd a&&>/dev/null cd b && gh pr create",
            "cd a\n&>/dev/null cd b && gh pr create",
        ):
            with self.subTest(cmd=cmd):
                inv = self.first(cmd)
                self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("a", "b"), None))

    def test_unresolvable_steps(self):
        cases = {
            "variable first": ('cd "$WT" && cd sub && gh pr create', "$WT"),
            "variable later": ('cd sub && cd "$WT" && gh pr create', "$WT"),
            "backtick": ("cd a && cd `pwd` && gh pr create", "`pwd`"),
            "oldpwd": ("cd a && cd - && gh pr create", "-"),
            "single oldpwd": ("cd - && gh pr create", "-"),
            "option": ("cd -P plugins && gh pr create", "-P"),
            "end of options": ("cd -- plugins && gh pr create", "--"),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.first(cmd).cd_unresolved, want)

    def test_relative_cd_after_a_cd_that_may_not_move_the_shell(self):
        # 先の cd がシェルの cwd を変えない (bash / zsh 実測) か、シェルで読みが割れる形
        cases = {
            "subshell": "(cd plugins/x) && cd ../.. && gh pr create",
            "command substitution": "x=$(cd a) ; cd b && gh pr create",
            "env": "env cd .. && cd repo && gh pr create",
            "nohup": "nohup cd ..; cd repo; gh pr create",
            "exec": "exec cd ..; cd repo; gh pr create",
            "pipe left": "cd .. | true; cd repo; gh pr create",
            "pipe right (zsh moves, bash does not)": "true | cd ..; cd repo; gh pr create",
            "background": "cd .. & cd repo && gh pr create",
            "relative inside subshell": "cd a && (cd b && gh pr create)",
            # case の `)` で深さが負にならない (負になると後のサブシェルを深さ 0 と読む)
            "subshell after case": "case a in a) true;; esac\n(cd x) && cd y && gh pr create",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                inv = self.first(cmd)
                self.assertIsNotNone(inv.cd_unresolved)
                self.assertIn("→", inv.cd_unresolved)

    def test_cd_around_or(self):
        # `cd A || cd B`: A に移れれば B は走らず、移れなければ起点からの B になる。どちらかに読むと
        # shell と違う repo を検査しうるので、続く相対の cd とまとめて静的に解決できないとする
        for cmd in (
            "cd a || cd b && gh pr create",
            "cd a || cd b; cd c && gh pr create",
            "true || cd a; cd b && gh pr create",
            "cd a || true; cd b && gh pr create",
            # 走らないかもしれない cd は、絶対パスでも前の移動先を置き換えるとは限らない
            "cd a || cd /abs && gh pr create",
            "cd a || cd ~/x; gh pr create",
            "cd a && (cd /abs && gh pr create)",
        ):
            with self.subTest(cmd=cmd):
                inv = self.first(cmd)
                self.assertIsNotNone(inv.cd_unresolved)
                self.assertIn("→", inv.cd_unresolved)
        # `cd a || exit` は、移れなければ後ろが走らないので a に決まる
        for cmd in (
            "cd a || exit; cd b && gh pr create",
            "cd a || exit 1; cd b && gh pr create",
            "cd a || return 1\ncd b\ngh pr create",
            "cd a || { exit 1; }; cd b && gh pr create",
        ):
            with self.subTest(cmd=cmd):
                inv = self.first(cmd)
                self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("a", "b"), None))
        inv = self.first("cd a; cd /abs || exit; gh pr create")
        self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("/abs",), None))

    def test_bare_cd_resets_to_the_hook_cwd(self):
        inv = self.first("cd plugins && cd && gh pr create")
        self.assertEqual((inv.cd, inv.cd_steps, inv.cd_unresolved), (None, (), None))

    def test_each_invocation_gets_the_cd_at_its_position(self):
        found = find_invocations("cd plugins/x && gh pr create --draft && cd ../.. && gh pr ready")
        self.assertEqual([i.cd_steps for i in found], [("plugins/x",), ("plugins/x", "../..")])


if __name__ == "__main__":
    unittest.main()
