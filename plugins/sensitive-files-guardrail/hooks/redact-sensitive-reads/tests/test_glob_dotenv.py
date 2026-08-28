"""``_glob_operand_is_dotenv_match`` (0.8.0 新設) の単体テスト。

operand glob (``*`` / ``?`` / ``[`` 含む) が dotenv literal stem (``.env`` /
``.envrc``) に ``fnmatchcase`` で一致するときだけ True を返す。0.3.2〜0.7.x で
deny 寄り過ぎだった既定 rules 候補列挙 (``_glob_candidates`` /
``_glob_operand_is_sensitive``) は 0.8.0 で撤廃され、この簡素な判定に置き換え
られた。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from handlers.bash.operand_lexer import (
    _command_enables_dotglob,
    _glob_operand_is_dotenv_match,
)


class TestDotenvGlobMatch(unittest.TestCase):
    """``.env`` / ``.envrc`` に fnmatch する glob を True と判定する。"""

    def test_dotenv_star(self):
        # fnmatchcase(".env", ".env*") = True
        self.assertTrue(_glob_operand_is_dotenv_match(".env*"))

    def test_dotenv_with_question(self):
        # fnmatchcase(".env", ".en?") = True
        self.assertTrue(_glob_operand_is_dotenv_match(".en?"))

    def test_dotenv_with_inner_char_class(self):
        # fnmatchcase(".env", ".e[n]v") = True
        self.assertTrue(_glob_operand_is_dotenv_match(".e[n]v"))

    def test_envrc_star(self):
        self.assertTrue(_glob_operand_is_dotenv_match(".envrc*"))

    def test_directory_glob_with_literal_dotenv_basename(self):
        # 0.22.0: shell の pathname expansion は path 要素ごと。``*/.env`` は
        # ``sub/.env`` に展開される (bash 3.2 / zsh 実測) ので basename 部分
        # ``.env`` で判定する。0.21.x は operand 全体を fnmatch していて
        # ``fnmatchcase(".env", "*/.env")`` = False → ask_or_allow に落ちていた
        self.assertTrue(_glob_operand_is_dotenv_match("*/.env"))
        self.assertTrue(_glob_operand_is_dotenv_match("**/.env"))
        self.assertTrue(_glob_operand_is_dotenv_match("*/.env*"))
        self.assertTrue(_glob_operand_is_dotenv_match("sub*/.envrc"))


class TestLeadingDotSemantics(unittest.TestCase):
    """0.22.0: shell (POSIX / bash / zsh) の pathname expansion では、ファイル名先頭の
    ``.`` は pattern 先頭の literal ``.`` でしか一致しない。``*`` / ``?`` /
    bracket 式は先頭ドットに一致しない (bash 3.2 / zsh 5 実測: ``echo *`` /
    ``echo ?env`` / ``echo [.]env`` / ``echo *.envrc`` は ``.env`` / ``.envrc``
    に展開されない)。fnmatch はこの規則を持たないため 0.21.x までは
    ``fnmatchcase(".env", "*")`` = True で裸の ``*`` が全 mode deny だった。
    """

    def test_bare_star_does_not_match_dotfiles(self):
        # `git add *` / `cp * dst/` / `tar czf out.tgz *` / heredoc 内の `kb * 1024`
        self.assertFalse(_glob_operand_is_dotenv_match("*"))

    def test_question_and_star_prefix_do_not_match_leading_dot(self):
        self.assertFalse(_glob_operand_is_dotenv_match("?env"))
        self.assertFalse(_glob_operand_is_dotenv_match("*env"))
        self.assertFalse(_glob_operand_is_dotenv_match("*.env"))

    def test_bracket_does_not_match_leading_dot(self):
        # POSIX は implementation-defined だが bash / zsh とも一致しない
        self.assertFalse(_glob_operand_is_dotenv_match("[.]env"))
        self.assertFalse(_glob_operand_is_dotenv_match("[.a]env"))

    def test_star_envrc_matches_only_non_dot_names(self):
        # ``*.envrc`` は ``foo.envrc`` にしか展開されない。``foo.envrc`` は既定
        # rules (``*.envrc``) の対象だが、``*.key`` / ``cred*.json`` と同じ
        # 「既定 rules との交差」クラスなので 0.8.0 の方針どおり ask_or_allow
        self.assertFalse(_glob_operand_is_dotenv_match("*.envrc"))

    def test_directory_glob_only(self):
        # basename 部分に glob が無く dotenv でもない / 空
        self.assertFalse(_glob_operand_is_dotenv_match("sub/*"))
        self.assertFalse(_glob_operand_is_dotenv_match(".env/*"))
        self.assertFalse(_glob_operand_is_dotenv_match("*/"))

    def test_explicit_leading_dot_still_matches(self):
        for op in (".env*", ".en?", ".e[n]v", ".*", ".[e]nv*"):
            with self.subTest(op=op):
                self.assertTrue(_glob_operand_is_dotenv_match(op))

    def test_dotglob_restores_conservative_semantics(self):
        # Codex R1 P1: ``shopt -s dotglob`` / ``GLOBIGNORE=x`` (zsh は
        # ``setopt globdots``) が有効だと ``*`` は dotfile にも展開される
        # (bash 3.2 実測: dotglob で ``*`` → .env、``[.]env`` → .env、
        # ``*.envrc`` → .envrc foo.envrc)。同じコマンド内で有効化されている
        # ときは 0.21.x までの fnmatch の意味論 (先頭ドットも一致) で判定する
        # ``sub/*`` も dotglob 下では ``sub/.env`` に展開される
        for op in ("*", "?env", "*env", "[.]env", "*.envrc", "*/.env", ".env*", "sub/*"):
            with self.subTest(op=op):
                self.assertTrue(_glob_operand_is_dotenv_match(op, dotglob=True))
        for op in ("*.log", "id_rsa*", ".env.*", "*.env.example"):
            with self.subTest(op=op):
                self.assertFalse(_glob_operand_is_dotenv_match(op, dotglob=True))


class TestCommandEnablesDotglob(unittest.TestCase):
    """同一コマンド内の dotglob / GLOB_DOTS / GLOBIGNORE の有効化を検出する。

    shell option は Bash tool の呼び出しごとに初期化されるため、同じコマンド文字列
    に現れる形だけが対象。profile (.bashrc / .zshenv) で常時有効な環境は hook から
    見えない (docs で開示)。検出は保守的 (``shopt -u dotglob`` や単なる言及でも
    True) で、効果は「fnmatch の意味論に戻る」= deny 寄りにしか倒れない。
    """

    def test_detects_enabling_forms(self):
        for cmd in (
            "shopt -s dotglob; cat *",
            "shopt -s extglob dotglob && cat *",
            "shopt -sq dotglob; cat *",
            "setopt globdots; cat *",
            "setopt GLOB_DOTS; cat *",
            "setopt glob_dots; cat *",
            "set -o globdots; cat *",
            "GLOBIGNORE=x; cat *",
            "export GLOBIGNORE=.git; cat *",
            "shopt -u dotglob; cat *",  # 保守側 (無効化も True)
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_command_enables_dotglob(cmd))

    def test_ignores_unrelated_commands(self):
        for cmd in (
            "cat *",
            "shopt -s nullglob; cat *",
            "shopt -s extglob; cat *",
            "git add *",
            "echo $GLOBIGNORE",
            "",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_command_enables_dotglob(cmd))


class TestNonDotenvGlobAskOrAllow(unittest.TestCase):
    """dotenv stem に fnmatch しない glob は False (呼出側で ask_or_allow に格下げ)。"""

    def test_dotenv_dot_star(self):
        # fnmatchcase(".env", ".env.*") = False (".env." 以降が必要)
        self.assertFalse(_glob_operand_is_dotenv_match(".env.*"))

    def test_dotenv_example_star(self):
        # fnmatchcase(".env", ".env.example*") = False
        self.assertFalse(_glob_operand_is_dotenv_match(".env.example*"))

    def test_id_rsa_star(self):
        self.assertFalse(_glob_operand_is_dotenv_match("id_rsa*"))

    def test_id_star(self):
        self.assertFalse(_glob_operand_is_dotenv_match("id_*"))

    def test_star_key(self):
        self.assertFalse(_glob_operand_is_dotenv_match("*.key"))

    def test_cred_star_json(self):
        self.assertFalse(_glob_operand_is_dotenv_match("cred*.json"))

    def test_star_log(self):
        self.assertFalse(_glob_operand_is_dotenv_match("*.log"))


class TestEmptyAndEdgeCases(unittest.TestCase):
    def test_empty_returns_false(self):
        self.assertFalse(_glob_operand_is_dotenv_match(""))

    def test_pure_star_returns_false(self):
        # 0.21.x までは fnmatchcase(".env", "*") = True を「危険な glob を打つ
        # 時点で日常から逸脱している」として deny で許容していたが、実シェルの
        # ``*`` は dotfile に展開されず、`git add *` / `cp * dst/` /
        # `tar czf out.tgz *` / heredoc 本文の `kb * 1024` が全 mode で止まって
        # いた (``TestLeadingDotSemantics``)。
        self.assertFalse(_glob_operand_is_dotenv_match("*"))

    def test_no_glob_chars_still_works(self):
        # glob 文字なし。fnmatchcase は exact match と同等になる
        # ".env" → fnmatchcase(".env", ".env") = True
        self.assertTrue(_glob_operand_is_dotenv_match(".env"))
        # "foo.txt" → False
        self.assertFalse(_glob_operand_is_dotenv_match("foo.txt"))


class TestCaseSensitivity(unittest.TestCase):
    """``SFG_CASE_SENSITIVE=1`` 設定で大文字小文字を区別する。未設定時は lower 比較。"""

    def test_uppercase_glob_case_insensitive_default(self):
        # SFG_CASE_SENSITIVE 未設定 (= 既定 case-insensitive) では .E* も hit
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SFG_CASE_SENSITIVE", None)
            self.assertTrue(_glob_operand_is_dotenv_match(".E*"))

    def test_uppercase_glob_case_sensitive_optout(self):
        with mock.patch.dict(os.environ, {"SFG_CASE_SENSITIVE": "1"}):
            self.assertFalse(_glob_operand_is_dotenv_match(".E*"))


if __name__ == "__main__":
    unittest.main()
