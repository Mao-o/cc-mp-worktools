"""``redaction/placeholders.py`` (0.9.0 新設、E2) の単体テスト。

literal 完全一致 / regex 一致 / 一致なし / クォート剥がし / 戻り値の label を網羅。

値 1 つ → 結果 1 つを見るテストは、クラスごとの表 (一致する側は ``(ラベル, 値, 期待する label)``、
一致しない側は ``(ラベル, 値)``)
と subTest に畳んである。ラベルは畳む前のテスト名で、失敗時は subTest の case で
どの行が落ちたか分かる。
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

    # (ラベル, 値, 期待する label)
    MATCH_CASES = (
        ("test_dummy", "dummy", "dummy"),
        # label は lower-case にそろう
        ("test_uppercase_dummy", "DUMMY", "dummy"),
        ("test_changeme", "changeme", "changeme"),
        ("test_change_me_underscore", "change_me", "change_me"),
        ("test_your_secret", "your_secret", "your_secret"),
        ("test_lorem", "lorem", "lorem"),
    )

    def test_literal_values_match(self):
        self.assertTrue(self.MATCH_CASES)
        for case, value, expected in self.MATCH_CASES:
            with self.subTest(case=case, value=value):
                ok, label = looks_placeholder(value)
                self.assertTrue(ok)
                self.assertEqual(label, expected)

    def test_all_literals_recognized(self):
        for literal in PLACEHOLDER_LITERALS:
            with self.subTest(literal=literal):
                ok, label = looks_placeholder(literal)
                self.assertTrue(ok, msg=f"{literal!r} should match")
                self.assertEqual(label, literal)


class TestPatternMatches(unittest.TestCase):
    """PLACEHOLDER_PATTERNS の各 regex がラベルを返す。"""

    # (ラベル, 値, 期待する label)
    MATCH_CASES = (
        ("test_your_anything_here", "your_jwt_secret_here", "your_*_here"),
        ("test_your_dash_here", "your-key-here", "your_*_here"),
        ("test_angle_brackets", "<your-key>", "<...>"),
        ("test_three_stars", "***", "***"),
        ("test_many_stars", "**********", "***"),
        # "xxx" は literal 辞書にも入っているので literal label 優先
        ("test_xxx_lower", "xxx", "xxx"),
        # literal 一致しないので regex のラベル
        ("test_xxxxx", "xxxxx", "xxx"),
        # "test" は literal 辞書にもあるので literal label が優先される
        ("test_test_is_literal_match", "test", "test"),
    )

    def test_pattern_values_match(self):
        self.assertTrue(self.MATCH_CASES)
        for case, value, expected in self.MATCH_CASES:
            with self.subTest(case=case, value=value):
                ok, label = looks_placeholder(value)
                self.assertTrue(ok)
                self.assertEqual(label, expected)

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


class TestNonPlaceholderValues(unittest.TestCase):
    """実値らしい文字列は False を返す (False positive 抑制)。"""

    # (ラベル, 値)
    REAL_VALUE_CASES = (
        ("test_real_jwt_like", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
        ("test_real_url", "postgresql://user:pass@db.example.com:5432/app"),
        ("test_random_secret", "9f3a8b2c1d4e5f6a7b8c9d0e1f2a3b4c"),
        ("test_aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
    )

    def test_real_values_are_not_placeholders(self):
        self.assertTrue(self.REAL_VALUE_CASES)
        for case, value in self.REAL_VALUE_CASES:
            with self.subTest(case=case, value=value):
                ok, label = looks_placeholder(value)
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

    # (ラベル, 値, 期待する label)
    MATCH_CASES = (
        ("test_double_quoted_literal", '"dummy"', "dummy"),
        ("test_single_quoted_literal", "'changeme'", "changeme"),
        ("test_quoted_pattern", '"your_token_here"', "your_*_here"),
    )

    def test_quoted_values_match(self):
        self.assertTrue(self.MATCH_CASES)
        for case, value, expected in self.MATCH_CASES:
            with self.subTest(case=case, value=value):
                ok, label = looks_placeholder(value)
                self.assertTrue(ok)
                self.assertEqual(label, expected)

    def test_unmatched_quotes_passthrough(self):
        # 開きと閉じが揃わなければ剥がさない (placeholder にもならない)
        ok, label = looks_placeholder('"dummy')
        self.assertFalse(ok)
        self.assertIsNone(label)


class TestEdgeCases(unittest.TestCase):
    # (ラベル, 値)
    NO_MATCH_CASES = (
        ("test_empty_string", ""),
        ("test_whitespace_only", "   "),
        ("test_quotes_with_whitespace_inside", '"   "'),
        ("test_non_string", None),
    )

    def test_edge_values_are_not_placeholders(self):
        self.assertTrue(self.NO_MATCH_CASES)
        for case, value in self.NO_MATCH_CASES:
            with self.subTest(case=case, value=value):
                ok, label = looks_placeholder(value)  # type: ignore[arg-type]
                self.assertFalse(ok)
                self.assertIsNone(label)

    def test_leading_trailing_spaces(self):
        ok, label = looks_placeholder("  dummy  ")
        self.assertTrue(ok)
        self.assertEqual(label, "dummy")


if __name__ == "__main__":
    unittest.main()
