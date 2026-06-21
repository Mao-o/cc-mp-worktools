"""Hook 出力 JSON ビルダー。"""
from __future__ import annotations

# deny メッセージの先頭に付ける出所タグ。CLI 本体のナマエラー
# (`Unable to locate credentials` 等) と誤認され、CLI レベルの切り分けに
# 時間を浪費するのを防ぐ。これは plugin による実行前検証だと明示する。
_SOURCE_TAG = (
    "[verify-cloud-account] クラウド CLI の実行前アカウント検証です "
    "(CLI 本体のエラーではありません)。"
)


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{_SOURCE_TAG}\n{reason}",
        }
    }


def warn(context: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }
