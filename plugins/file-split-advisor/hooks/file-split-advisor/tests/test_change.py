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
