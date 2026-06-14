"""大ファイル用の streaming 鍵名抽出。

``^(\\w[\\w.-]*)\\s*[:=]`` にマッチする行から鍵名のみを抽出する。
32KB 超のファイルや構造不明ファイルで使用。値には一切触れない。
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import IO

from .sanitize import sanitize_key

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][\w.\-]*)\s*[:=]")

# 最大抽出鍵数 (reason コンテキスト圧迫防止)
MAX_KEYS = 500


def scan_keys(text: str) -> list[str]:
    """テキスト全体から鍵名を抽出する (重複は順序を保って一意化)。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if not m:
            continue
        key = sanitize_key(m.group(1))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
        if len(ordered) >= MAX_KEYS:
            break
    return ordered


def scan_stream(f: IO[bytes], max_bytes: int = 1024 * 1024) -> tuple[list[str], int]:
    """file-like オブジェクトを streaming で読み鍵名を抽出する。

    呼出側は seek 位置を適切に戻しておく (先頭から読みたければ f.seek(0))。
    close はしない (呼出側責務)。

    Returns:
        (keys, total_bytes_read)
    """
    keys_seen: set[str] = set()
    ordered: list[str] = []
    read_bytes = 0
    try:
        while read_bytes < max_bytes:
            chunk = f.readline()
            if not chunk:
                break
            read_bytes += len(chunk)
            try:
                line = chunk.decode("utf-8", errors="replace")
            except Exception:
                continue
            m = _KEY_RE.match(line)
            if not m:
                continue
            key = sanitize_key(m.group(1))
            if key in keys_seen:
                continue
            keys_seen.add(key)
            ordered.append(key)
            if len(ordered) >= MAX_KEYS:
                break
    except OSError:
        pass
    return ordered, read_bytes


def scan_file(path: Path, max_bytes: int = 1024 * 1024) -> tuple[list[str], int]:
    """パスを開いて scan_stream を呼ぶ簡易ラッパ (テスト互換用)。

    本体ロジックは ``scan_stream`` に置き、``engine.redact_large_file`` からは
    fd 経由で呼ばれる。テストや他の呼出からは path 経由で呼んで良い。
    """
    try:
        with path.open("rb") as f:
            return scan_stream(f, max_bytes=max_bytes)
    except OSError:
        return [], 0


def format_keyonly(keys: list[str], total_bytes: int, fmt_hint: str = "unknown") -> str:
    lines = [
        f"format: {fmt_hint} (large, keys-only scan)",
        f"entries: {len(keys)}",
        f"scanned_bytes: {total_bytes}",
    ]
    if not keys:
        lines.append("(no keys matched)")
        return "\n".join(lines)
    preview_cap = 60
    shown = keys[:preview_cap]
    lines.append("keys: " + ", ".join(shown))
    if len(keys) > preview_cap:
        lines.append(f"... ({len(keys) - preview_cap} more)")
    lines.append("note: file too large for full parse. values never read.")
    return "\n".join(lines)
