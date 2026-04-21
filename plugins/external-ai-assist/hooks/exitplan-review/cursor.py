"""Cursor によるプランレビュー (既存コードベース整合観点、primary)。

プラン本文はプロンプト末尾に埋め込んで cursor agent の -p で渡す。
Cursor はセマンティック検索でコードベース全体を参照しながらレビューする。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

NAME = "cursor"
TIMEOUT_SEC = 600
MAX_OUTPUT_BYTES = 16000

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-cursor.md"


def is_available() -> bool:
    return shutil.which("cursor") is not None


def review(plan_text: str) -> str | None:
    """Cursor でプランをレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        template = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    full_prompt = (
        f"{template}\n\n---\n\n## レビュー対象プラン\n\n{plan_text}"
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
