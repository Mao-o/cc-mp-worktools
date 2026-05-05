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

from handlers.bash.operand_lexer import _glob_operand_is_dotenv_match


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

    def test_dotenv_with_outer_char_class(self):
        # fnmatchcase(".env", "[.]env") = True
        self.assertTrue(_glob_operand_is_dotenv_match("[.]env"))

    def test_envrc_star(self):
        self.assertTrue(_glob_operand_is_dotenv_match(".envrc*"))

    def test_star_envrc(self):
        # fnmatchcase(".envrc", "*.envrc") = True
        self.assertTrue(_glob_operand_is_dotenv_match("*.envrc"))


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
        # fnmatchcase(".env", "*") = True だが、"*" 単体は意図しない過検出を
        # 避けるため False が望ましい — ただし fnmatchcase("*", ...) の挙動上
        # ".env" は "*" に match するので True を返してしまう。これは「危険な
        # glob を打つ時点で日常から逸脱している」として deny で許容する。
        # (実機の `cat *` は通常意図した結果ではないため deny 倒れで問題なし)
        self.assertTrue(_glob_operand_is_dotenv_match("*"))

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
