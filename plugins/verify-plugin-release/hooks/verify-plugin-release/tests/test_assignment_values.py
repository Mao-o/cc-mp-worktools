"""代入語の値の読み方と、二重引用符の中のコマンド置換の heredoc (command.find_invocations / unresolved_reason)。

期待値は、PATH に偽の gh だけを置いた /bin/bash (3.2) と zsh -f で実際に走らせて確かめた形。
`--base m<N>` / `gh pr ready m<N>` は、どの呼び出しを見つけたかを見分ける目印。
"""
from __future__ import annotations

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
    except (ValueError, RecursionError) as e:
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
    def test_value_forms_before_the_command_word(self):
        # どれも bash / zsh とも gh が走る。旧版は入れ子・引用の入れ子・`${...}`・算術・配列を止めずに通していた
        values = {
            "substitution": "$(echo a)",
            "space": "$(echo a b)",
            "pipe": "$(cat f | head -1)",
            "semicolon": "$(a; b)",
            "nested": "$(echo $(echo a))",
            "deeply nested": "$(a $(b $(c)))",
            "double quoted": '"$(cat f | head -1)"',
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
            "comment in a substitution": "$(echo a # )\n)",
            "gh without a PR operation": "$(gh auth token)",
        }
        prefixes = {
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
        }
        for vlabel, value in values.items():
            for plabel, (pre, post) in prefixes.items():
                cmd = f"{pre}X={value} gh pr create --base m1{post}"
                with self.subTest(value=vlabel, prefix=plabel):
                    self.assert_clean(cmd, ["m1"])

    def test_continuation_inside_the_assignment_word(self):
        self.assert_clean("X=\\\n$(echo a b) gh pr create --base m1", ["m1"])

    def test_redirect_directly_after_the_value(self):
        self.assert_clean("X=$(a)>f gh pr create --base m1", ["m1"])

    def test_dynamic_gh_repo_is_kept_as_the_repo(self):
        # 値が動的な GH_REPO は、呼び出し側が「origin と同じか確認できない」で止める
        for cmd, want in (
            ("GH_REPO=$(basename $(pwd)) gh pr create --base m1", "$(basename $(pwd))"),
            ('GH_REPO="$(echo "a b")" gh pr create --base m1', '"$(echo "a b")"'),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(_first(cmd).repo, want)

    def test_substitutions_in_the_value_are_read_before_the_command(self):
        self.assert_clean("X=$(gh pr create --base m1) gh pr ready m2", ["m1", "m2"])
        self.assert_clean('X="$(gh pr create --base m1)" gh pr ready m2', ["m1", "m2"])
        self.assert_clean("X=`gh pr create --base m1` gh pr ready m2", ["m1", "m2"])
        self.assert_clean("X=$(true; gh pr create --base m1) gh pr ready m2", ["m1", "m2"])
        self.assert_clean("X=$(a)$(gh pr create --base m1) gh pr ready m2", ["m1", "m2"])
        self.assert_clean("x=$(V=$(echo $(echo a)) gh pr create --base m1)", ["m1"])

    def test_cd_inside_and_around_the_value(self):
        # 値の中の cd はサブシェルで走るので、続く相対の cd の起点が決まらない
        found = find_invocations("X=$(cd a) gh pr create; cd b && gh pr ready")
        self.assertEqual(len(found), 2)
        inv = found[1]
        self.assertIsNotNone(inv.cd_unresolved)
        # 代入の後ろの cd も cd として読み、区切りの種類は元のまま
        inv = _first("X=$(a b) cd sub && gh pr create")
        self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("sub",), None))
        inv = _first("cd a || X=$(b c) cd d; gh pr create")
        self.assertIsNotNone(inv.cd_unresolved)
        inv = _first("true | X=$(b c) cd d; cd e; gh pr create")
        self.assertIsNotNone(inv.cd_unresolved)
        # `cd a || X=$(b) exit 1` の exit は shell を終わらせるので a に決まる
        inv = _first("cd a || X=$(b c) exit 1; cd d && gh pr create")
        self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("a", "d"), None))

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
        # 単一引用符は `$(` の中では引用だが、二重引用符の中の `${` では文字
        self.assert_clean('X="${y:-it\'s}" gh pr create --base m1', ["m1"])
        self.assert_clean("""X="$(echo 'a)b')" gh pr create --base m1""", ["m1"])
        self.assert_clean('''X="${y:-${z:-it's}}" gh pr create --base m1''', ["m1"])
        self.assert_clean("X=${y:-'a b'} gh pr create --base m1", ["m1"])
        self.assert_clean('X=${y:-"a b"} gh pr create --base m1', ["m1"])
        self.assertEqual(len(command._scan_assignments("X=$'a\\'b $(' Y=$(c) d")[0]), 2)
        # 閉じていない引用符・置換の後ろは読まない
        self.assertEqual(command._scan_assignments("X=$(a ' b) gh pr create")[0], [])
        self.assertEqual(command._scan_assignments("X=$(a b gh pr create")[0], [])
        self.assertEqual(command._scan_assignments('X="$(a b) gh pr create')[0], [])

    def test_scan_records_only_words_that_need_it(self):
        scan = lambda s: [s[w.start : w.end] for w in command._scan_assignments(s)[0]]  # noqa: E731
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
        # 波括弧の入れ子と、その中の `'`
        self.assert_clean("X=${y:-{a}b c} gh pr create --base m1", ["m1"])
        self.assert_clean("X=\"${y:-{it's}a b}\" gh pr create --base m1", ["m1"])
        # 二重引用符の中は書き換えない (引用が文字になる)
        self.assertEqual(command._opaque_assignments('echo "$(V=$(a) gh)"'), ('echo "$(V=$(a) gh)"', frozenset()))
        # 別の代入語の値の中のものは、外側の語を読むときに読む
        self.assert_clean("X=$(Y=$(a b) gh pr create --base m1) gh pr ready m2", ["m1", "m2"])
        # `case` の分岐の後ろは前置きの位置
        self.assert_stops("case a in a) X=$(b $(c)) gh pr create;; esac")

    def test_single_quote_is_a_letter_in_a_parameter_expansion_in_double_quotes(self):
        # 値の後半の引用符の入れ子は語を書き換えないと読めない。前半の `'` を引用と読むと、そこで走査が止まる
        for head in ("${y:-it's}", "${y:-${z:-it's}}", "${y:-{it's}a}"):
            cmd = f'X="{head}$(echo "a b")" gh pr create --base m1'
            with self.subTest(head=head):
                self.assert_clean(cmd, ["m1"])

    def test_comment_in_a_substitution_in_double_quotes(self):
        # _strip_comments は二重引用符の中のコメントを落とさないので、走査が読み飛ばす
        self.assert_clean('X="$(echo "a b" # it\'s\n)" gh pr create --base m1', ["m1"])

    def test_only_the_outermost_substitution_is_read(self):
        # 値の中の置換の中の置換は構文解析で読まない。安全網が数えて止める (2 回読むと数が合ってしまう)
        self.assert_stops("X=$(a $(gh pr create --base m1)) gh pr ready m2")
        # 語の途中でない `>(` は置換の範囲に数えない
        self.assertEqual(command._scan_assignments("echo >(a b) c")[1], [])

    def test_unreadable_rewrite_keeps_the_old_reading(self):
        # 語を引用すると残りの引用符の対応がずれる形は、従来の読みに戻す
        cmd = 'X="$(echo \'a"b\')" \'c" d"'
        try:
            tokens, opaque = command._tokens_opaque(cmd)
        except ValueError as e:
            self.fail(f"従来の読みに戻らなかった: {e}")
        self.assertEqual(opaque, frozenset())
        self.assertEqual(tokens, command._tokens(cmd))
        # 読めない置換の中は構文解析で読まない (後ろの gh は読む)
        self.assert_clean('X=`echo "` gh pr create --base m1', ["m1"])

    def test_values_are_not_read_as_a_shell_for_stdin(self):
        # 代入の後ろの shell は本文を読む。引用符を挟んだ名前も見つける
        self.assert_stops("X=$(a b) b'as'h <<'EOF'\ngh pr create --fill\nEOF")
        self.assert_stops("X=$(a b) bash <<'EOF'\ngh pr create --fill\nEOF")
        # 値の中の shell も従来どおり数える (従来の読みと両方で探す)
        self.assert_stops("X=$(b'as'h -s) true <<'EOF'\ngh pr create --fill\nEOF")
        self.assert_clean("X=$(a b) cat > f <<'EOF'\ngh pr create --fill\nEOF\ngh pr create --base m1", ["m1"])

    def test_counting_does_not_hide_another_missed_operation(self):
        # 構文解析が見つけるようになった gh を安全網も数える。数えないと、見落とした gh と数が合って通る
        for cmd in (
            "V=$(a $(b)) gh pr create; f() { gh pr create; }",
            "f() { V=$(a $(b)) gh pr create; }",
            'f() { X="$(echo "a b")" gh pr create; }',
            "f() { X=${y:-a b} gh pr create; }",
            "f() { X=(a b) gh pr create; }",
            "V=$(a $(b)) gh pr create; f() { X=$(c $(d)) gh pr ready; }",
            'echo "$(V=$(a $(b)) gh pr create)"',
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

    def test_nesting_limit(self):
        def nested(depth: int) -> str:
            cmd = "gh pr create --base m1"
            for _ in range(depth):
                cmd = f"X=$({cmd}) true"
            return cmd

        limit = command._NESTED_LIMIT
        self.assertEqual(limit, 8)
        for depth in (limit - 1, limit):
            with self.subTest(depth=depth):
                self.assert_clean(nested(depth), ["m1"])
        for depth in (limit + 1, limit + 40):
            with self.subTest(depth=depth):
                self.assertEqual(_marks(nested(depth)), [])
                self.assert_stops(nested(depth))
        # 上限を超える深さで他の構文と混ぜても、読めた分と数が合わないので止める
        self.assert_stops(nested(limit + 1) + "; " + nested(1))
        self.assert_stops(nested(limit + 1).replace("X=$(", 'X="$(', 1).replace(") true", ')" true', 1))

    def test_ansi_c_quote_keeps_the_old_reading(self):
        # shlex は `$'\\''` の引用符の対応を shell と違う形で読む。語を書き換えず、本文も消さない
        self.assertEqual(command._opaque_assignments("X=$(a) gh $'b'"), ("X=$(a) gh $'b'", frozenset()))
        self.assert_stops(
            "V=$'\\''y gh pr create --base m1 #' & gh pr create --base m2 \"$(cat <<-'EOF'\nsay \"hi\nEOF\n)\""
        )
        self.assert_stops("gh pr create --base m1 --body \"$(cat <<'EOF'\nsay \"hi\nEOF\n)\" --title $'x'")
        # 入れ子の値は、従来の読みのままでも安全網が数える
        self.assert_stops("true $'x'; V=$(a $(b)) gh pr create --base m1")

    def test_time_is_linear(self):
        # 走査を 1 回で終わらせる。1000 個と 4000 個の時間の比で見る
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
        inv = _first("x \"$(cat <<'EOF'\nsay \"hi\ncd /other\nsay \"there\nEOF\n)\"\ngh pr create")
        self.assertIsNone(inv.cd)

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
