"""codex backend (CLI の起動と結果の分類だけを持つ)。

`codex exec -s read-only --ephemeral -` で **read-only 起動**し、プロンプトは stdin 一本で
渡す (引数と piped stdin の併用挙動に依存しない。経緯は `exitplan-review/codex.py`)。

存在確認は `shutil.which("codex")` のみ。cursor と違って「IDE ランチャーと同名」という
曖昧さが無いため probe しない。**未ログイン状態の検出は cursor と同じく未実装** — 実機で
失敗時の文言を確認できていないので、推測でパターンを書いて正常な出力を誤判定するより、
起動して失敗させる (= `failed` として次の候補へ回る) ほうを選ぶ。

プロンプト・timeout の既定値・出力の切り詰めは hook 固有なので持たない。
"""
from __future__ import annotations

from .. import subproc
from . import base

NAME = "codex"

BINARY = "codex"

#: 起動 argv の固定部分。`-s read-only` と `--ephemeral` が読み取り専用の担保、
#: 末尾の `-` が「プロンプトは stdin から読む」指定。
EXEC_ARGS = ("exec", "-s", "read-only", "--ephemeral", "-")


def is_available(deadline: float | None = None) -> bool:
    """codex CLI を起動できるか。`deadline` は受け取るが使わない (probe しないため)。"""
    return subproc.cli_available(BINARY)


def run(prompt_text: str, *, cwd: str | None = None, timeout: float) -> base.Result:
    """read-only でプロンプトを stdin から 1 回実行し、結果を分類して返す。"""
    return base.from_captured(
        subproc.run_captured(
            [BINARY, *EXEC_ARGS],
            timeout_sec=timeout,
            input_text=prompt_text,
            cwd=cwd,
        )
    )
