"""Stop hook の block reason が除外レシピの影響範囲を開示していること (0.23.0)。

0.24.0 で path 形 (1 ファイルだけ) と basename 形 (同名すべて) の 2 形に書き分けた。
"""
from __future__ import annotations

import unittest

from _shared.patterns import EXCLUDE_SCOPE_WARNING


class TestStopReasonDisclosure(unittest.TestCase):
    def test_warning_constant_is_shared(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="同じ名前のファイル")
        self.assertIn("path 形", text)
        self.assertIn("1 ファイルだけ", text)
        self.assertIn("basename 形", text)
        self.assertIn("同じ名前のファイルが**すべて**対象", text)
        self.assertIn("保護そのもの", text)
        self.assertIn("同名ディレクトリの配下", text)
        self.assertIn("別の include 行", text)
        # 0.23.0 の「1 ファイルだけの除外は不可」は path 形の導入で事実と矛盾する
        self.assertNotIn("除外は不可", text)


if __name__ == "__main__":
    unittest.main()
