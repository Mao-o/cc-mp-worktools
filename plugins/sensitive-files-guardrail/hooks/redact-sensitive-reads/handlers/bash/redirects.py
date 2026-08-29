"""Bash リダイレクト関連 pure helper (0.3.3 分解、0.7.0 縮小)。

このモジュールは副作用なし・plugin 状態非依存。安全リダイレクト剥離
(``2>/dev/null`` / ``2>&1`` / ``&>/dev/null`` 等) と、剥離後の残留 metachar
(``>`` ``&`` ``|`` ``<``) 検出のみに責任を限定する。0.25.0 から残留 metachar
判定は raw segment を ``segmentation._lex`` で quote-aware に走査する
(``_live_operator_metachars``)。0.24.0 までの token ベース判定
(``_segment_has_residual_metachar``) は shlex でクォートが剥がれた後の token
しか見えず、クォート内の literal データを演算子と誤認していたため撤去した
(呼び出し元は全て ``_live_operator_metachars`` の結果を渡す)。

quote-aware 走査では **演算子の判定と target の quote removal を分離する**
(``_is_safe_redirect_word`` / ``_quote_removed``)。演算子部が live (クォート外・
非エスケープ) であることだけを剥離の条件にし、target 側はクォートを外してから
安全 target と突き合わせる — bash は target をクォート除去してから使うので、
``2>"/dev/null"`` は ``2>/dev/null`` と同じ挙動になるため。

0.3.4〜0.6.x で持っていた ``<`` 入力リダイレクト target 抽出用の
character-level quote-aware parser (``_scan_input_redirect_targets_with_form`` /
``_scan_input_redirect_targets_chars`` / ``_consume_redirect_target`` /
``_classify_redirect_form`` / ``RedirectForm`` 等) は **0.7.0 で撤廃**。
``cat <(echo \\(\\)) < .env`` のような escape paren depth tracking や
``[[ ... ]]`` 引数位置判定は思想 1 (うっかり露出予防が目的、敵対的防御は非目的)
に反するため、``<`` を含む command は丸ごと hard-stop として ``ask_or_allow``
(default で ask、autonomous で allow) に倒す形に格下げした。
"""
from __future__ import annotations

import re

from handlers.bash.constants import (
    _REDIRECT_OP_TOKENS,
    _SAFE_REDIRECT_OP_RE,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGET_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
)
from handlers.bash.segmentation import _ST_ESCAPED, _ST_PLAIN, _lex

# 書き込みリダイレクト演算子 (任意 fd 番号 / ``&`` + ``>`` / ``>>`` / ``>|``)。
# target が fused (``>.env``) でも別トークン (``> .env``) でも拾えるよう、前半を
# 演算子として切り出して残りを target にする。``<`` 入力リダイレクトと fd 複製
# (``>&1``) は対象外 (前者は hard-stop、後者は target が ``&N`` になり除外)。
_WRITE_REDIRECT_RE = re.compile(r"^(?:[0-9]+|&)?>>?\|?(?P<target>.*)$")


def _is_safe_redirect_token(tok: str) -> bool:
    """``2>/dev/null`` / ``&>/dev/null`` / ``2>&1`` 等、単一トークンの安全リダイレクト。"""
    return bool(_SAFE_REDIRECT_RE.match(tok))


