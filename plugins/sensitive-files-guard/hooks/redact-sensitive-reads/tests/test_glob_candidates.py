"""Glob 候補列挙 (``_glob_operand_is_sensitive``) の単体テスト (0.3.2)。

operand に ``*`` ``?`` ``[`` を含むケースで、既定 patterns.txt と交差する候補が
1 つでも生成され ``is_sensitive`` で True になるかを確認する。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from handlers.bash_handler import (
    _glob_candidates,
    _glob_operand_is_sensitive,
    _literalize,
)


# 既定 patterns.txt の主要 rules を抜粋した定数 (matcher_test 系と整合)
_DEFAULT_RULES: list[tuple[str, bool]] = [
    ("*.local.json", False),
    ("*.local.yaml", False),
    ("*.local.yml", False),
    ("*.local.toml", False),
    ("*.secret*", False),
    (".env", False),
    (".env.*", False),
    (".envrc", False),
    ("*.envrc", False),
    ("*.pem", False),
    ("*.key", False),
    ("*.p12", False),
    ("*.pfx", False),
    ("*.keystore", False),
    ("*.jks", False),
    ("id_rsa*", False),
    ("id_dsa*", False),
    ("id_ecdsa*", False),
    ("id_ed25519*", False),
    ("credentials*.json", False),
    ("service-account*.json", False),
    (".npmrc", False),
    (".pypirc", False),
    (".netrc", False),
    ("*.example", True),
    ("*.template", True),
    ("*.sample", True),
    ("*.dist", True),
    ("*.example.*", True),
    ("*.template.*", True),
    ("*.sample.*", True),
    ("*.dist.*", True),
    ("*.pub", True),
]


class TestLiteralize(unittest.TestCase):
    """``_literalize`` は glob 文字を取り除き最小 literal 表現を返す。"""

    def test_trailing_star(self):
        self.assertEqual(_literalize(".env*"), ".env")

    def test_inner_star(self):
        self.assertEqual(_literalize("*.env.*"), ".env.")

    def test_char_class(self):
        self.assertEqual(_literalize("[.]env"), "env")

    def test_question_mark(self):
        self.assertEqual(_literalize("?ecret*"), "ecret")

    def test_id_rsa_prefix(self):
        self.assertEqual(_literalize("id_rsa*"), "id_rsa")

    def test_no_glob(self):
        self.assertEqual(_literalize(".env"), ".env")

    def test_only_glob(self):
        self.assertEqual(_literalize("*"), "")

    def test_unclosed_bracket(self):
        # ``[`` で `]` が無ければ literal の `[` として扱う
        self.assertEqual(_literalize("[abc"), "[abc")


class TestGlobCandidates(unittest.TestCase):
    """``_glob_candidates`` は操作対象の op_stem + rule stem 経由の候補を生成する。"""

    def test_dotenv_star_includes_dot_env(self):
        cands = _glob_candidates(".env*", _DEFAULT_RULES)
        self.assertIn(".env", cands)

    def test_star_log_returns_log_stem(self):
        cands = _glob_candidates("*.log", _DEFAULT_RULES)
        # op_stem (".log") は必ず入る
        self.assertIn(".log", cands)

    def test_id_star_pulls_id_rsa(self):
        cands = _glob_candidates("id_*", _DEFAULT_RULES)
        # id_rsa の literal stem が op glob (id_*) に match → 候補入り
        self.assertIn("id_rsa", cands)


class TestGlobOperandIsSensitive(unittest.TestCase):
    """``_glob_operand_is_sensitive`` の判定。"""

    def test_dotenv_star_true(self):
        self.assertTrue(_glob_operand_is_sensitive(".env*", _DEFAULT_RULES))

    def test_dotenv_dot_star_true(self):
        self.assertTrue(_glob_operand_is_sensitive(".env.*", _DEFAULT_RULES))

    def test_envrc_star_true(self):
        self.assertTrue(_glob_operand_is_sensitive(".envrc*", _DEFAULT_RULES))

    def test_id_rsa_star_true(self):
        self.assertTrue(_glob_operand_is_sensitive("id_rsa*", _DEFAULT_RULES))

    def test_id_star_true(self):
        self.assertTrue(_glob_operand_is_sensitive("id_*", _DEFAULT_RULES))

    def test_star_key_true(self):
        self.assertTrue(_glob_operand_is_sensitive("*.key", _DEFAULT_RULES))

    def test_cred_star_json_true(self):
        # cred*.json: op_stem = "cred.json"。rule "credentials*.json" の stem は
        # "credentials.json"。連結 op_stem + pt_stem = "cred.jsoncredentials.json"
        # は op glob に match しないが、pt_stem 単体 "credentials.json" が
        # op glob "cred*.json" に match → 候補入り → is_sensitive True
        self.assertTrue(
            _glob_operand_is_sensitive("cred*.json", _DEFAULT_RULES)
        )

    def test_star_log_false(self):
        self.assertFalse(_glob_operand_is_sensitive("*.log", _DEFAULT_RULES))

    def test_dotenv_example_literal_false(self):
        # literal だが is_sensitive 経由で見たときに exclude 決着で False。
        # _glob_operand_is_sensitive は has_glob 前提だが op_stem 単体でも
        # 既存 is_sensitive を呼ぶため確認できる。
        self.assertFalse(_glob_operand_is_sensitive(".env.example", _DEFAULT_RULES))

    def test_dotenv_example_star_false(self):
        # 全候補が exclude 決着 (.env.* と !*.example の last-match-wins)
        self.assertFalse(_glob_operand_is_sensitive(".env.example*", _DEFAULT_RULES))

    def test_dotenv_sample_literal_false(self):
        self.assertFalse(_glob_operand_is_sensitive(".env.sample", _DEFAULT_RULES))

    def test_uppercase_glob_case_insensitive_default(self):
        # SFG_CASE_SENSITIVE 未設定 (= 既定 case-insensitive) では .E* も hit
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SFG_CASE_SENSITIVE", None)
            self.assertTrue(_glob_operand_is_sensitive(".E*", _DEFAULT_RULES))

    def test_uppercase_glob_case_sensitive_optout(self):
        with mock.patch.dict(os.environ, {"SFG_CASE_SENSITIVE": "1"}):
            self.assertFalse(_glob_operand_is_sensitive(".E*", _DEFAULT_RULES))


class TestExcludeRulesBehavior(unittest.TestCase):
    """exclude rules の last-match-wins 整合性確認。

    rules の tuple は ``(pattern, is_exclude)`` 形式で、``!`` プレフィクスは
    ``_parse_patterns_text`` がパース時に取り除く。テストでも `!` を含めない。
    """

    def test_minimal_include_only(self):
        rules = [(".env", False), ("*.example", True)]
        # ".env*" → 候補 ".env" が include 決着 → True
        self.assertTrue(_glob_operand_is_sensitive(".env*", rules))

    def test_minimal_exclude_decides(self):
        rules = [(".env.*", False), ("*.example", True)]
        # ".env.example*" → 候補 ".env.example" が exclude 決着 → False
        self.assertFalse(_glob_operand_is_sensitive(".env.example*", rules))


if __name__ == "__main__":
    unittest.main()
