"""AWS アカウント検証。"""
from __future__ import annotations

import subprocess

PATTERNS = [r"^aws\b"]
READONLY = [r"^aws\s+sts\s+get-caller-identity\b"]
ACCOUNT_KEY = "aws"
SETUP_HINT = (
    'AWS 最小例: {"aws": "123456789012"}。'
    "aws sts get-caller-identity --query Account で現在値を確認可"
)


def _run_sts_get_caller_identity() -> tuple[str | None, str | None]:
    """aws sts get-caller-identity を実行し (account_id, error_reason) を返す。"""
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return None, "AWS: aws コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return None, "AWS: aws sts get-caller-identity がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。"
    current = result.stdout.strip()
    if not current:
        stderr_hint = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
        detail = f" ({stderr_hint})" if stderr_hint else ""
        return None, (
            f"AWS: 認証情報を取得できません{detail}。\n"
            "切り替え手順 (環境に応じて選択):\n"
            "  export AWS_PROFILE=<profile>   # プロファイル切り替え\n"
            "  aws sso login                  # SSO 再ログイン\n"
            "  aws configure                  # 認証情報の再設定"
        )
    return current, None


def get_active_account(project_dir: str) -> str | None:
    """現在アクティブな AWS Account ID を返す。取得不可なら None。"""
    current, _err = _run_sts_get_caller_identity()
    return current


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "aws" キーに書く値を提案する (Account ID 文字列)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str) -> str | None:
    if not isinstance(expected, str):
        return (
            f'AWS: accounts.local.json の "aws" は文字列で指定してください '
            f'(現在: {type(expected).__name__})。'
        )

    current, err = _run_sts_get_caller_identity()
    if err:
        return err

    if current != expected:
        return (
            f"AWS アカウント不一致: 現在={current}, 期待={expected}\n"
            "切り替え手順 (環境に応じて選択):\n"
            "  export AWS_PROFILE=<profile>   # 対象アカウントのプロファイルに切り替え\n"
            "  aws sso login --profile <profile>  # SSO プロファイルで再認証"
        )

    return None
