"""Firebase アカウント (プロジェクト) 検証。

accounts.local.json の "firebase" は 2 形式を受け付ける:
- 文字列: `"firebase": "my-project"` — 単一プロジェクト
- オブジェクト: `"firebase": {"default": "proj-dev", "prod": "proj-prod"}`
  alias 名 → project ID のマップ。現在のアクティブがいずれかの値に一致すれば OK
  (`.firebaserc` の projects マップ形式と対応。複数環境運用向け)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

PATTERNS = [r"^firebase\b"]
READONLY = [r"^firebase\s+use\s*$"]
ACCOUNT_KEY = "firebase"
SETUP_HINT = (
    "Firebase: builder で初期化してください: /verify-cloud-account:accounts-init\n"
    "(firebase use で現在のプロジェクトを事前確認可。"
    '複数 alias 運用は {"default":"proj-dev","prod":"proj-prod"} 形式も可)'
)


def _from_cli() -> str:
    try:
        result = subprocess.run(
            ["firebase", "use"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    out = result.stdout.strip()
    if not out:
        return ""
    return out.splitlines()[-1].split()[-1] if out.split() else ""


def _from_firebaserc(project_dir: str) -> str:
    path = Path(project_dir) / ".firebaserc"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("projects", {}).get("default", "") or ""


def get_active_account(project_dir: str) -> str | None:
    """現在アクティブな Firebase project ID を返す。取得不可なら None。"""
    current = _from_cli() or _from_firebaserc(project_dir)
    return current or None


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "firebase" キーに書く値を提案する (現状は scalar のみ)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str) -> str | None:
    current = _from_cli() or _from_firebaserc(project_dir)
    if not current:
        hint = expected if isinstance(expected, str) else "YOUR_PROJECT"
        return (
            f"Firebase: 現在のプロジェクトを取得できません。"
            f"firebase login && firebase use {hint} を実行してください。"
        )

    if isinstance(expected, dict):
        valid = [v for v in expected.values() if isinstance(v, str) and v]
        if not valid:
            return (
                'Firebase: accounts.local.json の "firebase" オブジェクトに'
                " 有効な (文字列値の) project ID が見つかりません。"
            )
        if current in valid:
            return None
        expected_display = ", ".join(sorted(set(valid)))
        return (
            f"Firebase プロジェクト不一致: 現在={current}, "
            f"期待={expected_display} のいずれか"
            f" — 切り替え: firebase use <alias>"
        )

    if not isinstance(expected, str):
        return (
            f'Firebase: accounts.local.json の "firebase" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    if current != expected:
        return (
            f"Firebase プロジェクト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: firebase use {expected}"
        )

    return None
