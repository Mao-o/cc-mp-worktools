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
    "kubectl: builder で初期化してください: /verify-cloud-account:accounts-init\n"
    "(kubectl config current-context で現在のコンテキストを事前確認可)"
)

_CONTEXT_OVERRIDE_RE = re.compile(r"(?:^|\s)--context(?:=|\s+)(\S+)")


def _context_override(command: str) -> str | None:
    """`kubectl --context foo ...` のようなオプション指定があれば抽出する。

    見つからなければ None。見つかった場合は値の文字列を返す。
    """
    m = _CONTEXT_OVERRIDE_RE.search(command)
    return m.group(1) if m else None


def _run_current_context() -> tuple[str | None, str | None]:
    """kubectl config current-context を実行し (context, error_reason) を返す。"""
    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "kubectl: kubectl コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return None, "kubectl: kubectl config current-context がタイムアウトしました。"
    current = result.stdout.strip()
    return (current or None), None


def get_active_account(project_dir: str) -> str | None:
    """現在のアクティブ kubectl context 名を返す。取得不可なら None。"""
    current, _err = _run_current_context()
    return current


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "kubectl" キーに書く値を提案する (context 文字列)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str) -> str | None:
    if not isinstance(expected, str):
        return (
            f'kubectl: accounts.local.json の "{ACCOUNT_KEY}" 値は文字列で指定してください。'
        )

    current, err = _run_current_context()
    if err:
        return err

    if current is None:
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
