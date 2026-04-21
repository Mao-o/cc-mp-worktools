"""Codex によるプランレビュー (要件・アーキ観点)。

<stdin> にプラン本文、プロンプトは prompts/planning-codex.md を読み込む。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

NAME = "codex"
TIMEOUT_SEC = 1500
MAX_OUTPUT_BYTES = 16000

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-codex.md"


def is_available() -> bool:
    return shutil.which("codex") is not None


def review(plan_text: str) -> str | None:
    """Codex でプランをレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        prompt = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        result = subprocess.run(
            ["codex", "exec", "-s", "read-only", "--ephemeral", prompt],
            input=plan_text,
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
