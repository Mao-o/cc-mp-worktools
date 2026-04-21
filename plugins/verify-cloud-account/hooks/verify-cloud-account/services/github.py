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
    "gh auth status で現在のアカウントを確認し、以下で作成してください: "
    'mkdir -p .claude && echo \'{"github":"YOUR_ACCOUNT"}\' > .claude/accounts.local.json'
    '\n(Enterprise hostname 別に指定する場合は "github": {"github.com":"USER_A",'
    ' "ghe.example.com":"USER_B"} 形式も可)'
)

_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")


def _parse_active_accounts(output_text: str) -> dict[str, str]:
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


def verify(expected, project_dir: str) -> str | None:
    combined, err = _run_gh_auth_status()
    if err:
        return err

    active = _parse_active_accounts(combined)
    if not active:
        return "GitHub: アクティブアカウントを取得できません。gh auth login を実行してください。"

    if isinstance(expected, dict):
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
