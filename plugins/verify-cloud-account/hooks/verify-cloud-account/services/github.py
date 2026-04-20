"""GitHub (gh CLI) アカウント検証。"""
from __future__ import annotations

import re
import subprocess

PATTERNS = [r"^gh\b"]
READONLY = [r"^gh\s+auth\s+(status|list)\b"]
ACCOUNT_KEY = "github"
SETUP_HINT = (
    "gh auth status で現在のアカウントを確認し、以下で作成してください: "
    'mkdir -p .claude && echo \'{"github":"YOUR_ACCOUNT"}\' > .claude/accounts.local.json'
)


def verify(expected: str, project_dir: str) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "GitHub: gh コマンドが見つかりません。brew install gh を実行してください。"
    except subprocess.TimeoutExpired:
        return "GitHub: gh auth status がタイムアウトしました。"

    combined = result.stdout + result.stderr

    # "Active account: true" の直前 3 行内に "Logged in ... account USERNAME" がある
    lines = combined.splitlines()
    current: str | None = None
    for i, line in enumerate(lines):
        if "Active account: true" in line:
            start = max(0, i - 3)
            for prev in lines[start : i + 1]:
                m = re.search(r"Logged in to \S+ account (\S+)", prev)
                if m:
                    current = m.group(1)
                    break
            break

    if current is None:
        return "GitHub: アクティブアカウントを取得できません。gh auth login を実行してください。"

    if current != expected:
        return (
            f"GitHub アカウント不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gh auth switch --user {expected}"
        )

    return None
