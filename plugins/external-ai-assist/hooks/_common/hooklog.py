"""stderr への hook ログ。

exit 0 で終わる hook の stderr は transcript に出ず debug log にだけ残る (公式 docs)。
利用者向けの表示ではなく `claude --debug` での追跡用。
"""
import sys
from collections.abc import Callable


def make_logger(prefix: str) -> Callable[[str], None]:
    """`[<prefix>] msg` を stderr に書く関数を返す。

    `sys.stderr` は呼び出しのたびに解決する (テストが差し替えられるように束縛しない)。
    """

    def log(msg: str) -> None:
        print(f"[{prefix}] {msg}", file=sys.stderr)

    return log
