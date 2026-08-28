"""Stop hook の block reason が除外レシピの影響範囲を開示していること (0.23.0)。"""
from __future__ import annotations

import unittest

from _shared.patterns import EXCLUDE_SCOPE_WARNING


class TestStopReasonDisclosure(unittest.TestCase):
    def test_warning_constant_is_shared(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="下記の各行と同じ名前のファイル")
        self.assertIn("basename 単位", text)
        self.assertIn("保護そのもの", text)
        self.assertIn("同名ディレクトリの配下", text)
        self.assertIn("別の include 行", text)


if __name__ == "__main__":
    unittest.main()
