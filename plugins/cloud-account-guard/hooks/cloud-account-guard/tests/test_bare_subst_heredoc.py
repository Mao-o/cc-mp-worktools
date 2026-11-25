"""引用符で包まないコマンド置換 (`$(...)`) の中の heredoc の本文の読みを固定する (v0.30.2)。

hook は置換の閉じを探すとき、中の heredoc の本文を読み分けない (`core/command_parser.py` の
`_command_substitution_end`)。本文の `'`・対にならない `"` や backtick・対応しない `(` を引用符や括弧の
始まりと読むので、置換の閉じを見失い、後ろの CLI が候補にならない。zsh ではこの形が走る
(bash 3.2 は `$(` の中の heredoc の本文にこれらの文字があると構文エラーにする)。

置換を二重引用符で包む普段の形 (`X="$(cat <<'EOF'` … `)"`) は、本文に `'` があっても後ろの CLI が
候補になる (守る範囲)。包まない形は、Claude Code のセッションで実際に使われた Bash のコマンド
39,009 件のうち 2 件で、どちらも本文に `'` も対にならない `"` も無かった。普段書かない形なので、
読みを足さずに守らない範囲とした (README「守らない範囲」、docs/DEVELOPMENT.md の実装の規則)。
この挙動を変えたら、README の守らない範囲の行も一緒に直す。

右の注記は、その CLI が走るシェル (bash 3.2 / zsh -f で実測)。
"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from core.command_parser import extract_candidates  # noqa: E402

AWS = "aws s3 rm s3://b/x"

# 置換の後ろの CLI が候補になる形。
IN_SCOPE_FORMS = [
    "X=\"$(cat <<'EOF'\ndon't\nEOF\n)\" && {a}",  # zsh (二重引用符で包む普段の形)
    "gh pr create --body \"$(cat <<'EOF'\nit's (x)\nEOF\n)\" && {a}",  # zsh
    "X=$(cat <<'EOF'\nhello\nEOF\n) && {a}",  # bash / zsh (本文に引用符・括弧が無い)
    "X=$(cat <<'EOF'\na # b\nEOF\n) && {a}",  # bash / zsh
]

# 守らない範囲: zsh では走るが、置換の閉じを見失って候補にならない形。
OUT_OF_SCOPE_FORMS = [
    "X=$(cat <<'EOF'\ndon't\nEOF\n) && {a}",  # zsh
    "X=$(cat <<EOF\ndon't\nEOF\n); {a}",  # zsh
    "gh pr create --body $(cat <<'EOF'\ndon't\nEOF\n) && {a}",  # zsh
    "X=$(cat <<'EOF'\ndon't\nEOF\n)\n{a}",  # zsh
    "X=$(cat <<'EOF'\n3\" screens\nEOF\n) && {a}",  # zsh
    "X=$(cat <<'EOF'\na (b\nEOF\n) && {a}",  # zsh
    "X=$(cat <<'EOF'\na ` b\nEOF\n) && {a}",  # zsh
]


class BareSubstitutionHeredocTests(unittest.TestCase):
    def cli_names(self, command):
        try:
            cands = [c for c, _e in extract_candidates(command)]
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        self.assertTrue(cands, command)
        return [c.split(None, 1)[0] for c in cands]

    def test_cli_after_the_substitution_is_a_candidate(self):
        for form in IN_SCOPE_FORMS:
            command = form.format(a=AWS)
            with self.subTest(command=command):
                self.assertIn("aws", self.cli_names(command))

    def test_out_of_scope_forms_do_not_yield_the_cli(self):
        """README の守らない範囲の例。候補になるように直したら、README の行も外す。"""
        for form in OUT_OF_SCOPE_FORMS:
            command = form.format(a=AWS)
            with self.subTest(command=command):
                self.assertNotIn("aws", self.cli_names(command))


if __name__ == "__main__":
    unittest.main()
