"""コマンド行の分解で、後ろのクラウド CLI を候補から落とさない (v0.29.1)。

- `#` を comment の始まりと読むのは語の先頭だけ。エスケープした空白・演算子
  (`echo \\ # ; aws ...`)、`$(...)` / `$((...))` の閉じ `)`、行継続の直後の `#` は語の続き
  (bash 3.2 / zsh とも後ろの aws が走る)
- ANSI-C 引用符 `$'...'` の中の `\\'` は引用符を閉じない (`echo $'a\\'b'; aws ...` の aws は走る)
- `$(` の中でも語の先頭の `#` から改行の手前までは comment (中の `)` / `'` で `$(` を閉じない)
- 代入の値の `$$'` の 2 つ目の `$` は ANSI-C 引用符の始まりではない
- `$(` の入れ子は再帰せずに辿る (`$(` を 1000 段ほど重ねると RecursionError になり、hook は
  内部エラーとして検証をスキップしていた)

各形の bash / zsh での実測は CHANGELOG の 0.29.1 の節。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core import command_parser  # noqa: E402
from core.command_parser import extract_candidates  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

AWS = "aws s3 rm s3://b/x"
_PKG_DIR = Path(__file__).resolve().parent.parent


def _cands(command):
    return [c for c, _e in extract_candidates(command)]


# (形, bash / zsh で aws が走るか)。`{a}` が aws。走る形は候補に入り、走らない形 (comment の
# 中・引用符の中) は候補に入らない (従来どおり)。
COMMENT_FORMS = [
    # エスケープした空白・タブ・演算子の後ろの `#` は語の続き
    ("echo \\ # ; {a}", True),
    ("echo a\\ # ; {a}", True),
    ("echo \\\\\\ # ; {a}", True),
    ("echo \\\t# ; {a}", True),
    ("echo \\;# ; {a}", True),
    ("echo \\|# ; {a}", True),
    ("echo \\&# ; {a}", True),
    ("echo \\(# ; {a}", True),
    ("echo \\)# ; {a}", True),
    # 置換・引用符の閉じの後ろも語の続き
    ("echo $(true)# ; {a}", True),
    ("echo $((1))# ; {a}", True),
    ("echo $'a\\'b'# ; {a}", True),
    ("echo 'a'# ; {a}", True),
    ("echo ${HOME}# ; {a}", True),
    # 行継続は取り除くので、その前の文字で決まる
    ("echo a\\\n# ; {a}", True),
    ("echo \\\n# ; {a}", False),
    ("echo \\\n#'\n{a}", True),
    ("echo a\\\n#'\n{a}", False),
    # 語の先頭の `#` は comment (エスケープした backslash の後ろの空白は素の空白)
    ("echo \\\\ # ; {a}", False),
    ("echo $(true) # ; {a}", False),
    ("echo a;# ; {a}", False),
    ("echo a|# ; {a}", False),
    ("echo a&# ; {a}", False),
    # 算術評価 `(( ))` とサブシェル `( )` の閉じの後ろは語の先頭 (comment)
    ("((1))# ; {a}", False),
    ("((1))#'\n{a}", True),
    ("(true)#'\n{a}", True),
]

ANSI_C_FORMS = [
    # チケットの形 (bash では aws が走る。zsh は最後の `}` で構文エラー)
    ("echo $'a\\'${'; {a}; echo }", True),
    ("echo $'a\\'b'; {a}", True),
    ("echo $'\\\\\\''; {a}", True),
    ("echo $'\\\\'; {a}", True),
    ("echo $(echo $'\\'' ); {a}", True),
    ("echo ${x:-$'\\''}; {a}", True),
    ("x=$'a\\'b'; {a}", True),
    ("echo $'a\\'b' ; {a} #'", True),
    # 引用符の中の aws は走らない
    ("echo $'a\\'; {a}; b'", False),
    ("echo $'a\\' ; {a}", False),
    # `$'` が ANSI-C 引用符でない場所 (二重引用符の中 / `$$` の後ろ / エスケープした `$`)
    ("echo \"$'\"; {a}", True),
    ("echo $$'a'; {a}", True),
    ("echo \\$'a; {a}", False),
]

# `$(` の中の comment。bash 3.2 と zsh の両方が comment と読む `#` から改行の手前までは comment で、
# 中の `'` / `)` / `$'\'` は `$(` を閉じず引用符も始めない。片方のシェルだけが comment と読む `#` は
# comment にしない (0.29.0 と同じ)。右の注記は aws が走るシェル。
SUBST_COMMENT_FORMS = [
    # マージ前レビューの指摘の形
    ("echo $(true #)#'\n)\n{a}", True),  # bash / zsh
    ("x=$(true #$'\\'\n)\n{a}", True),  # bash / zsh
    ("x=$(true # don't\n)\n{a}", True),  # bash / zsh (0.29.0 も落としていた)
    ("x=$( #(\n)\n{a}", True),  # bash / zsh (comment の中の `(` を数えない)
    ("x=$(echo $( #(\n)); {a}", True),  # bash / zsh
    # 両方が comment と読む: トップレベルの `$(` の先頭・素の空白・タブ・改行の直後
    ("x=$(#'\n)\n{a}", True),  # bash / zsh
    ("x=$(true #'\n)\n{a}", True),  # bash / zsh
    ("x=$(true\t#'\n)\n{a}", True),  # bash / zsh
    ("x=$(true\n#'\n)\n{a}", True),  # bash / zsh
    ("x=$(echo \\\n#'\n)\n{a}", True),  # bash / zsh (行継続は前の状態を引き継ぐ)
    # zsh だけが comment と読む: `;` / `&` / `|` / `(` / `)`・算術評価の閉じ・入れ子の `$(` の先頭の直後
    ("x=$(true;#)\n{a}", True),  # bash
    ("x=$(true&#)\n{a}", True),  # bash
    ("x=$(true|#)\n{a}", True),  # bash
    ("x=$( (#)\n)\n{a}", True),  # bash
    ("x=$( (true)#)\n{a}", True),  # bash
    ("x=$( ((1))#)\n{a}", True),  # bash
    ("x=$(echo $(#)\n)\n{a}", True),  # bash
    ("x=$(echo a$(#)\n)\n{a}", True),  # bash
    # zsh だけが comment と読む: パラメータ展開の中 (`$(` の中の `${` を 1 文字ずつ読む形も)
    ("echo ${x:-$( #)}\n{a}", True),  # bash
    ("echo ${x:-$(#)}\n{a}", True),  # bash
    ("x=$(${ #)});{a}", True),  # zsh
    # bash 3.2 だけが comment と読む: エスケープした空白の直後、行末が `\` の comment
    # (bash 3.2 は `\` + 改行で comment を次の行まで続ける)
    ("x=$(echo \\ #)\n{a}", True),  # zsh
    ("x=$(true #a\\\n)\n)\n{a}", True),  # bash
    ("x=$(true #\\\n)\n{a}", True),  # zsh
    ("x=$(true #'\\\n'\n)\n{a}", True),  # bash (改行で閉じる comment と読むと `'` から引用符が始まる)
    # どちらも comment と読まない: 素の文字・引用符・入れ子の置換の閉じ・行継続の直後
    ("x=$(echo a#)\n{a}", True),  # bash / zsh
    ("x=$(echo 'a'#)\n{a}", True),  # bash / zsh
    ("x=$(echo $(true)#)\n{a}", True),  # bash / zsh
    ("x=$(echo ${y $(true)#)\n{a}", True),  # bash (閉じない `${` の中で先に求めた `$(` の後ろ)
    ("x=$(echo a\\\n#)\n{a}", True),  # bash / zsh
    # comment で閉じない `$(` の後ろは置換の中 (どちらでも走らない)
    ("x=$(true #)\n; {a}", False),
    ("x=$(true #) ; {a}", False),
]

# 代入の値の `$$` (PID) の後ろの `$'` は ANSI-C 引用符ではない (bash 3.2 では aws が走る。
# zsh は `$'` と読んで走らない。走る側に合わせる)。
ENV_PID_FORMS = [
    "X=$$'a\\' {a}",
    "AWS_PROFILE=$$'a\\' {a}",
    "X=$$'a\\' {a}; echo \"'\"",
    "X=a$$'b\\' {a}",
    "X=$$$'a\\'b' {a}",  # bash / zsh (`$$` の後ろの `$'a\'b'` は ANSI-C 引用符)
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

    def test_comment_starts_only_at_word_start(self):
        self._check(COMMENT_FORMS)

    def test_comment_inside_command_substitution(self):
        self._check(SUBST_COMMENT_FORMS)

    def test_env_value_with_pid_before_quote(self):
        """`X=$$'a\\' aws ...` の 2 つ目の `$` を ANSI-C 引用符の始まりと読まない。"""
        for form in ENV_PID_FORMS:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command):
                try:
                    cands = _cands(command)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {e!r}")
                self.assertEqual(cands[0], AWS)

    def test_here_string_operator_keeps_hash(self):
        """`<<<` の直後の `#` は従来どおり comment にしない (bash では構文エラー。過剰検証側)。"""
        self.assertIn(AWS, _cands(f"echo <<<# ; {AWS}"))

    def test_ansi_c_quote_escaped_quote(self):
        self._check(ANSI_C_FORMS)

    def test_ansi_c_quote_in_parameter_expansion_keeps_one_word(self):
        """展開の中の `$'...'` の `&` は区切りではない (`--profile` を同じ候補に残す)。"""
        for command in (
            "aws s3 rm ${B:-$'a\\'&b'} --profile p",
            # `}` も引用符の中 (bash / zsh とも `'}&b` の 1 語)
            "aws s3 rm ${B:-$'\\'}&b'} --profile p",
        ):
            with self.subTest(command=command):
                self.assertEqual(_cands(command), [command])

    def test_unclosed_ansi_c_quote_in_parameter_expansion_is_not_closed(self):
        """展開の中の閉じない `$'` で展開を閉じたことにしない。

        bash / zsh では閉じない引用符で構文エラーになり何も走らない。`}` で展開を閉じると、
        引用符の中の ` & aws ...` を区切りとして外に出す (従来どおり残りは引用符の中)。
        """
        self.assertNotIn(AWS, _cands("echo ${x:-$'a} & " + AWS))

    def test_env_value_with_ansi_c_quote(self):
        """`X=$'a\\'b c' aws ...` の値の終わりを `\\'` で取り違えない (aws が残る)。"""
        for value in ("$'a\\'b c'", "$'a\\'b'", "$'\\\\' "):
            command = f"X={value} {AWS}"
            with self.subTest(command=command):
                self.assertEqual(_cands(command), [AWS])


class DeepCommandSubstitutionTests(unittest.TestCase):
    def test_deep_closed_command_substitution(self):
        command = "echo " + "$(" * 3000 + ")" * 3000 + " ; " + AWS
        try:
            cands = _cands(command)
        except RecursionError as e:
            self.fail(f"例外: {e!r}")
        self.assertIn(AWS, cands)

    def test_deep_unclosed_command_substitution_stays_conservative(self):
        """閉じない `$(` は深さに関係なく残り全部を 1 領域にする (浅いときと同じ)。"""
        for k in (1, 3000):
            command = "echo " + "$(" * k + " ; " + AWS
            with self.subTest(k=k):
                try:
                    cands = _cands(command)
                except RecursionError as e:
                    self.fail(f"例外: {e!r}")
                self.assertEqual(cands, [command])

    def test_revisited_inner_substitution_is_not_rescanned(self):
        """閉じない `${` の中を読み直すとき、入れ子の `$(` は記録した終端で飛ばす。

        `${x $(echo ` を重ねると、各 `${` が閉じない扱いになって外側の `$(` がその中身を
        1 文字ずつ読み直す。記録を引かないと、読み直すたびに中の `$(` を全部走査し直す。
        """
        import time

        command = AWS + "\necho " + "${x $(echo " * 4000 + ")" * 4000
        started = time.perf_counter()
        try:
            cands = _cands(command)
        except RecursionError as e:
            self.fail(f"例外: {e!r}")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertIn(AWS, cands)

    def test_recorded_inner_end_is_used(self):
        """入れ子の `$(` に記録がある (閉じない `${` の中で先に求めた) ときは、それを引く。

        記録を引かずに走査し直しても結果は同じなので、記録を差し替えて引いたことを確かめる。
        """
        text = "$(a $(b) c) d)"
        memo = command_parser._DOLLAR_MEMO
        memo.text = text
        memo.ends = {4: 11}  # 本当の終端は 8。11 から読むと最初の `)` は `d` の後ろ
        self.assertEqual(command_parser._skip_opaque(text, 0), len(text))
        memo.text = None
        self.assertEqual(command_parser._skip_opaque(text, 0), len("$(a $(b) c)"))

    def test_inner_ends_are_recorded(self):
        """入れ子の `$(` の終端も記録する (閉じたもの・閉じないもの)。"""
        text = "$(a $(b) $(c"
        self.assertEqual(command_parser._skip_opaque(text, 0), len(text))
        ends = command_parser._DOLLAR_MEMO.ends
        self.assertEqual(ends.get(4), 8)
        self.assertEqual(ends.get(9), len(text))
        text = "$(a $(b) c)"
        self.assertEqual(command_parser._skip_opaque(text, 0), len(text))
        self.assertEqual(command_parser._DOLLAR_MEMO.ends.get(4), 8)

    def test_depth_is_restored(self):
        for text in ("$(" * 50, "$(" * 50 + ")" * 50, "$(a ${x:-$(b)} )"):
            with self.subTest(text=text[:20]):
                command_parser._skip_opaque(text, 0)
                self.assertEqual(command_parser._DOLLAR_MEMO.depth, 0)
                self.assertEqual(command_parser._DOLLAR_MEMO.brace, 0)

    def test_open_substitutions_count_toward_the_nesting_limit(self):
        """開いている `$(` の段数も `${` の深さに数える (再帰版と同じ境界)。

        `$(` を k 段重ねた中の `${x:-(}` は、深さ k + 1 が上限以内なら 1 領域 (`(` を数えない)、
        超えたら 1 文字ずつ読んで `(` を数える (`$(` を閉じるのに `)` が 1 つ多く要る)。
        """
        limit = command_parser._DOLLAR_NEST_LIMIT
        for k, extra in ((limit - 1, 0), (limit, 1)):
            text = "$(" * k + "${x:-(}" + ")" * (k + 1) + " x"
            with self.subTest(k=k):
                self.assertEqual(command_parser._skip_opaque(text, 0), len(text) - 3 + extra)
        # 閉じた `$(` は数えない (並べた `$(x)` が何個あっても、中の `${` の深さは 2)
        text = "$(" + "$(x) " * (limit + 6) + "${x:-(}" + ")) x"
        self.assertEqual(command_parser._skip_opaque(text, 0), len(text) - 3)


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
            "echo \\ # ; {a}",
            "echo $(true)# ; {a}",
            "echo $'a\\'${'; {a}; echo }",
            "echo $'a\\'b'; {a}",
            "X=$'a\\'b c' {a}",
            "echo " + "$(" * 3000 + ")" * 3000 + " ; {a}",
            "X=$$'a\\' {a}",
            "AWS_PROFILE=$$'a\\' {a}",
            "X=$$'a\\' {a}; echo \"'\"",
            "echo $(true #)#'\n)\n{a}",
            "x=$(true #$'\\'\n)\n{a}",
            "x=$(true # don't\n)\n{a}",
        ]
        for form in forms:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command[:60]):
                decision, v = self._decide(command)
                self.assertTrue(v.called, f"検証されていない: {command[:60]!r}")
                self.assertEqual(decision, "deny")


class MainEntryTests(unittest.TestCase):
    """hook 全体 (`__main__`) に深い `$(` を流しても内部エラーにならない (検証をスキップしない)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = Path(self.tmp) / "project"
        self.project.mkdir()
        cache_tmp = Path(self.tmp) / "cache"
        cache_tmp.mkdir()
        home = Path(self.tmp) / "home"
        home.mkdir()
        self.env = _testutil.sanitized_env(
            {
                **os.environ,
                **_testutil.cli_config_env(Path(self.tmp) / "cliconfig", home),
                "TMPDIR": str(cache_tmp),
                "CLAUDE_PROJECT_DIR": str(self.project),
            }
        )

    def _run(self, command):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.project),
        }
        return subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    def test_deep_closed_substitution_then_cli_is_denied(self):
        """未設定の project の書込は deny (0.29.0 は RecursionError で警告だけ出して通していた)。"""
        res = self._run("echo " + "$(" * 1200 + ")" * 1200 + " ; " + AWS)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("内部エラー", res.stdout + res.stderr)
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")

    def test_deep_unclosed_substitution_is_not_an_internal_error(self):
        res = self._run("echo " + "$(" * 1200 + " ; " + AWS)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("内部エラー", res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
