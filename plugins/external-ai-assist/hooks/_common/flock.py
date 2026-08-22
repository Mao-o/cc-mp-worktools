"""flock 付き read-modify-write。

exitplan-review のマーカー (hash / count) と post-implementation-review の状態ファイルは
どちらも「a+ で開く → LOCK_EX → 全読み → 書き戻し → LOCK_UN」の同型なので、ここに寄せる。

非ブロッキングで review 実行中ずっと保持する `cursor_lock` は、ロックファイルを開けない
環境で直列化を諦める fail-open 分岐が固有なので共通化していない
(post-implementation-review/state.py)。
"""
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO


@contextmanager
def locked_file(path: str) -> Iterator[IO[str]]:
    """path を a+ で開き、排他ロックを取って file object を yield する。

    親ディレクトリは作る。OSError は呼び出し側に伝播させる (fail-open の扱いは hook ごと
    に決める)。読み書きは `read_all` / `rewrite` で行う (a+ はカーソル位置が末尾なので
    seek が要る)。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a+") as f:
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
