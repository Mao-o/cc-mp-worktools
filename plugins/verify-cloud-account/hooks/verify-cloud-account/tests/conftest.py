"""pytest 実行時の sys.path 整備。

unittest 経由では _testutil.py が同じ挿入を行う。このファイルは pytest 実行時
(`pytest hooks/verify-cloud-account/tests/`) のための重複セーフティ。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
for p in (_PKG_DIR, _HOOKS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
