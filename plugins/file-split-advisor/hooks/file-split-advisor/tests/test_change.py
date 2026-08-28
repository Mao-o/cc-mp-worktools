"""change.py: tool_input からの成長方向判定のテスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

import change


class TestClassifyGrowth(unittest.TestCase):
    def _edit(self, old: str, new: str, **extra) -> str:
        payload = {"file_path": "/repo/foo.py", "old_string": old, "new_string": new}
        payload.update(extra)
        return change.classify_growth("Edit", payload)

    def test_same_line_count_is_not_grew(self):
        self.assertEqual(self._edit("teh value", "the value"), change.NOT_GREW)

    def test_multiline_replacement_with_equal_lines_is_not_grew(self):
        self.assertEqual(self._edit("a\nb\nc", "x\ny\nz"), change.NOT_GREW)

    def test_fewer_lines_is_not_grew(self):
        self.assertEqual(self._edit("a\nb\nc", "abc"), change.NOT_GREW)

    def test_more_lines_is_grew(self):
        self.assertEqual(self._edit("abc", "a\nb\nc"), change.GREW)

    def test_replace_all_does_not_change_the_sign(self):
        # 1 箇所あたりの差分が同符号で N 箇所に適用されるため、符号は変わらない。
        self.assertEqual(self._edit("abc", "a\nb", replace_all=True), change.GREW)
        self.assertEqual(self._edit("a\nb", "abc", replace_all=True), change.NOT_GREW)

    def test_empty_strings_are_not_grew(self):
        self.assertEqual(self._edit("", ""), change.NOT_GREW)

    def test_insertion_into_empty_old_string_is_grew(self):
        self.assertEqual(self._edit("", "line\n"), change.GREW)


class TestEndOfFileCorrection(unittest.TestCase):
    """末尾の置換では、改行数の差と ``splitlines()`` の行数の差がずれる。"""

    def _classify(self, text: str, old: str, new: str) -> str:
        return change.classify_growth(
            "Edit", {"old_string": old, "new_string": new}, text
        )

    def _actual_delta(self, text: str, old: str, new: str) -> int:
        before = text[: len(text) - len(new)] + old
        return len(text.splitlines()) - len(before.splitlines())

    def test_append_line_without_trailing_newline_is_grew(self):
        text, old, new = "line0\nfoo\nbar", "foo\n", "foo\nbar"
        self.assertEqual(self._actual_delta(text, old, new), 1)
        self.assertEqual(self._classify(text, old, new), change.GREW)

    def test_adding_only_a_trailing_newline_is_not_grew(self):
        text, old, new = "line0\nfoo\n", "foo", "foo\n"
        self.assertEqual(self._actual_delta(text, old, new), 0)
        self.assertEqual(self._classify(text, old, new), change.NOT_GREW)

    def test_removing_the_trailing_newline_is_not_grew(self):
        text, old, new = "line0\nfoo", "foo\n", "foo"
        self.assertEqual(self._actual_delta(text, old, new), 0)
        self.assertEqual(self._classify(text, old, new), change.NOT_GREW)

    def test_mid_file_edit_is_unaffected_by_the_correction(self):
        text = "a\nb\nc\ntail"
        self.assertEqual(self._classify(text, "b", "b\nb2"), change.GREW)
        self.assertEqual(self._classify(text, "b\nc", "b"), change.NOT_GREW)

    def test_correction_is_skipped_without_text(self):
        # text 無しでも従来どおり改行数の差で判定する (近似)。
        self.assertEqual(
            change.classify_growth("Edit", {"old_string": "a", "new_string": "a\nb"}),
            change.GREW,
        )

    def test_empty_new_string_deletion_is_not_grew(self):
        self.assertEqual(self._classify("line0\nfoo\n", "bar\n", ""), change.NOT_GREW)

    def test_correction_is_skipped_when_new_string_also_occurs_elsewhere(self):
        """末尾に同じ文字列があっても、置換箇所が末尾とは限らない。

        ``"X\\nsomething\\nfoo\\n"`` の先頭 ``X`` を ``"foo\\n"`` に置換すると
        3 行 → 4 行に増えるが、結果の末尾にも元からあった ``"foo\\n"`` が並ぶ。
        ``endswith`` だけで補正すると増加分が打ち消されて NOT_GREW になる。
        """
        text = "foo\n\nsomething\nfoo\n"
        self.assertTrue(text.endswith("foo\n"))  # 誤補正の前提条件は成立している
        before = "X\nsomething\nfoo\n"
        self.assertEqual(len(text.splitlines()) - len(before.splitlines()), 1)
        self.assertEqual(self._classify(text, "X", "foo\n"), change.GREW)

    def test_correction_is_skipped_for_overlapping_tail_occurrences(self):
        # "aaa" に対する "aa" は非重複カウントでは 1 件だが出現位置は 2 箇所。
        # find/rfind が一致しないので補正しない。
        self.assertEqual(self._classify("aaa", "b", "aa"), change.NOT_GREW)


class TestUnknownCases(unittest.TestCase):
    def test_write_is_unknown(self):
        result = change.classify_growth("Write", {"file_path": "/repo/foo.py", "content": "x"})
        self.assertEqual(result, change.UNKNOWN)

    def test_edit_without_strings_is_unknown(self):
        result = change.classify_growth("Edit", {"file_path": "/repo/foo.py"})
        self.assertEqual(result, change.UNKNOWN)

    def test_edit_with_non_string_fields_is_unknown(self):
        # envelope の形が変わったときに通知が黙って全滅しないよう UNKNOWN に倒す。
        for old, new in [(None, "a"), ("a", None), (1, 2), (["a"], "b")]:
            with self.subTest(old=old, new=new):
                result = change.classify_growth(
                    "Edit", {"file_path": "/repo/foo.py", "old_string": old, "new_string": new}
                )
                self.assertEqual(result, change.UNKNOWN)

    def test_unrelated_tool_name_is_unknown(self):
        result = change.classify_growth("NotebookEdit", {"old_string": "a", "new_string": "a\nb"})
        self.assertEqual(result, change.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
