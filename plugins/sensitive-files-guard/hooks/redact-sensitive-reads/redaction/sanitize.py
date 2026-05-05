"""鍵名・basename・制御記号・DATA タグ衝突の sanitize + DATA タグエスケープ。

``<DATA untrusted="true">`` 包装だけに頼らず、鍵名そのものから制御記号・過度な長さ・
DATA タグ衝突文字列を除去する。モデルが敵対的文脈を扱う保証はない前提。

``escape_data_tag`` は body 全文を通すエスケープ層で、``</DATA>`` / ``<DATA`` /
大小混じりの ``<data>`` が本文中に現れても包装が破綻しないよう HTML エンティティで
エスケープする (0.7.0 で DATA タグ専用に縮約)。

0.8.0 で ``_INJECTION_PATTERNS`` を ``<DATA`` 系のみに縮小した。``system:`` /
``assistant:`` / ``ignore previous`` のような prompt 文言を鍵名・basename から
除去するロジックは思想 1 (うっかり露出予防、敵対的防御は非目的) 外の敵対的
シナリオなため撤廃。制御文字除去 + 長さ切り詰めは維持。
"""
from __future__ import annotations

import re

# 制御記号 (改行タブを除く) を削除
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

# 改行タブも鍵名からは除去 (値には残さないので基本不要だが防御的に)
_NEWLINE_TAB = re.compile(r"[\r\n\t]")

# DATA タグ衝突防止: ``<DATA`` / ``</DATA`` が鍵名・basename に混入した場合の
# 最低限の防御。Read 側の ``<DATA untrusted="true">`` 外殻を本文側が破壊する
# のを防ぐ。0.8.0 で prompt 文言系 (``ignore previous`` / ``system:`` /
# ``assistant:`` / ``</?system|</?user|</?assistant``) を除去 (思想 1 外)。
_INJECTION_PATTERNS = re.compile(r"</?\s*DATA", re.IGNORECASE)

# 鍵名の最大長 (実運用でこれより長い鍵はほぼ攻撃)
MAX_KEY_LEN = 128

# basename の最大長
MAX_BASENAME_LEN = 128

# DATA タグを保護するための regex (escape_data_tag 用)。case-insensitive。
_DATA_OPEN_RE = re.compile(r"<\s*DATA", re.IGNORECASE)
_DATA_CLOSE_RE = re.compile(r"<\s*/\s*DATA\s*>", re.IGNORECASE)


def sanitize_key(raw: str) -> str:
    """鍵名を sanitize する。

    - 制御記号・改行・タブを削除
    - 長さ 128 字で切り詰め
    - 注入パターンが含まれていたら [?] に置換
    """
    if not isinstance(raw, str):
        return "[?]"
    cleaned = _CTRL_CHARS.sub("", raw)
    cleaned = _NEWLINE_TAB.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return "[?]"
    if _INJECTION_PATTERNS.search(cleaned):
        return "[?]"
    if len(cleaned) > MAX_KEY_LEN:
        cleaned = cleaned[:MAX_KEY_LEN] + "..."
    return cleaned


def escape_data_tag(text: str) -> str:
    """body 内の ``<DATA ...>`` / ``</DATA>`` を HTML エンティティにエスケープする。

    Read 側 ``redaction/engine.py::build_reason`` で長く使われている API。
    ``<DATA untrusted="true">`` 外殻包装を body が破壊できないようにする最終防御層
    として、body 内に ``<DATA>`` / ``</DATA>`` 様の文字列が混入しても包装が
    壊れないように ``<`` (および閉じタグでは ``>``) のみエンティティ化する。
    大小文字と中間空白は温存し、body の情報量を壊さない。
    """
    if not isinstance(text, str):
        return ""

    def _close_repl(m: re.Match) -> str:
        s = m.group(0)
        return "&lt;" + s[1:-1] + "&gt;"

    def _open_repl(m: re.Match) -> str:
        s = m.group(0)
        return "&lt;" + s[1:]

    # 閉じタグ優先 (開きタグ置換で ``</DATA>`` が残らないようにするため)
    escaped = _DATA_CLOSE_RE.sub(_close_repl, text)
    escaped = _DATA_OPEN_RE.sub(_open_repl, escaped)
    return escaped


def sanitize_basename(raw: str) -> str:
    """ファイル名 (basename) を sanitize する。

    - ディレクトリ区切りを除去 (defensive)
    - 制御記号・改行・タブを削除
    - 長さ切り詰め
    - 注入パターンが含まれていたら [?] に置換
    """
    if not isinstance(raw, str):
        return "[?]"
    cleaned = raw.replace("/", "").replace("\\", "")
    cleaned = _CTRL_CHARS.sub("", cleaned)
    cleaned = _NEWLINE_TAB.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return "[?]"
    if _INJECTION_PATTERNS.search(cleaned):
        return "[?]"
    if len(cleaned) > MAX_BASENAME_LEN:
        cleaned = cleaned[:MAX_BASENAME_LEN] + "..."
    return cleaned
