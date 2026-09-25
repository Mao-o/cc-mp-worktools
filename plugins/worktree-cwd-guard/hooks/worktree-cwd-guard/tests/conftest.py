"""pytest 実行時の sys.path 整備 (unittest では _testutil.py が同じ挿入を行う)。"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
