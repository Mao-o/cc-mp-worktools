"""鍵名・basename・制御記号・注入パターンの sanitize + DATA タグエスケープ。

``<DATA untrusted="true">`` 包装だけに頼らず、鍵名そのものから命令文・制御記号・
過度な長さを除去する。モデルが敵対的文脈を扱う保証はない前提。

Step 4 で body 全文を通す ``escape_data_tag`` を追加。``</DATA>`` / ``<DATA`` /
大小混じりの ``<data>`` が本文中に現れても包装が破綻しないよう HTML エンティティで
エスケープする。
"""
from __future__ import annotations

import re

# 制御記号 (改行タブを除く) を削除
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

# 改行タブも鍵名からは除去 (値には残さないので基本不要だが防御的に)
_NEWLINE_TAB = re.compile(r"[\r\n\t]")

# 代表的なプロンプトインジェクションパターン (鍵名に混入したら警告)
_INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore\s+previous|ignore\s+all|system\s*:|assistant\s*:|"
    r"</?DATA|</?system|</?user|</?assistant)"
)

# 鍵名の最大長 (実運用でこれより長い鍵はほぼ攻撃)
MAX_KEY_LEN = 128

# basename の最大長
MAX_BASENAME_LEN = 128


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


def escape_xml_tag(text: str, tag_name: str) -> str:
    """body 内の ``<TAG ...>`` / ``</TAG>`` を HTML エンティティにエスケープする (0.4.2)。

    外殻タグ包装を body が破壊できないようにする最終防御。``escape_data_tag``
    の一般化で、``<DATA>`` だけでなく ``<SFG_DENY>`` 等の他タグも同じ仕組みで
    保護できる。

    実装方針: マッチした部分文字列を保ちつつ、``<`` と (閉じタグなら) ``>`` のみ
    ``&lt;`` / ``&gt;`` に置換する。大小文字と中間空白は温存し、body の情報量を
    壊さない。

    Args:
        text: エスケープ対象の文字列。
        tag_name: 保護したいタグ名 (``"DATA"`` / ``"SFG_DENY"`` 等)。
            case-insensitive で match する。
    """
    if not isinstance(text, str):
        return ""

    open_re = re.compile(rf"<\s*{re.escape(tag_name)}", re.IGNORECASE)
    close_re = re.compile(
        rf"<\s*/\s*{re.escape(tag_name)}\s*>", re.IGNORECASE
    )

    def _close_repl(m: re.Match) -> str:
        s = m.group(0)
        # s[0] は "<", s[-1] は ">"。中身 ("/<tag>" + 空白) は保つ。
        return "&lt;" + s[1:-1] + "&gt;"

    def _open_repl(m: re.Match) -> str:
        s = m.group(0)
        return "&lt;" + s[1:]

    # 閉じタグ優先 (開きタグ置換で ``</TAG>`` が残らないようにするため)
    escaped = close_re.sub(_close_repl, text)
    escaped = open_re.sub(_open_repl, escaped)
    return escaped


def escape_data_tag(text: str) -> str:
    """body 内の ``<DATA ...>`` / ``</DATA>`` を HTML エンティティにエスケープする。

    ``escape_xml_tag(text, "DATA")`` の薄い wrapper (0.4.2 移行)。Read 側の
    ``redaction/engine.py::build_reason`` で長く使われている API のため、
    後方互換のため残す。
    """
    return escape_xml_tag(text, "DATA")


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
