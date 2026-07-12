#!/usr/bin/env python3
"""I/O 境界: パス解決・早期 skip 判定・安全なファイル読み込み。

language.py / metrics.py / judge.py を純粋関数のまま保つため、ファイルシステム
アクセスをこのモジュールに閉じ込める。
"""
from __future__ import annotations

import fnmatch
import stat
from dataclasses import dataclass
from pathlib import Path

_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Pipfile.lock",
        "poetry.lock",
        "go.sum",
        "composer.lock",
    }
)

_MINIFIED_SUFFIXES = (".min.js", ".min.css", ".map")

_GENERATED_NAME_PATTERNS = (
    "*.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.g.dart",
    "*.freezed.dart",
    "*_generated.*",
)


@dataclass(frozen=True)
class LoadedFile:
    text: str
    lines: list[str]


def resolve_path(file_path: str, cwd: str) -> Path:
    """相対パスは cwd と結合、絶対パスはそのまま返す。

    ``Path.resolve()`` は使わない: symlink を先に正規化すると
    ``load_text()`` の ``lstat`` ベース symlink 判定が意味を失う。
    """
    path = Path(file_path)
    if path.is_absolute():
        return path
    return Path(cwd) / path if cwd else path


def should_skip_by_name(path: Path) -> bool:
    """lockfile / minified / generated-path パターンに一致するか (内容を見ない早期 skip)。"""
    name = path.name
    if name in _LOCKFILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in _MINIFIED_SUFFIXES):
        return True
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _GENERATED_NAME_PATTERNS):
        return True
    return False


def load_text(
    path: Path,
    max_bytes: int = 2_000_000,
    max_lines: int = 20_000,
) -> LoadedFile | None:
    """安全弁付きでファイルを読み込む。対象外/失敗時は None (呼び出し側は skip)。

    - symlink / FIFO 等の非通常ファイルは ``lstat`` で検出して None
    - ``max_bytes`` 超のファイルは読まずに None
    - 読込後の行数が ``max_lines`` 超なら None
    """
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > max_bytes:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    lines = text.splitlines()
    if len(lines) > max_lines:
        return None
    return LoadedFile(text=text, lines=lines)
