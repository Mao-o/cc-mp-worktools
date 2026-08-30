"""git 作業ツリーの root 解決 (複数 hook で共有)。

post-implementation-review (差分のパス正規化。実装は元々ここにあった) と
exitplan-review (cursor/codex を起動する cwd の解決) の両方が「hook の cwd から
git 作業ツリーの root を割り出す」処理を必要とするため、ここに 1 箇所だけ実装し、
`post-implementation-review/gitscan.py` の同名関数はここへ委譲する (2 箇所に
同じ realpath 対応ロジックを複製すると、片方だけ直して片方に穴が残る事故になる)。
"""
from __future__ import annotations

import os
import subprocess

# `git rev-parse --show-toplevel` 1 回分の timeout。post-implementation-review 側の
# `gitscan.REV_PARSE_TIMEOUT_SEC` と同じ値だが、こちらは exitplan-review 単体でも
# 使うため独立した定数として持つ。
TIMEOUT_SEC = 2


def worktree_root(cwd: str) -> str | None:
    """cwd を含む git 作業ツリーの root を realpath で返す。git 外なら None。

    realpath を通すのは必須。macOS では `/tmp` が `/private/tmp` の symlink で、
    hook payload の cwd と `git rev-parse --show-toplevel` の出力で表記が割れる。
    素の startswith 比較だと全パスが「作業ツリー外」に落ちる。
    """
    if not cwd:
        return None
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            timeout=TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if res.returncode != 0:
        return None
    top = res.stdout.decode("utf-8", errors="replace").strip()
    return os.path.realpath(top) if top else None
