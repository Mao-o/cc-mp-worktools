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
