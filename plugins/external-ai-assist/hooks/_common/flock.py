"""flock 付き read-modify-write。

exitplan-review のマーカー (hash / count) と post-implementation-review の状態ファイルは
どちらも「開く → LOCK_EX → 全読み → 書き戻し → LOCK_UN」の同型なので、ここに寄せる。

非ブロッキングで review 実行中ずっと保持する `cursor_lock` は、ロックファイルを開けない
環境で直列化を諦める fail-open 分岐が固有なので共通化していない
(post-implementation-review/state.py)。

## 権限 (0.9.0, 内部バックログ)

state / マーカーファイルは絶対パス一覧やレビュー本文 (コード抜粋) を含む。macOS の
`$TMPDIR` はユーザー専用ディレクトリだが、Linux の `/tmp` は共有で、既定 umask
(大半の環境で 022) のまま作成すると他ユーザーから読める (`-rw-r--r--` を実機で確認)。
このモジュールで作る新規ディレクトリは 0o700、新規ファイルは 0o600 に締める。

- `os.makedirs(path, mode=0o700)` は **最後の 1 階層にしか mode を適用しない**
  (中間ディレクトリは umask 既定のまま作られるのが Python 公式ドキュメントに明記された
  仕様)。`$TMPDIR/post-implementation-review/state/` のような多階層パスをこれで作ると、
  肝心の `post-implementation-review/` 自体が既定モードのまま残る。`_makedirs_private`
  で 1 階層ずつ `os.mkdir(component, 0o700)` して回避する
- 組み込み `open()` は permission bits を指定できないため、新規ファイルの作成は
  `os.open(..., 0o600)` + `os.fdopen` で行う (`write_private` / `locked_file` 内)
- **既存ファイル/ディレクトリの権限は変更しない** (旧版が既定 umask で作ったものは
  この関数を呼んだだけでは締まらない)。ディレクトリの retrofit は `harden_dir` を
  各 hook の periodic GC (Stop 契機) から呼ぶこと。個々のファイルまでは retrofit
  しない — 親ディレクトリを 0o700 にすれば他ユーザーは traversal 自体ができず、
  ファイル単体の mode に関わらず読めなくなるため (POSIX のディレクトリ権限の性質)
"""
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO


def _makedirs_private(path: str) -> None:
    """path までの各階層を 0o700 で作成する (既存の祖先には触らない)。

    `os.makedirs(path, mode=0o700)` は最後の階層にしか mode を適用しないため、
    共有 `$TMPDIR` 上で中間ディレクトリが既定 umask (0o755 相当) のまま残りうる
    (モジュール docstring 参照)。1 階層ずつ確認しながら作ることで、新規作成分は
    すべて 0o700 にする。
    """
    if not path or os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and parent != path:
        _makedirs_private(parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass


def harden_dir(path: str) -> None:
    """既存ディレクトリの権限が 0o700 より緩ければ締め直す (旧版からの retrofit)。

    共有 `$TMPDIR` では他ユーザー所有のディレクトリを掴む可能性があるため、
    chmod 失敗 (`PermissionError` 等) は fail-open で無視する。
    """
    try:
        if os.path.isdir(path) and os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path: str, content: str) -> None:
    """新規ファイルを 0o600 で作成して content を書き込む (既存ファイルは truncate)。

    親ディレクトリも `_makedirs_private` で 0o700 に作る。state ファイル以外
    (Bash スナップショット・レビュー結果の参照コピー) が使う、flock を要らない
    単発書込み用のヘルパー。
    """
    parent = os.path.dirname(path)
    if parent:
        _makedirs_private(parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


@contextmanager
def locked_file(path: str) -> Iterator[IO[str]]:
    """path を read+write で開き (無ければ 0o600 で新規作成)、排他ロックを取って
    file object を yield する。

    親ディレクトリは `_makedirs_private` で 0o700 に作る。OSError は呼び出し側に
    伝播させる (fail-open の扱いは hook ごとに決める)。読み書きは `read_all` /
    `rewrite` で行う。

    以前は組み込み `open(path, "a+")` を使っていた (permission bits を指定できず
    既定 umask で作られていた)。`O_CREAT` のみ (`a+` の「書込は常に末尾」という
    性質は使わない) にしても機能は変わらない — 呼び出し側 (`read_all` / `rewrite`)
    は必ず明示的に `seek()` してから読み書きするため。
    """
    parent = os.path.dirname(path)
    if parent:
        _makedirs_private(parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_all(f: IO[str]) -> str:
    f.seek(0)
    return f.read()


def rewrite(f: IO[str], content: str) -> None:
    """ファイル全体を content で置き換える (seek → truncate → write → flush)。"""
    f.seek(0)
    f.truncate()
    f.write(content)
    f.flush()
