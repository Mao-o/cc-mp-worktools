"""Hook 出力 JSON builder。

Phase 0 実測で確定した唯一信頼できる情報注入経路である
`permissionDecisionReason` を使い、deny/ask を返す。
systemMessage トップレベルは届かないため使用しない。
"""
from __future__ import annotations

# reason のハード上限 (プランの目標: 1-2KB)。
# Step 4 で 4KB → 3KB に縮小 (Phase 0 実測で 1KB/8KB/32KB のいずれも完全配信される
# ことは確認済みだが、他 hook と合算した全体コンテキスト圧迫を抑えるため余裕を持たせる)。
MAX_REASON_BYTES = 3 * 1024

# reason 末尾に付ける truncation マーカー
TRUNCATE_MARKER = "\n...[truncated]"


def _truncate(reason: str, limit: int = MAX_REASON_BYTES) -> str:
    """reason が limit byte を超えたら UTF-8 境界で安全に切る。"""
    encoded = reason.encode("utf-8")
    if len(encoded) <= limit:
        return reason
    keep = limit - len(TRUNCATE_MARKER.encode("utf-8"))
    if keep <= 0:
        return TRUNCATE_MARKER.strip()
    truncated = encoded[:keep]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore") + TRUNCATE_MARKER


def make_deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _truncate(reason),
        }
    }


def make_ask(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": _truncate(reason),
        }
    }


def make_allow() -> dict:
    """no-op allow (明示的な allow は出さず、空オブジェクトで通す)。"""
    return {}


def ask_or_deny(reason: str, envelope: dict) -> dict:
    """bypass モードでは ask が自動 allow されるため deny にフォールバック。

    Phase 0 実測で確定: bypassPermissions モード下では ask + reason は
    そのままツール実行に通ってしまう。この hook が機密ファイル検出で ask を
    返したい文脈では、bypass 判定を見て deny に倒す。
    """
    if envelope.get("permission_mode") == "bypassPermissions":
        return make_deny(reason)
    return make_ask(reason)
