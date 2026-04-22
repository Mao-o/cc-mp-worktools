"""コマンド → サービス振り分けと検証オーケストレーション。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core import cache, output
from core.command_parser import extract_candidates
from services import ALL as SERVICES


def _match_service(candidate: str):
    """候補セグメントにマッチする最初のサービスを返す。"""
    for svc in SERVICES:
        for pattern in svc.PATTERNS:
            if re.search(pattern, candidate):
                return svc
    return None


def _is_readonly(candidate: str, service) -> bool:
    for pattern in getattr(service, "READONLY", []):
        if re.search(pattern, candidate):
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


def _collect_targets(command: str) -> list[tuple]:
    """コマンドを分解し、検証対象 (non-readonly) のサービス候補リストを返す。

    同一サービスが複数セグメントで出現しても最初のみ残す
    (アクティブアカウントは 1 つなので検証は 1 回で十分)。
    """
    targets: list[tuple] = []
    seen: set = set()
    for cand in extract_candidates(command):
        svc = _match_service(cand)
        if svc is None or _is_readonly(cand, svc):
            continue
        if svc in seen:
            continue
        seen.add(svc)
        targets.append((svc, cand))
    return targets


def dispatch(command: str, cwd: str) -> dict | None:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    if not project_dir:
        return None

    targets = _collect_targets(command)
    if not targets:
        return None

    accounts_path, using_legacy = _find_accounts_file(project_dir)
    if accounts_path is None:
        hints = [getattr(svc, "SETUP_HINT", "") for svc, _ in targets]
        hint_block = "\n".join(h for h in hints if h)
        msg = ".claude/accounts.local.json が未設定です。"
        if hint_block:
            msg += "\n" + hint_block
        return output.deny(msg)

    try:
        accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return output.deny(
            f"{accounts_path} の JSON が不正です: {e.msg} (行 {e.lineno})。"
            "内容を確認・修正してください。"
        )
    except OSError as e:
        return output.deny(f"{accounts_path} の読み込みに失敗しました: {e}")

    if not isinstance(accounts, dict):
        return output.deny(
            f"{accounts_path} はオブジェクト ({{...}}) である必要があります。"
        )

    try:
        accounts_mtime = accounts_path.stat().st_mtime
    except OSError:
        accounts_mtime = 0.0

    errors: list[str] = []
    for svc, _cand in targets:
        entry = accounts.get(svc.ACCOUNT_KEY)
        if entry is None or entry == "":
            errors.append(
                f'{accounts_path} に "{svc.ACCOUNT_KEY}" キーがありません。'
                "対象サービスのアカウントを追加してください。"
            )
            continue

        if not isinstance(entry, (str, dict)):
            errors.append(
                f'{accounts_path} の "{svc.ACCOUNT_KEY}" 値は文字列または '
                f'オブジェクトであるべきです (現在: {type(entry).__name__})。'
            )
            continue

        svc_name = svc.__name__.rsplit(".", 1)[-1]
        if cache.get_success(svc_name, project_dir, entry, accounts_mtime):
            continue

        err = svc.verify(entry, project_dir)
        if err:
            errors.append(err)
        else:
            cache.set_success(svc_name, project_dir, entry, accounts_mtime)

    if errors:
        suffix = ""
        if using_legacy:
            suffix = (
                "\n(注意: accounts.json は旧名です。"
                "accounts.local.json へのリネームを推奨します)"
            )
        return output.deny("\n\n".join(errors) + suffix)

    if using_legacy:
        return output.warn(
            "accounts.json は旧名です。accounts.local.json にリネームしてください"
            "（git 管理外にするため）: "
            "mv .claude/accounts.json .claude/accounts.local.json"
        )

    return None
