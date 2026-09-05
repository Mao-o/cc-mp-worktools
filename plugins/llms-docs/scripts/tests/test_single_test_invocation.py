"""内部バックログ (DX): テストを 1 件だけ指定して実行できる回帰テスト。

`scripts/tests/` を sys.path に通さずに `import _loader` (トップレベル、副作用
専用) を書いていたため、`python3 -m unittest discover scripts/tests`
(ディレクトリ探索。探索先自体を検索パスに入れる) は通るが、
`python3 -m unittest scripts.tests.test_x.Class.method` や
`pytest scripts/tests/test_x.py::Class::method` (モジュールパス指定。
`scripts/tests/` 自体は検索パスに入らない) は
`ModuleNotFoundError: No module named '_loader'` になっていた
(詳細は `scripts/tests/__init__.py` の docstring)。

サブプロセスで実際に単体指定を実行し、修正が退行しないことを確認する。
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

import _loader  # noqa: F401

# scripts/tests/test_single_test_invocation.py -> scripts/tests -> scripts -> <plugin root>
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


class TestSingleTestInvocation(unittest.TestCase):
    def test_unittest_single_method(self):
        res = subprocess.run(
            [sys.executable, "-m", "unittest",
             "scripts.tests.test_common.ParseLlmsIndexTest.test_colon_description_form",
             "-v"],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ModuleNotFoundError", res.stderr)

    def test_unittest_single_class(self):
        res = subprocess.run(
            [sys.executable, "-m", "unittest",
             "scripts.tests.test_common.ParseLlmsIndexTest", "-v"],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ModuleNotFoundError", res.stderr)

    def test_pytest_single_node_id_if_available(self):
        """pytest が入っている環境でのみ確認する (標準ライブラリのみの方針上、
        pytest 自体は本 plugin の実行要件ではないため未インストールは skip)。"""
        # PATH 上の pytest 実行ファイルではなく、テストを走らせている interpreter
        # (sys.executable) が pytest を import できるかで判定する。clean な venv が
        # グローバルの pytest を PATH から継承していると which は成功するが
        # `-m pytest` は No module named pytest で落ちる (マージ前レビューの指摘)
        if importlib.util.find_spec("pytest") is None:
            self.skipTest("pytest not importable from this interpreter")
        res = subprocess.run(
            [sys.executable, "-m", "pytest",
             "scripts/tests/test_common.py::ParseLlmsIndexTest::test_colon_description_form",
             "-q"],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
