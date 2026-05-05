"""dotenv 値の placeholder 判定 (0.9.0 新設、E2)。

``looks_placeholder(value)`` で「値が placeholder っぽいか」を判定する。
LLM に「この鍵はまだ実値が入っていない (= rotate / set 不要)」を伝えて、
API 失敗デバッグの次の作業を判断できるようにする。

設計方針 (REVIEW_TASKS_2026-05-06.md 論点 Q1 = 簡易版で開始):

- 固定 literal セット (PLACEHOLDER_LITERALS) と 5 個の regex
  (PLACEHOLDER_PATTERNS) のみで判定
- ユーザー拡張点 (placeholders.local.txt 等) は **作らない**。要望が来たら段階的
  に対応する
- case-insensitive 比較。クォート (``"..."`` / ``'...'``) は剥がしてから判定
- 戻り値の第二要素は **辞書側 literal** または **pattern label** のいずれか。
  実値そのものは返さない (regex 一致時に値の一部が漏れるのを防ぐ)
"""
from __future__ import annotations

import re

# 完全一致 (case-insensitive) で placeholder と見なす literal セット
PLACEHOLDER_LITERALS: frozenset[str] = frozenset({
    "dummy", "sample", "example", "placeholder", "todo", "fixme",
    "tbd", "xxx", "changeme", "change_me", "replace_me",
    "your_key", "your_secret", "your_token", "your_password",
    "test", "fake", "lorem", "ipsum", "foobar", "asdf",
})

# (正規表現, ラベル) のリスト。一致時は label を戻り値に返す。
# 実値そのものは返さない (値の一部が LLM 文脈に漏れるのを防ぐ)。
PLACEHOLDER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^your[_-].*[_-]here$", re.IGNORECASE), "your_*_here"),
    (re.compile(r"^<.*>$"), "<...>"),
    (re.compile(r"^\*{3,}$"), "***"),
    (re.compile(r"^x{3,}$", re.IGNORECASE), "xxx"),
    (
        re.compile(r"^(test|dev|local|staging)[_-]?\w*$", re.IGNORECASE),
        "test/dev/local/staging",
    ),
]


def looks_placeholder(value: str) -> tuple[bool, str | None]:
    """値が placeholder っぽいか判定する。

    Returns:
        (is_placeholder, matched_label).
        is_placeholder が False のとき matched_label は None。
        is_placeholder が True のときは:
          - literal 一致 → lower-case 化した辞書 literal (例: ``"your_jwt_secret_here"`` 風)
          - regex 一致 → pattern label (例: ``"your_*_here"``, ``"<...>"``)

    Args:
        value: dotenv の値 (raw 文字列)。クォート (``"..."`` / ``'...'``) は
            内部で剥がして判定する。
    """
    if not isinstance(value, str):
        return (False, None)
    v = value.strip()
    # クォート剥がし (型判定と同じ規則)
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        v = v[1:-1].strip()
    if not v:
        return (False, None)

    lower = v.lower()
    if lower in PLACEHOLDER_LITERALS:
        return (True, lower)

    for pattern, label in PLACEHOLDER_PATTERNS:
        if pattern.match(v):
            return (True, label)

    return (False, None)
