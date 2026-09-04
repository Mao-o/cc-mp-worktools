"""内部バックログ (DX): テストを 1 件だけ指定して実行できる回帰テスト。

`tests/` を sys.path に通さずに `import _testutil` (トップレベル、副作用専用) を
書いていたため、`python3 -m unittest discover tests` (ディレクトリ探索。探索先
自体を検索パスに入れる) は通るが、`python3 -m unittest tests.test_x.Class.method`
や `pytest tests/test_x.py::Class::method` (モジュールパス指定。`tests/` 自体は
検索パスに入らない) は `ModuleNotFoundError: No module named '_testutil'` に
なっていた (詳細は `tests/__init__.py` の docstring)。

サブプロセスで実際に単体指定を実行し、修正が退行しないことを確認する。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import _testutil  # noqa: F401

_PKG_DIR = Path(__file__).resolve().parent.parent


class TestSingleTestInvocation(unittest.TestCase):
    def test_unittest_single_method(self):
        res = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.test_services.TestAws.test_match", "-v"],
            cwd=str(_PKG_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ModuleNotFoundError", res.stderr)

    def test_unittest_single_class(self):
        res = subprocess.run(
            [sys.executable, "-m", "unittest",
             "tests.test_dispatcher.TestRouting", "-v"],
            cwd=str(_PKG_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ModuleNotFoundError", res.stderr)

    def test_pytest_single_node_id_if_available(self):
        """pytest が入っている環境でのみ確認する (標準ライブラリのみの方針上、
        pytest 自体は本 plugin の実行要件ではないため未インストールは skip)。"""
        if shutil_which("pytest") is None:
            self.skipTest("pytest not installed")
        res = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_services.py::TestAws::test_match", "-q"],
            cwd=str(_PKG_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


def shutil_which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)


if __name__ == "__main__":
    unittest.main()
