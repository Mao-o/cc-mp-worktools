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
import shutil
import tempfile
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

# 配布されない手元専用ファイルの名前 (リポジトリの .gitignore が `**/CLAUDE.local.md`
# で無視する)。保守者の手元の plugin 直下にあると、その本文 (手元専用ファイル自身の
# 名前や内部トラッカーの ID を書く場所) が offender になり、clean clone の CI は通るのに
# 手元の test だけが落ちていた。名前で除外する — git ls-files は使わない (suite は
# `.git` の無い展開先でも走り、そこでは走査が空 = 常に通るテストになる)。
# .gitignore に無い名前 (AGENTS.local.md 等) は commit されうるので足さない。
_LOCAL_ONLY_NAMES = frozenset({_UNTRACKED_GUIDE})

# 内部トラッカー (beads) の ID 形状。外部の読者は解決できない。
_TICKET_ID_RE = re.compile(r"bd_[0-9a-f]{8}-")


def _distributed_files(root: Path = PLUGIN_ROOT) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
            continue
        if _SKIPPED_DIRS & set(path.parts):
            continue
        if path.name in _LOCAL_ONLY_NAMES:
            continue
        if path == Path(__file__).resolve():
            continue
        files.append(path)
    return files


def _offenders(root: Path, found) -> list[str]:
    """配布ファイルのうち、本文が `found(text)` に当たるものの root からの相対パス。"""
    return [
        str(p.relative_to(root))
        for p in _distributed_files(root)
        if found(p.read_text(encoding="utf-8", errors="replace"))
    ]


def _refers_to_untracked_guide(text: str) -> bool:
    return _UNTRACKED_GUIDE in text


def _has_ticket_id(text: str) -> bool:
    return bool(_TICKET_ID_RE.search(text))


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
        self.assertEqual(
            _offenders(PLUGIN_ROOT, _refers_to_untracked_guide),
            [],
            "gitignore 済みのローカルガイドを参照している配布ファイルがある。"
            " docs/DEVELOPMENT.md を指すこと",
        )

    def test_no_internal_tracker_ids(self):
        self.assertEqual(
            _offenders(PLUGIN_ROOT, _has_ticket_id),
            [],
            "内部トラッカーのチケット ID を含む配布ファイルがある。"
            "「内部バックログ」等の汎用表現にすること",
        )


class TestLocalOnlyFilesAreNotScanned(unittest.TestCase):
    """保守者の手元にだけある gitignore 済みのファイルは走査しない (配布されないため)。

    除外は名前だけで、配布ファイルに書かれた参照やチケット ID は今までどおり検出する。
    実際の plugin ではなく使い捨ての木で見る (手元の状態に結果を左右させない)。
    """

    # チケット ID の形をした文字列 (このファイル自体に ID の形を書かないよう分けて組む)。
    FAKE_TICKET_ID = "bd" + "_0123abcd" + "-x"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "docs").mkdir()
        (self.root / "README.md").write_text("# readme\n", encoding="utf-8")
        local_note = f"{_UNTRACKED_GUIDE} (手元のメモ)\n{self.FAKE_TICKET_ID}\n"
        for where in (self.root, self.root / "docs"):
            (where / _UNTRACKED_GUIDE).write_text(local_note, encoding="utf-8")

    def test_local_only_files_are_skipped(self):
        files = _distributed_files(self.root)
        self.assertIn(self.root / "README.md", files)
        self.assertNotIn(self.root / _UNTRACKED_GUIDE, files)
        self.assertNotIn(self.root / "docs" / _UNTRACKED_GUIDE, files)
        self.assertEqual(_offenders(self.root, _refers_to_untracked_guide), [])
        self.assertEqual(_offenders(self.root, _has_ticket_id), [])

    def test_distributed_files_are_still_checked(self):
        (self.root / "README.md").write_text(
            f"詳細は {_UNTRACKED_GUIDE} を参照 ({self.FAKE_TICKET_ID})\n", encoding="utf-8"
        )
        self.assertEqual(_offenders(self.root, _refers_to_untracked_guide), ["README.md"])
        self.assertEqual(_offenders(self.root, _has_ticket_id), ["README.md"])


if __name__ == "__main__":
    unittest.main()
