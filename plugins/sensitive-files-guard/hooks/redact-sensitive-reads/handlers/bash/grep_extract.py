"""grep / rg / ag / ack / egrep / fgrep の token 列から env-var 名候補を抽出
(0.10.0, E4 で導入)。

副作用なし・plugin ステート非依存。

抽出ルール:

- ``-e PATTERN`` / ``-E PATTERN`` / ``-G PATTERN`` の次 token を pattern と扱う
- ``--regex=PATTERN`` / ``--pattern=PATTERN`` の RHS を pattern と扱う
- それ以外の non-option token も pattern 候補として扱う (grep の最初の
  positional argument は通常 pattern)
- 各 pattern 内から ``[A-Z][A-Z0-9_]{2,}`` の連続した英大文字 + アンダースコア
  + 数字を全て抽出 (env-var 名の一般形)。``|`` 分割は regex.findall の境界が
  自然に処理する

返り値は出現順 dedup された list[str]。dotenv parse 結果との照合は
``core/messages.py::_bash_deny_search`` 側で行う。

抽出対象でない token は無視 (`-i`, `-r` 等の bool flag、path 候補)。
"""
from __future__ import annotations

import re

# grep family の first token (case-sensitive)。
_GREP_FIRST_TOKENS = frozenset({
    "grep", "rg", "ag", "ack", "egrep", "fgrep",
})

# env-var 名らしき連続 token を pattern 内から拾う regex。
# 大文字始まり、英大文字 + 数字 + アンダースコアの 3 文字以上。
_ENV_VAR_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")

# pattern を value として取る short / long option (1 token consume)。
_PATTERN_VALUE_OPTIONS_NEXT = frozenset({
    "-e", "-E", "-G", "--regex", "--pattern",
})

# ``--regex=...`` / ``--pattern=...`` 形式の prefix。
_PATTERN_VALUE_OPTIONS_INLINE = ("--regex=", "--pattern=", "-e=")


def is_grep_command(first_token: str) -> bool:
    """first token が grep family か。"""
    return first_token in _GREP_FIRST_TOKENS


def extract_grep_keys(tokens: list[str]) -> list[str]:
    """grep 系コマンドの token 列から env-var 名候補を抽出する。

    Args:
        tokens: ``shlex.split`` 済みの token 列。``tokens[0]`` は first token
            (``grep`` 等)。caller 側で ``is_grep_command(tokens[0])`` を確認した
            あとに呼ぶこと。

    Returns:
        出現順 dedup された env-var 名 (``DATABASE_URL`` 等) のリスト。
        1 つも見つからなければ空リスト。
    """
    if not tokens:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        for m in _ENV_VAR_NAME_RE.finditer(text):
            name = m.group(0)
            if name not in seen:
                seen.add(name)
                keys.append(name)

    i = 1  # skip first_token
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            # 以降は positional path のみ扱い、pattern 抽出はしない。
            break
        if tok in _PATTERN_VALUE_OPTIONS_NEXT:
            if i + 1 < n:
                _add(tokens[i + 1])
                i += 2
                continue
            # value 不足 (`-e` 単独) はそのまま skip
            i += 1
            continue
        inline_match = next(
            (p for p in _PATTERN_VALUE_OPTIONS_INLINE if tok.startswith(p)),
            None,
        )
        if inline_match:
            _add(tok[len(inline_match):])
            i += 1
            continue
        # ``-`` 始まりの short / long option はそれ以上見ない (path / pattern
        # ではない bool flag として skip)。``--`` のみは上で処理済み。
        if tok.startswith("-") and tok != "-":
            i += 1
            continue
        # それ以外の non-option token は pattern または path のいずれか。
        # 区別は厳密にはできないが、env-var 形式の token のみ抽出するため
        # ``.env`` のような path は ``_ENV_VAR_NAME_RE`` に一致せず除外される。
        _add(tok)
        i += 1
    return keys
