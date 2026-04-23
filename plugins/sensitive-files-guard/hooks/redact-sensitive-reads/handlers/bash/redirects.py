"""Bash リダイレクト関連 regex / pure helper (0.3.3 分解, 0.3.4 拡張)。

このモジュールは副作用なし・plugin 状態非依存。token 列の並べ替え / regex match /
character-level 解析のみ。0.3.4 で ``_scan_input_redirect_targets_chars`` /
``_consume_redirect_target`` を追加し、``<`` 入力リダイレクト target 抽出を
shlex に依存しない quote-aware parser に移行した。
"""
from __future__ import annotations

from handlers.bash.constants import (
    _REDIRECT_OP_TOKENS,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
)


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


def _consume_redirect_target(command: str, start: int) -> tuple[int, str]:
    """位置 ``start`` から redirect target を 1 つ消費する。

    quote で始まれば quote 剥離後の中身を返す (POSIX sh 相当の semantics)。
    bare なら whitespace / operator boundary 直前まで読む。backslash escape は
    double-quote と bare の双方で解釈 (``\\c`` → ``c`` を target に含める)。

    Returns:
        消費した文字数と target 文字列のタプル。
    """
    n = len(command)
    i = start
    parts: list[str] = []

    if i < n and command[i] in ('"', "'"):
        q = command[i]
        i += 1
        while i < n and command[i] != q:
            if q == '"' and command[i] == "\\" and i + 1 < n:
                parts.append(command[i + 1])
                i += 2
                continue
            parts.append(command[i])
            i += 1
        if i < n:
            i += 1  # closing quote
        return (i - start, "".join(parts))

    # bare: whitespace / operator まで読む
    while i < n and command[i] not in " \t\n|&;<>()":
        if command[i] == "\\" and i + 1 < n:
            parts.append(command[i + 1])
            i += 2
            continue
        parts.append(command[i])
        i += 1
    return (i - start, "".join(parts))


def _scan_input_redirect_targets_chars(command: str) -> list[str]:
    """character-level parser で input redirect target を抽出 (0.3.4)。

    quote state を追いながら以下を区別する:

    - ``<(`` (process substitution) → 深さ tracking で閉じ ``)`` までスキップ
      (内部の ``<`` / target を拾わない)
    - ``<<`` / ``<<<`` (heredoc / herestring) → ``<<`` を消費、``<<<`` は 3 つ目の
      ``<`` も明示的に追加スキップ
    - ``<&`` (fd dup, ``<&N``/``<&-``) → ``<&`` を消費
    - 単独 ``<`` → whitespace 飛ばして target を ``_consume_redirect_target`` で抽出

    ``0<`` / ``N<`` (fd 前置き) は ``<`` の直前の数字 prefix を意識しない設計
    (fd prefix は redirect の意味論上 target 抽出対象は ``<`` 以降のみ)。

    Returns:
        抽出した target のリスト (quote 剥離済み)。失敗しても例外は投げず、
        解析できた範囲の target を返す。
    """
    targets: list[str] = []
    i = 0
    n = len(command)
    quote: str | None = None

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
            continue

        # quote 開始
        if c in ('"', "'"):
            quote = c
            i += 1
            continue

        # quote 外の backslash escape
        if c == "\\" and i + 1 < n:
            i += 2
            continue

        if c != "<":
            i += 1
            continue

        # `<` 検出 (quote 外) — 直後の文字で分岐
        nxt = command[i + 1] if i + 1 < n else ""

        if nxt == "(":
            # process substitution <(...) — 閉じ `)` まで深さ tracking でスキップ。
            # 内部の `<` / `.env` を target として拾わない契約。
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
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                i += 1
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
            continue

        # 単独 `<` — target 抽出
        i += 1
        while i < n and command[i] in " \t":
            i += 1
        if i >= n:
            break
        consumed, value = _consume_redirect_target(command, i)
        if value:
            targets.append(value)
        i += consumed

    return targets
