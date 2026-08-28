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


def _unterminated_tail(text: str) -> int:
    """``splitlines()`` が数える「改行で終わらない最終行」の分 (0 か 1)。"""
    return 1 if text and not text.endswith("\n") else 0


def classify_growth(tool_name: str, tool_input: dict, text: str | None = None) -> str:
    """``GREW`` / ``NOT_GREW`` / ``UNKNOWN`` のいずれかを返す。

    - ``Edit``: ``new_string`` と ``old_string`` の改行数の差で判定する。
      ``replace_all`` は考慮不要 — 置換 1 箇所あたりの差分が同じ符号で
      N (>= 1) 箇所に適用されるため、合計の符号は 1 箇所あたりの符号と一致する。
      差分 0 (typo 修正・同じ行数のリファクタ) は ``NOT_GREW`` に倒す。
    - ``Write``: 編集前の内容が envelope に無いため ``UNKNOWN``。同一セッション
      内に直近の行数記録があれば state.py 側でそれと突き合わせる。
    - フィールドが欠けている / 型が違う場合も ``UNKNOWN``。envelope の形が将来
      変わったときに通知が黙って全滅するより、0.1.0 と同じ挙動に戻す方を選ぶ。

    ``text`` (編集後のファイル全文) を渡すと、**ファイル末尾の置換**を補正する。
    行数は ``splitlines()`` で数えるため「改行で終わらない最終行」が 1 行として
    数えられるが、改行の個数はこれを含まない。したがって末尾の置換が改行終端の
    有無を変える場合だけ、改行数の差と行数の差がずれる:

    - 末尾の ``"foo\\n"`` → ``"foo\\nbar"``: 改行数は同じだが 1 行増える
    - 末尾の ``"foo"`` → ``"foo\\n"``: 改行数は 1 増えるが行数は変わらない

    末尾以外の置換では改行数の差がそのまま行数の差になるため補正は不要。

    補正は「置換が末尾で起きたと**確定できる**とき」だけ適用する。``new_string``
    が編集後の全文にちょうど 1 箇所しか現れず、それが末尾にある場合に限る。
    ``text.endswith(new_string)`` だけでは足りない — 別の場所を置換した結果
    たまたま同じ文字列が末尾にもある場合に、誤った補正がかかるため。
    確定できないときは改行数の差による近似に戻す (0.2.0 と同じ精度)。
    """
    if tool_name != "Edit":
        return UNKNOWN

    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return UNKNOWN

    delta = new.count("\n") - old.count("\n")

    if isinstance(text, str) and new:
        tail_start = len(text) - len(new)
        # find == rfind == tail_start で「出現がちょうど 1 箇所、かつ末尾」を確定
        # する。endswith だけだと、前方を置換した結果たまたま同じ文字列が末尾に
        # もある場合に誤って補正がかかる。
        if tail_start >= 0 and text.find(new) == tail_start == text.rfind(new):
            before = text[:tail_start] + old
            delta += _unterminated_tail(text) - _unterminated_tail(before)

    return GREW if delta > 0 else NOT_GREW
