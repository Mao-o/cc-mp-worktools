"""Bash リダイレクト関連 regex / pure helper (0.3.3 分解, 0.3.4 拡張, 0.5.0 form 付与)。

このモジュールは副作用なし・plugin 状態非依存。token 列の並べ替え / regex match /
character-level 解析のみ。0.3.4 で ``_scan_input_redirect_targets_chars`` /
``_consume_redirect_target`` を追加し、``<`` 入力リダイレクト target 抽出を
shlex に依存しない quote-aware parser に移行した。

0.5.0 で M5 (リダイレクト形式タグ) を実装し、target 抽出と同時に **form**
(``bare`` / ``fd_prefixed`` / ``no_space`` / ``quoted``) を返す
``_scan_input_redirect_targets_with_form`` を新設。既存 list[str] 版
``_scan_input_redirect_targets_chars`` は form を捨てる thin wrapper として
保持し、74 件の戻り値型 assert テストの後方互換を維持する。
"""
from __future__ import annotations

from typing import Literal

from handlers.bash.constants import (
    _REDIRECT_OP_TOKENS,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
)


# Bash input redirect の構文形式タグ (M5, 0.5.0)。target 1 つにつき 1 種、
# 優先順位は ``fd_prefixed`` > ``no_space`` > ``quoted`` > ``bare``。
# 例:
#   ``cat < .env``      → ``bare``
#   ``cat <.env``       → ``no_space``
#   ``cat 0< .env``     → ``fd_prefixed``
#   ``cat 0<.env``      → ``fd_prefixed`` (no_space より優先)
#   ``cat < ".env"``    → ``quoted``
#   ``cat <".env"``     → ``quoted`` (no_space より優先)
#   ``cat 0< ".env"``   → ``fd_prefixed`` (quoted より優先)
RedirectForm = Literal["bare", "fd_prefixed", "no_space", "quoted"]


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


def _segment_has_residual_metachar(tokens: list[str]) -> bool:
    """``_strip_safe_redirects`` 後もセグメントに残っている ``>`` ``&`` ``|`` ``<``
    を持つトークンがあるか。
    """
    for t in tokens:
        if any(c in _SEGMENT_RESIDUAL_METACHARS for c in t):
            return True
    return False


# POSIX sh: double-quote 内の backslash が escape として機能する文字。
# それ以外の `\X` は backslash を literal として保持し、X も literal として続ける
# (例: `".\env"` → ``.\env``、`".env\*"` → ``.env\*``)。
_DQ_BACKSLASH_ESCAPABLE = frozenset({'$', '`', '"', '\\', '\n'})


def _consume_redirect_target(command: str, start: int) -> tuple[int, str]:
    """位置 ``start`` から redirect target (1 つの Bash word) を消費する。

    POSIX sh の word 概念に従い、**quote セクション / bare セクション /
    backslash escape が 1 つの word 内で mix できる** ことを許容する。
    word boundary (quote 外の whitespace / operator) に達するまで読み続け、
    連結された各セクションの内容 (quote 剥離済み) を結合して返す。

    例:
    - ``".env".example`` → ``.env.example`` (quote + bare の連結)
    - ``a"b"c`` → ``abc``
    - ``".env"*`` → ``.env*``
    - ``a\\ file`` → ``a file`` (backslash-escaped space は word boundary ではない)
    - ``".\\env"`` → ``.\\env`` (double-quote 内 ``\\e`` は literal ``\\e``)
    - ``".env\\$"`` → ``.env$`` (``\\$`` は escape として ``$`` 取り込み)

    Returns:
        消費した文字数と target 文字列のタプル。
    """
    n = len(command)
    i = start
    parts: list[str] = []

    while i < n:
        c = command[i]
        # word boundary (quote 外の whitespace / operator)
        if c in " \t\n|&;<>()":
            break

        # 開き quote: 対応する閉じ quote までを quote 剥離して取り込む
        if c in ('"', "'"):
            q = c
            i += 1
            while i < n and command[i] != q:
                if q == '"' and command[i] == "\\" and i + 1 < n:
                    # POSIX sh: double-quote 内では `\X` は X が
                    # `_DQ_BACKSLASH_ESCAPABLE` に含まれるときのみ escape として
                    # X を literal で取り込み、backslash は捨てる。それ以外は
                    # backslash も X も literal として保持。
                    nxt2 = command[i + 1]
                    if nxt2 in _DQ_BACKSLASH_ESCAPABLE:
                        parts.append(nxt2)
                        i += 2
                        continue
                    parts.append("\\")
                    parts.append(nxt2)
                    i += 2
                    continue
                parts.append(command[i])
                i += 1
            if i < n:
                i += 1  # closing quote
            continue

        # quote 外 backslash escape: 次の 1 文字を literal として取り込む
        if c == "\\" and i + 1 < n:
            parts.append(command[i + 1])
            i += 2
            continue

        parts.append(c)
        i += 1

    return (i - start, "".join(parts))


