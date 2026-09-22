"""外部 AI backend の registry (名前 → module の解決と、名前リストの絞り込み)。

`__init__.py` から再 export しているので、呼び出し側は `from _common import backends`
だけでよい (`backends.ALL` / `backends.select(...)` / `backends.cursor`)。

## backend の共通 interface

| 属性 | 役割 |
|---|---|
| `NAME` | 設定 (env) で書く名前。小文字・一意 |
| `is_available(deadline=None) -> bool` | その CLI を起動できるか (検出 probe の締切を渡せる) |
| `run(prompt_text, *, cwd, timeout) -> base.Result` | 1 回起動し `ok` / `failed` / `limit` に分類 |

**registry が持つのは「CLI の起動と結果の分類」だけ**。プロンプト本文・timeout の
既定値と上限・出力の切り詰め・レビュー結果の解釈 (REVIEW_CLEAN 判定) は hook 固有なので
各 hook 側に残す。

## backend を増やすとき

`_common/backends/<name>.py` を上の 3 つを満たす形で追加し、`ALL` に足す。**それだけでは
送信先は増えない** — 差分レビューの既定は `post-implementation-review/selection.py` の
`DEFAULT_BACKENDS` (= cursor のみ) で、プランレビューの既定は
`exitplan-review/__main__.py` の `REVIEWERS` で決まる。**アップグレードしただけで新しい
送信先が増えてはならない**ので、既定に足すかどうかは常に別の判断として扱うこと。
"""
from __future__ import annotations

from . import codex, cursor

#: 宣言順が既定の優先順 (絞り込み後もこの順序を保つ)。
ALL = (cursor, codex)


def names() -> tuple[str, ...]:
    return tuple(module.NAME for module in ALL)


def get(name: str):
    """名前から backend module を引く。未知なら None。"""
    for module in ALL:
        if module.NAME == name:
            return module
    return None


def select(modules, wanted: list[str] | None) -> tuple[list, list[str]]:
    """`(選ばれた module, 未知の名前)` を返す。`wanted` が None なら `modules` 全件。

    **事前チェックと実行で別々に集合を計算してはいけない**ので、選択は必ずこの 1 か所を
    通す (exitplan-review が 0.6.0 でこのズレを踏んでいる — 1 つも走らないレビュアー集合の
    ために枠を消費し、以後のプランが「レビュー済み」扱いで素通りしていた)。

    未知の名前は**無視して呼び出し側に返す** (通知用)。既定の全件に fallback しないのは、
    タイプミス 1 つで「外したはずの backend が黙って走る」= 送信先が増える方向に倒れる
    ため。`modules` の宣言順は保つ (`wanted` の並び順では並べ替えない — 優先順は
    呼び出し側の戦略が決める)。
    """
    known = {module.NAME for module in modules}
    unknown = [name for name in (wanted or []) if name not in known]
    chosen = [m for m in modules if wanted is None or m.NAME in wanted]
    return chosen, unknown
