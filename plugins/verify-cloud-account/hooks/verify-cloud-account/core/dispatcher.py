"""コマンド → サービス振り分けと検証オーケストレーション。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
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
    """コマンドを分解し、検証対象 (non-readonly) の (svc, cands, inline_env) リストを返す。

    同一サービスでも **インライン env が異なるセグメントは別エントリ**にする。
    `AWS_PROFILE=prod aws ... && AWS_PROFILE=dev aws ...` のような複合コマンドで
    後段 (dev) を検証せず誤 allow するのを防ぐため、(service, inline_env) の組
    ごとに 1 エントリへ集約する。同一 (service, env) のセグメントは 1 エントリに
    まとめる (アクティブアカウントは 1 つなので verify は組ごとに 1 回で十分)。

    cands はその (service, env) 組に属する全候補。self-remediation 判定が全
    セグメントを見る必要があることと、deny reason に検出コマンドを併記する (D14)
    ことの両方で使う。inline_env はコマンド行頭のインライン環境変数
    (`AWS_PROFILE=prod aws ...` の `{"AWS_PROFILE": "prod"}`) で、検証 subprocess
    に渡しコマンド実行時と同条件で検証する。
    """
    order: list = []
    cand_map: dict = {}
    for cand, inline_env in extract_candidates(command):
        svc = _match_service(cand)
        if svc is None or _is_readonly(cand, svc):
            continue
        key = (svc, tuple(sorted(inline_env.items())))
        if key not in cand_map:
            cand_map[key] = []
            order.append(key)
        cand_map[key].append(cand)
    targets: list = []
    for key in order:
        svc, env_items = key
        targets.append((svc, cand_map[key], dict(env_items)))
    return targets


def _all_self_remediation(cands: list, service, entry) -> bool:
    """全候補セグメントが「期待値へ向かう切替コマンド」なら True。

    deny reason が案内する remediation コマンド (例: gh auth switch --user
    <expected>) 自体が PATTERNS にマッチして deny される self-remediation
    loop を防ぐ。期待値以外への切替はここで True にならず通常検証に落ちる。
    切替 + write の合せ技 (`gh auth switch -u X && gh pr create`) も write
    側のセグメントが残るため通常検証される。判定中の例外は安全側 (通常検証)。
    """
    fn = getattr(service, "is_self_remediation", None)
    if fn is None:
        return False
    try:
        return all(fn(c, entry) for c in cands)
    except Exception:
        return False


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


_DEPRECATION_WARN_TTL = 86400  # 1 日


def _should_emit_deprecation_warn(project_dir: str) -> bool:
    """deprecation warn を出すべきか判定する (1 日 1 回に制限)。

    alert fatigue を避けるため、同一プロジェクトへの deprecation warn は
    1 日に 1 回のみ発火する。deny メッセージ内の note は制限しない
    (deny は実行阻止のため常に表示すべき)。
    """
    key = hashlib.sha256(f"deprecation-warn:{project_dir}".encode()).hexdigest()[:16]
    flag_dir = Path(tempfile.gettempdir()) / "cc-mp-verify-cloud-account"
    flag_path = flag_dir / f"deprecation-{key}.flag"
    try:
        if flag_path.exists():
            mtime = flag_path.stat().st_mtime
            if time.time() - mtime < _DEPRECATION_WARN_TTL:
                return False
        flag_dir.mkdir(parents=True, exist_ok=True)
        flag_path.write_text("")
    except OSError:
        pass
    return True


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
        hints = [getattr(svc, "SETUP_HINT", "") for svc, *_ in targets]
        hint_block = "\n".join(h for h in hints if h)
        msg = (
            ".claude/verify-cloud-account/accounts.local.json が未設定です。\n"
            "(使用するサービスのみ記述すれば OK。未記載のサービスは検証対象外)\n"
            "初期化: /verify-cloud-account:accounts-init"
        )
        if hint_block:
            msg += "\n\n" + hint_block
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
    for svc, cands, inline_env in targets:
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

        if _all_self_remediation(cands, svc, entry):
            continue

        svc_name = svc.__name__.rsplit(".", 1)[-1]
        if cache.get_success(svc_name, project_dir, entry, accounts_mtime, inline_env):
            continue

        # コマンド行頭のインライン env を hook プロセスの env にマージして渡す。
        # inline_env が空なら env=None (= 親環境継承) のままにする。空 dict を
        # subprocess.run(env={}) に渡すと環境変数が一切無い状態になり危険なため。
        proc_env = {**os.environ, **inline_env} if inline_env else None
        err = svc.verify(entry, project_dir, env=proc_env)
        if err:
            # D14: どのセグメントが検証を起動したかを deny reason に併記し、
            # 複合コマンドで原因コマンドを一目で特定できるようにする。
            errors.append(f"{err}\n(検出コマンド: {', '.join(cands)})")
        else:
            cache.set_success(svc_name, project_dir, entry, accounts_mtime, inline_env)

    note = _deprecation_note(kind) if kind in ("deprecated", "legacy") else ""

    if errors:
        # 同一サービスが複数 env で検証対象になると env 非依存のエラー
        # (キー欠落 / 型不正) が重複しうるため exact-duplicate を畳む。
        # verify 失敗は (検出コマンド: ...) でセグメントが異なれば残る。
        body = "\n\n".join(dict.fromkeys(errors))
        if ancestor_note:
            body = ancestor_note + "\n\n" + body
        if note:
            body = body + "\n\n" + note
        return output.deny(body)

    # warn は deprecation note が出るときのみ発火させる。verify 成功時は
    # ancestor_note 単独では warn を出さず silent (worktree で親採用は
    # 通常運用なので毎回通知するとノイズになる)。
    # alert fatigue 防止: warn (verify 成功時) は 1 日 1 回に制限する。
    # deny 内の note は常に表示 (実行阻止メッセージの一部のため)。
    if note and _should_emit_deprecation_warn(project_dir):
        body = note
        if ancestor_note:
            body = ancestor_note + "\n\n" + body
        return output.warn(body)

    return None
