"""accounts.local.json 配置パスの定数と helper。

verify-cloud-account は accounts.local.json の配置パスを 3 世代サポートする。
新規プロジェクトは `.claude/verify-cloud-account/accounts.local.json` (new) を
使い、旧パスは deprecation 案内しつつ後方互換で受け入れる。

このモジュールは `scripts/accounts_builder.py` と `core/dispatcher.py` の
両方から参照される。定数で配置パスを一元管理し、`accounts_file_new()` は
assertion で basename が "accounts.local.json" であることを保証する
(builder がこのパスに書き込むため、argv 経由で書込先が変わる余地を排除する)。
"""
from __future__ import annotations

from pathlib import Path

ACCOUNTS_FILE_NEW = Path(".claude") / "verify-cloud-account" / "accounts.local.json"
ACCOUNTS_FILE_DEPRECATED = Path(".claude") / "accounts.local.json"
ACCOUNTS_FILE_LEGACY = Path(".claude") / "accounts.json"

_ALLOWED_BASENAME = "accounts.local.json"


def accounts_file_new(project_dir: str) -> Path:
    """現行推奨パス (`<project>/.claude/verify-cloud-account/accounts.local.json`) を返す。

    basename が "accounts.local.json" であることを assert で保証する。
    builder はこのパスにのみ書き込むため、変更経路を固定する責務がある。
    """
    p = Path(project_dir) / ACCOUNTS_FILE_NEW
    assert p.name == _ALLOWED_BASENAME, (
        "accounts_file_new must point to accounts.local.json "
        f"(got {p.name!r})"
    )
    return p


def accounts_file_deprecated(project_dir: str) -> Path:
    """旧 "deprecated" パス (`.claude/accounts.local.json`) の絶対パス。"""
    return Path(project_dir) / ACCOUNTS_FILE_DEPRECATED


def accounts_file_legacy(project_dir: str) -> Path:
    """legacy パス (`.claude/accounts.json`) の絶対パス。"""
    return Path(project_dir) / ACCOUNTS_FILE_LEGACY


def discover_all_accounts_files(project_dir: str) -> list[tuple[str, Path]]:
    """配置候補のうち存在するものを (kind, absolute_path) のリストで返す。

    kind は "new" / "deprecated" / "legacy" のいずれか。優先度順
    (new → deprecated → legacy) で並ぶ。

    返却リストの長さが 2 以上なら、dispatcher は fail-closed で deny する。
    """
    candidates = [
        ("new", accounts_file_new(project_dir)),
        ("deprecated", accounts_file_deprecated(project_dir)),
        ("legacy", accounts_file_legacy(project_dir)),
    ]
    return [(kind, path) for kind, path in candidates if path.is_file()]


# 親ディレクトリ遡及の最大階層数。`project_dir` 自身を含めてこの段数まで
# 探索する。git worktree が `<repo>/.worktrees/<branch>/<subdir>/...` の
# ように深く配置されていても十分到達できる範囲を取りつつ、上方暴走を
# 防ぐためのガード値。
ANCESTOR_SEARCH_MAX_LEVELS = 10


def discover_accounts_files_with_ancestors(
    project_dir: str,
    *,
    max_levels: int = ANCESTOR_SEARCH_MAX_LEVELS,
) -> tuple[list[tuple[str, Path]], Path | None]:
    """`project_dir` から親ディレクトリへ遡って accounts.local.json を探す。

    各階層で `discover_all_accounts_files()` を呼び、最初に non-empty を
    返した階層の `(found_list, resolved_dir)` を返す。worktree が親 repo の
    `.claude/verify-cloud-account/accounts.local.json` を共有する運用に
    対応する。**worktree 自体に accounts.local.json を作りたくない**という
    要請を満たすため、cwd で見つからなくても親側で見つかれば採用する。

    探索ポリシー:
      - cwd 階層に何か 1 つでも見つかれば、そこで採用判定する
        (親階層は見ない、cwd 優先)
      - 同一階層に複数 tier が同居する場合は呼び出し側で fail-closed (D4)
      - 何も見つからずに `Path.parent == Path` (ルート) に到達したら諦める
      - `max_levels` で安全側の上限を設ける

    Args:
        project_dir: 検索を開始するディレクトリ (絶対パス推奨)。
        max_levels: 親を遡る最大階層数 (`project_dir` 自身を含む)。

    Returns:
        (found_list, resolved_dir):
          - found_list: 採用階層で見つかった `(kind, path)` のリスト。
            複数 (>=2) なら呼び出し側で fail-closed 判定すること。
          - resolved_dir: 採用した階層の絶対パス。何も見つからなければ None。
    """
    try:
        current = Path(project_dir).resolve()
    except OSError:
        return [], None
    for _ in range(max_levels):
        found = discover_all_accounts_files(str(current))
        if found:
            return found, current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return [], None
