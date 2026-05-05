"""operand トークンの glob 判定 / dotenv glob 判定 / path 候補抽出
(0.3.3 分解、0.8.0 で `_literalize` / `_glob_candidates` / `_is_absolute_or_relative_path_exec` 撤廃)。

このモジュールは副作用なし・plugin 状態非依存。``SFG_CASE_SENSITIVE`` 環境変数の
参照のみ外部状態 (全テストで同じ解釈をしたいため、``is_sensitive`` 側の opt-out
と整合させる)。

``_operand_is_sensitive`` は ``is_sensitive`` / ``normalize`` に依存する plugin
ステート側の処理なので ``bash_handler.py`` に残す。
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase

from handlers.bash.constants import _GLOB_CHARS

# dotenv ファミリーで「うっかり頻出」とする literal stem (0.8.0)。
# operand glob (例: ``.env*`` / ``*.envrc``) がこれらに ``fnmatchcase`` で
# 一致するときだけ deny に倒す。それ以外の glob (``id_rsa*`` / ``*.key`` /
# ``cred*.json`` / ``*.log`` 等) は ``ask_or_allow`` に格下げ (思想 1: うっかり
# 露出予防が目的、敵対的防御は非目的)。
_DOTENV_GLOB_STEMS = (".env", ".envrc")


def _has_glob(token: str) -> bool:
    """operand に shell glob 文字 (``*``, ``?``, ``[``) が含まれるか。"""
    return any(c in _GLOB_CHARS for c in token)


def _glob_operand_is_dotenv_match(operand: str) -> bool:
    """operand glob が dotenv 系の literal stem (``.env`` / ``.envrc``) に一致するか。

    判定: ``fnmatchcase(stem, op_glob)`` を ``stem ∈ _DOTENV_GLOB_STEMS`` で実施。

    例:
    - ``.env*`` → ``fnmatchcase(".env", ".env*")`` = True → deny
    - ``.env.*`` → ``fnmatchcase(".env", ".env.*")`` = False (".env." 以降が必要)
      → ask_or_allow に格下げ
    - ``*.envrc`` → ``fnmatchcase(".envrc", "*.envrc")`` = True → deny
    - ``.envrc*`` → ``fnmatchcase(".envrc", ".envrc*")`` = True → deny
    - ``.e[n]v`` / ``.en?`` / ``[.]env`` → ``fnmatchcase(".env", op)`` = True → deny
    - ``id_rsa*`` / ``*.key`` / ``cred*.json`` / ``*.log`` → どちらの stem にも
      一致しない → ask_or_allow に格下げ
    - ``.env.example*`` → ``.env`` にも ``.envrc`` にも一致しない → ask_or_allow

    0.3.2〜0.7.x で行っていた既定 rules への候補列挙
    (``_glob_candidates`` / ``_glob_operand_is_sensitive``) は思想 1 に対して
    deny 寄り過ぎる (``cat *.json`` / ``cat *.key`` / ``cat id_rsa*`` を全 mode
    deny する) と判断し 0.8.0 で撤廃した。dotenv stem (``.env`` / ``.envrc``)
    のうっかり頻出ケースだけを残す形に縮約。

    ``SFG_CASE_SENSITIVE=1`` 未設定時は lower 比較する (``is_sensitive`` 側の
    opt-out と整合)。
    """
    if not operand:
        return False
    cs = os.environ.get("SFG_CASE_SENSITIVE") == "1"
    op = operand if cs else operand.lower()
    for stem in _DOTENV_GLOB_STEMS:
        if fnmatchcase(stem, op):
            return True
    return False


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
