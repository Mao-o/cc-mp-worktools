"""Bash リダイレクト関連 pure helper (0.3.3 分解、0.7.0 縮小)。

このモジュールは副作用なし・plugin 状態非依存。token 列の並べ替え / regex match
のみ。安全リダイレクト剥離 (``2>/dev/null`` / ``2>&1`` / ``&>/dev/null`` 等) と、
剥離後の残留 metachar (``>`` ``&`` ``|`` ``<``) 検出のみに責任を限定する。

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
