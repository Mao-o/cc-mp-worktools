"""代入語の値の形と、二重引用符の中のコマンド置換の heredoc (command.find_invocations / unresolved_reason)。

期待値は、PATH に偽の gh だけを置いた /bin/bash (3.2) と zsh -f で実際に走らせて確かめた形。
`--base m<N>` / `gh pr ready m<N>` は、どの呼び出しを見つけたかを見分ける目印。
"""
from __future__ import annotations

import itertools
import subprocess
import sys
import unittest
from pathlib import Path

import _testutil  # noqa: F401

import command


def find_invocations(cmd: str) -> list[command.Invocation]:
    """例外は hook では「止める」になるので、テストでは判定の誤りとして数える。"""
    try:
        return command.find_invocations(cmd)
    except (ValueError, RecursionError, IndexError) as e:
        raise AssertionError(f"例外: {e!r}: {cmd}") from e


def unresolved_reason(cmd: str, found: list[command.Invocation]) -> str | None:
    try:
        return command.unresolved_reason(cmd, found)
    except (ValueError, RecursionError) as e:
        raise AssertionError(f"例外: {e!r}: {cmd}") from e


def _first(cmd: str) -> command.Invocation:
    found = find_invocations(cmd)
    if not found:
        raise AssertionError(f"PR 操作を見つけていない: {cmd}")
    return found[0]

_ODD_BODIES = {
    "odd double quote": 'say "hi',
    "odd single quote": "It's",
    "both": "say \"hi\nIt's",
    "balanced parens": '(a) say "hi',
    "open paren": 'fix (part 1 "x',
    "hash": '# say "hi',
    "mention mid line": 'run gh pr create "x',
    "backslash at end": 'say "hi \\\\',
}
_DELIMITERS = {
    "bare": ("<<EOF", "EOF"),
    "single quoted": ("<<'EOF'", "EOF"),
    "double quoted": ('<<"EOF"', "EOF"),
    "backslash": ("<<\\EOF", "EOF"),
    "space in delimiter": ("<<'END MSG'", "END MSG"),
    "leading digit": ("<<'2EOF'", "2EOF"),
    "dash strips tabs": ("<<-EOF", "\tEOF"),
}


def _marks(cmd: str) -> list[str]:
    return [i.base or i.target or i.kind for i in find_invocations(cmd)]


class _Base(unittest.TestCase):
    def assert_clean(self, cmd: str, want: list[str]) -> None:
        found = find_invocations(cmd)
        self.assertEqual([i.base or i.target or i.kind for i in found], want, cmd)
        self.assertTrue(all(i.parsed for i in found), cmd)
        self.assertIsNone(unresolved_reason(cmd, found), cmd)

    def assert_stops(self, cmd: str) -> None:
        self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)), cmd)


