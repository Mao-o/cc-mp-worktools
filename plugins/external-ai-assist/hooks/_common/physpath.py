"""パスの大文字小文字をディスク上の綴りに揃える (大文字小文字を区別しない FS 向け)。

`os.path.realpath` は symlink を解くが、大文字小文字を区別しない FS (macOS の既定) で
実体と大文字小文字だけ違う名前 (`B.py` と書いたが実体は `b.py`) の綴りはそのまま残す。
ここで構成要素ごとに大文字小文字だけをディスク上の綴りに揃える。

- ディレクトリ部分は macOS の `F_GETPATH` (getcwd と同じ仕組み) で得る
- 末尾の名前はディレクトリの一覧から選ぶ。書かれた名前がそのまま一覧にあればそれを使い、
  無ければ「大文字小文字と Unicode の正規化を無視して同じ名前」かつ「同じ実体 (inode)」の
  項目が 1 つだけのときにその綴りを使う。ファイル自体に `F_GETPATH` を使わないのは、
  hard link (同じ inode の別名) では別の名前を返しうるため。名前の候補を大文字小文字の
  違いに限るので、`h.py` と書いた hard link が `b.py` に化けることはない
- **Unicode の正規化形 (NFC / NFD) は書かれた形を保つ**。ディスク上が NFD (Finder で作った
  名前など) でも、git は `core.precomposeunicode` で index に NFC を持ち、`cat-file --batch`
  の stdin や呼び出し側の名前の比較は precompose されない。ディスク上の NFD に揃えると、
  書かれた NFC のままなら当たっていた git の名前から外れる (マージ前レビューの指摘)
- 構成要素の数が変わる・畳んでも一致しない構成要素がある (firmlink など、大文字小文字と
  正規化以外の違い) ときは、揃えずに realpath の結果を使う

揃えられないとき (`F_GETPATH` の無い OS、存在しないパス、読めないディレクトリ) は
`os.path.realpath` の結果をそのまま返す。大文字小文字を区別する FS では書かれた名前が
一覧にそのままあるので、結果は realpath と変わらない。

**git の index の綴りとは限らない**: git を通さずに大文字小文字だけ rename したファイルは、
ディスク上の綴りと index の綴りが食い違う。git に渡す名前の選択は呼び出し側
(`gitscan.to_relative`) が index を見て行う。
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

#: ディレクトリの一覧の memo。キーは (パス, inode, mtime_ns)。同じディレクトリのファイルを
#: 続けて揃えるときに毎回 listdir しない (5000 項目のディレクトリで 1 件ごとに一覧を読むと
#: 約 100 倍遅かった。マージ前レビューの指摘)。項目の追加・削除・rename で mtime が変わる
#: のでキーが外れる。上限を超えたら捨てる。
_LISTING_CACHE: dict[tuple[str, int, int], tuple[frozenset[str], dict[str, list[str]]]] = {}
_LISTING_CACHE_MAX = 256


def physical_dir(directory: str) -> str:
    """ディレクトリの実体のパスを、大文字小文字をディスク上の綴りにして返す。得られなければ realpath。"""
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
    # 相対の答えは構成要素の数が合わない (先頭の空要素が無い) ので `_align` が realpath に倒す
    return _align(real, os.fsdecode(raw.split(b"\0", 1)[0]))


def physical_path(path: str) -> str:
    """パスの実体を、大文字小文字をディスク上の綴りにして返す (symlink は realpath と同じく解く)。"""
    real = os.path.realpath(path)
    if _GETPATH is None:
        return real
    parent, name = os.path.split(real)
    if not name:
        return real
    phys_parent = physical_dir(parent)
    return os.path.join(phys_parent, _keep_form(name, _on_disk_name(phys_parent, name)))


def _fold(name: str) -> str:
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _keep_form(written: str, on_disk: str) -> str:
    """大文字小文字はディスク上の綴り、Unicode の正規化形は書かれた形にした名前。"""
    if written == on_disk:
        return written
    if unicodedata.normalize("NFC", written) == unicodedata.normalize("NFC", on_disk):
        return written  # 正規化形だけの違い: 書かれた形のまま (git に渡る綴りを変えない)
    for form in ("NFC", "NFD"):
        if unicodedata.normalize(form, written) == written:
            return unicodedata.normalize(form, on_disk)
    return on_disk


def _align(written_path: str, disk_path: str) -> str:
    """構成要素ごとに `_keep_form` を当てる。対応が取れなければ書かれたパスのまま。"""
    written = written_path.split(os.sep)
    disk = disk_path.split(os.sep)
    if len(written) != len(disk):
        return written_path
    out: list[str] = []
    for w, d in zip(written, disk):
        if _fold(w) != _fold(d):
            return written_path
        out.append(_keep_form(w, d))
    return os.sep.join(out)


def _listing(directory: str) -> tuple[frozenset[str], dict[str, list[str]]]:
    st = os.stat(directory)
    key = (directory, st.st_ino, st.st_mtime_ns)
    cached = _LISTING_CACHE.get(key)
    if cached is not None:
        return cached
    entries = os.listdir(directory)
    folded: dict[str, list[str]] = {}
    for entry in entries:
        folded.setdefault(_fold(entry), []).append(entry)
    if len(_LISTING_CACHE) >= _LISTING_CACHE_MAX:
        _LISTING_CACHE.clear()
    value = (frozenset(entries), folded)
    _LISTING_CACHE[key] = value
    return value


def _on_disk_name(directory: str, name: str) -> str:
    """`directory` の中で `name` が指す項目の、一覧上の綴り。決められなければ `name`。"""
    try:
        target = os.lstat(os.path.join(directory, name))
        entries, folded = _listing(directory)
    except OSError:
        return name
    if name in entries:
        return name
    found: list[str] = []
    for entry in folded.get(_fold(name), ()):
        try:
            st = os.lstat(os.path.join(directory, entry))
        except OSError:
            continue
        if os.path.samestat(st, target):
            found.append(entry)
    return found[0] if len(found) == 1 else name
