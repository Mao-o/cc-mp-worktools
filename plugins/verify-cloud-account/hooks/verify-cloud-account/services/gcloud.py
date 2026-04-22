"""Google Cloud (gcloud CLI) プロジェクト / アカウント検証。

accounts.local.json の "gcloud" は 2 形式を受け付ける:
- 文字列: `"gcloud": "my-project"` — project のみ検証 (後方互換)
- オブジェクト: `"gcloud": {"project": "my-project", "account": "me@example.com"}`
  project と account を個別検証。どちらかだけ省略も可
"""
from __future__ import annotations

import subprocess

PATTERNS = [r"^gcloud\b"]
READONLY = [
    r"^gcloud\s+auth\s+list\b",
    r"^gcloud\s+config\s+get-value\s+(project|account)\b",
]
ACCOUNT_KEY = "gcloud"
SETUP_HINT = (
    "gcloud config get-value project で現在のプロジェクトを確認し、"
    "以下で作成してください: "
    'mkdir -p .claude && echo \'{"gcloud":"YOUR_PROJECT_ID"}\' > .claude/accounts.local.json'
    '\n(account もあわせて検証する場合は'
    ' "gcloud": {"project":"p","account":"me@example.com"} 形式も可)'
)


def _get(key: str) -> tuple[str | None, str | None]:
    """`gcloud config get-value <key>` を実行し (value, error) を返す。"""
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", key],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "GCP: gcloud コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return None, f"GCP: gcloud config get-value {key} がタイムアウトしました。"
    value = result.stdout.strip()
    if not value or value == "(unset)":
        return None, None
    return value, None


def _check_project(expected: str) -> str | None:
    current, err = _get("project")
    if err:
        return err
    if current is None:
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


def _check_account(expected: str) -> str | None:
    current, err = _get("account")
    if err:
        return err
    if current is None:
        return (
            f"GCP: アクティブアカウントが設定されていません。"
            f"gcloud config set account {expected} を実行してください。"
        )
    if current != expected:
        return (
            f"GCP アカウント不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gcloud config set account {expected}"
        )
    return None


def verify(expected, project_dir: str) -> str | None:
    if isinstance(expected, dict):
        project_want = expected.get("project")
        account_want = expected.get("account")
        if not project_want and not account_want:
            return (
                'GCP: accounts.local.json の "gcloud" オブジェクトに '
                '"project" または "account" キーが必要です。'
            )
        errors: list[str] = []
        if project_want:
            if not isinstance(project_want, str):
                errors.append(
                    f"GCP: project 期待値は文字列で指定してください "
                    f"(現在: {type(project_want).__name__})。"
                )
            else:
                err = _check_project(project_want)
                if err:
                    errors.append(err)
        if account_want:
            if not isinstance(account_want, str):
                errors.append(
                    f"GCP: account 期待値は文字列で指定してください "
                    f"(現在: {type(account_want).__name__})。"
                )
            else:
                err = _check_account(account_want)
                if err:
                    errors.append(err)
        return "\n".join(errors) if errors else None

    if not isinstance(expected, str):
        return (
            f'GCP: accounts.local.json の "gcloud" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    return _check_project(expected)
