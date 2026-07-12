"""pytest 実行時の sys.path 整備 (unittest discover 経由でも invariant として動く)。

unittest では ``_testutil.py`` が同じ挿入を行う。このファイルは pytest 実行時
のための重複セーフティ。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
