"""AWS アカウント検証。"""
from __future__ import annotations

import subprocess

PATTERNS = [r"^aws\b"]
READONLY = [r"^aws\s+sts\s+get-caller-identity\b"]
ACCOUNT_KEY = "aws"
SETUP_HINT = (
    "aws sts get-caller-identity で現在のアカウントを確認し、以下で作成してください: "
    'mkdir -p .claude && echo \'{"aws":"YOUR_ACCOUNT_ID"}\' > .claude/accounts.local.json'
)


def verify(expected, project_dir: str) -> str | None:
    if not isinstance(expected, str):
        return (
            f'AWS: accounts.local.json の "aws" は文字列で指定してください '
            f'(現在: {type(expected).__name__})。'
        )

    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return "AWS: aws コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return "AWS: aws sts get-caller-identity がタイムアウトしました。"

    current = result.stdout.strip()
    if not current:
        return "AWS: 認証情報を取得できません。aws configure または aws sso login を実行してください。"

    if current != expected:
        return (
            f"AWS アカウント不一致: 現在={current}, 期待={expected}"
            f" — AWS_PROFILE を確認してください。"
        )

    return None
