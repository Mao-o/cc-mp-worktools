"""コマンド → サービス振り分けと検証オーケストレーション。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core import output
from services import ALL as SERVICES


def _match_service(command: str):
    """コマンドにマッチする最初のサービスモジュールを返す。"""
    for svc in SERVICES:
        for pattern in svc.PATTERNS:
            if re.search(pattern, command):
                return svc
    return None


def _is_readonly(command: str, service) -> bool:
    for pattern in getattr(service, "READONLY", []):
        if re.search(pattern, command):
            return True
    return False


def _find_accounts_file(project_dir: str) -> tuple[Path | None, bool]:
    """accounts.local.json (推奨) または accounts.json (旧名) を探す。

    Returns: (path or None, using_legacy)
    """
    claude_dir = Path(project_dir) / ".claude"
    local = claude_dir / "accounts.local.json"
    legacy = claude_dir / "accounts.json"

    if local.is_file():
        return local, False
    if legacy.is_file():
        return legacy, True
    return None, False


def dispatch(command: str, cwd: str) -> dict | None:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    if not project_dir:
        return None

    service = _match_service(command)
    if service is None:
        return None

    # 読み取り専用コマンドは常に許可
    if _is_readonly(command, service):
        return None

    # --- accounts 設定ファイル探索 ---
    accounts_path, using_legacy = _find_accounts_file(project_dir)
    if accounts_path is None:
        setup_hint = getattr(service, "SETUP_HINT", "")
        return output.deny(
            f"accounts.local.json が未設定です。{setup_hint}".rstrip()
        )

    # --- パースと読み込み ---
    try:
        accounts = json.loads(accounts_path.read_text())
    except json.JSONDecodeError:
        return output.deny(
            f"{accounts_path} の JSON が不正です。内容を確認・修正してください。"
        )
    except OSError as e:
        return output.deny(f"{accounts_path} の読み込みに失敗しました: {e}")

    if not isinstance(accounts, dict):
        return output.deny(
            f"{accounts_path} はオブジェクトである必要があります。"
        )

    expected = accounts.get(service.ACCOUNT_KEY)
    if not expected:
        return output.deny(
            f'{accounts_path} に "{service.ACCOUNT_KEY}" キーがありません。'
            "対象サービスのアカウントを追加してください。"
        )

    # --- サービス固有の検証 ---
    error = service.verify(expected, project_dir)
    if error:
        suffix = ""
        if using_legacy:
            suffix = " (注意: accounts.json は旧名です。accounts.local.json へのリネームを推奨します)"
        return output.deny(error + suffix)

    # --- 旧名使用時は許可するが警告 ---
    if using_legacy:
        return output.warn(
            "accounts.json は旧名です。accounts.local.json にリネームしてください"
            "（git 管理外にするため）: mv .claude/accounts.json .claude/accounts.local.json"
        )

    return None
