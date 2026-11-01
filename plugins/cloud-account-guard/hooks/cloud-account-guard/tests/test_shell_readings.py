"""コマンド行の分解で、`#` の手前の読み・ANSI-C 引用符・シェルで割れる読みの取りこぼしを固定する (v0.29.2)。

- 閉じ括弧・エスケープ・置換の直後の `#` は語の続き (comment ではない)。直前の 1 文字ではなく、
  直前に読んだものの種類で決める
- `$'...'` は ANSI-C 引用符 (中の `\\'` で閉じない)。分解 (別の読み)・代入値・heredoc の
  delimiter・`core/workdir.py` の `_unquoted` が同じ規則で読む
- bash 3.2 と zsh で分解が割れる形 (パラメータ展開の中の素の `{`、配列代入) と `$'` を含む形は、
  別の読みでも分解した候補の和集合を取る (既定の読みは 0.29.1 と同じ)

各形の bash 3.2 / zsh での実測は CHANGELOG の 0.29.2 の節。右の注記は aws が走るシェル。
"""
from __future__ import annotations

import unittest
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core import command_parser, workdir  # noqa: E402
from core.command_parser import (  # noqa: E402
    extract_candidate_readings,
    extract_candidates,
    split_on_operators,
)
from core.dispatcher import dispatch  # noqa: E402

AWS = "aws s3 rm s3://b/x"


def _cands(command):
    return [c for c, _e in extract_candidates(command)]


# 閉じ括弧・エスケープ・置換の直後の `#` は comment ではない (0.29.1 は comment と読んで落としていた)。
HASH_AFTER_CLOSER_FORMS = [
    ("echo <(true)# ; {a}", True),  # bash / zsh
    ("echo >(true)# ; {a}", True),  # bash / zsh
    ("echo <( (true) )# ; {a}", True),  # bash / zsh
    ("echo <((1))# ; {a}", True),  # bash / zsh (`<((` はプロセス置換)
    ("echo $(true)# ; {a}", True),  # bash / zsh
    ("echo $((1))# ; {a}", True),  # bash / zsh
    ("echo \\)# ; {a}", True),  # bash / zsh
    ("echo \\(# ; {a}", True),  # bash / zsh
    ("echo \\;# ; {a}", True),  # bash / zsh
    ("echo \\&# ; {a}", True),  # bash / zsh
    ("echo \\|# ; {a}", True),  # bash / zsh
    ("echo \\ # ; {a}", True),  # bash / zsh
    ("echo \\\t# ; {a}", True),  # bash / zsh
    ("echo a\\\n# ; {a}", True),  # bash / zsh (行継続は前の状態を引き継ぐ)
    # どちらのシェルでも comment (0.29.1 と同じ)
    ("(true)# ; {a}", False),
    ("((1))# ; {a}", False),
    ("echo a \\\n# ; {a}", False),
    ("echo <(true) # ; {a}", False),
    ("echo a # ; {a}", False),
    # 区切りの直後の `#` は comment (comment の中の `'` で引用符を始めない。0.29.1 と同じ)
    ("true ;# it's\n{a}", True),  # bash / zsh
    ("true &&# it's\n{a}", True),  # bash / zsh
    ("false ||# it's\n{a}", True),  # bash / zsh
    ("true |# it's\n{a}", True),  # bash / zsh
    ("true &# it's\n{a}", True),  # bash / zsh
    ("cat <<E\nb\nE\n# it's\n{a}", True),  # bash / zsh (heredoc の本文の後ろ)
    ("(# it's\n{a})", True),  # bash / zsh
]

# `$'...'` の中の `\'` で引用符を閉じない (0.29.1 は 2 つ目の `'` で閉じ、3 つ目から残り全部を
# 引用符の中と読んで落としていた)。
ANSI_C_FORMS = [
    ("echo $'\\'' ; {a}", True),  # bash / zsh
    ("echo $'\\''# ; {a}", True),  # bash / zsh
    ("echo $'a\\\\' ; {a}", True),  # bash / zsh
    ("A=$'\\'' ; {a}", True),  # bash / zsh
    ("echo ${x:-$'\\''} ; {a}", True),  # bash / zsh
    ("echo ${x:-$$'\\''} ; {a}", True),  # bash / zsh (展開の中の `$$'` は `$` + `$'`)
    ("echo $(echo $'\\'') ; {a}", True),  # bash / zsh
    ("echo `echo $'\\''` ; {a}", True),  # bash / zsh
    ("cat <<$'E\\'F'\nbody\nE'F\n{a}", True),  # bash / zsh (delimiter は `E'F`)
    # `$$'` は `$$` の後ろの普通の引用符・引用符の中の `$'` は ANSI-C ではない (どちらでも走らない)
    ("echo $$'\\'' ; {a} '", False),
    ("echo \\$'\\'' ; {a}", False),
    ("echo $'\\'' # ; {a}", False),
]

