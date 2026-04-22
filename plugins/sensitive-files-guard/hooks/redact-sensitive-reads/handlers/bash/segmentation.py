"""Bash command の quote-aware 分割 / hard-stop 検出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。文字列処理のみ。
"""
from __future__ import annotations

from handlers.bash.constants import _HARD_STOP_CHARS


def _has_hard_stop(command: str) -> bool:
    """動的評価 / 入力リダイレクト / グループ化 chars が含まれるか。"""
    return any(c in _HARD_STOP_CHARS for c in command)


def _split_command_on_operators(command: str) -> list[str]:
    """quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` でセグメントに分割。

    クォート内の演算子は区切らない (``echo "a && b"`` は 1 セグメント)。

    ダブルクォート内のバックスラッシュエスケープは Bash 仕様どおり数える:
    直前の連続バックスラッシュが **偶数個** なら ``"`` はエスケープ**されていない**
    (= クォートを閉じる)、**奇数個** ならエスケープされている (= クォート内に留まる)。
    シングルクォートは Bash 仕様上エスケープ不可なので ``'`` 単発で常に閉じる。
    """
    segments: list[str] = []
    buf: list[str] = []
    bs_run = 0
    i = 0
    in_single = False
    in_double = False
    n = len(command)
    while i < n:
        c = command[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(c)
            if c == '"' and bs_run % 2 == 0:
                in_double = False
                bs_run = 0
            elif c == "\\":
                bs_run += 1
            else:
                bs_run = 0
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            bs_run = 0
            buf.append(c)
            i += 1
            continue
        # 2 文字演算子: && / ||
        if c in "&|" and i + 1 < n and command[i + 1] == c:
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        # 1 文字区切り: ; | \n
        if c in ";|\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if buf:
        segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]
