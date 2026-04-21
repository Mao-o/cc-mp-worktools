"""Kubernetes (kubectl CLI) アクティブコンテキスト検証。"""
from __future__ import annotations

import re
import subprocess

PATTERNS = [r"^kubectl\b"]
READONLY = [
    r"^kubectl\s+config\s+(current-context|get-contexts|view|get-clusters|get-users)\b",
    r"^kubectl\s+cluster-info\b",
]
ACCOUNT_KEY = "kubectl"
SETUP_HINT = (
    "kubectl config current-context で現在のコンテキストを確認し、"
    "以下で作成してください: "
    'mkdir -p .claude && echo \'{"kubectl":"YOUR_CONTEXT"}\' > .claude/accounts.local.json'
)

_CONTEXT_OVERRIDE_RE = re.compile(r"(?:^|\s)--context(?:=|\s+)(\S+)")


def _context_override(command: str) -> str | None:
    """`kubectl --context foo ...` のようなオプション指定があれば抽出する。

    見つからなければ None。見つかった場合は値の文字列を返す。
    """
    m = _CONTEXT_OVERRIDE_RE.search(command)
    return m.group(1) if m else None


def verify(expected, project_dir: str) -> str | None:
    if not isinstance(expected, str):
        return (
            f'kubectl: accounts.local.json の "{ACCOUNT_KEY}" 値は文字列で指定してください。'
        )

    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "kubectl: kubectl コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return "kubectl: kubectl config current-context がタイムアウトしました。"

    current = result.stdout.strip()
    if not current:
        return (
            f"kubectl: アクティブコンテキストが設定されていません。"
            f"kubectl config use-context {expected} を実行してください。"
        )

    if current != expected:
        return (
            f"kubectl コンテキスト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: kubectl config use-context {expected}"
        )

    return None