# zsh はパラメータ展開の中の素の `{` を数える (bash は最初の `}` で閉じる)。
BRACE_FORMS = [
    ("echo ${x:-{} #}; {a}", True),  # zsh
    ("echo ${x:-{{}} #}; {a}", True),  # zsh
    ("echo ${x:-{{} #}}; {a}", True),  # zsh
    ("echo ${x:-{${y}} #}; {a}", True),  # zsh
    ("echo ${x:-{'}'} #}; {a}", True),  # zsh
    ("echo ${x:-{} #it's}\n{a}", True),  # bash (zsh の読みだけだと `'` から引用符が始まる)
    ("echo ${x:-{}; {a}; echo }", True),  # bash
    ("echo ${x:-{ #}; {a}", True),  # bash
    # どちらでも走らない (エスケープ・引用符の中の `{` は zsh も数えない)
    ("echo ${x:-\\{} #}; {a}", False),
    ("echo ${x:-'{'} #}; {a}", False),
    ("echo ${x:-}{} #}; {a}", False),
]

# 配列代入の閉じの直後の `#` (bash は語の続き、zsh は comment) と、中の `(` (bash 3.2 は構文
# エラーにして行の残りを読み捨てる)。
ARRAY_FORMS = [
    ("a=(1 2)# ; {a}", True),  # bash
    ("a+=(1 2)# ; {a}", True),  # bash
    ("a[1]=(1)# ; {a}", True),  # bash
    ("a=(1 2)#'\n{a}", True),  # zsh
    ("a=(1 2)# it's\n{a}", True),  # zsh
    ("a=(x (y) 'q\n{a}", True),  # bash
    ("a=(=(x 'q ; b\n{a}", True),  # bash
    ("a=(1 2) # ; {a}", False),
]


class CandidateTests(unittest.TestCase):
    def _check(self, forms):
        for form, runs in forms:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command):
                try:
                    cands = _cands(command)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {e!r}")
                if runs:
                    self.assertIn(AWS, cands)
                else:
                    self.assertNotIn(AWS, cands)

    def test_hash_after_closer_or_escape(self):
        self._check(HASH_AFTER_CLOSER_FORMS)

    def test_ansi_c_quote(self):
        self._check(ANSI_C_FORMS)

    def test_zsh_braces_in_parameter_expansion(self):
        self._check(BRACE_FORMS)

    def test_array_assignment(self):
        self._check(ARRAY_FORMS)

    def test_process_substitution_spanning_segments_keeps_comment(self):
        """`<(` が区切りをまたいで閉じるときは、閉じの直後の `#` を 0.29.1 と同じく comment と読む。

        語の続きと読むと `#c` が中のコマンドの最後の引数に付き、候補の形が崩れる。
        """
        self.assertIn(AWS, _cands("cat <(true\n" + AWS + ")#c"))

    def test_heredoc_strip_reads_quotes_across_lines(self):
        """heredoc の宣言を探すとき、前の行から続く引用符の中を構文として読まない。"""
        command = "echo  \nE'F\n\\;<<$'E\\'F''<<'EOF' & " + AWS
        self.assertIn(AWS, _cands(command))

    def test_heredoc_declaration_after_multiline_quote(self):
        """引用符が改行をまたいだ後ろの宣言も落とす (閉じない heredoc の後ろの `&` で再分割する)。"""
        self.assertIn(AWS, _cands("true 'q\nq' <<EOF & " + AWS))  # bash / zsh

    def test_heredoc_body_after_ansi_c_delimiter_is_not_a_candidate(self):
        """`<<$'E\\'F'` の delimiter は `E'F` (本文の行を候補にしない)。"""
        cands = _cands("cat <<$'E\\'F'\naws s3 ls body\nE'F\n" + AWS)
        self.assertIn(AWS, cands)
        self.assertNotIn("aws s3 ls body", cands)

    def test_semicolon_inside_pending_heredoc_starts_a_word(self):
        """heredoc の宣言の後ろ (本文の前) の `;` の直後の `#` は comment。"""
        self.assertIn(AWS, _cands("cat <<E ;# it's\nb\nE\n" + AWS))  # bash / zsh

    def test_heredoc_delimiter_does_not_span_line_continuation(self):
        """delimiter の語は改行をまたがない (0.29.1 の行ごとの読みと同じ)。"""
        self.assertEqual(
            command_parser._strip_heredoc_bodies("<<-EOF\\\na=(; " + AWS),
            "a=(; " + AWS,
        )


class ArrayAssignmentPrefixTests(unittest.TestCase):
    """コマンドの前の配列の代入・追記・要素の代入は剥がす (値は集めない)。

    0.29.1 までは剥がさず、`a=(1 2) aws ...` / `a+=(x) aws ...` の aws を検証していなかった
    (bash 3.2 / zsh とも aws は走る)。
    """

    def test_prefix_is_stripped(self):
        for prefix in ("a=(1 2)", "a+=(x)", "a=()", "a=(1 \"x)\" $'\\')')", "a[1]=x", "a+=x",
                       "b=1 a=(x)", "a=(x) b=1"):
            command = prefix + " " + AWS
            with self.subTest(command=command):
                cands = extract_candidates(command)
                self.assertIn(AWS, [c for c, _e in cands])

    def test_values_are_not_collected(self):
        cases = {
            "AWS_PROFILE=(p) " + AWS: {},
            "AWS_PROFILE+=x " + AWS: {},
            "AWS_PROFILE[0]=x " + AWS: {},
            "AWS_PROFILE=p a=(x) " + AWS: {"AWS_PROFILE": "p"},
        }
        for command, env in cases.items():
            with self.subTest(command=command):
                self.assertEqual(extract_candidates(command), [(AWS, env)])

    def test_unclosed_array_is_not_stripped(self):
        command = "a=(1 2 " + AWS
        self.assertEqual(_cands(command), [command])


class ReadingTests(unittest.TestCase):
    def test_plain_command_has_one_reading(self):
        for command in ("gh pr list && " + AWS, "echo ${x} ; " + AWS, "{ true; } ; " + AWS):
            with self.subTest(command=command):
                self.assertEqual(len(extract_candidate_readings(command)), 1)

    def test_default_reading_is_unchanged(self):
        """別の読みは足すだけ: 先頭の読みは従来の分解 (`extract_candidates_with_separators`)。"""
        for command in ("echo ${x:-{} #}; " + AWS, "a=(1 2)# ; " + AWS, "echo $'\\'' ; " + AWS):
            with self.subTest(command=command):
                readings = extract_candidate_readings(command)
                self.assertEqual(len(readings), 2)
                self.assertEqual(
                    readings[0], command_parser.extract_candidates_with_separators(command)
                )
                self.assertNotIn(AWS, [c for c, _e, _s in readings[0]])
                self.assertIn(AWS, [c for c, _e, _s in readings[1]])

    def test_ansi_c_reading_details(self):
        """ANSI-C の読みの細部 (既定の読みとの和集合では見えないので、読みを直接見る)。"""
        cases = [
            # 閉じない `$'` は残り全部が引用符の中 (どちらのシェルでも走らない)
            ("echo $'a\\' ; " + AWS, False),
            # 代入の値の `$'` (`A=$'\'' aws ...` の aws は bash / zsh とも走る)
            ("A=$'\\'' " + AWS, True),
        ]
        for command, runs in cases:
            with self.subTest(command=command):
                readings = extract_candidate_readings(command)
                self.assertEqual(len(readings), 2)
                cands = [c for c, _e, _s in readings[1]]
                if runs:
                    self.assertIn(AWS, cands)
                else:
                    self.assertNotIn(AWS, cands)

    def test_double_dollar_assignment_value_is_plain_quote(self):
        """代入の値の `$$'` は `$$` の後ろの普通の引用符 (`A=$$'\\'' aws ... '` はどちらでも走らない)。"""
        cands = _cands("A=$$'\\'' " + AWS + " '")
        self.assertFalse([c for c in cands if c.startswith("aws ")], cands)

    def test_reading_switch_is_restored(self):
        extract_candidates("echo ${x:-{} #}; a=(1)# ; echo $'\\'' ; " + AWS)
        self.assertFalse(command_parser._ZSH_READING)
        self.assertFalse(command_parser._BASH_READING)
        self.assertFalse(command_parser._ANSI_C_READING)
        # 既定の読みに戻っている: 素の `{` を数えない・`$'` を ANSI-C と読まない
        self.assertEqual(split_on_operators("echo ${x:-{} #}; " + AWS), ["echo ${x:-{}"])
        command = "echo $'\\'' ; " + AWS
        self.assertEqual(split_on_operators(command), [command])

    def test_same_command_object_gives_same_result(self):
        """別の読みで求めた `${` / `$(` の終端の memo を、次の呼び出しの既定の読みが引かない。

        dispatcher は同じ文字列 (同じオブジェクト) を何度も分解する。
        """
        command = "echo ${x:-{} #}; " + AWS
        first = extract_candidate_readings(command)
        self.assertEqual(extract_candidate_readings(command), first)
        self.assertEqual(split_on_operators(command), ["echo ${x:-{}"])
        command = "echo ${x:-{$(true)} #}; " + AWS
        first = extract_candidate_readings(command)
        self.assertEqual(len(first), 2)
        self.assertEqual(extract_candidate_readings(command), first)

    def test_cd_is_tracked_per_reading(self):
        """別の読みにだけある候補も、その読みの `cd` を辿ったディレクトリで走るものとして扱う。"""
        dirs = workdir.segment_dirs("cd sub; echo ${x:-{} #}; " + AWS, "/p")
        self.assertEqual(dirs.get(AWS), {"/p/sub"})
        dirs = workdir.segment_dirs("cd sub; a=(1)# ; " + AWS, "/p")
        self.assertEqual(dirs.get(AWS), {"/p/sub"})

    def test_env_changes_are_tracked_per_reading(self):
        changes = workdir.segment_env_changes("export AWS_PROFILE=x; echo ${x:-{} #}; " + AWS)
        self.assertIn("AWS_PROFILE", changes.get(AWS, set()))


