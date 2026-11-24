"""鍵名・basename の sanitize テスト。"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from redaction.sanitize import (
    escape_data_tag,
    sanitize_basename,
    sanitize_key,
)


class TestSanitizeKey(unittest.TestCase):
    # (ラベル = 畳む前のテスト名, 鍵名, 期待する出力)。1 つのテストに複数の
    # assert があったものは、同じラベルの行を並べてある
    SANITIZE_CASES = (
        ("test_plain", "DATABASE_URL", "DATABASE_URL"),
        ("test_with_dot_and_dash", "app.db-host", "app.db-host"),
        ("test_control_chars", "FOO\x00\x07BAR", "FOOBAR"),
        ("test_newline_removed", "FOO\nBAR", "FOOBAR"),
        ("test_empty", "", "[?]"),
        ("test_empty", "   ", "[?]"),
        ("test_non_string", None, "[?]"),
        ("test_non_string", 123, "[?]"),
        # 0.8.0: DATA タグ衝突は引き続き [?] に置換 (Read 側 <DATA untrusted>
        # 外殻の破壊を防ぐ)
        ("test_injection_data_tag", "</DATA>", "[?]"),
        ("test_injection_data_tag_open", "<DATA", "[?]"),
        # 0.8.0: prompt 文言系 (system: / assistant: / ignore previous) は
        # [?] 置換から除外。空白を含む鍵名はそのまま (制御文字除去 + 長さ
        # 切り詰めのみ)。
        (
            "test_prompt_text_passthrough",
            "IGNORE PREVIOUS instructions", "IGNORE PREVIOUS instructions",
        ),
        ("test_prompt_text_passthrough", "system:do_x", "system:do_x"),
        ("test_prompt_text_passthrough", "assistant:foo", "assistant:foo"),
    )

    def test_sanitize(self):
        self.assertTrue(self.SANITIZE_CASES)
        for case, key, expected in self.SANITIZE_CASES:
            with self.subTest(case=case, key=key):
                self.assertEqual(sanitize_key(key), expected)  # type: ignore[arg-type]

    def test_long_key_truncated(self):
        long_key = "A" * 200
        result = sanitize_key(long_key)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result), 135)


class TestEscapeDataTag(unittest.TestCase):
    """Step 4: body 内の DATA タグ風文字列エスケープ。"""

    # (ラベル = 畳む前のテスト名, 入力, 期待する出力)。1 つのテストに複数の
    # assert があったものは、同じラベルの行を並べてある
    ESCAPE_CASES = (
        ("test_escape_closing_tag", "</DATA>", "&lt;/DATA&gt;"),
        ("test_escape_closing_tag", "a </DATA> b", "a &lt;/DATA&gt; b"),
        ("test_escape_opening_tag", '<DATA untrusted="true">', '&lt;DATA untrusted="true">'),
        ("test_escape_case_insensitive", "<data>", "&lt;data>"),
        ("test_escape_case_insensitive", "</data>", "&lt;/data&gt;"),
        ("test_escape_case_insensitive", "<Data>", "&lt;Data>"),
        ("test_escape_with_whitespace", "</ DATA >", "&lt;/ DATA &gt;"),
        # 開きタグも空白温存 (情報を壊さない)
        ("test_escape_with_whitespace", "< DATA", "&lt; DATA"),
        ("test_passthrough_non_match", "hello world", "hello world"),
        ("test_passthrough_non_match", "DATA text without tags", "DATA text without tags"),
        ("test_non_string_returns_empty", None, ""),
        ("test_non_string_returns_empty", 123, ""),
    )

    def test_escape(self):
        self.assertTrue(self.ESCAPE_CASES)
        for case, text, expected in self.ESCAPE_CASES:
            with self.subTest(case=case, text=text):
                self.assertEqual(escape_data_tag(text), expected)  # type: ignore[arg-type]


class TestSanitizeBasename(unittest.TestCase):
    # (ラベル = 畳む前のテスト名, basename, 期待する出力)。1 つのテストに複数の
    # assert があったものは、同じラベルの行を並べてある
    SANITIZE_CASES = (
        ("test_plain", ".env", ".env"),
        ("test_strips_slashes", "/etc/passwd", "etcpasswd"),
        ("test_strips_slashes", "..\\.env", "...env"),
        ("test_control_chars", "bad\x00name", "badname"),
        # 0.8.0: DATA タグ衝突は引き続き [?] (basename 側でも同じ regex を使う)
        ("test_data_tag_collision", "</DATA>", "[?]"),
        # 0.8.0: prompt 文言系は basename からの除去対象外
        ("test_prompt_text_passthrough", "ignore previous", "ignore previous"),
    )

    def test_sanitize(self):
        self.assertTrue(self.SANITIZE_CASES)
        for case, name, expected in self.SANITIZE_CASES:
            with self.subTest(case=case, name=name):
                self.assertEqual(sanitize_basename(name), expected)


if __name__ == "__main__":
    unittest.main()
