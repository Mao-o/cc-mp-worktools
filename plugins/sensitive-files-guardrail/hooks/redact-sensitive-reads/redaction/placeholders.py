"""dotenv 値の placeholder 判定 (0.9.0 新設、E2)。

``looks_placeholder(value)`` で「値が placeholder っぽいか」を判定する。
LLM に「この鍵はまだ実値が入っていない (= rotate / set 不要)」を伝えて、
API 失敗デバッグの次の作業を判断できるようにする。

設計方針 (REVIEW_TASKS_2026-05-06.md 論点 Q1 = 簡易版で開始):

- 固定 literal セット (PLACEHOLDER_LITERALS) と regex 群 (PLACEHOLDER_PATTERNS)
  のみで判定
- ユーザー拡張点 (placeholders.local.txt 等) は **作らない**。要望が来たら段階的
  に対応する
- case-insensitive 比較。クォート (``"..."`` / ``'...'``) は剥がしてから判定
- 戻り値の第二要素は **辞書側 literal** または **pattern label** のいずれか。
  実値そのものは返さない (regex 一致時に値の一部が漏れるのを防ぐ)

失敗方向 (0.31.0 で明示): ここでの誤りは 2 方向ある。

- **実値を placeholder と誤標識する** → モデルは「まだ値が入っていない」と読み、
  rotate / 再設定を提案しなくなる = **見落としの方向**
- **placeholder を ``<set>`` と表示する** → 「値がある」と読むだけで、次の作業を
  妨げない

したがって判定は**後者に倒す** (疑わしければ placeholder と言わない)。0.31.0 の
厳格化はこの原則の適用で、``verdict`` (deny) には一切影響しない。
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

# 山括弧 placeholder (``<your-key>`` / ``<...>``) の中身に許す文字と長さ
# (0.31.0)。単語文字・空白・``.``・``-`` のみ、40 文字以下。
#
# 0.30.0 までは ``^<.*>$`` で、``<soap:Envelope>`` や ``<root attr="x">`` の
# ような **XML/HTML の実値** まで placeholder と判定していた。placeholder の
# 慣習は「名前らしい短いトークンを山括弧で囲む」形なので、``:`` ``=`` ``"``
# ``/`` を含む形と長すぎる形を外す。``<html>`` のような短いタグ名は原理的に
# 区別できないので残るが (ヒューリスティックの限界)、``.env`` の値として
# 現れる頻度は低い。
_ANGLE_INNER_MAX = 40

# (正規表現, ラベル) のリスト。一致時は label を戻り値に返す。
# 実値そのものは返さない (値の一部が LLM 文脈に漏れるのを防ぐ)。
PLACEHOLDER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^your[_-].*[_-]here$", re.IGNORECASE), "your_*_here"),
    (re.compile(r"^<[\w .\-]{0,%d}>$" % _ANGLE_INNER_MAX), "<...>"),
    (re.compile(r"^\*{3,}$"), "***"),
    (re.compile(r"^x{3,}$", re.IGNORECASE), "xxx"),
    # 環境名**そのもの** (``ENV=local`` のような値) だけを placeholder と見なす
    # (0.31.0)。0.30.0 までは ``^(test|dev|local|staging)[_-]?\w*$`` で後続の
    # 任意語を許していたため、``test_51H8xKqL9mNpQrStUvWxYz`` (Stripe テスト
    # キー) / ``dev_a8f3c2e1b9d7`` / ``staging_9f8e7d6c5b4a3210`` /
    # ``local_dbpassword_x9f2`` のような **実在するクレデンシャル**を軒並み
    # placeholder と判定していた。``<placeholder>`` は ``<set>`` を置き換える
    # ので、モデルには「この鍵はまだ実値が入っていない (rotate / set 不要)」と
    # 伝わる = 思想 2 (block 時に意図を汲んだ正しい情報を返す) の破綻。
    # ``dev_`` / ``test_`` 接頭の実トークンを ``.env`` に置く構成は一般的で
    # 再発性が高いため、後続許容を外して完全一致に絞る。verdict (deny) は
    # 変わらない — 表示タグが ``<placeholder>`` から ``<set>`` に戻るだけで、
    # 「値がある」と伝える側 = 安全方向。
    # ``development`` は完全一致形なので alternation に足しても実トークンを
    # 拾う穴は開かない (``development`` で終わる実クレデンシャルは無い)。
    # 0.31.0 の初版は後続許容を外すのと同時に落としていたため、
    # ``ENV=development`` / ``NODE_ENV=development`` が ``<placeholder>`` から
    # ``<set>`` に退行していた (隔離内レビュー P3-5)。
    (
        re.compile(r"^(test|dev|local|staging|development)$", re.IGNORECASE),
        "test/dev/local/staging/development",
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
