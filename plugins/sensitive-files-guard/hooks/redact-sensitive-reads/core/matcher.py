"""matcher の互換 re-export。実装は ``_shared.matcher`` にある。

既存テストや呼出が ``from core.matcher import is_sensitive`` を使い続けられる
ように薄い shim として維持する。
"""
from __future__ import annotations

from _shared.matcher import (  # noqa: F401
    _last_match_verdict,
    is_sensitive,
)
