"""cursor agent backend (CLI の起動と結果の分類だけを持つ)。

存在確認は `_common/cursorcli.py` に**既にある検出ロジックをそのまま使う** (候補順
`cursor-agent` → `cursor`、`--version` probe の 3 値判定、キャッシュの所有者検査)。
ここで別実装を持つと、`agent` のような汎用名が候補に戻る・IDE ランチャーの除外が
緩むといった形で **送信先が増える方向**に倒れうるため、検出は 1 か所に保つ
(`cursorcli` は explore-parallel も使う)。

プロンプト・timeout の既定値・出力の切り詰めは hook 固有なので持たない
(`exitplan-review/cursor.py` / `post-implementation-review/cursor.py` 側)。
"""
from __future__ import annotations

from .. import cursorcli, subproc
from . import base

NAME = cursorcli.NAME


def is_available(deadline: float | None = None) -> bool:
    """cursor agent CLI を起動できるか。`deadline` は検出 probe の締切 (monotonic)。"""
    return cursorcli.is_available(deadline)


def run(prompt_text: str, *, cwd: str | None = None, timeout: float) -> base.Result:
    """読み取り専用 (`--mode plan`) でプロンプトを 1 回実行し、結果を分類して返す。"""
    return base.from_captured(
        subproc.run_captured(
            cursorcli.readonly_argv(prompt_text), timeout_sec=timeout, cwd=cwd
        )
    )
