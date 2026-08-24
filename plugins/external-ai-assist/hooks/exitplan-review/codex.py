"""Codex によるプランレビュー (要件・アーキ観点)。

プロンプト (prompts/planning-codex.md) を引数、プラン本文を stdin で渡す。

起動は `_common.subproc` 経由 (独自 process group + timeout + 残出力の読み捨て)。
`_common` は `__main__.py` (テストでは `tests/_testutil.py`) が sys.path に載せる。
"""
from __future__ import annotations

from pathlib import Path

from _common import settings, subproc

NAME = "codex"
BINARY = "codex"

#: 既定の timeout。**0.6.0 で 1500 → 600 に短縮** (挙動変更)。cursor と並列に走るので
#: 承認前の待ち時間は max(cursor, codex) = 25 分 → 10 分になる。長考させたい場合は
#: `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT=1500` で従来値に戻せる。
TIMEOUT_SEC = 600

#: env で伸ばせる上限 (従来の既定値)。根拠は cursor.py の同名定数を参照。
MAX_TIMEOUT_SEC = 1500

MAX_OUTPUT_BYTES = 16000

ENV_TIMEOUT = "EXTERNAL_AI_PLAN_REVIEW_TIMEOUT"

_PROMPT_FILE = Path(__file__).parent / "prompts" / "planning-codex.md"


def is_available() -> bool:
    return subproc.cli_available(BINARY)


def timeout_sec() -> float:
    """実効 timeout。未設定なら `TIMEOUT_SEC`、`MAX_TIMEOUT_SEC` で clamp。"""
    return settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)


def review(plan_text: str) -> str | None:
    """Codex でプランをレビューし、整形済み結果を返す。失敗時は None。"""
    try:
        prompt = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    return subproc.run_for_output(
        [BINARY, "exec", "-s", "read-only", "--ephemeral", prompt],
        timeout_sec=timeout_sec(),
        input_text=plan_text,
        max_output_chars=MAX_OUTPUT_BYTES,
    )
