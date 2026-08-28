"""恒久除外レシピの影響範囲を開示していること (0.21.0)。

除外行は basename 単位で評価されるため、承認した 1 ファイルではなく
プロジェクト内の同名ファイル全部の保護が落ちる。しかも効果は Stop の報告に
留まらず Read / Bash / Edit / Write の保護そのものに及ぶ。

範囲を狭める作業とは別に、**黙った過剰付与を informed consent に変える**ため
案内文がその 2 点を述べていることを固定する。
"""
from __future__ import annotations

import unittest

from _shared.patterns import EXCLUDE_SCOPE_WARNING
from core.messages import _exclude_hint


class TestExcludeScopeWarningText(unittest.TestCase):
    def test_states_basename_granularity(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("basename 単位", text)
        self.assertIn("すべて", text)

    def test_states_protection_not_just_reporting(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        # 「報告が止まる」だけでなく「保護が外れる」ことを述べていること
        self.assertIn("保護そのものが外れます", text)
        for tool in ("Read", "Bash", "Edit", "Write"):
            self.assertIn(tool, text)

    def test_says_single_file_cannot_be_scoped(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("1 ファイルだけを外すことはできません", text)


class TestExcludeHintCarriesWarning(unittest.TestCase):
    """Read / Bash の deny reason に載る除外案内が開示を含むこと。"""

    def test_named_basename_hint_includes_scope(self):
        hint = _exclude_hint(".env")
        self.assertIn("basename 単位", hint)
        self.assertIn("保護そのものが外れます", hint)
        # 対象の basename が範囲説明に埋め込まれていること
        self.assertIn("`.env` という名前のファイル", hint)

    def test_generic_hint_includes_scope(self):
        hint = _exclude_hint("")
        self.assertIn("basename 単位", hint)
        self.assertIn("同名ファイル", hint)

    def test_hint_still_requires_user_approval(self):
        """既存の「承認なしに追加しない」規律が消えていないこと。"""
        self.assertIn("承認なしに自分で追加しないこと", _exclude_hint(".env"))

    def test_hint_does_not_claim_report_only(self):
        """「報告されなくなります」だけで済ませる旧文面に戻っていないこと。"""
        hint = _exclude_hint(".env")
        self.assertNotIn("で報告されなくなります", hint)


if __name__ == "__main__":
    unittest.main()
