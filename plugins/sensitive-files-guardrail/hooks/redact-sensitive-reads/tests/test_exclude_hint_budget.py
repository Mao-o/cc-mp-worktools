"""除外案内の **固定文言の byte 予算** を床として固定する (0.24.0)。

reason 全体は ``core.output.MAX_REASON_BYTES`` (3KB) で、``_join_with_exclude_hint``
は案内を必ず全文残す代わりに **可変長側 (minimal info) を削る**。したがって案内が
伸びるほど minimal info が押し出され、ある境界を越えると大きな ``.env`` の鍵一覧が
丸ごと消える (情報量の劣化。deny 自体は変わらない)。

実測 (0.24.0、32KB 超の ``.env`` を Edit する ``test_edit_handler`` の経路):
案内 1,205 byte のとき鍵一覧は残り、**+170 byte (案内 1,375 byte) で消える**。
0.24.0 の初版は root 直下の書き方まで警告文に書いて 1,427 byte になり、既存テスト
2 件が偶然これを検出した。次に文言を足すときに同じ事故を「案内の長さ」そのもので
検出できるよう、固定文言 (短い basename / relpath) の上限を 1,300 byte に置く
(可変部分 = 実際の basename / relpath の長さに 75 byte 以上の余地を残す)。
"""
from __future__ import annotations

import unittest

from core.messages import _exclude_hint

# 実測境界 1,375 byte から可変部分の余地を引いた固定文言の上限
_HINT_BUDGET = 1_300


class TestExcludeHintBudget(unittest.TestCase):
    def _size(self, *args, **kwargs) -> int:
        return len(_exclude_hint(*args, **kwargs).encode("utf-8"))

    def test_fixed_wording_stays_within_budget(self):
        cases = {
            "generic": ((""), {}),
            "basename": ((".env",), {}),
            "basename_literal": ((".env", True), {}),
            "relpath": ((".env", True), {"relpath": "sub/.env"}),
            "root_level": ((".env", True), {"relpath": ".env"}),
            "bash_glob": ((".env*",), {}),
        }
        for label, (args, kwargs) in cases.items():
            with self.subTest(label=label):
                args = args if isinstance(args, tuple) else (args,)
                self.assertLessEqual(
                    self._size(*args, **kwargs),
                    _HINT_BUDGET,
                    f"{label}: 案内が予算を超えた。伸ばした文言は docs へ移すこと",
                )

    def test_path_form_costs_little_over_basename_form(self):
        """path 形の併記 (relpath あり) が basename 形単独より大きく膨らまないこと。"""
        base = self._size(".env", True)
        with_path = self._size(".env", True, relpath="sub/.env")
        self.assertLessEqual(with_path - base, 120)


if __name__ == "__main__":
    unittest.main()
