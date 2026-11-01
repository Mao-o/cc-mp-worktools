"""パスのディスク上の綴り (大文字小文字を区別しない FS 向けの正規化)。

`os.path.realpath` は symlink を解くが、大文字小文字を区別しない FS (macOS の既定) で
実体と大文字小文字だけ違う名前 (`B.py` と書いたが実体は `b.py`) の綴りはそのまま残す。
git は作業ツリーのパスを index の綴りで扱い、literal pathspec の比較は大文字小文字を
区別するので、`B.py` のまま渡すと実体に当たらない (差分が空になる)。ここでディスク上の
綴りに揃える。

- ディレクトリ部分は macOS の `F_GETPATH` (getcwd と同じ仕組み) で得る
- 末尾の名前はディレクトリの一覧から選ぶ。書かれた名前がそのまま一覧にあればそれを使い、
  無ければ「大文字小文字と Unicode の正規化を無視して同じ名前」かつ「同じ実体 (inode)」の
  項目が 1 つだけのときにその綴りを使う。ファイル自体に `F_GETPATH` を使わないのは、
  hard link (同じ inode の別名) では別の名前を返しうるため。名前の候補を大文字小文字の
  違いに限るので、`h.py` と書いた hard link が `b.py` に化けることはない

揃えられないとき (`F_GETPATH` の無い OS、存在しないパス、読めないディレクトリ) は
`os.path.realpath` の結果をそのまま返す。大文字小文字を区別する FS では書かれた名前が
一覧にそのままあるので、結果は realpath と変わらない。
"""
from __future__ import annotations

import os
import unicodedata

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX 以外
    fcntl = None  # type: ignore[assignment]

#: macOS の `F_GETPATH`。他の OS では None (ディレクトリ部分は realpath のまま)。
_GETPATH = getattr(fcntl, "F_GETPATH", None) if fcntl is not None else None
_MAXPATHLEN = 1024


def physical_dir(directory: str) -> str:
    """ディレクトリの実体のパスを、ディスク上の綴りで返す。得られなければ realpath。"""
    real = os.path.realpath(directory)
    if _GETPATH is None:
        return real
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NONBLOCK)
    except OSError:
        return real
    try:
        raw = fcntl.fcntl(fd, _GETPATH, bytes(_MAXPATHLEN))
    except OSError:
        return real
    finally:
        os.close(fd)
    path = os.fsdecode(raw.split(b"\0", 1)[0])
    return path if os.path.isabs(path) else real


def physical_path(path: str) -> str:
    """パスの実体を、ディスク上の綴りで返す (symlink は realpath と同じく解く)。"""
    real = os.path.realpath(path)
    if _GETPATH is None:
        return real
    parent, name = os.path.split(real)
    if not name:
        return real
    phys_parent = physical_dir(parent)
    return os.path.join(phys_parent, _on_disk_name(phys_parent, name))


def _fold(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


def _on_disk_name(directory: str, name: str) -> str:
    """`directory` の中で `name` が指す項目の、一覧上の綴り。決められなければ `name`。"""
    try:
        target = os.lstat(os.path.join(directory, name))
        entries = os.listdir(directory)
    except OSError:
        return name
    if name in entries:
        return name
    key = _fold(name)
    found: list[str] = []
    for entry in entries:
        if _fold(entry) != key:
            continue
        try:
            st = os.lstat(os.path.join(directory, entry))
        except OSError:
            continue
        if os.path.samestat(st, target):
            found.append(entry)
    return found[0] if len(found) == 1 else name
