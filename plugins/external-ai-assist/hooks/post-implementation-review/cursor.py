"""Cursor による実装直後の差分レビュー。

git diff 本文をプロンプト末尾に埋め込んで cursor agent の --print で渡す。
Cursor がコードベース全体を参照しながら影響範囲・リグレッションリスクを評価する。

起動は `_common.subproc` 経由 (独自 process group + timeout + 残出力の読み捨て)。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import cursorcli, subproc

NAME = cursorcli.NAME
TIMEOUT_SEC = 600
MAX_OUTPUT_BYTES = 16000

_PROMPT_FILE = Path(__file__).parent / "prompts" / "post-implementation-cursor.md"


def is_available() -> bool:
    return cursorcli.is_available()


def review(diff_text: str) -> str | None:
    """Cursor で差分をレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = (
        f"{template}\n\n---\n\n## レビュー対象 git diff\n\n```diff\n{diff_text}\n```"
    )
    return subproc.run_for_output(
        cursorcli.review_argv(full_prompt),
        timeout_sec=TIMEOUT_SEC,
        max_output_chars=MAX_OUTPUT_BYTES,
    )
