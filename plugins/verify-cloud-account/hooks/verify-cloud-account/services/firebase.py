"""Firebase アカウント (プロジェクト) 検証。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

PATTERNS = [r"^(npx\s+|pnpm\s+exec\s+|mise\s+exec\s+--\s+)?firebase\b"]
READONLY = [r"^(npx\s+|pnpm\s+exec\s+|mise\s+exec\s+--\s+)?firebase\s+use\s*$"]
ACCOUNT_KEY = "firebase"
SETUP_HINT = (
    "firebase use で現在のプロジェクトを確認し、以下で作成してください: "
    'mkdir -p .claude && echo \'{"firebase":"YOUR_PROJECT_ID"}\' > .claude/accounts.local.json'
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
    # firebase use はいくつかの出力形式を取るので末尾の単語を採用
    return out.splitlines()[-1].split()[-1] if out.split() else ""


def _from_firebaserc(project_dir: str) -> str:
    path = Path(project_dir) / ".firebaserc"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("projects", {}).get("default", "") or ""


def verify(expected: str, project_dir: str) -> str | None:
    current = _from_cli() or _from_firebaserc(project_dir)

    if not current:
        return (
            f"Firebase: 現在のプロジェクトを取得できません。"
            f"firebase login && firebase use {expected} を実行してください。"
        )

    if current != expected:
        return (
            f"Firebase プロジェクト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: firebase use {expected}"
        )

    return None
