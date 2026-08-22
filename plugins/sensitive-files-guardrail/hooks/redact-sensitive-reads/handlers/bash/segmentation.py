"""Bash command の quote-aware 分割 / hard-stop 検出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。文字列処理のみ。

0.18.0 で ``_has_hard_stop`` を quote-aware にした際、2 つの scanner
(``_split_command_on_operators`` / ``_has_hard_stop``) の字句状態が一致して
いないと guard が落ちることが review で繰り返し判明した (クォート外の ``\\'`` /
コメント / 行継続 / 行継続で合成される演算子)。review R7 で **両 scanner が共有
する 1 つの lexer** (``_lex``) に統一した: 行継続 ``\\<newline>`` を除去し、各文字に
quote / escape / comment の字句状態を付けた列を作り、分割と hard-stop 判定は
その列だけを見る。字句規則を直すときは ``_lex`` だけを直すこと。
"""
from __future__ import annotations

from handlers.bash.constants import _HARD_STOP_CHARS

# 単語区切り: この直後は「単語の先頭」= ``#`` がコメントを開始できる位置。
# Bash 上は ``(`` ``)`` ``{`` ``}`` も単語境界だが hard-stop で先に ask に倒れる
# ため含めない (検出範囲は Bash より **狭く** 保つ: コメントでないものを
# コメント扱いすると実コマンドを落とし guard が落ちる。逆は ask に倒れるだけ)。
_WORD_BREAKS = frozenset(" \t\n;|&")

# ``_lex`` の字句状態
_ST_PLAIN = "p"     # クォート外の通常文字 (演算子・空白・単語・クォート区切り自体)
_ST_ESCAPED = "e"   # クォート外のバックスラッシュエスケープ (``\\`` と次の 1 文字)
_ST_SINGLE = "s"    # シングルクォート内 (Bash は一切展開しない)
_ST_DOUBLE = "d"    # ダブルクォート内 (展開される)
_ST_COMMENT = "c"   # コメント (``#`` 〜 行末。改行自体は plain)


