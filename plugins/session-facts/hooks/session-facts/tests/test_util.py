"""core/util.py の汎用ヘルパーのテスト。

aggregate_paths() は既に専用の test_path_aggregation.py を持つため、ここでは
それ以外の純関数 (internal backlog で追加された truncate_text() から開始) を
対象にする。
"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from core.util import truncate_text


class TruncateTextTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(truncate_text("short", 120), "short")

    def test_exact_length_unchanged(self):
        text = "x" * 120
        self.assertEqual(truncate_text(text, 120), text)

    def test_long_text_is_cut_with_ellipsis(self):
        text = "x" * 354  # matches the reported everything-claude-code case
        result = truncate_text(text, 120)
        self.assertEqual(len(result), 120)
        self.assertTrue(result.endswith("…"))
        self.assertEqual(result[:-1], "x" * 119)

    def test_does_not_strip_markdown_looking_characters(self):
        # Unlike truncate_purpose(), a literal shell command containing **
        # or _ must survive untouched -- these are not markdown emphasis.
        command = "echo **not markdown** && run_this_thing --flag=_value_"
        result = truncate_text(command, 120)
        self.assertEqual(result, command)  # under 120 chars: untouched

    def test_zero_max_chars_returns_empty_string(self):
        # internal backlog P3-6: the contract is "never exceed max_chars"
        # for any input. truncate_text("anything", 0) used to return "…"
        # (length 1), which is itself a contract violation for a 0 budget.
        self.assertEqual(truncate_text("anything", 0), "")

    def test_negative_max_chars_returns_empty_string(self):
        self.assertEqual(truncate_text("anything", -5), "")


if __name__ == "__main__":
    unittest.main()
