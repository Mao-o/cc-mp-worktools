"""TOML の minimal-info 化 (Python 3.11+ の tomllib を使用)。

3.11 未満では tomllib が無いため ImportError を raise し、engine 側で
opaque にフォールバックする。
"""
from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from .jsonlike import _walk  # 構造処理は流用


def redact_toml(text: str) -> dict:
    """TOML テキストから minimal info を抽出する。

    Raises:
        RuntimeError: tomllib 未搭載 (3.11 未満)
        tomllib.TOMLDecodeError: パース失敗
    """
    if tomllib is None:
        raise RuntimeError("tomllib unavailable (Python < 3.11)")
    data = tomllib.loads(text)
    root = _walk(data, 0)
    entries = root.get("_count", 0) if root.get("_type") == "object" else 1
    return {"format": "toml", "root": root, "entries": entries}


def format_toml(info: dict) -> str:
    from .jsonlike import format_jsonlike
    base = format_jsonlike(info)
    return base.replace("format: json", "format: toml")
