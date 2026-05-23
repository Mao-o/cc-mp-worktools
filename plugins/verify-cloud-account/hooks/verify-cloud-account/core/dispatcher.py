"""コマンド → サービス振り分けと検証オーケストレーション。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core import cache, output, paths
from core.command_parser import extract_candidates
from services import ALL as SERVICES

_MIGRATE_HINT = (
    "旧パスから統合するには builder の migrate サブコマンドを使用してください: "
    "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py migrate --commit"
)


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


def _find_accounts_file(
    project_dir: str,
) -> tuple[Path | None, str | None, list[tuple[str, Path]], Path | None]:
    """accounts.local.json を 3-tier + 親ディレクトリ遡及で探す。

    cwd 階層から始めて 1 階層ずつ親へ遡り、最初に accounts.local.json が
    見つかった階層を採用する。同一階層に複数 tier が同居する場合は
    fail-closed (D4) のため `conflicts` を返す。worktree から親 repo の
    `.claude/verify-cloud-account/accounts.local.json` を継承する運用を
    透過的にサポートし、worktree 内に同名ファイルを複製する必要を無くす。

    Returns:
        (path, kind, conflicts, resolved_dir):
          - path: 採用するファイルのパス。見つからない or 競合時は None
          - kind: "new" / "deprecated" / "legacy" のいずれか (採用されたもの)
          - conflicts: 同一階層に複数 tier が存在した場合の検出リスト
                       (採用は保留。呼び出し側で fail-closed deny する)
          - resolved_dir: 採用 (または競合検出) した階層の絶対パス。
                          親遡及で worktree 外を採用した場合は project_dir の
                          祖先を指す。何も見つからなければ None
    """
    found, resolved_dir = paths.discover_accounts_files_with_ancestors(project_dir)
    if len(found) >= 2:
        return None, None, found, resolved_dir
    if len(found) == 1:
        kind, path = found[0]
        return path, kind, [], resolved_dir
    return None, None, [], None


def _ancestor_note(project_dir: str, resolved_dir: Path | None) -> str:
    """親ディレクトリの accounts.local.json を採用した場合の 1 行注釈。

    deny / warn のメッセージに前置きとして埋め込み、worktree 利用者が
    「どこから読まれているか」を把握できるようにする。
    cwd 階層で見つかった場合や、まったく見つからなかった場合は空文字を返す。
    """
    if resolved_dir is None:
        return ""
    try:
        project = Path(project_dir).resolve()
    except OSError:
        return ""
    if resolved_dir == project:
        return ""
    return (
        f"accounts.local.json は親ディレクトリ {resolved_dir} から継承して "
        "います (worktree 内に同名ファイルは不要)。"
    )


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


def _deprecation_note(kind: str) -> str:
    """kind に応じた旧パス移行案内 (deny/warn の suffix 用) を返す。"""
    if kind == "deprecated":
        return (
            ".claude/accounts.local.json は旧パスです。"
            ".claude/verify-cloud-account/accounts.local.json への移行を推奨します。\n"
            + _MIGRATE_HINT
        )
    if kind == "legacy":
        return (
            "accounts.json は旧名です。"
            ".claude/verify-cloud-account/accounts.local.json に移行してください。\n"
            + _MIGRATE_HINT
        )
    return ""


def _format_conflicts(conflicts: list[tuple[str, Path]]) -> str:
    lines = ["複数のパスに accounts.local.json が存在します (曖昧さを避けるため検証を停止):"]
    for kind, path in conflicts:
        lines.append(f"  - {path} ({kind})")
    lines.append("どれか 1 つに統合してください:")
    lines.append("  " + _MIGRATE_HINT)
    # migrate --commit は旧ファイルを残すため、その後の手動削除を案内しないと
    # `len(found) >= 2` の deny が解消されず remediation loop になる (R4)。
    legacy_paths = [path for kind, path in conflicts if kind != "new"]
    if legacy_paths:
        lines.append("  migrate --commit 完了後、以下の旧ファイルを手動で削除してください:")
        for path in legacy_paths:
            lines.append(f"    rm {path}")
    return "\n".join(lines)


def dispatch(command: str, cwd: str) -> dict | None:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    if not project_dir:
        return None

    targets = _collect_targets(command)
    if not targets:
        return None

    accounts_path, kind, conflicts, resolved_dir = _find_accounts_file(project_dir)
    ancestor_note = _ancestor_note(project_dir, resolved_dir)

    if conflicts:
        body = _format_conflicts(conflicts)
        if ancestor_note:
            body = ancestor_note + "\n\n" + body
        return output.deny(body)

    if accounts_path is None:
        hints = [getattr(svc, "SETUP_HINT", "") for svc, _ in targets]
        hint_block = "\n".join(h for h in hints if h)
        msg = ".claude/verify-cloud-account/accounts.local.json が未設定です。"
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

    note = _deprecation_note(kind) if kind in ("deprecated", "legacy") else ""

    if errors:
        body = "\n\n".join(errors)
        if ancestor_note:
            body = ancestor_note + "\n\n" + body
        if note:
            body = body + "\n\n" + note
        return output.deny(body)

    # warn は deprecation note が出るときのみ発火させる。verify 成功時は
    # ancestor_note 単独では warn を出さず silent (worktree で親採用は
    # 通常運用なので毎回通知するとノイズになる)。
    if note:
        body = note
        if ancestor_note:
            body = ancestor_note + "\n\n" + body
        return output.warn(body)

    return None
