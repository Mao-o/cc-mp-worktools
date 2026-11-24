"""二重引用符の中の置換 (`"$(...)"` / `"${...}"` / ``"`...`"``) の中の引用符の読みを固定する (v0.30.1)。

hook は二重引用符を中の次の `"` で閉じると読む (置換の中を新しい引用の文脈として読まない。
`core/command_parser.py` の `_skip_opaque`)。シェル (bash 3.2 / zsh -f) は置換の中を新しい引用の文脈として
読むので、中の引用符の対応で読みがずれる:

- 中の引用符が対になる普段の形 (`--format="%h %s"`、本文に `'` がある heredoc) は、区切りの後ろの
  CLI がシェルと同じく候補になる (守る範囲)
- 対応がずれる形 (README「守らない範囲」の「二重引用符の中の置換」) は、後ろの CLI が候補にならない。
  普段書かない形なので、読みを足さずに守らない範囲とした (docs/DEVELOPMENT.md の実装の規則)。
  この挙動を変えたら、README の守らない範囲の行も一緒に直す

右の注記は、その CLI が走るシェル (bash 3.2 / zsh -f で実測。CHANGELOG の 0.30.1 の節)。
`$(` の中の heredoc は bash 3.2 が構文エラーにするので、zsh でだけ走る。
"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from core.command_parser import extract_candidates  # noqa: E402

AWS = "aws s3 rm s3://b/x"
GH = "gh pr create --fill"

# 区切りの後ろ・代入の後ろの CLI が候補になる普段の形。
IN_SCOPE_FORMS = [
    ('gh pr create --title "$(git log -1 --format="%s")" && {a}', AWS),  # bash / zsh
    ('gh pr create --title "$(git log -1 --format="%h %s")" && {a}', AWS),  # bash / zsh
    ('a="$(git log --format="%h %s")"; {a}', AWS),  # bash / zsh
    ('X="$(git log -1 --format="%s")" {g}', GH),  # bash / zsh
    ('X="$(echo "a" "b")" {a}', AWS),  # bash / zsh
    ("git commit -m \"$(cat <<'EOF'\nfix: don't drop \"x\"\nEOF\n)\" && {g}", GH),  # zsh
    ("gh pr create --body \"$(cat <<'EOF'\ndon't\nEOF\n)\" && {a}", AWS),  # zsh
    ('echo "$(printf "%s && %s" a b)"; {a}', AWS),  # bash / zsh
    ('echo "$(echo ")")"; {a}', AWS),  # bash / zsh
    ('echo "${{x:-"a b"}}"; {a}', AWS),  # bash / zsh
    ('echo "`echo "a"`"; {a}', AWS),  # bash / zsh
]

# 守らない範囲: シェルでは走るが、引用符の対応を読み違えて候補にならない形。
OUT_OF_SCOPE_FORMS = [
    ('X="$(git log --format="%h %s")" {g}', "gh"),  # bash / zsh (中の二重引用符に空白)
    ('X="$(echo "a b")" {a}', "aws"),  # bash / zsh
    ('echo "$(printf "Refs #12")"; {a}', "aws"),  # bash / zsh (空白の後ろの `#` を comment と読む)
    ('git commit -m "$(printf "fix: x\\n\\nRefs #12")" && {g}', "gh"),  # bash / zsh
    ("echo \"$(grep -c '\"' f)\"; {a}", "aws"),  # bash / zsh (一重引用符の中の `"`)
    ("git commit -m \"$(cat <<'EOF'\nfix: 3\" screens\nEOF\n)\" && git push && {g}", "gh"),  # zsh
    ("X=\"$(cat <<'EOF'\n3\" screens\nEOF\n)\" {a}", "aws"),  # zsh (heredoc の本文の対応しない `"`)
    ('echo "${{x:-"a #b"}}"; {a}', "aws"),  # bash / zsh
    ('X="${{Y:-"a b"}}" {a}', "aws"),  # bash / zsh
    ('echo "`echo "a #b"`"; {a}', "aws"),  # bash / zsh
]


class DoubleQuotedSubstitutionTests(unittest.TestCase):
    def cands(self, command):
        try:
            return [c for c, _e in extract_candidates(command)]
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_cli_after_balanced_inner_quotes_is_a_candidate(self):
        for form, cli in IN_SCOPE_FORMS:
            command = form.format(a=AWS, g=GH)
            with self.subTest(command=command):
                self.assertIn(cli, self.cands(command))

    def test_out_of_scope_forms_do_not_yield_the_cli(self):
        """README の守らない範囲の例。候補になるように直したら、README の行も外す。"""
        for form, name in OUT_OF_SCOPE_FORMS:
            command = form.format(a=AWS, g=GH)
            with self.subTest(command=command):
                cands = self.cands(command)
                self.assertTrue(cands, command)
                self.assertNotIn(name, [c.split(None, 1)[0] for c in cands])

    def test_separator_in_inner_quotes_splits(self):
        """README の既知の制限 (止めすぎ側): 中の二重引用符の `&&` で区切り、文字列の中の aws も候補になる
        (シェルでは gh だけが走る)。"""
        cands = self.cands('gh pr create --body "$(printf "a && ' + AWS + '")"')
        self.assertIn(AWS + '")"', cands)


if __name__ == "__main__":
    unittest.main()
