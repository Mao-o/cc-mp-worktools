"""Cursor による実装直後の差分レビュー。

git diff 本文をプロンプト末尾に埋め込んで cursor agent の -p で渡す。
Cursor がコードベース全体を参照しながら影響範囲・リグレッションリスクを評価する。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

NAME = "cursor"
TIMEOUT_SEC = 600
MAX_OUTPUT_BYTES = 16000

_PROMPT_FILE = Path(__file__).parent / "prompts" / "post-implementation-cursor.md"


def is_available() -> bool:
    return shutil.which("cursor") is not None


def review(diff_text: str) -> str | None:
    """Cursor で差分をレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = (
        f"{template}\n\n---\n\n## レビュー対象 git diff\n\n```diff\n{diff_text}\n```"
    )

    try:
        result = subprocess.run(
            ["cursor", "agent", "--trust", "--print", "--mode", "plan", full_prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    if not output:
        return None

    return output[:MAX_OUTPUT_BYTES]