def _classify_redirect_form(
    command: str,
    lt_pos: int,
    has_space_before_target: bool,
    target_starts_with_quote: bool,
) -> RedirectForm:
    """単独 ``<`` 検出位置から redirect の構文形式 (form) を分類する (M5, 0.5.0)。

    優先順位 (target 1 つにつき 1 種):

    1. ``fd_prefixed``: ``<`` の直前に digit run (0-9 連続) があり、digit run の
       前が word boundary (空白 / operator / 行頭)。``0<`` ``2<`` ``10<`` 等。
       word 内部の数字 (例: ``abc0<``) は fd prefix と区別する。
    2. ``no_space``: ``<`` 直後に空白なしで target が始まる (fd 前置きなし、
       target 冒頭が quote でもない)。``cat<.env`` 等。
    3. ``quoted``: target word の冒頭が quote (``"`` / ``'``)。fd_prefixed 以外で
       優先される (``cat<".env"`` も ``no_space`` ではなく ``quoted``)。
    4. ``bare``: 上記以外 (空白あり、bare word target)。``cat < .env`` 等。

    Args:
        command: 元の command 文字列 (digit prefix 検出のため必要)
        lt_pos: 単独 ``<`` の位置
        has_space_before_target: ``<`` の直後に空白 (space / tab) を 1 つ以上
            飛ばしたか
        target_starts_with_quote: target word の最初の文字が quote か

    Returns:
        ``RedirectForm`` のいずれか 1 種。
    """
    # fd_prefixed 判定: `<` の直前に digit run がある + その前が word boundary
    j = lt_pos - 1
    digit_run = False
    while j >= 0 and command[j].isdigit():
        digit_run = True
        j -= 1
    if digit_run:
        # digit run の前が word boundary (空白 / operator / 行頭) なら fd prefix
        # `abc0<` のような word 内部数字は除外 (j < 0 は行頭で OK)
        if j < 0 or command[j] in " \t\n;|&()":
            return "fd_prefixed"
        # それ以外 (`abc0<` 等) は fd prefix ではない → 通常判定へ
    if target_starts_with_quote:
        return "quoted"
    if not has_space_before_target:
        return "no_space"
    return "bare"


