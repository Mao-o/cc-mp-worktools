"""pytest 実行時の sys.path 整備 (unittest 経由では ``_testutil`` が同じ挿入を行う)。"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent  # hooks/session-facts
_HOOKS_DIR = _PKG_DIR.parent                        # hooks
for p in (_PKG_DIR, _HOOKS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
