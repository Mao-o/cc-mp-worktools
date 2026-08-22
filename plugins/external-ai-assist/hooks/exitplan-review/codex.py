"""Codex によるプランレビュー (要件・アーキ観点)。

プロンプト (prompts/planning-codex.md) を引数、プラン本文を stdin で渡す。

起動は `_common.subproc` 経由 (独自 process group + timeout + 残出力の読み捨て)。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import subproc

NAME = "codex"
BINARY = "codex"
TIMEOUT_SEC = 1500
MAX_OUTPUT_BYTES = 16000

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-codex.md"


def is_available() -> bool:
    return subproc.cli_available(BINARY)


def review(plan_text: str) -> str | None:
    """Codex でプランをレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        prompt = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    return subproc.run_for_output(
        [BINARY, "exec", "-s", "read-only", "--ephemeral", prompt],
        timeout_sec=TIMEOUT_SEC,
        input_text=plan_text,
        max_output_chars=MAX_OUTPUT_BYTES,
    )
