"""配布ファイルが「外部の読者が解決できない参照」を持たないことを固定する。

内部バックログ: 実装者向けガイド (service の契約 / verify() の実装規則 /
PATTERNS の先頭アンカ規則 / hook timeout の考え方 / リリース手順) が、追跡
されないローカルファイルにしか無かった。README と監査記録がそのファイルを
「本体はこちら」と案内していたため、**plugin を clone した人にも worktree で
作業している自分自身にも辿れない**参照になっていた。

同じ形の欠陥は「内部トラッカーのチケット ID」でも起きる。どちらも
「その場では読めるが、配布物として見ると宛先が無い」参照なので、まとめて
機械検出する。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import _testutil  # noqa: F401

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DEVELOPMENT_DOC = PLUGIN_ROOT / "docs" / "DEVELOPMENT.md"

# 配布物として git に載り、外部の読者が読むファイル。
_SCANNED_SUFFIXES = (".md", ".py", ".json")
_SKIPPED_DIRS = {"__pycache__", ".pytest_cache"}

# gitignore 済みのローカル専用ガイド。**このファイル自身は検査対象から外す**
# (検出対象の文字列をここに書く必要があるため)。
_UNTRACKED_GUIDE = "CLAUDE" + ".local.md"

# 内部トラッカー (beads) の ID 形状。外部の読者は解決できない。
_TICKET_ID_RE = re.compile(r"bd_[0-9a-f]{8}-")


def _distributed_files() -> list[Path]:
    files = []
    for path in PLUGIN_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
            continue
        if _SKIPPED_DIRS & set(path.parts):
            continue
        if path == Path(__file__).resolve():
            continue
        files.append(path)
    return files


class TestDeveloperGuideIsDistributed(unittest.TestCase):
    def test_development_doc_exists(self):
        self.assertTrue(
            DEVELOPMENT_DOC.is_file(),
            "実装者ガイドが配布物として存在しない",
        )
        self.assertGreater(
            len(DEVELOPMENT_DOC.read_text(encoding="utf-8")), 2000,
            "実装者ガイドが実質空",
        )

    def test_readme_points_at_the_development_doc(self):
        text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/DEVELOPMENT.md", text)

    def test_audit_doc_points_at_the_development_doc(self):
        text = (PLUGIN_ROOT / "docs" / "wrapper-env-audit.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEVELOPMENT.md", text)


class TestNoUnresolvableReferences(unittest.TestCase):
    """配布ファイルが未同梱ファイル / 内部チケット ID を指していないこと。"""

    def test_scan_actually_sees_files(self):
        """空振り防止: 走査対象が実際に集まっていること。"""
        files = _distributed_files()
        self.assertGreater(len(files), 20, "配布ファイルの走査が空振りしている")
        self.assertIn(PLUGIN_ROOT / "README.md", files)

    def test_no_reference_to_the_untracked_local_guide(self):
        offenders = [
            str(p.relative_to(PLUGIN_ROOT))
            for p in _distributed_files()
            if _UNTRACKED_GUIDE in p.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertEqual(
            offenders,
            [],
            "gitignore 済みのローカルガイドを参照している配布ファイルがある。"
            " docs/DEVELOPMENT.md を指すこと",
        )

    def test_no_internal_tracker_ids(self):
        offenders = [
            str(p.relative_to(PLUGIN_ROOT))
            for p in _distributed_files()
            if _TICKET_ID_RE.search(p.read_text(encoding="utf-8", errors="replace"))
        ]
        self.assertEqual(
            offenders,
            [],
            "内部トラッカーのチケット ID を含む配布ファイルがある。"
            "「内部バックログ」等の汎用表現にすること",
        )


if __name__ == "__main__":
    unittest.main()
