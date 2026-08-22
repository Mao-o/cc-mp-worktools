"""REVIEW_CLEAN sentinel 判定 (フェンス / 装飾 / 前置き 1 文 / 指摘混在)。"""
import unittest

import _testutil
from _testutil import FENCED_CLEAN, FENCED_CLEAN_WITH_PREAMBLE

from _common import sentinel
from _common.sentinel import is_clean_review, is_no_findings_statement


class TestBareSentinel(unittest.TestCase):
    def test_plain_and_decorated_forms(self):
        for text in (
            "REVIEW_CLEAN",
            "  `REVIEW_CLEAN`  ",
            "**REVIEW_CLEAN**",
            "# REVIEW_CLEAN",
            "review_clean",
            "REVIEW_CLEAN.",
            "> REVIEW_CLEAN",
            "(REVIEW_CLEAN)",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_clean_review(text))

    def test_empty_output_is_clean(self):
        self.assertTrue(is_clean_review(""))
        self.assertTrue(is_clean_review("\n\n  \n"))

    def test_fence_or_rule_only_output_is_not_clean(self):
        """sentinel を含まない非空出力 (フェンス / 罫線だけ) は指摘扱いに倒す。"""
        for text in ("```\n```", "---", "```text\n```\n", "***\n---"):
            with self.subTest(text=text):
                self.assertFalse(is_clean_review(text))

    def test_duplicated_sentinel_is_clean(self):
        self.assertTrue(is_clean_review("REVIEW_CLEAN\nREVIEW_CLEAN"))


class TestFencesAndDecorations(unittest.TestCase):
    """zh5.1: prompts が提示していたフェンス付き形式を指摘扱いしない。"""

    def test_fenced_sentinel(self):
        for text in (
            FENCED_CLEAN,
            "```text\nREVIEW_CLEAN\n```",
            "```markdown\nREVIEW_CLEAN\n```\n",
            "~~~\nREVIEW_CLEAN\n~~~",
            "```REVIEW_CLEAN```",
            "```REVIEW_CLEAN\n```",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_clean_review(text))

    def test_horizontal_rules_are_ignored(self):
        for text in ("---\nREVIEW_CLEAN\n---", "REVIEW_CLEAN\n\n***", "===\nREVIEW_CLEAN", "- - -\nREVIEW_CLEAN"):
            with self.subTest(text=text):
                self.assertTrue(is_clean_review(text))

    def test_fence_with_content_inside_is_not_clean(self):
        self.assertFalse(is_clean_review("```\n1. **直接影響** — 壊れる\n```"))
        self.assertFalse(is_clean_review("```\nREVIEW_CLEAN\n1. **直接影響** — 壊れる\n```"))


class TestNoFindingsPreamble(unittest.TestCase):
    """「指摘なし」を述べる短い 1 文 + sentinel は clean (実出力 fixture を含む)。"""

    def test_real_world_fixture(self):
        self.assertTrue(is_clean_review(FENCED_CLEAN_WITH_PREAMBLE))

    def test_accepted_preambles(self):
        for preamble in (
            "critical 指摘はない",
            "critical な指摘はありません。",
            "critical な指摘は 1 件もありません",
            "指摘事項なし",
            "特に問題ありません",
            "問題は見当たりません。",
            "修正すべき点はないと判断します",
            "**Critical な指摘はなし**",
            "(特にありません)",
            "No critical issues found.",
            "No critical issues were found.",
            "There are no blocking concerns.",
            "There are no critical issues in the plan.",
            "I found no issues.",
            "Nothing critical.",
            "Looks good to me",
            "LGTM",
            "このプランに critical な指摘はありません",
            "Critical な問題は確認できませんでした",
            "「問題なし」",
        ):
            with self.subTest(preamble=preamble):
                self.assertTrue(is_clean_review(f"{preamble}\n\n```\nREVIEW_CLEAN\n```"))
                self.assertTrue(is_clean_review(f"REVIEW_CLEAN\n{preamble}"))

    def test_rejected_single_lines(self):
        """否定語を含んでいても、指摘本文の兆候がある行は前置きとして認めない。

        後半は L2 レビューで見つかった「本文の後ろに否定文」型 (search + 末尾アンカーでは
        素通りしていた)。fullmatch + 文区切り / 例外表現の拒否で弾く。
        """
        for line in (
            "critical 指摘はないが、`retry()` が無限ループする",
            "No critical issues, but the retry loop never exits",
            "問題ない箇所もあるが X は壊れる",
            "問題なし: src/app.py:12 のガードは不要",
            "- 問題なし",
            "1. **直接影響** — 問題なし",
            "## 指摘なし",
            "No critical issues found in services/auth.py",
            "critical な指摘はない。ただし移行手順が未定義",
            "critical な指摘はない " + "x" * sentinel.MAX_PREAMBLE_CHARS,
            "以上です",
            "レビューを実施しました",
            # 本文 → 否定文 (鏡像)
            "認可チェックが抜けている点以外は問題なし",
            "retry() の無限ループ以外に問題はない",
            "トークン失効の考慮漏れを除き指摘なし",
            "null チェック漏れあり。他に問題なし",
            "SQL インジェクションの可能性あり。その他の問題は見当たりません",
            "Missing null check in the handler; otherwise no issues",
            "Retry loop never terminates. No concerns",
            "Apart from the missing auth guard, no critical issues",
            "Auth guard missing - no blocking issues",
            "Exception handling is missing. No issues",
            "Minor nit: unused import. LGTM",
            "認可チェック漏れ → LGTM",
            "Race in claim_pending will double-review, otherwise looks good",
            "migration step is undefined; all clean",
            "軽微な懸念はあるが critical な指摘はない",
            "critical な指摘はない (軽微な点は後述",
        ):
            with self.subTest(line=line):
                self.assertFalse(is_no_findings_statement(line))
                self.assertFalse(is_clean_review(f"{line}\nREVIEW_CLEAN"))

    def test_two_extra_lines_are_not_clean(self):
        self.assertFalse(is_clean_review("critical 指摘はない\n以上です\nREVIEW_CLEAN"))
        self.assertFalse(is_clean_review("critical 指摘はない\n特に問題ありません\nREVIEW_CLEAN"))


class TestFindingsAreNeverClean(unittest.TestCase):
    def test_sentinel_followed_by_findings(self):
        self.assertFalse(is_clean_review("REVIEW_CLEAN\n\n1. **直接影響** — 実は壊れる"))
        self.assertFalse(is_clean_review("```\nREVIEW_CLEAN\n```\n\n1. **直接影響** — 実は壊れる"))

    def test_findings_only(self):
        self.assertFalse(is_clean_review("1. **直接影響** — 壊れる"))
        self.assertFalse(is_clean_review("critical 指摘はない"))  # sentinel 無し

    def test_sentinel_embedded_in_sentence_is_not_clean(self):
        self.assertFalse(is_clean_review("Result: REVIEW_CLEAN"))
        self.assertFalse(is_clean_review("REVIEW_CLEAN は返せません。X が壊れます"))


if __name__ == "__main__":
    unittest.main()
