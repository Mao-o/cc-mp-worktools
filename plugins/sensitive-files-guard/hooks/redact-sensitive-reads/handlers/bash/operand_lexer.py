"""operand トークンの glob 判定 / literalize / path 候補抽出 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。``SFG_CASE_SENSITIVE`` 環境変数の
参照のみ外部状態 (全テストで同じ解釈をしたいため、``is_sensitive`` 側の opt-out
と整合させる)。

``_glob_operand_is_sensitive`` / ``_operand_is_sensitive`` は ``is_sensitive`` /
``normalize`` に依存する plugin ステート側の処理なので ``bash_handler.py`` に残す。
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase

from handlers.bash.constants import _GLOB_CHARS


def _is_absolute_or_relative_path_exec(token: str) -> bool:
    """``/bin/cat`` / ``./script`` / ``../foo`` のような path 実行か。"""
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
    )


def _has_glob(token: str) -> bool:
    """operand に shell glob 文字 (``*``, ``?``, ``[``) が含まれるか。"""
    return any(c in _GLOB_CHARS for c in token)


def _literalize(pattern: str) -> str:
    """fnmatch glob 文字 (``*`` ``?`` ``[...]``) を除去した最小 literal 表現。

    例: ``.env*`` → ``.env``, ``*.env.*`` → ``.env.``, ``[.]env`` → ``env``,
    ``?ecret*`` → ``ecret``, ``id_rsa*`` → ``id_rsa``。
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c in ("*", "?"):
            i += 1
            continue
        if c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(c)
                i += 1
            else:
                i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _glob_candidates(
    operand: str, rules: list[tuple[str, bool]]
) -> list[str]:
    """operand の具体化候補を既定 rules の stem から生成する。

    生成源:
    1. operand 自身の literal stem (``_literalize(operand)``)
    2. 各 rule の literal stem を operand の glob に fnmatch して match するもの
       (例: op=``id_*`` に rule pt_stem=``id_rsa`` が match → 候補入り)

    ``SFG_CASE_SENSITIVE`` 未設定時は lower 比較で候補生成する (``is_sensitive``
    側の opt-out と整合)。

    Note: プランの初期案には (op_stem+pt_stem) / (pt_stem+op_stem) の **連結候補**
    を加える項目もあったが、``*.log`` に対して ``.env`` rule との連結 ``.env.log``
    が候補化されてしまい、``is_sensitive(".env.log")`` が ``.env.*`` rule で True に
    なる結果 ``cat *.log`` が deny されてしまう問題があった。usability 上 ``*.log``
    は allow しておきたいので、連結候補は採用しない。``cred*.json`` ``id_*``
    ``*.envrc`` 等の交差は (2) の rule pt_stem direct match だけで網羅できる。
    """
    cs = os.environ.get("SFG_CASE_SENSITIVE") == "1"
    op = operand if cs else operand.lower()
    op_stem = _literalize(op)
    candidates: set[str] = {op_stem} if op_stem else set()

    for pattern, _ in rules:
        pat = pattern if cs else pattern.lower()
        pt_stem = _literalize(pat)
        if pt_stem and fnmatchcase(pt_stem, op):
            candidates.add(pt_stem)
    return [c for c in candidates if c]


def _find_path_candidates(tokens: list[str]) -> list[str]:
    """第 1 トークン以降から、path 候補を抽出。

    拾う形式:
    - ``--`` より後ろは無条件で path 扱い
    - 非 option トークン (``-`` で始まらない) はそのまま path 候補
    - ``--opt=value`` / ``-o=value`` の ``=`` 以降 (RHS) を候補に追加
    - 短形 option に value が **連結** した形 ``-X<value>`` (``-f.env`` 等) は
      ``tok[2:]`` を候補に追加
    """
    candidates: list[str] = []
    in_ddash = False
    for tok in tokens[1:]:
        if tok == "--":
            in_ddash = True
            continue
        if in_ddash:
            candidates.append(tok)
            continue
        if tok.startswith("--"):
            if "=" in tok:
                rhs = tok.split("=", 1)[1]
                if rhs:
                    candidates.append(rhs)
            continue
        if tok.startswith("-"):
            if "=" in tok:
                rhs = tok.split("=", 1)[1]
                if rhs:
                    candidates.append(rhs)
            elif len(tok) > 2:
                candidates.append(tok[2:])
            continue
        candidates.append(tok)
    return candidates
