"""Hook 出力 JSON builder。

Phase 0 実測で確定した唯一信頼できる情報注入経路である
`permissionDecisionReason` を使い、deny/ask を返す。
systemMessage トップレベルは届かないため使用しない。

## 三態判定の使い分け

- ``make_deny``: 機密パターン確定一致、policy 入力欠如など、ユーザーの permission
  mode に関わらず必ず block したいケース。
- ``ask_or_deny``: 判定不能だが、機密の可能性があり bypass モードではデフォルト
  allow に倒したくないケース。Read/Edit handler の symlink/special/parent-dir
  fail、非 bash tool の catch-all 例外などで使う。non-bypass = ask、bypass = deny。
- ``ask_or_allow``: 判定不能だが、autonomous / planning 実行 (auto /
  bypassPermissions / plan) では日常コマンドを止めない方を優先するケース。Bash
  handler の opaque wrapper (``bash -c``, ``eval``, ``python3 -c``, ``sudo``,
  ``awk``, ``sed`` 等) や hard-stop metachar、shell keyword、abs/rel path exec、
  residual metachar、shlex/normalize 失敗で使う。default = ask、
  auto/bypass/plan = allow。acceptEdits / dontAsk は明示的に非 lenient
  (ask 維持)。機密確定は使わず ``make_deny`` 固定。
"""
from __future__ import annotations

# reason のハード上限 (プランの目標: 1-2KB)。
# Step 4 で 4KB → 3KB に縮小 (Phase 0 実測で 1KB/8KB/32KB のいずれも完全配信される
# ことは確認済みだが、他 hook と合算した全体コンテキスト圧迫を抑えるため余裕を持たせる)。
MAX_REASON_BYTES = 3 * 1024

# reason 末尾に付ける truncation マーカー
TRUNCATE_MARKER = "\n...[truncated]"

# autonomous / planning 実行モード: ``ask_or_allow`` がここに含まれる
# permission_mode で allow に倒す。
#   - "auto": CLI 2.1.83+ の前段 classifier モード
#   - "bypassPermissions": 全確認スキップモード
#   - "plan": plan mode (0.3.3 追加)
# それ以外 ("default" / "acceptEdits" / "dontAsk") は ask に倒す。
# "acceptEdits" は Edit/Write 専用モードで Bash lenient の意図なし、"dontAsk" は
# 明示的な非 lenient 判断として既存方針を維持する。
#
# Phase 0 実測 (0.3.3): 現行 CLI (2.1.101 系) では plan mode で PreToolUse hook が
# 発火しない観測あり。その場合 ``"plan"`` エントリは dead だが害なく、将来 CLI が
# plan mode でも hook を発火させる仕様に変わったとき **自動的に正しい挙動に収束
# する互換層** として機能する。詳細は ``docs/DESIGN.md`` の Phase 0 記述を参照。
LENIENT_MODES = frozenset({"auto", "bypassPermissions", "plan"})


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


def _is_lenient_mode(envelope: dict) -> bool:
    """envelope.permission_mode が autonomous / planning 実行モード (auto /
    bypassPermissions / plan) か。"""
    return envelope.get("permission_mode") in LENIENT_MODES


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

    Read/Edit handler の symlink/special/parent-dir fail、非 bash tool の catch-all
    例外など、「判定不能だが機密の可能性があり bypass で allow してはいけない」
    用途で使う。
    """
    if envelope.get("permission_mode") == "bypassPermissions":
        return make_deny(reason)
    return make_ask(reason)


def ask_or_allow(reason: str, envelope: dict) -> dict:
    """autonomous / planning 実行モード (auto / bypassPermissions / plan) では
    allow に倒す。

    Bash handler の静的解析不能ケース (opaque wrapper、hard-stop metachar、shell
    keyword、abs/rel path exec、residual metachar、shlex/normalize 失敗) 用。

    autonomous / plan 実行を選んでいるユーザーは「日常コマンドが片っ端から止まる」
    のを避けたい意図がある。これらは「機密かもしれない」だけで「機密と確定した」
    わけではないため、確定 match (``make_deny``) より弱い保護に倒す。

    ``acceptEdits`` / ``dontAsk`` は意図的に lenient 扱いしない (明示的に ask 維持)。

    機密パターン確定 (literal or glob 候補列挙で True) のときはこの関数を使わず
    ``make_deny`` を直接呼ぶこと。
    """
    if _is_lenient_mode(envelope):
        return make_allow()
    return make_ask(reason)
