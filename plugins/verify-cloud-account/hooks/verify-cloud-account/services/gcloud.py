"""Google Cloud (gcloud CLI) プロジェクト検証。"""
from __future__ import annotations

import subprocess

PATTERNS = [r"^gcloud\b"]
READONLY = [
    r"^gcloud\s+auth\s+list\b",
    r"^gcloud\s+config\s+get-value\s+(project|account)\b",
]
ACCOUNT_KEY = "gcloud"
SETUP_HINT = (
    "gcloud config get-value project で現在のプロジェクトを確認し、以下で作成してください: "
    'mkdir -p .claude && echo \'{"gcloud":"YOUR_PROJECT_ID"}\' > .claude/accounts.local.json'
)


def verify(expected: str, project_dir: str) -> str | None:
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "GCP: gcloud コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return "GCP: gcloud config get-value project がタイムアウトしました。"

    current = result.stdout.strip()
    if not current or current == "(unset)":
        return (
            f"GCP: アクティブプロジェクトが設定されていません。"
            f"gcloud config set project {expected} を実行してください。"
        )

    if current != expected:
        return (
            f"GCP プロジェクト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gcloud config set project {expected}"
        )

    return None
