"""Bash command の quote-aware 分割 / hard-stop 検出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。文字列処理のみ。

0.18.0 で ``_has_hard_stop`` を quote-aware にした際、2 つの scanner
(``_split_command_on_operators`` / ``_has_hard_stop``) の字句状態が一致して
いないと guard が落ちることが review で判明した (クォート外の ``\\'`` /
コメント / 行継続)。両関数は **同じ規則** で字句状態 (quote / escape / comment /
単語先頭) を進める。片方だけ直さないこと。
"""
from __future__ import annotations

from handlers.bash.constants import _HARD_STOP_CHARS

# 単語区切り: この直後は「単語の先頭」= ``#`` がコメントを開始できる位置。
# Bash 上は ``(`` ``)`` ``{`` ``}`` も単語境界だが hard-stop で先に ask に倒れる
# ため含めない (検出範囲は Bash より **狭く** 保つ: コメントでないものを
# コメント扱いすると実コマンドを落とし guard が落ちる。逆は ask に倒れるだけ)。
_WORD_BREAKS = frozenset(" \t\n;|&")


def _skip_comment(command: str, i: int) -> int:
    """コメント開始位置 ``i`` から改行 (exclusive) までを読み飛ばした位置を返す。"""
    j = command.find("\n", i)
    return len(command) if j < 0 else j


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
    - **Bash コメント (単語先頭の ``#`` 〜 行末) はクォート状態を変えない**
      (0.18.0 review)。``echo ok # ' {`` の ``'`` を quote 開始と誤認すると、
      続く行の ``cat .env`` がクォート内扱いになり hard-stop が解除されて
      しまう。コメント内の文字は hard-stop 判定から除外するが、``\\r`` だけは
      コメント内でも hard-stop (表示偽装 guard はコメントでも成立する)
    - **「単語先頭」は直前文字ではなく字句状態で判定**する。``\\<newline>``
      (行継続) は Bash が先に取り除くため単語状態を変えない:
      ``echo safe\\<newline>#joined; cat .env`` は ``safe#joined`` の 1 単語で
      コメントではない (0.18.0 review)

    シングルクォート内は Bash にとって不活性だが、**呼び出されるプログラムに
    とっては不活性ではない** (``awk 'BEGIN { system("cat .env") }'``)。その
    扱いは ``handlers.bash.interpreters`` が operand scan の後に行う。

    ``_split_command_on_operators`` と字句状態の持ち方を **完全に揃える**。
    どちらが desync しても guard は落ちる: 分割側が ``\\'`` を quote 開始と
    誤認すると ``echo \\' ; cat .env ; echo '{'`` が 1 segment に潰れ、
    こちらは ``'{'`` をクォート内と見て False を返すため、先頭 token ``echo``
    の metadata-only 経路で ``.env`` read が素通りする (0.18.0 review で修正)。
    「シングルクォート内と見なす範囲」は Bash より広くならないようにする。
    """
    in_single = False
    in_double = False
    bs_run = 0
    word_start = True
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
            nxt = command[i + 1] if i + 1 < n else ""
            if nxt == "\n":
                # 行継続: 対を落とすだけで単語状態は変えない。
                i += 2
                continue
            # 次の 1 文字を literal 化する (quote は開かない)。
            if nxt in _HARD_STOP_CHARS:
                return True
            word_start = False
            i += 2
            continue
        if c == "#" and word_start:
            # コメントは Bash に解釈されない = quote 状態も hard-stop も変えない。
            # ただし CR は表示偽装 guard なのでコメント内でも hard-stop。
            j = _skip_comment(command, i)
            if "\r" in command[i:j]:
                return True
            i = j
            continue
        if c == "'":
            in_single = True
            word_start = False
            i += 1
            continue
        if c == '"':
            in_double = True
            bs_run = 0
            word_start = False
            i += 1
            continue
        if c in _HARD_STOP_CHARS:
            return True
        word_start = c in _WORD_BREAKS
        i += 1
    return False


def _split_command_on_operators(command: str) -> list[str]:
    """quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` でセグメントに分割。

    クォート内の演算子は区切らない (``echo "a && b"`` は 1 セグメント)。

    ダブルクォート内のバックスラッシュエスケープは Bash 仕様どおり数える:
    直前の連続バックスラッシュが **偶数個** なら ``"`` はエスケープ**されていない**
    (= クォートを閉じる)、**奇数個** ならエスケープされている (= クォート内に留まる)。
    シングルクォートは Bash 仕様上エスケープ不可なので ``'`` 単発で常に閉じる。

    クォート外のバックスラッシュは次の 1 文字を literal 化する (``\\'`` は quote を
    開かない / ``\\;`` は区切らない / ``\\<newline>`` は行継続で両文字を落とし、
    単語状態は変えない)。
    Bash コメント (クォート外・単語先頭の ``#`` 〜 行末) は segment から落とす
    (Bash が解釈しない文字列を shlex に渡さない)。改行は区切りとして残す。
    ``\\r`` 入りのコメントだけは丸ごと segment に残し、``_has_hard_stop`` の
    表示偽装 guard に到達させる。
    ``_has_hard_stop`` と同じ字句状態を保つこと (desync すると guard が落ちる)。
    """
    segments: list[str] = []
    buf: list[str] = []
    bs_run = 0
    i = 0
    in_single = False
    in_double = False
    word_start = True
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
        # --- クォート外 ---
        if c == "\\":
            nxt = command[i + 1] if i + 1 < n else ""
            if nxt == "\n":
                # 行継続: 両文字を落とし、単語状態は変えない (Bash 仕様)。
                i += 2
                continue
            # 次の 1 文字を literal 化する (quote も開かず演算子にもならない)。
            # ``_has_hard_stop`` と同じ字句状態。ここが desync すると
            # ``echo \\' ; cat .env ; echo '{'`` が 1 segment に潰れ、hard-stop
            # 側は ``'{'`` をクォート内と見て False を返し、先頭 token ``echo``
            # の metadata-only 経路で ``.env`` read が素通りする (0.18.0 review)。
            buf.append(c)
            if nxt:
                buf.append(nxt)
            word_start = False
            i += 2
            continue
        if c == "#" and word_start:
            j = _skip_comment(command, i)
            if "\r" in command[i:j]:
                # CR 入りコメントは落とさず丸ごと残し (末尾 strip で CR が消えない
                # ように)、``_has_hard_stop`` の表示偽装 guard に到達させる。
                buf.append(command[i:j])
            i = j
            continue
        if c == "'":
            in_single = True
            word_start = False
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            bs_run = 0
            word_start = False
            buf.append(c)
            i += 1
            continue
        # 2 文字演算子: && / ||
        if c in "&|" and i + 1 < n and command[i + 1] == c:
            segments.append("".join(buf))
            buf = []
            word_start = True
            i += 2
            continue
        # 1 文字区切り: ; | \n
        if c in ";|\n":
            segments.append("".join(buf))
            buf = []
            word_start = True
            i += 1
            continue
        buf.append(c)
        word_start = c in _WORD_BREAKS
        i += 1
    if buf:
        segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]