def _scan_input_redirect_targets_with_form(
    command: str,
) -> list[tuple[str, RedirectForm]]:
    """character-level parser で input redirect target + form を抽出する (0.5.0 / M5)。

    0.3.4 で導入した quote/comment/conditional/arith 対応のロジックを保持しつつ、
    各 target に構文形式タグ (``RedirectForm``) を付与して返す。form 判定は
    ``_classify_redirect_form`` に委譲。

    quote state を追いながら以下を区別する:

    - ``<(`` (process substitution) → 深さ tracking で閉じ ``)`` までスキップ
      (内部の ``<`` / target を拾わない)
    - ``<<`` / ``<<<`` (heredoc / herestring) → ``<<`` を消費、``<<<`` は 3 つ目の
      ``<`` も明示的に追加スキップ
    - ``<&`` (fd dup, ``<&N``/``<&-``) → ``<&`` を消費
    - 単独 ``<`` → whitespace 飛ばして target を ``_consume_redirect_target`` で抽出
    - quote 外の ``#`` (word start 位置) → 行末までシェルコメントとして skip
    - ``[[ ... ]]`` / ``(( ... ))`` (command word 位置のみ予約語扱い) → 内部の
      ``<`` を比較演算子として skip

    Returns:
        ``(target, form)`` タプルのリスト (quote 剥離済み)。失敗しても例外は
        投げず、解析できた範囲の target を返す。
    """
    targets: list[tuple[str, RedirectForm]] = []
    i = 0
    n = len(command)
    quote: str | None = None
    # 直前に消費した文字が word boundary か (シェルコメントは word start 位置のみ)
    at_word_start = True
    # 現在位置が command word 位置か (segment separator 直後 or 入力先頭)。
    # bash の `[[` `((` 予約語は command word 位置でのみ有効。引数位置の `[[`
    # (例: `tee [[ "$x" < .env ]]`) は通常 word として扱う必要がある (R8)。
    at_command_start = True

    while i < n:
        c = command[i]

        # quote 内: 閉じを追うだけ (redirect 検出しない)
        if quote:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            at_word_start = False
            continue

        # quote 外のシェルコメント: word start 位置の `#` 以降を改行まで skip。
        # Bash の comment は word の先頭 (空白 / operator / 行頭) で `#` が来たときのみ。
        # `abc#def` のような word 内部の `#` は通常文字扱い (comment ではない)。
        if c == "#" and at_word_start:
            while i < n and command[i] != "\n":
                i += 1
            at_word_start = True
            # 改行は次反復で `c != "<":` ブランチに入り at_command_start=True
            continue

        # `[[ ... ]]` 条件式: 内部の `<` `>` は文字列比較演算子 (redirect ではない)。
        # 閉じ `]]` まで quote / escape を尊重しつつスキップする。
        # `[[` 予約語の発火条件 (R6, R7, R8):
        #   - word start 位置である (R6)
        #   - 直後が空白 / 改行 (R7。bash 仕様で `[[foo` は通常 word)
        #   - **command word 位置である** (R8。`tee [[ ...` の引数位置 `[[` は
        #     通常 word なので skip 対象外。skip すると後続 redirect を取りこぼし
        #     auto/plan で機密 bypass を招く)
        if (
            c == "["
            and at_word_start
            and at_command_start
            and i + 2 < n
            and command[i + 1] == "["
            and command[i + 2] in " \t\n"
        ):
            i += 2
            inner_quote: str | None = None
            while i + 1 < n:
                ch = command[i]
                if inner_quote:
                    if inner_quote == '"' and ch == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if ch == inner_quote:
                        inner_quote = None
                    i += 1
                    continue
                if ch in ('"', "'"):
                    inner_quote = ch
                    i += 1
                    continue
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "]" and command[i + 1] == "]":
                    i += 2
                    break
                i += 1
            else:
                # closing `]]` 未発見 — 残りを全消費 (fail-closed としては不問)
                i = n
            at_word_start = False
            at_command_start = False
            continue

        # `(( ... ))` 算術評価: 内部の `<` `>` は比較演算子。depth tracking で `))` まで。
        # `((` は word start 位置 + command word 位置で算術評価開始 (R6, R8)。
        if (
            c == "("
            and at_word_start
            and at_command_start
            and i + 1 < n
            and command[i + 1] == "("
        ):
            depth = 1
            i += 2
            inner_quote = None
            while i < n and depth > 0:
                ch = command[i]
                if inner_quote:
                    if inner_quote == '"' and ch == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if ch == inner_quote:
                        inner_quote = None
                    i += 1
                    continue
                if ch in ('"', "'"):
                    inner_quote = ch
                    i += 1
                    continue
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
            at_word_start = False
            at_command_start = False
            continue

        # quote 開始
        if c in ('"', "'"):
            quote = c
            i += 1
            at_word_start = False
            at_command_start = False
            continue

        # quote 外の backslash escape
        if c == "\\" and i + 1 < n:
            i += 2
            at_word_start = False
            at_command_start = False
            continue

        if c != "<":
            # word boundary 判定の更新 (次反復の `#` comment / `[[` `((` 判定に使う)
            if c in "|&;\n":
                # segment separator → 次は command word 位置
                at_word_start = True
                at_command_start = True
            elif c in " \t":
                # whitespace: word 境界、command 位置の True/False は維持
                at_word_start = True
            elif c in ">()":
                # operator (output redirect / subshell): word 境界だが command
                # 位置ではない。subshell 内の command 位置検出は scope 外 (内部の
                # `[[ ... ]]` を予約語と認識しないが、hard_stop で fail-closed)。
                at_word_start = True
                at_command_start = False
            else:
                at_word_start = False
                at_command_start = False
            i += 1
            continue

        # `<` 検出 (quote 外) — 直後の文字で分岐
        nxt = command[i + 1] if i + 1 < n else ""

        if nxt == "(":
            # process substitution <(...) — 閉じ `)` まで深さ tracking でスキップ。
            # 内部の `<` / `.env` を target として拾わない契約。
            # quote 外の backslash escape (`\\(` / `\\)`) は **depth 計算から除外**。
            # 除外しないと `cat <(echo \\() < .env` で escape された `(` が depth を
            # 増やし、`)` で 0 に戻らず、後続の `< .env` を取りこぼして auto/plan で
            # bypass を許す (R3, security regression)。
            depth = 1
            i += 2
            while i < n and depth > 0:
                ch = command[i]
                if quote:
                    if quote == '"' and ch == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if ch == quote:
                        quote = None
                    i += 1
                    continue
                if ch in ('"', "'"):
                    quote = ch
                    i += 1
                    continue
                # quote 外 backslash escape: 次の 1 文字を literal 扱いで skip。
                # depth 計算に影響させない。
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
            # process sub `<(...)` 自体が 1 つの bash word なので、直後の位置は
            # word boundary ではない。`at_word_start = False` にしておかないと、
            # `cat <(echo x)#< .env` で続く `#` をシェルコメントと誤認して
            # 後続の `< .env` を取りこぼし、auto/plan で bypass を招く
            # (R5, security regression)。
            at_word_start = False
            at_command_start = False
            continue

        if nxt in ("<", "&"):
            # heredoc / herestring / fd dup — `<<` / `<&` を消費して next iter。
            # heredoc body は範囲外 (body 内の `< path` は後続 loop で拾う
            # 可能性あり。これは 0.3.3 の regex 挙動と整合: false-positive 側
            # deny に倒す fail-closed 原則)。
            i += 2
            # `<<<` (herestring) の 3 つ目の `<` も追加スキップ (body を拾わない)
            if nxt == "<" and i < n and command[i] == "<":
                i += 1
            # `<<` / `<&` 等は metachar 演算子。直後は新しい word の開始位置だが
            # redirect の delimiter / target を期待する位置で、command 位置ではない。
            at_word_start = True
            at_command_start = False
            continue

        # 単独 `<` — target 抽出 + form 判定 (M5)
        lt_pos = i
        i += 1
        ws_consumed = 0
        while i < n and command[i] in " \t":
            i += 1
            ws_consumed += 1
        if i >= n:
            break
        target_first_char = command[i]
        target_starts_with_quote = target_first_char in ('"', "'")
        consumed, value = _consume_redirect_target(command, i)
        if value:
            form = _classify_redirect_form(
                command,
                lt_pos,
                has_space_before_target=(ws_consumed > 0),
                target_starts_with_quote=target_starts_with_quote,
            )
            targets.append((value, form))
        i += consumed
        # target を 1 word 消費したので word boundary ではない位置にいる。
        # 次反復で新たに word boundary 文字が来ない限り `at_word_start` は False。
        at_word_start = False
        at_command_start = False

    return targets


def _scan_input_redirect_targets_chars(command: str) -> list[str]:
    """既存戻り値型 (``list[str]``) の thin wrapper (0.3.4 互換、0.5.0 で thin 化)。

    M5 (0.5.0) で form 付き版 ``_scan_input_redirect_targets_with_form`` を新設
    したため、本関数は test seam (``test_input_redirect.py`` の 74 件の戻り値型
    assert) と既存 import path の後方互換維持のため form を捨てる thin wrapper
    として保持する。新規実装では form 付き版を直接呼ぶこと。
    """
    return [target for target, _form in _scan_input_redirect_targets_with_form(command)]
