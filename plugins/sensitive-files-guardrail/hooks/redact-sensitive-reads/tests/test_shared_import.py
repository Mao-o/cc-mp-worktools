"""_shared パッケージが両 hook から import 可能であることの契約テスト。

Step 1 (hooks/_shared/) の単一 source 化が崩れると、Read 側と Stop 側で matcher
ロジックが剥離する。このテストはそれを検知する。
"""
from __future__ import annotations

import importlib
# ``importlib.machinery`` は ``import importlib`` では読み込まれない
# (submodule)。unittest / pytest が先に import する副作用に依存していると、
# 実行形態を変えた瞬間に ``AttributeError`` になる (0.31.0 隔離内レビュー P3-7)。
import importlib.machinery
import sys
import unittest
from pathlib import Path

from _testutil import FIXTURES, checker_dir_on_path  # noqa: F401


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
        checker_dir_on_path()
        import checker
        importlib.reload(checker)
        # checker が _shared 経由で is_sensitive / _parse_patterns_text を参照する
        from _shared.matcher import is_sensitive
        from _shared.patterns import _parse_patterns_text
        self.assertIs(checker._parse_patterns_text, _parse_patterns_text)


class TestTestsPackageResolution(unittest.TestCase):
    """0.31.0 (内部バックログ): Stop 側 dir を通しても ``tests`` の解決先が
    入れ替わらないこと。

    両 hook はどちらも ``tests`` パッケージを持つ。Stop 側を ``sys.path`` の
    **先頭**に入れると、以降このプロセスで ``tests.*`` が Stop 側に解決され、
    ``test_logging.py`` の spawn 子プロセスが ``tests.test_logging`` を import
    できず ``ModuleNotFoundError`` で落ちる (実行順に依存するため flaky に
    見えていた)。``_testutil.checker_dir_on_path`` が末尾に足す契約を固定する。
    """

    def setUp(self):
        self.pkg_dir = Path(__file__).resolve().parent.parent  # redact-sensitive-reads

    def test_checker_dir_is_appended_after_own_hook_dir(self):
        checker_dir = checker_dir_on_path()
        self.assertGreater(
            sys.path.index(str(checker_dir)),
            sys.path.index(str(self.pkg_dir)),
            msg="Stop 側 dir が自 hook dir より前にある (tests の解決先が入れ替わる)",
        )

    def test_tests_package_resolves_to_this_hook(self):
        """``sys.modules`` のキャッシュを見ずに **path 解決だけ**で確認する。

        spawn 子プロセスは真っ新な interpreter で ``tests.test_logging`` を
        import するので、親の ``sys.modules`` ではなく ``sys.path`` 順が効く。
        """
        checker_dir_on_path()
        spec = importlib.machinery.PathFinder.find_spec("tests", sys.path)
        self.assertIsNotNone(spec, "tests パッケージが解決できない")
        self.assertEqual(
            Path(spec.origin).resolve().parent.parent,
            self.pkg_dir,
            msg=f"tests の解決先が別 hook 側になっている: {spec.origin}",
        )


if __name__ == "__main__":
    unittest.main()
