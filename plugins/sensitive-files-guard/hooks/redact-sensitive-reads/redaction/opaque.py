"""YAML / 不明形式 / 大ファイル fallback の minimal-info 化。

YAML は regex parser を採用しない (形式の仕様上、安全な regex 抽出が困難)。
代わりに streaming の keyonly_scan に流す。
"""
from __future__ import annotations

from .keyonly_scan import format_keyonly, scan_keys


def redact_opaque(text: str, fmt_hint: str = "opaque") -> dict:
    """不明/YAML/fallback 用: text 全体を keyonly_scan に流す。"""
    keys = scan_keys(text)
    return {
        "format": fmt_hint,
        "entries": len(keys),
        "keys": keys,
        "scanned_bytes": len(text.encode("utf-8", errors="replace")),
    }


def format_opaque(info: dict) -> str:
    return format_keyonly(
        info["keys"],
        info["scanned_bytes"],
        fmt_hint=info["format"],
    )
