"""Read tool 用 handler。

normalize → classify → (O_NOFOLLOW で fd open) → redact → deny/ask のパイプライン。
path の再 open は行わず、``open_regular`` で得た fd をそのまま engine に渡すことで
TOCTOU を緩和する。全ての内部例外は fail-closed で ``ask_or_deny`` に倒す。
"""
from __future__ import annotations

import os

from core import logging as L
from core import output
from _shared.matcher import is_sensitive
from core.patterns import load_patterns
from core.safepath import classify, normalize, open_regular
from redaction.engine import MAX_INLINE_BYTES, redact, redact_large_file


def handle(envelope: dict) -> dict:
    """Read tool の PreToolUse envelope を受け取り、hook 出力 dict を返す。

    envelope 例:
        {"tool_input": {"file_path": "..."}, "cwd": "...",
         "permission_mode": "bypassPermissions" | ...}
    """
    tool_input = envelope.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    cwd = envelope.get("cwd", "")

    if not isinstance(raw_path, str) or not raw_path:
        return output.make_allow()

    try:
        rules = load_patterns()
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(
            "patterns.txt が読めないため安全側で一時停止します。"
            "hook 管理者に連絡してください。",
            envelope,
        )

    if not rules:
        return output.make_allow()

    try:
        path = normalize(raw_path, cwd)
    except (ValueError, OSError) as e:
        L.log_error("normalize_failed", type(e).__name__)
        return output.ask_or_deny(
            "file_path の正規化に失敗しました。安全側で一時停止します。",
            envelope,
        )

    basename = path.name
    if not is_sensitive(path, rules):
        return output.make_allow()

    cls = classify(path)
    L.log_info("classify", cls)

    if cls == "symlink":
        return output.ask_or_deny(
            "symlink 経由で機密パターンに一致するファイルを読もうとしています。"
            "続行しますか？",
            envelope,
        )
    if cls == "special":
        return output.ask_or_deny(
            "非通常ファイル (FIFO/socket/device) が機密パターンに一致します。"
            "続行しますか？",
            envelope,
        )
    if cls == "missing":
        return output.make_allow()
    if cls == "error":
        return output.ask_or_deny(
            "ファイル状態の確認に失敗しました (権限/IO)。安全側で一時停止します。",
            envelope,
        )

    try:
        fd, size = open_regular(path)
    except OSError as e:
        L.log_error("open_regular_failed", type(e).__name__)
        return output.ask_or_deny(
            "安全な open に失敗しました (symlink race / 非通常ファイル疑い)。"
            "安全側で一時停止します。",
            envelope,
        )

    try:
        with os.fdopen(fd, "rb") as f:
            if size > MAX_INLINE_BYTES:
                reason = redact_large_file(f, basename)
            else:
                reason = redact(f, basename, size)
    except Exception as e:
        L.log_error("redaction_failed", type(e).__name__)
        return output.ask_or_deny(
            "redaction 処理に失敗しました。安全側で一時停止します。",
            envelope,
        )

    return output.make_deny(reason)
