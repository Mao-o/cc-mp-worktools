"""Google Cloud (gcloud CLI) プロジェクト / アカウント検証。

accounts.local.json の "gcloud" は 2 形式を受け付ける:
- 文字列: `"gcloud": "my-project"` — project のみ検証 (後方互換)
- オブジェクト: `"gcloud": {"project": "my-project", "account": "me@example.com"}`
  project と account を個別検証。どちらかだけ省略も可
"""
from __future__ import annotations

import re
import subprocess

PATTERNS = [r"^gcloud\b"]
READONLY = [
    r"^gcloud\s+auth\s+list\b",
    r"^gcloud\s+config\s+get-value\s+(project|account)\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^gcloud\s+(--version|--help|version|help)\b",
]
ACCOUNT_KEY = "gcloud"
SETUP_HINT = (
    'GCP 最小例: {"gcloud": "my-project-id"}。'
    "gcloud config get-value project で現在値を確認可。"
    'account 併用: {"gcloud": {"project":"p","account":"me@example.com"}}'
)


def _get(key: str, env=None) -> tuple[str | None, str | None]:
    """`gcloud config get-value <key>` を実行し (value, error) を返す。

    env: コマンド行頭のインライン環境変数をマージした完全 env
    (`CLOUDSDK_CORE_PROJECT` / `CLOUDSDK_ACTIVE_CONFIG_NAME` 等)。
    None なら hook プロセスの環境を継承する。
    """
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", key],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except FileNotFoundError:
        return None, "GCP: gcloud コマンドが見つかりません。"
    except subprocess.TimeoutExpired:
        return None, f"GCP: gcloud config get-value {key} がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。"
    value = result.stdout.strip()
    if not value or value == "(unset)":
        return None, None
    return value, None


def _check_project(expected: str, env=None) -> str | None:
    current, err = _get("project", env)
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


def _check_account(expected: str, env=None) -> str | None:
    current, err = _get("account", env)
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


def get_active_account(project_dir: str) -> dict[str, str | None] | None:
    """{"project": ..., "account": ...} を返す。両方取得不可なら None。

    片方だけ取れた場合は、取れなかった側のキーの値を None にして返す。
    """
    project, _ = _get("project")
    account, _ = _get("account")
    if project is None and account is None:
        return None
    return {"project": project, "account": account}


def suggest_accounts_entry(project_dir: str) -> str | dict | None:
    """accounts.local.json の "gcloud" キーに書く値を提案する。

    - project のみ取得可 → scalar (project 文字列)
    - account も取得可 → dict[project, account]
    - 両方取得不可 → None
    """
    active = get_active_account(project_dir)
    if not active:
        return None
    project = active.get("project")
    account = active.get("account")
    if project and not account:
        return project
    entry: dict[str, str] = {}
    if project:
        entry["project"] = project
    if account:
        entry["account"] = account
    return entry or None


def verify(expected, project_dir: str, env=None) -> str | None:
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
                err = _check_project(project_want, env)
                if err:
                    errors.append(err)
        if account_want:
            if not isinstance(account_want, str):
                errors.append(
                    f"GCP: account 期待値は文字列で指定してください "
                    f"(現在: {type(account_want).__name__})。"
                )
            else:
                err = _check_account(account_want, env)
                if err:
                    errors.append(err)
        if not errors:
            return None
        if len(errors) == 1:
            return errors[0]
        return "GCP 検証エラー (複数):\n" + "\n".join(f"  - {e}" for e in errors)

    if not isinstance(expected, str):
        return (
            f'GCP: accounts.local.json の "gcloud" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    return _check_project(expected, env)


_CONFIG_SET_RE = re.compile(r"^gcloud\s+config\s+set\s+(project|account)\s+(\S+)\s*$")


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待値への gcloud config set」なら True。

    str 期待値は project のみ照合 (verify と同じ解釈)。dict 期待値は set 対象
    キー (project / account) の期待値と照合する。余分なフラグ付きは保守的に
    False で通常検証に落とす。
    """
    m = _CONFIG_SET_RE.match(candidate)
    if not m:
        return False
    key, value = m.group(1), m.group(2)
    if isinstance(expected, str):
        return key == "project" and value == expected
    if isinstance(expected, dict):
        want = expected.get(key)
        return isinstance(want, str) and value == want
    return False