class WorkdirUnquotedTests(unittest.TestCase):
    def test_ansi_c_backslash_quote_does_not_close(self):
        command = "echo $'\\'' ; (cd sub; true) ; kubectl apply -f x"
        self.assertFalse(workdir.chdir_is_sequential(command))
        # `$'` を外した同じ形 (引用符の外の `(`) と同じ判定
        self.assertFalse(workdir.chdir_is_sequential("echo x ; (cd sub; true) ; kubectl apply -f x"))

    def test_ansi_c_quote_hides_its_content(self):
        """`$'...'` の中の `(` / `|` は引用符の中 (順に走る `cd` の判定を変えない)。"""
        for command in (
            "echo $'(\\'' ; cd sub ; kubectl apply -f x",
            "echo $'a|b\\\\' ; cd sub ; kubectl apply -f x",
        ):
            with self.subTest(command=command):
                self.assertTrue(workdir.chdir_is_sequential(command))

    def test_unclosed_ansi_c_quote_hides_the_rest(self):
        """閉じない `$'` は残り全部が引用符の中 (後ろの `(` を引用符の外と読まない)。"""
        self.assertTrue(workdir.chdir_is_sequential("cd sub ; kubectl apply -f x ; echo $'a (b"))

    def test_double_dollar_then_plain_quote(self):
        """`$$'` は `$$` の後ろの普通の引用符 (`\\` で閉じを飛ばさない)。"""
        self.assertEqual(workdir._unquoted("$$'\\' (x"), "$$___ (x")

    def test_quote_inside_double_quotes_is_not_ansi_c(self):
        self.assertEqual(workdir._unquoted("\"$'\" (x"), "____ (x")

    def test_length_is_preserved(self):
        for command in ("echo $'\\'' (x", "$'unclosed", "a$'b'c"):
            with self.subTest(command=command):
                self.assertEqual(len(workdir._unquoted(command)), len(command))


class DispatchTests(BaseWithTmpProject):
    ACCOUNTS = {"aws": "123456789012"}

    def setUp(self):
        super().setUp()
        self._write_accounts(self.ACCOUNTS)

    def _decide(self, command):
        with self.isolated_cache(), mock.patch(
            "services.aws.verify", return_value="不一致"
        ) as v:
            try:
                result = dispatch(command, str(self.project_dir))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        decision = "allow" if result is None else result["hookSpecificOutput"].get(
            "permissionDecision", "allow"
        )
        return decision, v

    def test_mismatch_is_denied(self):
        forms = [
            "echo ${x:-{} #}; {a}",
            "echo <(true)# ; {a}",
            "echo >(true)# ; {a}",
            "a=(1 2)# ; {a}",
            "echo $(true)# ; {a}",
            "echo \\)# ; {a}",
            "echo $'\\'' ; {a}",
        ]
        for form in forms:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command):
                decision, v = self._decide(command)
                self.assertTrue(v.called, f"検証されていない: {command!r}")
                self.assertEqual(decision, "deny")


if __name__ == "__main__":
    unittest.main()
