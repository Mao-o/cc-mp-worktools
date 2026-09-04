"""Read tool 用 handler。

normalize → classify → (O_NOFOLLOW で fd open) → redact → deny/ask のパイプライン。
path の再 open は行わず、``open_regular`` で得た fd をそのまま engine に渡すことで
TOCTOU を緩和する。全ての内部例外は fail-closed で ``ask_or_deny`` に倒す。
"""
from __future__ import annotations

import os

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive
from _shared.patterns import resolve_project_root
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
        rules = load_patterns(cwd=cwd)
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(M.policy_unavailable("pause"), envelope)

    if not rules:
        return output.make_allow()

    try:
        path = normalize(raw_path, cwd)
    except (ValueError, OSError) as e:
        L.log_error("normalize_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("normalize_failed"), envelope)

    basename = path.name
    # root は [project:] セクションの key と同じ値 (path 形 rule の基準、0.24.0)
    if not is_sensitive(path, rules, root=resolve_project_root(cwd)):
        return output.make_allow()

    cls = classify(path)
    L.log_info("classify", cls)

    if cls == "symlink":
        return output.ask_or_deny(M.read_ask("symlink"), envelope)
    if cls == "directory":
        return output.ask_or_deny(M.read_ask("directory"), envelope)
    if cls == "special":
        return output.ask_or_deny(M.read_ask("special"), envelope)
    if cls == "missing":
        return output.make_allow()
    if cls == "error":
        return output.ask_or_deny(M.read_ask("io_error"), envelope)

    try:
        fd, size = open_regular(path)
    except OSError as e:
        L.log_error("open_regular_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("open_failed"), envelope)

    try:
        with os.fdopen(fd, "rb") as f:
            if size > MAX_INLINE_BYTES:
                reason = redact_large_file(f, basename)
            else:
                reason = redact(f, basename, size)
    except Exception as e:
        L.log_error("redaction_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("redaction_failed"), envelope)

    # 0.26.0: reason (<DATA> 包装の 1 ブロック) が 3KB 予算を超える
    # 場合、以前は core.output._truncate の盲目 byte cut だけに頼っており、
    # 鍵数の多い dotenv / json / yaml で閉じタグと末尾 note が key 行の途中で
    # 失われていた。M.fit_read_reason が閉じタグ・末尾 note を保護したまま
    # 折り畳む (収まらなければ入力をそのまま返し、_truncate が最終防御を担う
    # ので verdict には影響しない)。
    return output.make_deny(M.fit_read_reason(reason))