def _lex(command: str) -> list[tuple[str, str]]:
    """command を「(文字, 字句状態)」の列に展開する。行継続は除去する。

    Bash 仕様 (実測 2026-08-22, bash 5):

    - ``\\<newline>`` (行継続) は **クォート外とダブルクォート内で除去** され、
      単語状態も演算子も「除去後の隣接文字」で決まる (``ls &\\<nl>& cat`` は
      ``ls && cat``、``echo safe\\<nl>#joined`` は 1 単語)。バックスラッシュ自体が
      エスケープされていれば (``\\\\<nl>``) 行継続ではない
    - シングルクォート内の ``\\<newline>`` は literal (除去しない)
    - **コメント内の行末 ``\\`` は行継続にならない** (コメントは改行で終わり、
      次行は実行される)
    - クォート外のバックスラッシュは次の 1 文字を literal 化する (``\\'`` は
      quote を開かない / ``\\;`` は区切らない)。``\\$`` ``\\(`` 自体は hard-stop
      として数え続ける (``_has_hard_stop`` 側の非対称)
    - ``#`` は単語先頭 (直前が空白 / ``;`` / ``|`` / ``&`` / 文字列先頭) でのみ
      コメントを開始する。クォートを閉じた直後 (``'a'#b``) は単語の続き
    - ダブルクォート内の ``\\"`` は Bash 仕様どおり連続バックスラッシュの偶奇で判定
    """
    out: list[tuple[str, str]] = []
    n = len(command)
    i = 0
    in_single = False
    in_double = False
    in_comment = False
    bs_run = 0
    word_start = True
    while i < n:
        c = command[i]
        if in_comment:
            if c == "\n":
                in_comment = False
                out.append((c, _ST_PLAIN))
                word_start = True
            else:
                out.append((c, _ST_COMMENT))
            i += 1
            continue
        if in_single:
            if c == "'":
                in_single = False
                out.append((c, _ST_PLAIN))
            else:
                out.append((c, _ST_SINGLE))
            i += 1
            continue
        if in_double:
            if c == "\\" and bs_run % 2 == 0 and i + 1 < n and command[i + 1] == "\n":
                i += 2  # 行継続 (ダブルクォート内でも除去)
                continue
            if c == '"' and bs_run % 2 == 0:
                in_double = False
                bs_run = 0
                out.append((c, _ST_PLAIN))
                i += 1
                continue
            bs_run = bs_run + 1 if c == "\\" else 0
            out.append((c, _ST_DOUBLE))
            i += 1
            continue
        # --- クォート外 ---
        if c == "\\":
            nxt = command[i + 1] if i + 1 < n else ""
            if nxt == "\n":
                i += 2  # 行継続: 両文字を落とし、単語状態は変えない
                continue
            out.append((c, _ST_ESCAPED))
            if nxt:
                out.append((nxt, _ST_ESCAPED))
            word_start = False
            i += 2
            continue
        if c == "#" and word_start:
            in_comment = True
            out.append((c, _ST_COMMENT))
            i += 1
            continue
        if c == "'":
            in_single = True
            word_start = False
        elif c == '"':
            in_double = True
            bs_run = 0
            word_start = False
        else:
            word_start = c in _WORD_BREAKS
        out.append((c, _ST_PLAIN))
        i += 1
    return out


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
    - **``\\r`` はクォート状態・コメントを問わず hard-stop**。CR は展開ではなく
      端末表示偽装の guard なので「展開されない = 安全」の理屈が当てはまらない
    - **コメント内の文字は hard-stop 判定から除外** (``\\r`` を除く)

    シングルクォート内は Bash にとって不活性だが、**呼び出されるプログラムに
    とっては不活性ではない** (``awk 'BEGIN { system("cat .env") }'`` /
    ``find -exec sh -c '…'``)。その扱いは ``handlers.bash.interpreters`` が
    operand scan の後に行う (緩和は inert な first token に限定)。
    """
    for c, st in _lex(command):
        if c == "\r":
            return True
        if st in (_ST_PLAIN, _ST_ESCAPED, _ST_DOUBLE) and c in _HARD_STOP_CHARS:
            return True
    return False


def _has_quoted_hard_stop(segment: str) -> bool:
    """``_has_hard_stop`` が False の segment に、シングルクォート内の hard-stop
    char が残っているか (= 0.18.0 の quote-aware 化で緩んだ segment か)。

    前提: ``_has_hard_stop(segment)`` が False であること。その条件下では、
    ダブルクォート内・クォート外 (エスケープ込み) の hard-stop char は既に True
    を返しているので、残る hard-stop char はシングルクォート内のものだけ
    (コメントは splitter が落としている)。呼び出し側はこれが True のときだけ
    ``interpreters._quoted_hard_stop_reason`` を適用し、inert でない first token
    やクォート文字列を別のインタプリタに渡す形 (``find -exec sh -c '...'``) を
    ask に戻す (0.18.0 review R5 / R6)。
    """
    return any(c in _HARD_STOP_CHARS for c in segment)


def _split_command_on_operators(command: str) -> list[str]:
    """quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` でセグメントに分割。

    ``_lex`` が付けた字句状態だけを見る: クォート内・エスケープ済みの演算子は
    区切らない (``echo "a && b"`` / ``echo \\; x`` は 1 セグメント)。行継続は
    ``_lex`` が除去済みなので、継続をまたいで合成される ``&&`` / ``||``
    (``ls &\\<nl>& cat .env``) も正しく区切る (review R7)。

    Bash コメントは segment から落とす (Bash が解釈しない文字列を shlex に
    渡さない)。改行は区切りとして残す。``\\r`` 入りのコメントだけは丸ごと
    segment に残し、``_has_hard_stop`` の表示偽装 guard に到達させる。
    """
    lx = _lex(command)
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(lx)
    while i < n:
        c, st = lx[i]
        if st == _ST_COMMENT:
            j = i
            while j < n and lx[j][1] == _ST_COMMENT:
                j += 1
            text = "".join(ch for ch, _ in lx[i:j])
            if "\r" in text:
                # CR 入りコメントは落とさず丸ごと残し (末尾 strip で CR が消えない
                # ように)、``_has_hard_stop`` の表示偽装 guard に到達させる。
                buf.append(text)
            i = j
            continue
        if st == _ST_PLAIN:
            # 2 文字演算子: && / ||
            if c in "&|" and i + 1 < n and lx[i + 1] == (c, _ST_PLAIN):
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
