"""Firebase アカウント (プロジェクト) 検証。

accounts.local.json の "firebase" は 2 形式を受け付ける:
- 文字列: `"firebase": "my-project"` — 単一プロジェクト
- オブジェクト: `"firebase": {"default": "proj-dev", "prod": "proj-prod"}`
  alias 名 → project ID のマップ。現在のアクティブがいずれかの値に一致すれば OK
  (`.firebaserc` の projects マップ形式と対応。複数環境運用向け)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

PATTERNS = [r"^firebase\b"]
READONLY = [
    r"^firebase\s+use\s*$",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^firebase\s+(--version|--help|version|help)\b",
]
ACCOUNT_KEY = "firebase"
SETUP_HINT = (
    'Firebase 最小例: {"firebase": "my-project-id"}。'
    "firebase use で現在値を確認可。"
    '複数 alias: {"firebase": {"default":"proj-dev","prod":"proj-prod"}}'
)


def _from_cli(env=None) -> str:
    try:
        result = subprocess.run(
            ["firebase", "use"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return ""
    out = result.stdout.strip()
    if not out:
        return ""
    # firebase use はアクティブ project があれば project ID を 1 行で出力。
    # アクティブ project がなければ複数行のヘルプを出すので、単一行・単一トークン
    # 以外は project ID とみなさない (ヘルプ末尾の "folder." 等の誤検出を防ぐ)。
    lines = out.splitlines()
    if len(lines) != 1:
        return ""
    tokens = lines[0].split()
    if len(tokens) != 1:
        return ""
    return tokens[0]


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
    # .firebaserc を優先 (JSON で構造化された Firebase CLI 標準ファイル)。
    # firebase use の出力は version 依存 + active project の有無で構造が変動する
    # ため、fallback として最後に評価する。
    current = _from_firebaserc(project_dir) or _from_cli()
    return current or None


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "firebase" キーに書く値を提案する (現状は scalar のみ)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str, env=None) -> str | None:
    current = _from_firebaserc(project_dir) or _from_cli(env)
    if not current:
        if shutil.which("firebase") is None:
            return (
                "Firebase: firebase コマンドが見つかりません。"
                "npm install -g firebase-tools でインストールしてください。"
            )
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
        alias_list = [
            f"  firebase use {k}  # → {v}"
            for k, v in expected.items()
            if isinstance(v, str) and v
        ]
        alias_hint = "\n".join(alias_list)
        return (
            f"Firebase プロジェクト不一致: 現在={current}, "
            f"期待={expected_display} のいずれか\n"
            f"切り替え:\n{alias_hint}"
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


_USE_RE = re.compile(r"^firebase\s+use\s+(\S+)\s*$")


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待プロジェクト / alias への firebase use」なら True。

    dict 期待値は alias 名 (キー) と project ID (値) の両方を受け付ける
    (deny メッセージが `firebase use <alias>` を案内するため)。
    """
    m = _USE_RE.match(candidate)
    if not m:
        return False
    target = m.group(1)
    if isinstance(expected, str):
        return target == expected
    if isinstance(expected, dict):
        for alias, project in expected.items():
            if not (isinstance(project, str) and project):
                continue
            if target in (alias, project):
                return True
    return False
