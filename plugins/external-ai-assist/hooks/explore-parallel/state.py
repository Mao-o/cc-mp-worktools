"""tool_use_id ベースの一時ファイルパス管理。

複数アナライザが同時実行されても衝突しないよう、
パス命名は `<name>-<tool_use_id>.{txt,pid}` とする。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "explore-parallel"


def paths(name: str, tool_use_id: str) -> tuple[Path, Path]:
    """(result_file, pid_file) のタプルを返す。親ディレクトリも作成する。"""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    result_file = BASE_DIR / f"{name}-{tool_use_id}.txt"
    pid_file = BASE_DIR / f"{name}-{tool_use_id}.pid"
    return result_file, pid_file


def cleanup(*files: Path) -> None:
    """ファイルを削除する。存在しない場合は無視。"""
    for f in files:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
