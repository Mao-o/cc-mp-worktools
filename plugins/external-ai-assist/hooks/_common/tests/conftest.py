"""pytest 実行時の sys.path 整備 (unittest discover 経由では `_testutil.py` が同じ挿入を行う)。"""
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_HOOKS_DIR = _TESTS_DIR.parent.parent
for path in (_HOOKS_DIR, _TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
