"""Bash command の quote-aware 分割 / hard-stop 検出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。文字列処理のみ。

0.18.0 で ``_has_hard_stop`` を quote-aware にした際、2 つの scanner
(``_split_command_on_operators`` / ``_has_hard_stop``) の字句状態が一致して
いないと guard が落ちることが review で繰り返し判明した (クォート外の ``\\'`` /
コメント / 行継続 / 行継続で合成される演算子)。review R7 で **両 scanner が共有
する 1 つの lexer** (``_lex``) に統一した: 行継続 ``\\<newline>`` を除去し、各文字に
quote / escape / comment / heredoc の字句状態を付けた列を作り、分割と hard-stop
判定はその列だけを見る。字句規則を直すときは ``_lex`` だけを直すこと。

0.22.0: heredoc (``<<`` / ``<<-``) の本文を ``_lex`` が ``_ST_HEREDOC`` にし、
segment 分割から外す (``docs/DESIGN.md`` の「``<<`` heredoc | delimiter/body は
read 対象外」に実装を合わせた)。
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
_ST_HEREDOC = "h"   # heredoc 本文 + terminator 行 (0.22.0。Bash はコマンドとして
                    # 解釈しない。terminator 行の後ろの改行は plain)

# heredoc の delimiter 語を終える文字 (Bash の単語区切り + リダイレクト /
# グループ化の演算子)。
_HEREDOC_WORD_ENDS = frozenset(" \t\n;|&<>()")


def _read_heredoc_word(
    command: str, i: int, out: list[tuple[str, str]],
) -> tuple[int, str, bool]:
    """``<<`` の直後 (index ``i``) から delimiter 語を読む。

    ``<<-`` の ``-`` (本文と terminator の先頭 tab を剥がす)、空白、そして
    ``'EOF'`` / ``"EOF"`` / ``\\EOF`` / ``E'O'F`` のクォート込みの語を解釈し、
    クォートを外した delimiter を返す。読んだ文字は全て plain として ``out`` に
    出す (segment 文字列には ``<<'PY'`` が原文のまま残り、``_has_hard_stop`` が
    ``<`` を見て ask に倒す)。

    Returns:
        ``(次の index, delimiter, tab を剥がすか)``
    """
    n = len(command)
    strip_tabs = False
    if i < n and command[i] == "-":
        strip_tabs = True
        out.append(("-", _ST_PLAIN))
        i += 1
    while i < n and command[i] in " \t":
        out.append((command[i], _ST_PLAIN))
        i += 1
    delim: list[str] = []
    while i < n:
        ch = command[i]
        if ch in _HEREDOC_WORD_ENDS:
            break
        if ch == "'":
            k = command.find("'", i + 1)
            if k < 0:
                k = n
            delim.append(command[i + 1:k])
            for x in command[i:k + 1]:
                out.append((x, _ST_PLAIN))
            i = k + 1
            continue
        if ch == '"':
            k = i + 1
            while k < n and command[k] != '"':
                if command[k] == "\\" and k + 1 < n:
                    delim.append(command[k + 1])
                    k += 2
                    continue
                delim.append(command[k])
                k += 1
            for x in command[i:k + 1]:
                out.append((x, _ST_PLAIN))
            i = k + 1
            continue
        if ch == "\\" and i + 1 < n:
            delim.append(command[i + 1])
            out.append((ch, _ST_PLAIN))
            out.append((command[i + 1], _ST_PLAIN))
            i += 2
            continue
        delim.append(ch)
        out.append((ch, _ST_PLAIN))
        i += 1
    return i, "".join(delim), strip_tabs


def _consume_heredoc_bodies(
    command: str,
    i: int,
    pending: list[tuple[str, bool]],
    out: list[tuple[str, str]],
) -> int:
    """plain な改行の直後 (index ``i``) から、pending の heredoc 本文を順に読み飛ばす。

    各 heredoc について terminator 行 (``<<-`` なら先頭 tab を剥がして比較、それ
    以外は完全一致) を探し、本文と terminator 行を ``_ST_HEREDOC`` で ``out`` に
    出す。terminator の後ろの改行は plain (segment 区切り)。

    terminator が見つからない heredoc は **heredoc として扱わない** (その位置で
    やめて残りは通常の字句解析に戻す = 0.21.x までと同じ行分割)。``$((1<<2))``
    の ``<<`` を heredoc と誤認して以降を丸ごと本文にすると、後続の
    ``cat .env`` が解析されず auto で素通りするため。本物の未終端 heredoc
    (Bash は警告付きで末尾まで本文にする) は本文の各行が segment になり従来と
    同じ false positive が出るだけ。

    Returns:
        次に字句解析を再開する index
    """
    n = len(command)
    for delim, strip_tabs in pending:
        j = i
        term_start = -1
        term_end = -1
        while j <= n:
            k = command.find("\n", j)
            end = n if k < 0 else k
            line = command[j:end]
            cmp = line.lstrip("\t") if strip_tabs else line
            if cmp == delim:
                term_start, term_end = j, end
                break
            if k < 0:
                break
            j = k + 1
        if term_start < 0:
            return i
        for ch in command[i:term_end]:
            out.append((ch, _ST_HEREDOC))
        i = term_end
        if i < n:  # terminator の後ろの改行
            out.append(("\n", _ST_PLAIN))
            i += 1
    return i


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
    - **heredoc (0.22.0)**: クォート外の ``<<`` / ``<<-`` (``<<<`` here-string は
      除く) は delimiter 語を読んで記憶し、その行の次の plain な改行から
      terminator 行までを ``_ST_HEREDOC`` にする (同じ行に複数あれば順番に)。
      Bash は本文をコマンドとして解釈しないので segment にも hard-stop 判定にも
      載せない。演算子と delimiter 語自体は plain のまま残るので、その segment は
      ``<`` で従来どおり hard-stop (ask_or_allow)。terminator が無い ``<<`` は
      heredoc として扱わない (``_consume_heredoc_bodies``)
    - **算術式の中の ``<<`` はシフト演算子** (Codex R1 P1): クォート外の ``((``
      (``$((`` 算術展開 / ``((`` 算術コマンド) から対応する ``))`` までは heredoc
      を検出しない。``echo $((1<<2))`` の後ろに右オペランドと同じ行 (``2``) が
      あると terminator 未検出の fallback が効かず、間の ``cat .env`` が本文
      扱いで消えるため。文字自体は plain のまま (``$`` ``(`` は hard-stop として
      数える)。``$( (cmd) )`` のような擬似形は ``))`` が来ず算術扱いのまま終わる
      が、その場合は heredoc 検出が止まる (= 0.21.x の行分割) だけ
    """
    out: list[tuple[str, str]] = []
    n = len(command)
    i = 0
    in_single = False
    in_double = False
    in_comment = False
    in_arith = False    # クォート外の ``((`` 〜 ``))`` の中
    arith_depth = 0     # 算術式内の ``(`` の入れ子深さ
    bs_run = 0
    word_start = True
    pending_heredocs: list[tuple[str, bool]] = []
    while i < n:
        c = command[i]
        if in_comment:
            if c == "\n":
                in_comment = False
                out.append((c, _ST_PLAIN))
                word_start = True
                i += 1
                if pending_heredocs:
                    i = _consume_heredoc_bodies(command, i, pending_heredocs, out)
                    pending_heredocs = []
                continue
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
        if in_arith:
            if c == "(":
                arith_depth += 1
            elif c == ")":
                if arith_depth > 0:
                    arith_depth -= 1
                elif command.startswith("))", i):
                    out.append((c, _ST_PLAIN))
                    out.append((c, _ST_PLAIN))
                    in_arith = False
                    word_start = False
                    i += 2
                    continue
            # 算術式の中は heredoc / コメント / 行継続を見ない (``<<`` はシフト)。
            # クォートは上の in_single / in_double 分岐が先に処理する
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
                bs_run = 0
            out.append((c, _ST_PLAIN))
            word_start = False
            i += 1
            continue
        if c == "(" and command.startswith("((", i):
            in_arith = True
            arith_depth = 0
            out.append((c, _ST_PLAIN))
            out.append((c, _ST_PLAIN))
            word_start = False
            i += 2
            continue
        if c == "<" and command.startswith("<<", i):
            if command.startswith("<<<", i):
                # here-string: literal 渡しで heredoc ではない (``<`` は hard-stop)
                for ch in "<<<":
                    out.append((ch, _ST_PLAIN))
                word_start = False
                i += 3
                continue
            out.append(("<", _ST_PLAIN))
            out.append(("<", _ST_PLAIN))
            i, delim, strip_tabs = _read_heredoc_word(command, i + 2, out)
            pending_heredocs.append((delim, strip_tabs))
            word_start = False
            continue
        if c == "\n" and pending_heredocs:
            out.append((c, _ST_PLAIN))
            word_start = True
            i = _consume_heredoc_bodies(command, i + 1, pending_heredocs, out)
            pending_heredocs = []
            continue
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
    (``ls &\\<nl>& cat .env``) も正しく区切る (review R7)。単独の ``&``
    (非同期リスト) も区切る (review R9: ``ls '{' & cat .env`` が 1 segment に
    潰れ ``ls`` の metadata-only 経路で素通りしていた)。``2>&1`` ``&>`` の
    ``&`` はリダイレクトの一部なので区切らない。

    Bash コメントは segment から落とす (Bash が解釈しない文字列を shlex に
    渡さない)。改行は区切りとして残す。``\\r`` 入りのコメントだけは丸ごと
    segment に残し、``_has_hard_stop`` の表示偽装 guard に到達させる。

    heredoc 本文 (``_ST_HEREDOC``、0.22.0) も同じく落とす。0.21.x までは本文の
    各行が segment になり、``cat > x.py <<'PY'`` の本文 ``n = kb * 1024`` が
    ``n`` コマンドの operand ``*`` として判定されていた (実ログの
    hard_stop_quoted 193 件のほぼ全部)。``<<`` を含む行自体は ``<`` で hard-stop
    なので、本文を落としても verdict が allow 側へ緩むことはない (ask_or_allow
    のまま)。
    """
    lx = _lex(command)
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(lx)
    while i < n:
        c, st = lx[i]
        if st == _ST_HEREDOC:
            i += 1
            continue
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
            # 単独の & (非同期リスト区切り、review R9)。``2>&1`` / ``>&`` / ``<&``
            # (fd 複製) と ``&>`` (両出力リダイレクト) の & は演算子ではない。
            if c == "&":
                prev = lx[i - 1][0] if i > 0 and lx[i - 1][1] == _ST_PLAIN else ""
                nxt = lx[i + 1][0] if i + 1 < n and lx[i + 1][1] == _ST_PLAIN else ""
                if prev not in ("<", ">") and nxt != ">":
                    segments.append("".join(buf))
                    buf = []
                    i += 1
                    continue
        buf.append(c)
        i += 1
    if buf:
        segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]
