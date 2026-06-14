"""_shared パッケージが両 hook から import 可能であることの契約テスト。

Step 1 (hooks/_shared/) の単一 source 化が崩れると、Read 側と Stop 側で matcher
ロジックが剥離する。このテストはそれを検知する。
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401


class TestSharedImport(unittest.TestCase):
    def test_shared_matcher_importable_from_redact(self):
        from _shared.matcher import is_sensitive
        self.assertTrue(callable(is_sensitive))

    def test_shared_patterns_importable_from_redact(self):
        from _shared.patterns import (
            _parse_patterns_text,
            _resolve_local_patterns_path,
            load_patterns,
        )
        self.assertTrue(callable(_parse_patterns_text))
        self.assertTrue(callable(_resolve_local_patterns_path))
        self.assertTrue(callable(load_patterns))

    def test_core_patterns_delegates_to_shared(self):
        from _shared.patterns import _parse_patterns_text as shared_parse
        from core.patterns import _parse_patterns_text as core_parse
        self.assertIs(shared_parse, core_parse)

    def test_shared_importable_from_checker(self):
        """Stop 側からも _shared が import できる (別プロセスを模倣して確認)。"""
        checker_dir = (
            Path(__file__).resolve().parent.parent.parent / "check-sensitive-files"
        )
        if str(checker_dir) not in sys.path:
            sys.path.insert(0, str(checker_dir))
        import checker
        importlib.reload(checker)
        # checker が _shared 経由で is_sensitive / _parse_patterns_text を参照する
        from _shared.matcher import is_sensitive
        from _shared.patterns import _parse_patterns_text
        self.assertIs(checker._parse_patterns_text, _parse_patterns_text)


if __name__ == "__main__":
    unittest.main()
