"""機密ファイルパターン検出 — Stop hook 用。

matcher / patterns のロジックは ``_shared`` パッケージに一元化されている。
このモジュールは:
- ``_shared.patterns.load_patterns`` を Stop 固有の warn_callback で呼ぶ
- ``_shared.matcher.is_sensitive`` で評価する
- git 管理下の tracked / untracked ファイル一覧を取得する
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _shared.matcher import is_sensitive
from _shared.patterns import (
    _parse_patterns_text,
    _resolve_local_patterns_path,
)
from _shared.patterns import load_patterns as _shared_load_patterns


def _warn_local_oserror(err_name: str) -> None:
    sys.stderr.write(
        f"[check-sensitive-files] local_patterns_unavailable: {err_name}\n"
    )


def load_patterns(patterns_file: Path) -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    Stop 側は hook 間の Python 依存を避けるため stderr 直書きで warn する
    (``core.logging`` を import しない)。
    """
    return _shared_load_patterns(patterns_file, warn_callback=_warn_local_oserror)


def _run_git(args: list[str], cwd: str) -> list[str]:
    """git コマンドを実行してファイル一覧を返す。失敗時は空リスト。"""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def is_git_repo(cwd: str) -> bool:
    """cwd が git リポジトリ内かどうか"""
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return bool(result) and result[0] == "true"


def _ls_tracked(cwd: str) -> list[str]:
    """tracked ファイル一覧を取得する (submodule 内の tracked を含む)。

    ``git ls-files --recurse-submodules`` を使用。未対応の古い git ではフラグが
    認識されず空リストが返るため、その場合は素の ``ls-files`` にフォールバック。
    submodule 内の**untracked** は ``--others`` と ``--recurse-submodules`` を
    組み合わせるサポートが無いため範囲外 (README 既知制限)。

    必要 git バージョン: 1.7+ (``--recurse-submodules`` 対応)。
    """
    result = _run_git(["ls-files", "--recurse-submodules"], cwd)
    if result:
        return result
    # fallback: --recurse-submodules 非対応の古い git、または repo が本当に空の場合
    return _run_git(["ls-files"], cwd)


def find_sensitive_files(
    cwd: str,
    rules: list[tuple[str, bool]],
) -> list[dict]:
    """git 管理下の tracked + untracked ファイルから機密パターン一致を抽出する。

    - tracked: 無条件で検査対象 (``.gitignore`` 済みでも block する)。
      Step 6 で submodule 内 tracked も検査対象に追加 (``--recurse-submodules``)。
    - untracked: ``git ls-files --others --exclude-standard`` を使うため
      ``.gitignore`` 済みは既に除外されている。submodule 内 untracked は範囲外。

    Returns:
        ``[{"path": "relative/path", "status": "tracked" | "untracked"}, ...]``
    """
    if not rules:
        return []

    tracked = _ls_tracked(cwd)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"], cwd)

    results: list[dict] = []

    for filepath in tracked:
        if is_sensitive(filepath, rules):
            results.append({"path": filepath, "status": "tracked"})

    for filepath in untracked:
        if is_sensitive(filepath, rules):
            results.append({"path": filepath, "status": "untracked"})

    return results
