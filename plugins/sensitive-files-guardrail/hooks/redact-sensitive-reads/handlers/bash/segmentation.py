"""Bash command の quote-aware 分割 / hard-stop 検出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。文字列処理のみ。
"""
from __future__ import annotations

from handlers.bash.constants import _HARD_STOP_CHARS


def _has_hard_stop(command: str) -> bool:
    """動的評価 / 入力リダイレクト / グループ化 chars が含まれるか (quote-aware)。

    0.11.0 (F1) 以降、呼び出し側 (``bash_handler.handle``) は **segment 単位で
    再評価** する前提。command 全体に対して呼ぶと ``cat .env | sed 's/(=)/X/'``
    のような複合で sed segment の ``(`` が原因で全体 ask に倒れ、autonomous で
    ``cat .env`` 系が素通りする (0.10.0 までの挙動)。``_split_command_on_operators``
    で分割した各 segment ごとに ``_has_hard_stop`` を呼ぶこと。

    0.18.0: **シングルクォート内の hard-stop char を無視** する。Bash はシングル
    クォート内を一切展開しないため ``awk '{print}' .env`` の ``{`` ``}`` ``$`` は
    静的解析を妨げない。これで最頻形の ``awk`` / ``sed`` が operand scan に到達
    する (0.17.0 で opaque から外した際に残っていた穴を塞ぐ)。

    緩めない部分 (誤 deny 漏れ防止のため意図的に strict):

    - **ダブルクォート内は従来どおり hard-stop**。``"$(cat .env)"`` は展開される
    - **クォート外のバックスラッシュエスケープは quote を開かない**。``\\'`` は
      literal ``'`` であってシングルクォート開始ではない (Bash 仕様)。ここを
      取り違えると ``cat \\'$(cat .env)\\'`` で guard が落ちる。一方 ``\\$``
      ``\\(`` 自体は hard-stop として**数え続ける** (``cat <(echo \\(\\)) < .env``
      の挙動不変を担保する非対称)
    - **``\\r`` はクォート状態を問わず hard-stop**。CR は展開ではなく端末表示
      偽装の guard なので「展開されない = 安全」の理屈が当てはまらない

    ``_split_command_on_operators`` と字句状態の持ち方を揃えてあるが、こちらは
    desync すると guard が落ちる (分割側は segment 境界がずれるだけ) ため、
    「シングルクォート内と見なす範囲」は Bash より広くならないようにする。
    """
    in_single = False
    in_double = False
    bs_run = 0
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        # CR はクォート状態に関係なく hard-stop (表示偽装 guard)。
        if c == "\r":
            return True
        if in_single:
            # シングルクォート内は展開されない = hard-stop 判定から除外。
            # Bash 仕様上エスケープ不可なので ``'`` 単発で常に閉じる。
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == '"' and bs_run % 2 == 0:
                in_double = False
                bs_run = 0
                i += 1
                continue
            if c == "\\":
                bs_run += 1
            else:
                bs_run = 0
            if c in _HARD_STOP_CHARS:
                return True
            i += 1
            continue
        # --- クォート外 ---
        if c == "\\":
            # 次の 1 文字を literal 化する (quote は開かない)。
            nxt = command[i + 1] if i + 1 < n else ""
            if nxt in _HARD_STOP_CHARS:
                return True
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            bs_run = 0
            i += 1
            continue
        if c in _HARD_STOP_CHARS:
            return True
        i += 1
    return False


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
