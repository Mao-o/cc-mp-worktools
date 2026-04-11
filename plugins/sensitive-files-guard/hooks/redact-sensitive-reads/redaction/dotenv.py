"""dotenv (.env / .env.*) の minimal-info 化。

保持: 鍵名の順序付きリスト + 型クラスのみ。
破棄: 値そのもの、コメント、空行、先頭 `export`。
"""
from __future__ import annotations

import re

from .sanitize import sanitize_key

# KEY=VALUE / export KEY=VALUE の行をざっくり捕捉
# 完全な dotenv spec parser ではないが、Vibe Coder が書く .env を想定した範囲で十分
_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:export\s+)?
    ([A-Za-z_][A-Za-z0-9_.\-]*)   # key
    \s*=\s*
    (.*?)                          # value (raw; used for type classification only)
    \s*$
    """,
    re.VERBOSE,
)

# JWT 判定 (header.payload.signature の base64url 3 パート)
_JWT_RE = re.compile(r"^ey[A-Za-z0-9_-]+\.ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def _classify_value(raw: str) -> str:
    """値の型クラスだけを返す。値自体は捨てる。

    非クォート値の ``\\s+#`` 以降は inline comment として切り落とす。
    クォート値 (``"..."`` / ``'...'``) と空白なしの ``#`` (``KEY=value#frag``)
    は値の一部として扱い、一切変更しない。
    """
    if raw is None:
        return "str"
    v = raw.strip()
    was_quoted = False
    # クォート剥がし (型判定のみに使用、値は返さない)
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        v = v[1:-1]
        was_quoted = True
    if not was_quoted:
        m = re.search(r"\s+#", v)
        if m:
            v = v[:m.start()].rstrip()
    if v == "":
        return "str"
    lower = v.lower()
    if lower in ("true", "false"):
        return "bool"
    if lower in ("null", "nil", "none"):
        return "null"
    try:
        int(v)
        return "num"
    except ValueError:
        pass
    try:
        float(v)
        return "num"
    except ValueError:
        pass
    if _JWT_RE.match(v):
        return "jwt"
    return "str"


def redact_dotenv(text: str) -> dict:
    """dotenv テキストから minimal info を抽出する。

    Returns:
        {
          "format": "dotenv",
          "entries": int,
          "keys": [{"name": str, "type": str}, ...],
        }
    """
    keys: list[dict] = []
    for line in text.splitlines():
        # コメント・空行スキップ
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        raw_key, raw_val = m.group(1), m.group(2)
        keys.append({
            "name": sanitize_key(raw_key),
            "type": _classify_value(raw_val),
        })
    return {
        "format": "dotenv",
        "entries": len(keys),
        "keys": keys,
    }


def format_dotenv(info: dict) -> str:
    """redact_dotenv の結果を人間可読文字列に整形 (reason 用)。"""
    lines = [f"format: dotenv", f"entries: {info['entries']}"]
    if info["entries"] == 0:
        lines.append("(no entries)")
        return "\n".join(lines)
    lines.append("keys (in order):")
    for i, k in enumerate(info["keys"], 1):
        lines.append(f"  {i}. {k['name']}  <type={k['type']}>")
    lines.append("note: all values and comments removed for safety.")
    return "\n".join(lines)
