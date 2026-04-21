"""テスト共通のパス設定。verify-cloud-account/ と hooks/ を sys.path に通す。"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))
