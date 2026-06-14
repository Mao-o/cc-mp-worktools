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

import re

from handlers.bash.constants import (
    _REDIRECT_OP_TOKENS,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
)

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


def _segment_has_residual_metachar(tokens: list[str]) -> bool:
    """``_strip_safe_redirects`` 後もセグメントに残っている ``>`` ``&`` ``|`` ``<``
    を持つトークンがあるか。
    """
    for t in tokens:
        if any(c in _SEGMENT_RESIDUAL_METACHARS for c in t):
            return True
    return False


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
