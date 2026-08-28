#!/usr/bin/env python3
"""純粋関数: tool_input から「その編集がファイルを大きくしたか」を分類する。

`PostToolUse` は編集の**後**に発火するため、ファイル自体からは編集前の行数が
分からない。`Edit` は `old_string` / `new_string` を持つので、置換によって
行数がどちら向きに動いたかだけは envelope から厳密に決まる。

ファイルシステムアクセスは行わない (state.py / source.py の責務)。
"""
from __future__ import annotations

GREW = "grew"
NOT_GREW = "not_grew"
UNKNOWN = "unknown"


def classify_growth(tool_name: str, tool_input: dict) -> str:
    """``GREW`` / ``NOT_GREW`` / ``UNKNOWN`` のいずれかを返す。

    - ``Edit``: ``new_string`` と ``old_string`` の改行数の差で判定する。
      ``replace_all`` は考慮不要 — 置換 1 箇所あたりの差分が同じ符号で
      N (>= 1) 箇所に適用されるため、合計の符号は 1 箇所あたりの符号と一致する。
      差分 0 (typo 修正・同じ行数のリファクタ) は ``NOT_GREW`` に倒す。
    - ``Write``: 編集前の内容が envelope に無いため ``UNKNOWN``。同一セッション
      内に直近の行数記録があれば state.py 側でそれと突き合わせる。
    - フィールドが欠けている / 型が違う場合も ``UNKNOWN``。envelope の形が将来
      変わったときに通知が黙って全滅するより、0.1.0 と同じ挙動に戻す方を選ぶ。
    """
    if tool_name != "Edit":
        return UNKNOWN

    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return UNKNOWN

    delta = new.count("\n") - old.count("\n")
    return GREW if delta > 0 else NOT_GREW
