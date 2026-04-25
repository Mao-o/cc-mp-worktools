"""GitHub (gh CLI) アカウント検証。

accounts.local.json の "github" は 2 形式を受け付ける:
- 文字列: `"github": "Mao-o"`
  任意の host のアクティブアカウントを照合 (後方互換)
- オブジェクト: `"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}`
  hostname ごとのアクティブアカウントを個別照合 (GHE / 複数インスタンス対応)
"""
from __future__ import annotations

import re
import subprocess

PATTERNS = [r"^gh\b"]
READONLY = [r"^gh\s+auth\s+(status|list)\b"]
ACCOUNT_KEY = "github"
SETUP_HINT = (
    "GitHub: builder で初期化してください: /verify-cloud-account:accounts-init\n"
    "(gh auth status で現在のアカウントを事前確認可。"
    'Enterprise 別指定は {"github.com":"USER_A","ghe.example.com":"USER_B"} 形式も可)'
)

_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")


def parse_active_accounts(output_text: str) -> dict[str, str]:
    """gh auth status の出力から {hostname: active_account} を返す。

    各 `Active account: true` について、直前の `Active account: true` より後の
    範囲を逆順にスキャンして最初の `Logged in to <host> account <user>` を採用する。
    これにより複数 host がある場合も各 host のアクティブアカウントが正しく
    ペア化される。
    """
    result: dict[str, str] = {}
    lines = output_text.splitlines()
    last_active_idx = -1
    for i, line in enumerate(lines):
        if "Active account: true" not in line:
            continue
        start = last_active_idx + 1
        for j in range(i, start - 1, -1):
            m = _LOGGED_IN_RE.search(lines[j])
            if m:
                host, user = m.group(1), m.group(2)
                result.setdefault(host, user)
                break
        last_active_idx = i
    return result


_parse_active_accounts = parse_active_accounts


def _run_gh_auth_status() -> tuple[str, str | None]:
    """gh auth status を実行し (combined_output, error) を返す。"""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "", "GitHub: gh コマンドが見つかりません。brew install gh を実行してください。"
    except subprocess.TimeoutExpired:
        return "", "GitHub: gh auth status がタイムアウトしました。"
    return result.stdout + result.stderr, None


def _fetch_active_accounts() -> tuple[dict[str, str] | None, str | None]:
    """gh auth status を実行し (active_accounts, error_reason) を返す。"""
    combined, err = _run_gh_auth_status()
    if err:
        return None, err
    active = parse_active_accounts(combined)
    if not active:
        return None, "GitHub: アクティブアカウントを取得できません。gh auth login を実行してください。"
    return active, None


def _cli_error_reason() -> str | None:
    """CLI エラーまたは未ログイン時の理由を返す (正常取得時は None)。"""
    _active, err = _fetch_active_accounts()
    return err


def get_active_account(project_dir: str) -> dict[str, str] | None:
    """現在のアクティブ GitHub アカウントを {hostname: user} の dict で返す。

    取得不可・未ログインの場合は None。詳細な理由は `_cli_error_reason()` で
    取得できる。
    """
    active, _err = _fetch_active_accounts()
    return active


def suggest_accounts_entry(project_dir: str) -> str | dict | None:
    """accounts.local.json の "github" キーに書く値を提案する。

    - host が 1 つだけなら scalar (user 文字列)
    - 複数 host なら dict[host, user]
    - 取得不可なら None
    """
    active = get_active_account(project_dir)
    if not active:
        return None
    if len(active) == 1:
        return next(iter(active.values()))
    return dict(active)


def verify(expected, project_dir: str) -> str | None:
    active, err = _fetch_active_accounts()
    if err:
        return err

    if isinstance(expected, dict):
        if not expected:
            return (
                'GitHub: accounts.local.json の "github" オブジェクトが空です。'
                ' {"github": {"github.com": "YOUR_ACCOUNT"}} の形式で'
                ' ホスト名とアカウントのマップを記述してください。'
            )
        errors: list[str] = []
        for host, want in expected.items():
            if not isinstance(want, str):
                errors.append(
                    f"GitHub [{host}]: 期待値は文字列で指定してください "
                    f"(現在: {type(want).__name__})。"
                )
                continue
            current = active.get(host)
            if current is None:
                errors.append(
                    f"GitHub [{host}]: このホストにログインしていません — "
                    f"gh auth login --hostname {host} を実行してください。"
                )
            elif current != want:
                errors.append(
                    f"GitHub [{host}] アカウント不一致: 現在={current}, 期待={want}"
                    f" — 切り替え: gh auth switch --hostname {host} --user {want}"
                )
        return "\n".join(errors) if errors else None

    if not isinstance(expected, str):
        return (
            f'GitHub: accounts.local.json の "github" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    current = next(iter(active.values()), None)
    if current is None:
        return "GitHub: アクティブアカウントを取得できません。gh auth login を実行してください。"

    if current != expected:
        return (
            f"GitHub アカウント不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gh auth switch --user {expected}"
        )

    return None
