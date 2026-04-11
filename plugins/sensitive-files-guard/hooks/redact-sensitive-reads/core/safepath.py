"""パス正規化と特殊ファイル分類 + 安全な fd ベース open。

方針:
- ``normalize`` は ``os.path.abspath`` のみ。``resolve()`` は使わない
  (symlink follow すると classify が意味を失う)
- ``classify`` は ``lstat`` で判定し follow しない
- ``open_regular`` は ``O_NOFOLLOW`` で open し、``fstat`` で最終要素が通常ファイル
  であることを再確認してから fd を返す (path の再 open を排除 → TOCTOU 緩和)
- ``is_regular_directory`` は Edit/Write handler 向け (Step 6)。最終要素が
  symlink/special でないことだけ保証する (親ディレクトリの差し替え race は範囲外)

Windows 分岐:
- ``O_NOFOLLOW`` / ``O_CLOEXEC`` が無い環境では fallback。最終要素の symlink 検知は
  lstat 判定に依存する (classify ステップで落とす前提)。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal

Classification = Literal["regular", "symlink", "special", "missing", "error"]


def normalize(raw: str, cwd: str) -> Path:
    """相対→絶対正規化のみ。resolve はしない。"""
    if os.path.isabs(raw):
        return Path(os.path.normpath(raw))
    return Path(os.path.normpath(os.path.join(cwd, raw)))


def classify(path: Path) -> Classification:
    """`lstat` で分類。一切 follow しない。"""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "missing"
    except (OSError, PermissionError):
        return "error"
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if not stat.S_ISREG(mode):
        return "special"
    return "regular"


def _open_flags() -> int:
    """プラットフォームに応じた OS open フラグを組み立てる。

    Windows は ``__main__._is_unsupported_platform()`` で最初に deny exit
    するため、現状は UNIX (O_NOFOLLOW / O_CLOEXEC) 前提で運用される。
    Step 0-c 実測結果で Windows 対応する場合にバイナリモード対応が必要に
    なれば、ここで ``os.O_BINARY`` を拾う分岐を追加する。
    """
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def open_regular(path: Path) -> tuple[int, int]:
    """通常ファイルの fd と総 byte 数を返す。

    classify 済みで ``regular`` のパスでのみ呼ぶこと。``O_NOFOLLOW`` で open し、
    ``fstat`` で regular 再確認してから fd を返す。

    呼出側は必ず ``with os.fdopen(fd, "rb") as f:`` か
    ``try: ... finally: os.close(fd)`` で close 責務を担うこと。
    close に失敗したときのエラー握り潰しはしない。

    Raises:
        OSError: open 失敗、symlink 経由 (UNIX: ``ELOOP``)、regular でないなど
    """
    fd = os.open(os.fspath(path), _open_flags())
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError("open_regular: fd is not a regular file (fstat re-check)")
    return fd, st.st_size


def is_regular_directory(path: Path) -> bool:
    """最終要素が symlink でない通常ディレクトリか判定する (Edit/Write 用)。

    Step 6 の Edit/Write handler が file_path の親を検査するときに使う。
    親ディレクトリ差し替え race (途中要素の差し替え) は範囲外。
    """
    try:
        lst = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    if stat.S_ISLNK(lst.st_mode):
        return False
    return stat.S_ISDIR(lst.st_mode)