class AssignmentValueTest(_Base):
    """代入語の値は読まない。値に空白や置換がある形の後ろの PR 操作は、数えて止める (どれも bash / zsh とも gh が走る)。"""

    _STOPPED_VALUES = {
        "substitution": "$(echo a)",
        "space": "$(echo a b)",
        "pipe": "$(cat f | head -1)",
        "semicolon": "$(a; b)",
        "nested": "$(echo $(echo a))",
        "deeply nested": "$(a $(b $(c)))",
        "quotes inside double quotes": '"$(echo "a b")"',
        "backtick with space": "`echo a b`",
        "parameter expansion with space": "${y:-a b}",
        "parameter expansion with a separator": "${y:-a;b}",
        "substitution in an expansion": "${y:-$(a b)}",
        "arithmetic": "$((1 + 2))",
        "array": "(a b)",
        "process substitution": "<(echo a)",
        "text after": "$(echo a)b",
        "hash after": "$(echo a)#b",
        "empty quotes after": '$(echo a)""',
        "escaped space after": "$(echo a)\\ ",
        "single quote in a substitution": "$(echo 'a)')",
        "double quote in a substitution": '$(echo ")")',
        "gh without a PR operation": "$(gh auth token)",
        "cd in the value": "$(cd a && pwd)",
    }
    _PREFIXES = {
        "none": ("", ""),
        "env": ("env ", ""),
        "time": ("time ", ""),
        "redirect": (">f ", ""),
        "fd redirect": ("2>&1 ", ""),
        "and": ("true && ", ""),
        "semicolon": ("true; ", ""),
        "newline": ("true\n", ""),
        "subshell": ("(", ")"),
        "group": ("{ ", "; }"),
        "if": ("if true; then ", "; fi"),
        "two assignments": ("A=$(a b) ", ""),
        "cd and": ("cd a && ", ""),
        "cd semicolon": ("cd a; ", ""),
        "cd newline": ("cd a\n", ""),
        "two cds": ("cd a && cd b; ", ""),
        "subshell cd": ("(cd d); ", ""),
        "pipe cd": ("cd d | cat; ", ""),
        "or cd": ("cd d || true; ", ""),
        "if cd": ("if false; then cd d; fi; ", ""),
        "for cd": ("for i in; do true; cd d; done; ", ""),
        "wrapper cd": ("env cd d; ", ""),
        "no argument cd": ("cd; ", ""),
        "chdir": ("chdir d; ", ""),
        "pushd": ("pushd d; ", ""),
        "popd": ("popd; ", ""),
        "eval": ('eval "cd d"; ', ""),
        "source": ("source f; ", ""),
        "dot": (". f; ", ""),
        "builtin": ("builtin cd d; ", ""),
        "expansion": ("$c d; ", ""),
    }

    def test_value_with_space_or_substitution_stops(self):
        for (vlabel, value), (plabel, (pre, post)) in itertools.product(self._STOPPED_VALUES.items(), self._PREFIXES.items()):
            cmd = f"{pre}X={value} gh pr create --base m1{post}"
            with self.subTest(value=vlabel, prefix=plabel):
                self.assert_stops(cmd)

    def test_operation_only_in_the_value_stops(self):
        for pre, _post in self._PREFIXES.values():
            for value in (
                "X=`gh pr create --base m1` true",
                'X="$(gh pr create --base m1)" true',
                "X=$(p q) Y=$(gh pr create --base m1) true",
                "X=$(gh pr create --base m1) gh pr ready m2",
                "X=$(a)$(gh pr create --base m1) gh pr ready m2",
                "x=$(V=$(echo $(echo a)) gh pr create --base m1)",
            ):
                with self.subTest(prefix=pre, value=value):
                    self.assert_stops(pre + value)

    def test_reading_around_a_value(self):
        # 語の数が合う形は、値を読み直さず語の並びで読む。代入の後ろに続く cd は移るが読まないので、検査先を決めずに止める
        for cmd, steps, unresolved in (
            ("X=$(a b) cd sub && gh pr create", (), True),
            ("cd a || X=$(b c) cd d; gh pr create", ("a",), True),
            ("X=$(cd a) cd b; gh pr create", (), True),
            ("X=$(cd a; gh pr create) true", ("a",), False),
            ("cd b; X=$(cd a; gh pr create) true", ("b", "a"), True),
        ):
            with self.subTest(cmd=cmd):
                self.assert_clean(cmd, ["create"])
                inv = _first(cmd)
                self.assertEqual((inv.cd_steps, inv.cd_unresolved is not None), (steps, unresolved))

    def test_continuation_and_redirect_after_the_value(self):
        self.assert_stops("X=\\\n$(echo a b) gh pr create --base m1")
        self.assert_stops("X=$(a)>f gh pr create --base m1")

    def test_value_in_one_shlex_word_keeps_the_old_reading(self):
        # 引用符の中に閉じた値は shlex が 1 語に読むので、これまでどおり検査する。値の中の cd は検査先を変えない
        for cmd in (
            'ROOT="$(git rev-parse --show-toplevel)" gh pr create --base m1',
            'ROOT="$(cd .. && pwd)" gh pr create --base m1',
            'X="$(cat f | head -1)" gh pr create --base m1',
            "X='a b' gh pr create --base m1",
            "X=a\\ b gh pr create --base m1",
            "X=1 gh pr create --base m1",
            "X=${Y:-a} gh pr create --base m1",
            # 改行の後ろの閉じ括弧は区切りと一緒に読まれ、後ろの gh が見つかる
            "X=$(echo a # )\n) gh pr create --base m1",
        ):
            with self.subTest(cmd=cmd):
                self.assert_clean(cmd, ["m1"])
                self.assertEqual(_first(cmd).cd_steps, ())

    def test_backslash_pairs_do_not_hide_what_the_plain_reading_counts(self):
        for inner in (
            'V=\\"p q\\" gh pr create --base m1',
            "V=\\$(p q) gh pr create --base m1",
            "V=\\`p q\\` gh pr create --base m1",
            '2>\\"p q\\" gh pr create --base m1',
        ):
            for form in (
                'bash -c "true; %s"',
                'nohup bash -c "true; %s"',
                'sh -c "cd a; %s"',
                'x="`%s`"',
                'echo "`%s`"',
                'echo "a `%s` b"',
            ):
                with self.subTest(form=form, inner=inner):
                    self.assert_stops(form % inner)
        self.assert_stops('bash -c "V=\\$(p q) gh pr create --base m1"')
        # 組で読まないと代入語に見えない形も、別の shell の中で数える
        self.assert_stops("bash -c 'V=\\ y gh pr create --base m1'")
        # 空白以外の `\X` と `$'...'` の中の `\'` は、組で読む方でしか語が閉じない
        self.assert_stops("bash -c 'V=\\\" gh pr create --base m1'")
        self.assert_stops("bash -c \"V=$'a\\'b' gh pr create --base m1\"")

    def test_long_prefix_with_backslash_pairs_stops(self):
        prefix = 'V=\\"p q\\" ' * (command._PREFIX_LIMIT + 1)
        self.assert_stops(f'echo "true; {prefix}gh pr create --base m1"')

    def test_escaped_character_in_a_value_is_counted(self):
        # 二重引用符の中の置換は構文解析が読まないので、安全網が数える
        odd = "git commit -m m -m \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\"; "
        for inner in ("V=\\ y", "V=\\'a", 'V=\\"a', "V=a\\ b", "2>&1 V=\\\n\\ y"):
            cmd = f'echo "$({inner} gh pr create --base m1)"'
            with self.subTest(inner=inner):
                self.assert_stops(cmd)
                # 本文の読み飛ばしで、本文の奇数個の引用符という止める理由が無くなる形
                self.assert_stops(odd + cmd)
        # `$'a\\'\\''` は 1 語で、後ろの gh は引用の中ではない
        self.assert_stops(">f V=$'a\\'\\''y gh pr create --base m1 '&$(|\\'")
        self.assert_stops('echo "$(>f\\ g gh pr create --base m1)"')
        self.assert_clean("V=a\\ b gh pr create --base m1", ["m1"])

    def test_assignment_in_backticks_is_counted(self):
        # backtick の中の代入語の後ろの gh も、$( の中と同じく見失わない (旧新とも走る形)
        for cmd in (
            "echo `V=$(echo $(echo a)) gh pr create --base m1`",
            "x=`V=$(echo $(echo a)) gh pr create --base m1`",
            'x="`V=$(echo $(echo a)) gh pr create --base m1`"',
            "echo `true; V=$(echo $(echo a)) gh pr create --base m1`",
            # $( の中の backtick と、エスケープした入れ子の backtick
            "x=$(echo `V=$(echo $(echo a)) gh pr create --base m1`)",
            "echo `echo \\`V=$(echo $(echo a)) gh pr create --base m1\\``",
            "echo `V=\\$(echo \\$(echo a)) gh pr create --base m1`",
            "echo `echo \\`echo \\\\\\`V=$(echo $(echo a)) gh pr create --base m1\\\\\\`\\``",
            "X=`V=$(echo $(echo a)) gh pr create --base m1` gh pr ready m2",
        ):
            with self.subTest(cmd=cmd):
                self.assert_stops(cmd)
        # 引数の言及は数えない
        for cmd in (
            "echo `echo gh pr create`",
            "echo `V=$(a b) echo gh pr create`",
            "echo `echo V=$(a b) gh pr create`",
            "echo `echo 'V=$(a b) gh pr create'`",
            "echo 'x `V=$(echo $(echo a)) gh pr create`'",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(find_invocations(cmd), [])
                self.assertIsNone(unresolved_reason(cmd, []))

    def test_function_keyword_body_is_read_like_a_function_definition(self):
        for cmd in (
            "function f { X=$(echo $(echo a)) gh pr create --base m1; }; f",
            "f() { X=$(echo $(echo a)) gh pr create --base m1; }; f",
            # 波括弧の無い本文 (zsh は走らせる)
            "f() X=$(echo $(echo a)) gh pr create --base m1; f",
            "function f {\nX=$(echo $(echo a)) gh pr create --base m1\n}; f",
        ):
            with self.subTest(cmd=cmd):
                self.assert_stops(cmd)
        # { の無い引数は前置きと読まない
        self.assertEqual(find_invocations("echo f X=$(a b) gh pr create"), [])
        self.assertIsNone(unresolved_reason("echo f X=$(a b) gh pr create", []))

    def test_assignment_that_is_not_in_the_prefix_is_left_alone(self):
        # 引数の位置の NAME=... はコマンド語の前置きではない (後ろの gh は echo の引数)
        for cmd in (
            "echo X=$(date) gh pr create",
            "echo X=$(a b) Y=$(c) gh pr create",
            'echo "X=$(date) gh pr create"',
            "echo 'X=$(date) gh pr create'",
            "make FOO=$(x) gh pr create",
            "cd sub; echo >&2 X=$(a) gh pr create",
            "echo a >| f X=$(a) gh pr create",
            "echo X=$(a) gh pr create # X=$(b) gh pr create",
        ):
            with self.subTest(cmd=cmd):
                self.assert_clean(cmd, [])

    def test_assignment_without_a_following_word_keeps_the_old_reading(self):
        self.assert_clean("X=$(a b)", [])
        self.assert_clean("X=$(a b); gh pr create --base m1", ["m1"])
        self.assert_clean("X=$(a b) && gh pr create --base m1", ["m1"])
        self.assert_clean("(X=$(a b)) && gh pr create --base m1", ["m1"])
        self.assert_clean("url=$(gh pr create --base m1)", ["m1"])
        # 代入の直後の括弧は代入の後ろの語ではない
        self.assertEqual(command._scan_assignments("X=$(a)(b) gh pr create")[0], [])

    def test_quotes_in_the_scan(self):
        scan = lambda s: [s[start:end] for start, end in command._scan_assignments(s)[0]]  # noqa: E731
        # 単一引用符は `$(` の中では引用だが、二重引用符の中の `${` では文字 (引用と読むと走査が止まり、後ろを数えない)
        for cmd in (
            """X="$(echo 'a)b')" Y=$(a) gh""",
            '''X="${y:-it's}" Y=$(a) gh''',
            '''X="${y:-${z:-it's}}" Y=$(a) gh''',
            '''X="${y:-{it's}a b}" Y=$(a) gh''',
        ):
            with self.subTest(cmd=cmd):
                self.assertIn("Y=$(a)", scan(cmd))
        # 二重引用符の中の置換のコメントの `'` を引用と読まない (_strip_comments は二重引用符の中のコメントを落とさない)
        self.assertIn("Y=$(b)", scan('''X="$(a # it's\n)" Y=$(b) gh'''))
        self.assertEqual(scan("X=${y:-'a b'} gh"), ["X=${y:-'a b'}"])
        self.assertEqual(scan('X=${y:-"a b"} gh'), ['X=${y:-"a b"}'])
        self.assertEqual(scan("X=${y:-{a}b c} gh"), ["X=${y:-{a}b c}"])
        self.assertEqual(len(scan("X=$'a\\'b $(' Y=$(c) d")), 2)
        # 閉じていない引用符・置換の後ろは読まない
        self.assertEqual(scan("X=$(a ' b) gh pr create"), [])
        self.assertEqual(scan("X=$(a b gh pr create"), [])
        self.assertEqual(scan('X="$(a b) gh pr create'), [])

    def test_scan_records_only_words_that_need_it(self):
        scan = lambda s: [s[start:end] for start, end in command._scan_assignments(s)[0]]  # noqa: E731
        self.assertEqual(scan("X=1 gh pr create"), [])
        self.assertEqual(scan("X='a b' gh pr create"), [])
        self.assertEqual(scan("X=$(a) ; gh pr create"), [])
        self.assertEqual(scan("X=$(a) | gh pr create"), [])
        self.assertEqual(scan("X=$(a)"), [])
        self.assertEqual(scan("X=$(a) "), [])
        self.assertEqual(scan("(X=$(a) )"), [])
        self.assertEqual(scan("X=$(a) gh"), ["X=$(a)"])
        self.assertEqual(scan("X=${a} gh"), ["X=${a}"])
        self.assertEqual(scan("X=`a` gh"), ["X=`a`"])
        self.assertEqual(scan("X=(a) gh"), ["X=(a)"])
        self.assertEqual(scan("X=<(a) gh"), ["X=<(a)"])
        self.assertEqual(scan("X=>(a) gh"), ["X=>(a)"])
        # 配列の要素の位置は前置きではない
        self.assertEqual(scan("X=(Y=$(a) b) c"), ["X=(Y=$(a) b)"])
        # 語の途中でない `<(` / `>(` は語の値ではない
        self.assertEqual(scan("echo >(X=$(a) b) c"), ["X=$(a)"])
        self.assertEqual(command._scan_assignments("X=$(a) `b` c")[1], [(4, 5), (8, 9)])
        # 語の途中でない `>(` は置換の範囲に数えない
        self.assertEqual(command._scan_assignments("echo >(a b) c")[1], [])
        # `case` の分岐の後ろは前置きの位置
        self.assert_stops("case a in a) X=$(b $(c)) gh pr create;; esac")

    def test_only_the_outermost_substitution_is_split(self):
        # 値の中の置換の中の置換も数える (外側の直前で分ければ、正規表現が中身を実行位置と読む)
        self.assert_stops("X=$(a $(gh pr create --base m1)) gh pr ready m2")
        self.assertEqual(command._scan_assignments("X=$(a $(b)) c")[1], [(4, 10)])

    def test_stdin_shell_after_a_value(self):
        self.assert_stops("X=$(a b) bash <<'EOF'\ngh pr create --fill\nEOF")
        self.assert_stops("X=$(b'as'h -s) true <<'EOF'\ngh pr create --fill\nEOF")
        self.assert_clean("X=$(a b); cat > f <<'EOF'\ngh pr create --fill\nEOF\ngh pr create --base m1", ["m1"])

    def test_counting_does_not_hide_another_missed_operation(self):
        # 見落とした gh と、ほかで見つけた gh の数が合って通らないこと
        for cmd in (
            "V=$(a $(b)) gh pr create; f() { gh pr create; }",
            "f() { V=$(a $(b)) gh pr create; }",
            'f() { X="$(echo "a b")" gh pr create; }',
            "f() { X=${y:-a b} gh pr create; }",
            "f() { X=(a b) gh pr create; }",
            "V=$(a $(b)) gh pr create; f() { X=$(c $(d)) gh pr ready; }",
            'echo "$(V=$(a $(b)) gh pr create)"',
            "gh pr create --base m1; V=$(a $(b)) gh pr ready m2",
        ):
            with self.subTest(cmd=cmd):
                self.assert_stops(cmd)

    def test_pr_operation_in_the_arguments_of_a_pr_operation_stops(self):
        # shell は 2 つとも走らせる。構文解析は 1 つの呼び出しの引数と読む
        for cmd in (
            "gh pr create --base m1 >(gh pr create --base m2)",
            "gh pr create --base m1 $(gh pr ready m2)",
            'gh pr create --base m1 "$(gh pr ready m2)"',
            "X=$(a) gh pr create --base m1 >(gh pr create --base m2)",
            'X=`a` gh pr create --base m1 "$(gh pr create --base m2 x)"',
        ):
            with self.subTest(cmd=cmd):
                self.assert_stops(cmd)
        # 説明の文字列は数えない
        self.assert_clean("gh pr create --base m1 --title 'use $(gh pr create)'", ["m1"])
        self.assert_clean("gh pr create --base m1 --title 'gh pr create'", ["m1"])
        self.assert_clean('gh pr create --base m1 --title "$(git log -1)"', ["m1"])
        self.assertTrue(command._has_pr_op(["gh", "pr", "new"]))
        self.assertTrue(command._has_pr_op(["x", "gh", "pr", "ready"]))
        self.assertFalse(command._has_pr_op(["gh", "pr", "view"]))
        self.assertFalse(command._has_pr_op(["gh", "issue", "create"]))
        self.assertFalse(command._has_pr_op(["hg", "pr", "create"]))

    def test_ansi_c_quote_keeps_the_old_reading(self):
        # shlex は `$'\\''` の引用符の対応を shell と違う形で読む。本文を消さない
        self.assert_stops(
            "V=$'\\''y gh pr create --base m1 #' & gh pr create --base m2 \"$(cat <<-'EOF'\nsay \"hi\nEOF\n)\""
        )
        self.assert_stops("gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\" --title $'x'")
        # 入れ子の値は安全網が数える
        self.assert_stops("true $'x'; V=$(a $(b)) gh pr create --base m1")

    def test_time_is_linear(self):
        # 走査を 1 回で終わらせる。500 個と 2000 個の時間の比で見る
        script = (
            "import time\n"
            "from command import find_invocations, unresolved_reason\n"
            "units = {\n"
            "  'open': ('X=$( ', ''),\n"
            "  'open dq': ('X=\"$( ', ''),\n"
            "  'word': ('X=$(a) ', 'gh pr create'),\n"
            "  'word 2': ('X=$(a $(b)) ', 'gh pr create'),\n"
            "  'dq': ('\"$(a) ', ''),\n"
            "  'dq heredoc': ('\"$(cat <<E\\nb\\nE\\n)\" ', 'gh pr create'),\n"
            "  'quote': (\"X=$(a ' \", ''),\n"
            "  'brace': ('${', ''),\n"
            "  'escape': ('X=a\\\\\\\\ ', 'gh pr create'),\n"
            "  'ansi': (\"X=$'a\\\\\\\\'b' \", 'gh pr create'),\n"
            "  'backtick': ('`V=$(a $(b)) c` ', 'gh pr create'),\n"
            "  'nested backtick': ('`a \\\\\\\\`V=$(b) c\\\\\\\\`` ', 'gh pr create'),\n"
            "}\n"
            "def run(k):\n"
            "    best = {}\n"
            "    for name, (unit, tail) in units.items():\n"
            "        cmd = 'gh pr create <<x; ' + unit * k + tail\n"
            "        for _ in range(3):\n"
            "            t = time.perf_counter()\n"
            "            unresolved_reason(cmd, find_invocations(cmd))\n"
            "            d = time.perf_counter() - t\n"
            "            best[name] = min(best.get(name, d), d)\n"
            "    return best\n"
            "small, large = run(500), run(2000)\n"
            "for name in small:\n"
            "    print(name, small[name], large[name])\n"
        )
        pkg = Path(__file__).resolve().parent.parent
        try:
            r = subprocess.run([sys.executable, "-c", script], cwd=pkg, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("60 秒を超えた")
        self.assertEqual(r.returncode, 0, r.stderr)
        for line in r.stdout.splitlines():
            name, small, large = line.rsplit(" ", 2)
            with self.subTest(name):
                self.assertLess(float(large), 2.0, line)
                # 線形なら約 4 倍、2 乗なら約 16 倍。ごく短い時間の揺れは見ない
                if float(large) > 0.05:
                    self.assertLess(float(large) / float(small), 9.0, line)


_PLAIN_BODY = "--body \"$(cat <<'EOF'\nbody text\nEOF\n)\""
_ODD_BODY = "--body \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\""


class WordsThatMayMoveTest(_Base):
    """cwd を変えうる語があるコマンドは、本文を読み飛ばさず `$(a)#b` の `#` も従来どおりに読む (旧と同じ判定)。"""

    _MOVES = {
        "cd": "cd a; ",
        "cd and": "cd a && ",
        "cd newline": "cd a\n",
        "chdir": "chdir a; ",
        "pushd": "pushd a; ",
        "popd": "popd; ",
        "eval": 'eval "true"; ',
        "source": "source .env; ",
        "builtin": "builtin true; ",
        "trap": "trap 'true' DEBUG; ",
        "HOME": "HOME=/x; ",
        "CDPATH": "CDPATH=/x; ",
        # 引用符と `\` を挟んだ語も同じ語
        "single quoted": "c'd' a; ",
        "double quoted": 'c"d" a; ',
        "backslash": "c\\d a; ",
        "continuation": "c\\\nd a; ",
        "quoted pushd": "'pushd' a; ",
        "backslash pushd": "p\\ushd a; ",
        "backslash popd": "p\\opd; ",
        "cd as an argument": "echo cd; ",
        "chdir as an argument": "grep -rn chdir . ; ",
    }

    def test_odd_body_after_a_word_that_may_move_stops(self):
        for label, pre in self._MOVES.items():
            with self.subTest(label):
                self.assert_stops(f"{pre}gh pr create --base m1 {_ODD_BODY}")
        self.assert_stops(f"gh pr create --base m1 {_ODD_BODY} && cd ..")
        self.assert_stops("gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi, the source\nEOF\n)\"")

    def test_odd_body_without_those_words_is_inspected(self):
        for pre in ("", "git push && ", "git add . && git commit -m m && ", "X=1 ", "true; ", "BRANCH=$(git branch --show-current); "):
            with self.subTest(pre=pre):
                self.assert_clean(f"{pre}gh pr create --base m1 {_ODD_BODY}", ["m1"])
        for post in (" | cat", " 2>&1"):
            with self.subTest(post=post):
                self.assert_clean(f"gh pr create --base m1 {_ODD_BODY}{post}", ["m1"])

    def test_plain_body_keeps_the_old_verdict(self):
        cases = {
            "": (),
            "source .env && ": (),
            "git fetch && cd a && ": ("a",),
            "cd a || exit 1; ": ("a",),
            "cd a 2>/dev/null && ": ("a",),
            'cd "$PWD" && ': ("$PWD",),
            "for d in a; do (cd $d && make); done; ": (),
            "echo cd; ": (),
            "grep -rn chdir . ; ": (),
        }
        for pre, steps in cases.items():
            with self.subTest(pre=pre):
                cmd = f"{pre}gh pr create --base m1 {_PLAIN_BODY}"
                self.assert_clean(cmd, ["m1"])
                self.assertEqual(_first(cmd).cd_steps, steps)
        cmd = f"gh pr create --base m1 {_PLAIN_BODY} && cd .."
        self.assert_clean(cmd, ["m1"])
        self.assertEqual(_first(cmd).cd_steps, ())

    def test_reading_of_a_cd_with_a_plain_body(self):
        for cmd, steps, unresolved in (
            ("cd a && if cd b; then gh pr create --base m1; fi", ("a", "b"), False),
            ("(cd a) && gh pr create --base m1", (), False),
            ("false && cd a; gh pr create --base m1", ("a",), True),
            ("[ -d a ] && cd a; gh pr create --base m1", ("a",), True),
            ("if [ -d a ]; then cd a; fi; gh pr create --base m1", ("a",), True),
        ):
            with self.subTest(cmd=cmd):
                found = find_invocations(cmd)
                self.assertEqual([i.cd_steps for i in found], [steps])
                self.assertEqual(found[0].cd_unresolved is not None, unresolved)
                self.assertIsNone(unresolved_reason(cmd, found))

    def test_word_middle_hash_with_those_words_is_the_old_reading(self):
        for cmd in (
            "return $((1))# ; 2>&1 ';' cd /elsewhere ; gh pr create --base m1",
            "echo $(p)#; (cd /elsewhere); gh pr create --base m1",
            "echo $((1))#; (cd /elsewhere); gh pr create --base m1",
            "echo $(p)#; cd /elsewhere | cat; gh pr create --base m1",
            "echo $(p)#; env cd /elsewhere; gh pr create --base m1",
            "echo $(p)#; cd /elsewhere; gh pr create --base m1",
        ):
            with self.subTest(cmd=cmd):
                self.assert_stops(cmd)
        try:
            self.assertEqual(command._strip_for_parsing("cd a; echo $(p)#b c\nd"), "cd a; echo $(p)\nd")
        except RecursionError:
            self.fail("読み直しが終わらない")
        self.assertEqual(command._strip_for_parsing("cd a; echo $((1))#b c\nd"), "cd a; echo $((1))\nd")
        # 従来の読みのコメントに隠れた値も、安全網は数える
        self.assert_stops("cd a; echo $(p)#b; V=$(a $(b)) gh pr create --base m1")

    def test_word_middle_hash_without_those_words(self):
        self.assert_clean("echo $(p)#b; gh pr create --base m1", ["m1"])
        self.assert_clean("echo $((1))#b; gh pr create --base m1", ["m1"])
        self.assert_clean("gh pr create --title $(p)#b --base m1", ["m1"])

    def test_the_two_readings_are_separate_switches(self):
        # `$'` は本文の読み飛ばしだけを止める
        self.assertEqual(command._strip_for_parsing("echo $'x' $(p)#b c"), "echo $'x' $(p)#b c")
        cmd = "x \"$(cat <<'EOF'\nsay\nEOF\n)\" $'y'"
        self.assertEqual(command._strip_for_parsing(cmd), cmd)
        self.assertEqual(command._strip_for_parsing(cmd.replace(" $'y'", "")), 'x "$(cat <<\'EOF\'\n)"')


class SubstitutionInDoubleQuotesHeredocTest(_Base):
    def test_every_delimiter_and_body(self):
        templates = {
            "body option": ('gh pr create --base m1 --body "$(cat {op}\n{body}\n{end}\n)"', ["m1"]),
            "next command": ('gh pr create --draft --base m1 --body "$(cat {op}\n{body}\n{end}\n)" && gh pr ready m2', ["m1", "m2"]),
            "next line": ('git commit -m "$(cat {op}\n{body}\n{end}\n)"\ngh pr create --base m1', ["m1"]),
            "assignment then newline": ('X="$(cat {op}\n{body}\n{end}\n)"\ngh pr create --base m1', ["m1"]),
            "assignment then word": ('X="$(cat {op}\n{body}\n{end}\n)" gh pr create --base m1', ["m1"]),
            "two bodies": ('gh pr create --base m1 --body "$(cat {op}\n{body}\n{end}\n)$(cat {op}\n{body}\n{end}\n)"', ["m1"]),
            "two quoted": ('git commit -m "$(cat {op}\n{body}\n{end}\n)" -m "$(cat {op}\n{body}\n{end}\n)"\ngh pr create --base m1', ["m1"]),
            "no pr": ('git commit -m "$(cat {op}\n{body}\n{end}\n)"', []),
        }
        for dlabel, (op, end) in _DELIMITERS.items():
            for blabel, body in _ODD_BODIES.items():
                for tlabel, (tmpl, want) in templates.items():
                    cmd = tmpl.format(op=op, body=body, end=end)
                    with self.subTest(delimiter=dlabel, body=blabel, template=tlabel):
                        self.assert_clean(cmd, want)

    def test_comment_and_other_quotes_in_the_substitution(self):
        self.assert_clean("gh pr create --base m1 --body \"$(cat <<'EOF' # it's\nsay \"hi\nEOF\n)\"", ["m1"])
        self.assert_clean('gh pr create --base m1 --body "$(sed "s/a/b/" <<\'EOF\'\nsay "hi\nEOF\n)"', ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$(cat <<'EOF' <<<x\nsay \"hi\nEOF\n)\"", ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$( (cat <<'EOF'\nsay \"hi\nEOF\n) )\"", ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\nEOF\ncat <<'E2'\nIt's\nE2\n)\"", ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$(cat <<'A' <<B\nsay \"hi\nA\nit's\nB\n)\"", ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$(printf \\\" | cat <<'EOF'\nsay \"hi\nEOF\n)\"", ["m1"])
        # 引用符の中の `<<` は heredoc ではない
        self.assert_clean("""gh pr create --base m1 --body "$(echo '<<EOF')"\ngh pr ready m2""", ["m1", "m2"])
        # 引用符の外と読む `<<` は従来どおり heredoc と読み、後ろの行は安全網が数えて止める
        self.assert_stops('gh pr create --base m1 --body "$(echo "<<EOF")"\ngh pr ready m2')
        # 本文を持たない heredoc (本文が始まる前に置換が閉じる)
        self.assert_clean('gh pr create --base m1 --body "$(cat <<2)"\ngh pr ready m2\n2', ["m1", "m2"])

    def test_scan_of_the_substitution(self):
        scan = command._dq_substitution_bodies
        # 置換の中の括弧と引用符で、閉じる位置を取り違えない
        self.assert_clean("gh pr create --base m1 --body \"$( (a) ; cat <<'EOF'\nsay \"hi\nEOF\n)\"", ["m1"])
        self.assert_clean("gh pr create --base m1 --body \"$(echo 'a)' ; cat <<'EOF'\nsay \"hi\nEOF\n)\"", ["m1"])
        # 読めない区切り語・閉じない置換は読み飛ばさない
        self.assertIsNone(scan("cat <<'E\nx'\n)", 0))
        self.assertIsNone(scan("<<x\ny\nx\n", 0))
        self.assertEqual(scan("<<x\ny\nx\n)", 0), ({4: 8}, 9))

    def test_old_reading_flag(self):
        cmd = "x \"$(cat <<'EOF'\nsay\nEOF\n)\""
        # 読み直しても本文の範囲を二重に数えない
        spans: list[tuple[int, int]] = []
        try:
            self.assertEqual(command._strip_comments(cmd, dq_bodies=False), cmd)
            command._strip_comments(
                "cat > a <<'EOF'\nx\nEOF\ngh \"$(cat <<'E2'\nno end\n)\"",
                quoted_bodies=spans,
            )
        except RecursionError:
            self.fail("読み直しが終わらない")
        self.assertEqual(len(spans), 1)

    def test_old_reading_is_not_mixed_with_the_new_one(self):
        # 読み直すときに、先に読み飛ばした本文の範囲を残さない (安全網の数え上げが壊れる)
        self.assert_clean(
            "cat > a <<'EOF'\ngh pr create --fill\nEOF\ngh pr create --base m1 --body \"$(cat <<'E2'\nno end\n)\"",
            ["m1"],
        )
        # 引用符の外の置換で全体を従来の読みに戻したときは、二重引用符の中の本文も読み飛ばさない
        self.assert_stops(
            "x=$(cat <<'E'\nIt\nE\n)\ngh pr create --base m1 --body \"$(cat <<'F'\nsay \"hi\nF\n)\""
        )

    def test_cd_in_a_body_is_not_followed(self):
        # 本文の語でも cd があると従来の読み (引用符の対応だけで読むので、ここでは本文の行は 1 語の中)
        inv = _first("x \"$(cat <<'EOF'\nsay\ncd /other\nsay\nEOF\n)\"\ngh pr create")
        self.assertIsNone(inv.cd)
        self.assertEqual(command._strip_comments("x \"$(cat <<'EOF'\nsay \"hi\ncd /other\nEOF\n)\""), "x \"$(cat <<'EOF'\n)\"")

    def test_the_safety_net_still_reads_the_body(self):
        # 本文の行頭の gh pr create は従来どおり数えて止める (bash 3.2 がコードとして走らせる形と区別しない)
        self.assert_stops("gh pr create --base m1 --body \"$(cat <<'EOF'\ngh pr create --fill\nEOF\n)\"")
        self.assert_stops("gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\ngh pr create --fill\nEOF\n)\"")
        self.assert_stops('gh pr create --base m1 --body "$(cat <<EOF\n$(gh pr ready 3) "\nEOF\n)"')
        # 区切り語に引用がある本文を安全網から隠さない
        spans: list[tuple[int, int]] = []
        command._strip_comments("x \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\"", quoted_bodies=spans)
        self.assertEqual(spans, [])

    def test_ambiguous_end_of_the_body_keeps_the_old_reading(self):
        cases = {
            # 終わりの行が無い (bash は閉じ括弧で切り、zsh は構文エラー)
            "no terminator": "gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\n)\"",
            "terminator with a tail": "gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\nEOF)\"\n",
            "not the whole line": "gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\n EOF\n)\"",
            # 本文の `)` で置換が閉じ、`"` で引用符も閉じる読み方がある (bash 3.2)。後ろの行はコードとして走る
            "close then quote": "gh pr create --base m0 --body \"$(cat <<'EOF'\nfoo)\"\ngh pr create --base mA\nEOF\n)\"",
            "list marker then quote": "gh pr create --base m1 --body \"$(cat <<'EOF'\n1) say \"hi\nEOF\n)\"",
            # 置換が閉じない
            "unclosed": "gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\"\nEOF\n",
            # 区切り語の引用符が閉じない
            "open delimiter": "gh pr create --base m1 --body \"$(cat <<'EOF\nsay \"hi\n)\"",
            # 読み飛ばせる本文と読み飛ばせない本文が混ざっても、全体を従来の読みで読む
            "first ok, second not": "gh pr create --base m1 -b \"$(cat <<'E'\nsay \"hi\nE\n)\" -t \"$(cat <<'F'\nIt\n)\"",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assert_stops(cmd)
        # 引用符がそろっていれば従来どおり読める。本文の行もコードとして読むので cd も読む
        inv = _first("x \"$(cat <<'EOF'\nfoo)\"\ncd /other\nEOF\n\")\"\ngh pr create")
        self.assertEqual(inv.cd, "/other")
        self.assertEqual(_marks("gh pr create --base m0 -b \"$(cat <<'EOF'\nfoo)\"\ngh pr create --base mA\nEOF\n\")\""), ["m0", "mA"])
        self.assertIsNotNone(command._dq_substitution_bodies("a)<<x\ny\nx\n)", 0))
        self.assertIsNone(command._dq_substitution_bodies("<<x\ny\n)", 0))

    def test_substitution_in_an_unquoted_body_keeps_the_old_reading(self):
        # 引用の無い区切り語の本文の置換は shell が走らせる (zsh で m2 が走る)
        for op in ("<<EOF", "<<-EOF"):
            with self.subTest(op=op):
                self.assert_stops(f"gh pr create --base m0 --body \"$(cat {op}\n$(\n'gh' pr create --base m2 )\"\nEOF\n)\"")
        self.assertIsNone(command._dq_substitution_bodies("cat <<EOF\n$(a)\nEOF\n)", 0))
        self.assertIsNone(command._dq_substitution_bodies("cat <<EOF\n`a`\nEOF\n)", 0))
        # 置換の無い本文と、引用のある区切り語の本文は読み飛ばす
        self.assertIsNotNone(command._dq_substitution_bodies("cat <<EOF\nsay $x\nEOF\n)", 0))
        for op in ("<<'EOF'", '<<"EOF"', "<<\\EOF"):
            with self.subTest(op=op):
                self.assertIsNotNone(command._dq_substitution_bodies(f"cat {op}\n$(a) `b`\nEOF\n)", 0))
                self.assert_clean(f"gh pr create --base m1 --body \"$(cat {op}\nuse `b` and say \"hi\nEOF\n)\"", ["m1"])

    def test_closes_early(self):
        for body in ('a)"', '1) "', 'x)\n"', "foo) 'a' \"", '(a))"', '")" ) "'):
            with self.subTest(body=body):
                self.assertTrue(command._closes_early(body))
        for body in ("a)", '"a"', '(a)"', '")"', "')\"'", '\\)"', '"\\")"'):
            with self.subTest(body=body):
                self.assertFalse(command._closes_early(body))

    def test_nothing_changes_without_a_heredoc(self):
        cases = {
            "substitution": 'gh pr create --base m1 --body "$(git log -1)"',
            "many": 'gh pr create --base m1 --body "$(a)" --title "$(b)$(c)"',
            "arithmetic": 'gh pr create --base m1 --body "$((1<<2))"\ngh pr ready m2',
            "text": 'gh pr create --base m1 --body "a <<EOF"\ngh pr ready m2',
        }
        wants = {"arithmetic": ["m1", "m2"], "text": ["m1", "m2"]}
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assert_clean(cmd, wants.get(label, ["m1"]))
        self.assertEqual(command._dq_substitution_bodies("git log -1)", 0), ({}, 10 + 1))
        self.assertEqual(command._dq_substitution_bodies("git log", 0), ({}, 7))


class WordMiddleHashTest(_Base):
    def test_hash_right_after_a_substitution_is_not_a_comment(self):
        # `$(a)#b` / `$((1))#b` の `#` は語の途中。後ろはコマンドとして走る (bash / zsh 実測)
        self.assertEqual(command._strip_comments("echo $(a)#b c\nd"), "echo $(a)#b c\nd")
        self.assertEqual(command._strip_comments("echo $((1))#b c\nd"), "echo $((1))#b c\nd")
        # サブシェルや算術コマンドの後ろの `#` はコメント
        self.assertEqual(command._strip_comments("(a)#b c\nd"), "(a)\nd")
        self.assertEqual(command._strip_comments("((1))#b c\nd"), "((1))\nd")


if __name__ == "__main__":
    unittest.main()
