"""``redaction/placeholders.py`` (0.9.0 新設、E2) の単体テスト。

literal 完全一致 / regex 一致 / 一致なし / クォート剥がし / 戻り値の label を網羅。
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from redaction.placeholders import (
    PLACEHOLDER_LITERALS,
    PLACEHOLDER_PATTERNS,
    looks_placeholder,
)


class TestLiteralMatches(unittest.TestCase):
    """PLACEHOLDER_LITERALS の各 literal に case-insensitive で一致する。"""

    def test_dummy(self):
        ok, label = looks_placeholder("dummy")
        self.assertTrue(ok)
        self.assertEqual(label, "dummy")

    def test_uppercase_dummy(self):
        ok, label = looks_placeholder("DUMMY")
        self.assertTrue(ok)
        self.assertEqual(label, "dummy")  # lower-cased

    def test_changeme(self):
        ok, label = looks_placeholder("changeme")
        self.assertTrue(ok)
        self.assertEqual(label, "changeme")

    def test_change_me_underscore(self):
        ok, label = looks_placeholder("change_me")
        self.assertTrue(ok)
        self.assertEqual(label, "change_me")

    def test_your_secret(self):
        ok, label = looks_placeholder("your_secret")
        self.assertTrue(ok)
        self.assertEqual(label, "your_secret")

    def test_lorem(self):
        ok, label = looks_placeholder("lorem")
        self.assertTrue(ok)
        self.assertEqual(label, "lorem")

    def test_all_literals_recognized(self):
        for literal in PLACEHOLDER_LITERALS:
            with self.subTest(literal=literal):
                ok, label = looks_placeholder(literal)
                self.assertTrue(ok, msg=f"{literal!r} should match")
                self.assertEqual(label, literal)


class TestPatternMatches(unittest.TestCase):
    """PLACEHOLDER_PATTERNS の各 regex がラベルを返す。"""

    def test_your_anything_here(self):
        ok, label = looks_placeholder("your_jwt_secret_here")
        self.assertTrue(ok)
        self.assertEqual(label, "your_*_here")

    def test_your_dash_here(self):
        ok, label = looks_placeholder("your-key-here")
        self.assertTrue(ok)
        self.assertEqual(label, "your_*_here")

    def test_angle_brackets(self):
        ok, label = looks_placeholder("<your-key>")
        self.assertTrue(ok)
        self.assertEqual(label, "<...>")

    def test_three_stars(self):
        ok, label = looks_placeholder("***")
        self.assertTrue(ok)
        self.assertEqual(label, "***")

    def test_many_stars(self):
        ok, label = looks_placeholder("**********")
        self.assertTrue(ok)
        self.assertEqual(label, "***")

    def test_xxx_lower(self):
        ok, label = looks_placeholder("xxx")
        self.assertTrue(ok)
        # "xxx" は literal 辞書にも入っているので literal label 優先
        self.assertEqual(label, "xxx")

    def test_xxxxx(self):
        ok, label = looks_placeholder("xxxxx")
        self.assertTrue(ok)
        # literal 一致しないので regex のラベル
        self.assertEqual(label, "xxx")

    def test_env_name_itself(self):
        """環境名**そのもの** (``ENV=local`` 等) は placeholder 扱いを続ける。

        0.31.0 で後続語の許容 (``^(test|dev|local|staging)[_-]?\\w*$``) を外した
        が、本来の目的である「値が環境名そのもの」は維持する。``development``
        は完全一致形で穴を開けないため alternation に含める (隔離内レビュー
        P3-5 — 後続許容を外した際に一緒に落ちて ``NODE_ENV=development`` が
        ``<set>`` に退行していた)。
        """
        for value in (
            "dev", "local", "staging", "DEV", "Staging",
            "development", "DEVELOPMENT", "Development",
        ):
            with self.subTest(value=value):
                ok, label = looks_placeholder(value)
                self.assertTrue(ok)
                self.assertEqual(label, "test/dev/local/staging/development")

    def test_test_is_literal_match(self):
        # "test" は literal 辞書にもあるので literal label が優先される
        ok, label = looks_placeholder("test")
        self.assertTrue(ok)
        self.assertEqual(label, "test")


class TestNonPlaceholderValues(unittest.TestCase):
    """実値らしい文字列は False を返す (False positive 抑制)。"""

    def test_real_jwt_like(self):
        ok, label = looks_placeholder("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_real_url(self):
        ok, label = looks_placeholder("postgresql://user:pass@db.example.com:5432/app")
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_random_secret(self):
        ok, label = looks_placeholder("9f3a8b2c1d4e5f6a7b8c9d0e1f2a3b4c")
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_aws_access_key(self):
        ok, label = looks_placeholder("AKIAIOSFODNN7EXAMPLE")
        self.assertFalse(ok)
        self.assertIsNone(label)


class TestEnvPrefixedRealCredentials(unittest.TestCase):
    """0.31.0 (内部バックログ): ``test_`` / ``dev_`` / ``staging_`` / ``local_``
    **接頭の実トークン**を placeholder と誤標識しない。

    ``<placeholder>`` は ``<set>`` を置き換えるため、誤標識するとモデルには
    「この鍵はまだ実値が入っていない (rotate / set 不要)」と伝わる。``.env`` に
    環境名接頭のトークンを置く構成は一般的で、0.30.0 までは軒並み誤標識して
    いた。verdict (deny) は変わらないので、これは出力品質 (思想 2) の修正。
    """

    def test_env_prefixed_tokens_are_not_placeholders(self):
        for value in (
            "test_51H8xKqL9mNpQrStUvWxYz",   # Stripe テストキー形
            "dev_a8f3c2e1b9d7",
            "staging_9f8e7d6c5b4a3210",
            "local_dbpassword_x9f2",
            "testpassword123",
            "devops-service-token",
            "STAGING_API_KEY_9f8e7d",
        ):
            with self.subTest(value=value):
                ok, label = looks_placeholder(value)
                self.assertFalse(ok, msg=f"{value!r} は実値として扱う")
                self.assertIsNone(label)

    def test_control_real_secret_still_not_placeholder(self):
        ok, label = looks_placeholder("sk_live_" + "x" * 24)  # 連結形 (push protection 回避、内部バックログ)
        self.assertFalse(ok)
        self.assertIsNone(label)


class TestAngleBracketNarrowing(unittest.TestCase):
    """0.31.0: ``^<.*>$`` が XML/HTML の実値まで拾っていたのを絞った。

    山括弧 placeholder の慣習は「名前らしい短いトークン」なので、``:`` ``=``
    ``"`` ``/`` を含む形と長すぎる形を除く。
    """

    def test_markup_values_are_not_placeholders(self):
        for value in (
            "<soap:Envelope>",
            "<root attr=\"x\">",
            "</ns:item>",
            "<" + "a" * 41 + ">",     # 41 文字 = 上限超過
        ):
            with self.subTest(value=value):
                ok, label = looks_placeholder(value)
                self.assertFalse(ok, msg=f"{value!r} は実値として扱う")
                self.assertIsNone(label)

    def test_name_like_angle_placeholders_still_match(self):
        for value in ("<...>", "<your-key>", "<YOUR TOKEN>", "<api_key>",
                      "<" + "a" * 40 + ">"):
            with self.subTest(value=value):
                ok, label = looks_placeholder(value)
                self.assertTrue(ok, msg=f"{value!r} は placeholder のまま")
                self.assertEqual(label, "<...>")


class TestQuoteStripping(unittest.TestCase):
    """前後のクォートは剥がしてから判定する。"""

    def test_double_quoted_literal(self):
        ok, label = looks_placeholder('"dummy"')
        self.assertTrue(ok)
        self.assertEqual(label, "dummy")

    def test_single_quoted_literal(self):
        ok, label = looks_placeholder("'changeme'")
        self.assertTrue(ok)
        self.assertEqual(label, "changeme")

    def test_quoted_pattern(self):
        ok, label = looks_placeholder('"your_token_here"')
        self.assertTrue(ok)
        self.assertEqual(label, "your_*_here")

    def test_unmatched_quotes_passthrough(self):
        # 開きと閉じが揃わなければ剥がさない (placeholder にもならない)
        ok, label = looks_placeholder('"dummy')
        self.assertFalse(ok)
        self.assertIsNone(label)


class TestEdgeCases(unittest.TestCase):
    def test_empty_string(self):
        ok, label = looks_placeholder("")
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_whitespace_only(self):
        ok, label = looks_placeholder("   ")
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_quotes_with_whitespace_inside(self):
        ok, label = looks_placeholder('"   "')
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_non_string(self):
        ok, label = looks_placeholder(None)  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertIsNone(label)

    def test_leading_trailing_spaces(self):
        ok, label = looks_placeholder("  dummy  ")
        self.assertTrue(ok)
        self.assertEqual(label, "dummy")


if __name__ == "__main__":
    unittest.main()