def _strip_safe_redirects(tokens: list[str]) -> list[str]:
    """安全リダイレクト (/dev/null 等への出力 / fd 複製) を剥がす。

    入力リダイレクト (``<``) は hard-stop 側で扱う前提。書き込み先が /dev/null 以外の
    リダイレクト (``> file.txt``) は残して後段で fail-closed (``ask_or_allow``) させる。
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _is_safe_redirect_token(tok):
            i += 1
            continue
        if tok in _REDIRECT_OP_TOKENS and i + 1 < n:
            nxt = tokens[i + 1]
            if nxt in _SAFE_REDIRECT_TARGETS:
                i += 2
                continue
        out.append(tok)
        i += 1
    return out


def _quote_removed(chars: list[tuple[str, str]]) -> str:
    """word の ``(文字, 字句状態)`` 列に Bash の quote removal を適用する (0.25.0)。

    - **クォート外の ``'`` / ``"`` は区切り記号なので落とす**。``_lex`` は区切り
      文字自体を ``_ST_PLAIN`` で出し、中身だけを ``_ST_SINGLE`` /
      ``_ST_DOUBLE`` にする。クォート外に literal の quote 文字が現れるのは
      ``\\'`` の形だけで、それは ``_ST_ESCAPED`` になるのでここには来ない
    - ``_ST_ESCAPED`` は ``\\`` + 次の 1 文字の対 (``_lex`` の仕様) なので、
      ``\\`` を落として次の 1 文字を literal として残す
    - クォート内の文字はそのまま残す。**ダブルクォート内の ``\\`` は解釈しない**
      — 解釈しないと安全 target に一致しなくなる = 剥離しない = 保守側 (ask)
      にしか倒れないため、精度を上げる価値がない
    """
    out: list[str] = []
    i = 0
    n = len(chars)
    while i < n:
        c, st = chars[i]
        if st == _ST_ESCAPED:
            if c == "\\" and i + 1 < n and chars[i + 1][1] == _ST_ESCAPED:
                out.append(chars[i + 1][0])
                i += 2
                continue
            out.append(c)
            i += 1
            continue
        if st == _ST_PLAIN and c in "'\"":
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _is_safe_redirect_word(chars: list[tuple[str, str]]) -> bool:
    """1 word で完結する安全リダイレクトか (quote-aware、0.25.0)。

    ``2>/dev/null`` / ``&>/dev/null`` / ``2>&1`` に加え、**target がクォート
    されている変種** (``2>"/dev/null"`` / ``2>'/dev/null'`` / ``2>&"1"`` /
    ``2>"&1"``) も同じ安全リダイレクトとして扱う。Bash は target を quote
    removal してから使うので、クォートの有無で挙動は変わらない (Codex R1 P2)。
    0.24.0 までは「word 全体が非クォートであること」を剥離の条件にしていたため、
    target をクォートすると剥離されず、後段が live な ``>`` を見て
    ``git commit -m x 2>"/dev/null"`` / ``make build 2>"/dev/null"`` のような
    日常形を ask に倒していた。

    判定は 2 つに分ける:

    1. **演算子部** (``_SAFE_REDIRECT_OP_RE`` = fd 番号 / ``&`` + ``>``) は
       word の **クォート外の接頭部だけ** に照合する。``"2>"/dev/null`` /
       ``2\\>/dev/null`` / ``'2>/dev/null'`` は bash では引数であってリダイレクト
       ではないので剥離してはいけない (剥離すると ``>`` が消えて書込み判定が
       落ちる)。``_lex`` はクォート / エスケープの区切り文字自体も word に含めて
       出すため、接頭部を先に切っておけば「リダイレクト表記そのものがデータ」の
       形は演算子として一致しようがない
    2. **target 部** は ``_quote_removed`` してから安全 target と突き合わせる

    演算子の形は ``constants`` のフラグメントが唯一の定義。``>>`` / ``>|`` は
    含まれない (``>|`` は noclobber を明示的に無視する破壊的 truncate なので
    安全リダイレクトに数えない — 0.25.0 の clobber 修正と整合)。
    """
    plain_prefix: list[str] = []
    for c, st in chars:
        if st != _ST_PLAIN:
            break
        plain_prefix.append(c)
    m = _SAFE_REDIRECT_OP_RE.match("".join(plain_prefix))
    if m is None:
        return False
    return bool(
        _SAFE_REDIRECT_TARGET_RE.fullmatch(_quote_removed(chars[m.end():]))
    )


def _live_operator_metachars(segment: str) -> frozenset[str]:
    """raw segment 中で **演算子になり得る** residual metachar (``&`` ``|`` ``<``
    ``>``) の集合を返す (quote-aware、0.25.0)。

    Bash でこれらの文字が演算子として働くのは **クォート外・非エスケープ**
    (``_lex`` の ``_ST_PLAIN``) のときだけ。シングル / ダブルクォート内・
    ``\\`` エスケープ済みは literal データで、リダイレクトにも pipe にも
    background にもならない (bash 5 実測: ``echo \\> x`` は ``> x`` を出力する
    だけでファイルを作らない)。0.24.0 までの token ベース判定
    (``_segment_has_residual_metachar``) はこの区別ができず、
    ``git commit -m 'fix: a & b'`` / ``curl 'https://x/a?b=1&c=2'`` /
    ``echo "a > b"`` のような日常コマンドを ask に倒していた (実ログ 429 件 /
    0.5%)。

    安全リダイレクト (``2>/dev/null`` / ``2>&1`` / ``>`` + ``/dev/null`` 等) は
    token 版の ``_strip_safe_redirects`` と同じ規則で除外する。0.25.0 (Codex R1
    P2) から、除外の条件は「word 全体が非クォート」ではなく **演算子部が live
    (クォート外・非エスケープ) であること** で、target 側は quote removal して
    から安全 target と突き合わせる (``_is_safe_redirect_word`` /
    ``_quote_removed``)。bash は target をクォート除去してから使うため
    ``2>"/dev/null"`` は無クォート形と同じ安全リダイレクトで、ここを分離しないと
    ``git commit -m x 2>"/dev/null"`` が ask に倒れていた。``echo '2>/dev/null'``
    は演算子部までクォート内なので従来どおり operand 扱い (``>`` は数えない)。

    戻り値の使い方 (``bash_handler._analyze_segment``):

    - 空でなければ従来どおり ``ask_or_allow("residual_metachar")``
    - ``>`` を含まなければ書き込みリダイレクトは存在し得ないので、
      metadata-only 経路の ``_sensitive_redirect_target`` 判定を skip する
      (``ls '>.env'`` のようなクォート済み operand を fused redirect と誤認して
      deny しない)

    注意: ``|`` と単独 ``&`` は ``_split_command_on_operators`` が segment 境界
    として先に消費するため、ここで観測されるのは主に ``>`` (書き込み) と
    リダイレクト複合の ``&`` (``3>&2`` 等の非 safe 形)。``<`` はクォート外なら
    hard-stop が先に効くので、実際にはクォート内 = 非演算子側でしか現れない。
    """
    found: set[str] = set()
    # word = クォート外の空白で区切った並び。各 word について
    # ((文字, 字句状態) 列, クォート外の metachar 集合) を持つ。字句状態を
    # 保つのは、安全リダイレクト判定が「演算子部は live か」「target は quote
    # removal 後に安全か」を別々に見るため (0.25.0)。
    words: list[tuple[list[tuple[str, str]], set[str]]] = []
    buf: list[tuple[str, str]] = []
    metas: set[str] = set()

    def flush() -> None:
        nonlocal buf, metas
        if buf:
            words.append((buf, metas))
        buf = []
        metas = set()

    for c, st in _lex(segment):
        if st == _ST_PLAIN and c in " \t\n":
            flush()
            continue
        buf.append((c, st))
        if st == _ST_PLAIN and c in _SEGMENT_RESIDUAL_METACHARS:
            metas.add(c)
    flush()

    i = 0
    n = len(words)
    while i < n:
        chars, word_metas = words[i]
        if _is_safe_redirect_word(chars):
            i += 1
            continue
        # 空白区切りの分離形 (``2>`` + ``/dev/null``)。target word は quote
        # removal 後に突き合わせる (``2> "/dev/null"``)。演算子 word 側の
        # liveness を別途見ないのは、``_lex`` がクォート / エスケープの区切り
        # 文字自体も word に残すため — ``"2>"`` / ``2\>`` の raw は ``2>`` に
        # ならず、``_REDIRECT_OP_TOKENS`` との一致がそのまま「全文字クォート外」
        # を意味するから。
        if (
            "".join(c for c, _ in chars) in _REDIRECT_OP_TOKENS
            and i + 1 < n
            and _quote_removed(words[i + 1][0]) in _SAFE_REDIRECT_TARGETS
        ):
            i += 2
            continue
        found |= word_metas
        i += 1
    return frozenset(found)


def _redirect_write_targets(tokens: list[str]) -> list[str]:
    """書き込みリダイレクト (``>`` / ``>>`` / ``n>`` / ``&>`` / ``>|``) の target
    path 一覧を返す (0.14.0, Codex P2 対応)。

    ``_strip_safe_redirects`` 後の token 列を前提 (/dev/null 等の安全 target は
    除去済み)。fused 形 (``>.env``) は同トークンから、bare 形 (``> .env``) は次
    トークンから target を取り出す。fd 複製 (``>&1``) は target が ``&1`` になる
    ため除外、入力リダイレクト (``<``) は対象外 (hard-stop 側で処理)。

    用途: metadata-only コマンド (``ls`` / ``stat`` 等) が機密 path へ redirect
    して書き込む (``ls > .env`` で .env を truncate する) ケースを、operand の
    内容露出とは別の「破壊的書込み」懸念として検出するため。
    """
    targets: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        m = _WRITE_REDIRECT_RE.match(tokens[i])
        if m:
            target = m.group("target")
            if target:
                if not target.startswith("&"):  # ``>&1`` 等の fd 複製を除外
                    targets.append(target)
            elif i + 1 < n:
                targets.append(tokens[i + 1])
                i += 1
        i += 1
    return targets
