"""外部 AI backend の registry。

使い方は `registry.py` の docstring を参照。ここでは `from _common import backends` の
1 行で registry・結果型・各 backend module に届くよう再 export だけを行う。
"""
from __future__ import annotations

from . import base, codex, cursor, registry
from .base import (
    STATUS_FAILED,
    STATUS_LIMIT,
    STATUS_OK,
    Result,
    failed,
    from_captured,
    limit,
    ok,
)
from .registry import ALL, get, names, select

__all__ = [
    "ALL",
    "Result",
    "STATUS_FAILED",
    "STATUS_LIMIT",
    "STATUS_OK",
    "base",
    "codex",
    "cursor",
    "failed",
    "from_captured",
    "get",
    "limit",
    "names",
    "ok",
    "registry",
    "select",
]
