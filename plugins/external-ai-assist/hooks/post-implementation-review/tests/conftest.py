"""pytest 実行時の sys.path 整備 (unittest discover 経由でも invariant として動く)。

unittest では ``_testutil.py`` が同じ挿入を行う。このファイルは pytest 実行時
(`pytest hooks/post-implementation-review/tests/`) のための重複セーフティ。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent
# tests/ 自身も通す: `__init__.py` があるため pytest は `tests.test_x` として import し、
# unittest discover と違って tests/ が sys.path に載らない (`import _testutil` が解決不能になる)。
for path in (_PKG_DIR, _TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
