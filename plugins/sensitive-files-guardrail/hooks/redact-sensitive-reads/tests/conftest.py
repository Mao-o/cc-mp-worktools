"""pytest 実行時の sys.path 整備 (unittest discover 経由でも invariant として動く)。

unittest では各テストファイルが ``from _testutil import FIXTURES`` を先頭で呼ぶため
``_testutil.py`` 側でも同じ sys.path 挿入を行っている。このファイルは pytest 実行時
(`pytest hooks/redact-sensitive-reads/tests/`) に対応するための重複セーフティ。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
for p in (_PKG_DIR, _HOOKS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
