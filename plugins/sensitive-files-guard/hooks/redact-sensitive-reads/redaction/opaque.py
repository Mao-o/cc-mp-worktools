"""YAML / 不明形式 / 大ファイル fallback の minimal-info 化。

0.14.0 (E5): YAML は専用の簡易 top-level key 抽出器を使う。
``^([A-Za-z_][A-Za-z0-9_-]*)\\s*:`` 行で top-level key、インデント先頭の
同形は nested として件数カウントのみ。値そのものは破棄。完全パースはしない
(思想 1 = うっかり露出予防の射程、完全な情報遮断ではない)。

その他の未知形式は従来通り ``keyonly_scan`` の streaming 抽出にフォールバック。
"""
from __future__ import annotations

import re

from .keyonly_scan import format_keyonly, scan_keys
from .sanitize import sanitize_key

_YAML_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:")
_YAML_NESTED_KEY_RE = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_\-]*)\s*:")

_YAML_MAX_TOP_KEYS = 500


def _redact_yaml(text: str) -> dict:
    """YAML テキストから top-level key 名 + nested 件数だけを抽出する。

    list 形式 (``- item:``) は無視。完全 YAML 仕様は満たさない (anchor / alias /
    flow style / multi-document などは対象外)。
    """
    top_keys: list[str] = []
    seen: set[str] = set()
    nested_count = 0
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m_top = _YAML_TOP_KEY_RE.match(line)
        if m_top:
            key = sanitize_key(m_top.group(1))
            if key not in seen:
                seen.add(key)
                top_keys.append(key)
                if len(top_keys) >= _YAML_MAX_TOP_KEYS:
                    break
            continue
        if _YAML_NESTED_KEY_RE.match(line):
            nested_count += 1
    return {
        "format": "yaml",
        "entries": len(top_keys),
        "keys": top_keys,
        "nested_count": nested_count,
        "scanned_bytes": len(text.encode("utf-8", errors="replace")),
    }


def redact_opaque(text: str, fmt_hint: str = "opaque") -> dict:
    """yaml は専用抽出器、その他は keyonly_scan に流す。"""
    if fmt_hint == "yaml":
        return _redact_yaml(text)
    keys = scan_keys(text)
    return {
        "format": fmt_hint,
        "entries": len(keys),
        "keys": keys,
        "scanned_bytes": len(text.encode("utf-8", errors="replace")),
    }


def format_opaque(info: dict) -> str:
    if info.get("format") == "yaml" and "nested_count" in info:
        return _format_yaml(info)
    return format_keyonly(
        info["keys"],
        info["scanned_bytes"],
        fmt_hint=info["format"],
    )


def _format_yaml(info: dict) -> str:
    lines = [
        "format: yaml",
        f"entries: {info['entries']} (top-level)",
    ]
    if info["entries"] == 0:
        lines.append("(no top-level keys matched)")
    else:
        lines.append("top-level keys (in order):")
        for i, k in enumerate(info["keys"], 1):
            lines.append(f"  {i}. {k}")
    if info["nested_count"] > 0:
        lines.append(f"nested entries: {info['nested_count']} (not parsed)")
    lines.append(
        "note: nested structure not parsed. only top-level key names returned."
    )
    return "\n".join(lines)
