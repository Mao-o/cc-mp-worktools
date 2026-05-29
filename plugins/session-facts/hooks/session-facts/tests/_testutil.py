"""sys.path 整備 (unittest discover 経由で import されるためのヘルパー)。

session-facts のモジュールは ``from core.x import ...`` のように pkg ルート
(``hooks/session-facts``) を基準に絶対 import する。テストからもこれを再現する
ため pkg ルートと hooks ルートを sys.path に挿入する。pytest 実行時は同じ挿入を
``conftest.py`` が行う。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent  # hooks/session-facts
_HOOKS_DIR = _PKG_DIR.parent                        # hooks
for p in (_PKG_DIR, _HOOKS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
