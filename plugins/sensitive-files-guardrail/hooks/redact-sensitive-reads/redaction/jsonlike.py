"""JSON (および JSON 類似の構造データ) の minimal-info 化。

保持: 鍵名の階層構造、各鍵の型クラス、配列長/オブジェクト子要素数、
str スカラ値の status (set/empty/placeholder/long) と length。
破棄: 値そのもの (str/num/bool/null 全て)。

0.14.0 (E5): 文字列スカラ値に対して dotenv と同じ placeholder 判定 + length +
``<empty>`` / ``<placeholder>`` / ``<set>`` / ``<long>`` / ``<looks_truncated>``
を付与する。bool / num / null / array / object には status を出さない (構造側
は値を持たないため意味がない)。``<short>`` は型クラス前提のため json では
非対象。
"""
from __future__ import annotations

import json
from typing import Any

from .placeholders import looks_placeholder
from .sanitize import sanitize_key

MAX_DEPTH = 8
MAX_KEYS_PER_NODE = 200

_MAX_STR_LENGTH_GENERIC = 4096


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def _classify_str_status(v: str) -> tuple[list[str], int, str | None]:
    """文字列値から status タグ群・length・placeholder ラベルを返す。

    Returns:
        (tags, length, placeholder_label).
        - 空文字列の場合: (["<empty>"], 0, None)
        - placeholder 一致: (["<placeholder>", ...], length, label)
        - それ以外: (["<set>", ...], length, None)
        - ``<long>`` (4096 byte 超) と ``<looks_truncated>`` (末尾が ``...`` /
          ``<truncated>`` / バックスラッシュ) は併記しうる
    """
    if v == "":
        return (["<empty>"], 0, None)

    is_ph, ph_label = looks_placeholder(v)
    tags: list[str] = ["<placeholder>"] if is_ph else ["<set>"]
    n = len(v)
    if n > _MAX_STR_LENGTH_GENERIC:
        tags.append("<long>")
    if v.endswith("...") or v.endswith("<truncated>") or v.endswith("\\"):
        tags.append("<looks_truncated>")
    return (tags, n, ph_label if is_ph else None)


def _walk(v: Any, depth: int = 0) -> Any:
    """値を再帰的に minimal-info 化する。値は type 情報だけに潰す。"""
    if depth >= MAX_DEPTH:
        return {"_type": _type_name(v), "_truncated": True}
    t = _type_name(v)
    if t == "object":
        children: list[dict] = []
        items = list(v.items())
        truncated = len(items) > MAX_KEYS_PER_NODE
        for k, sub in items[:MAX_KEYS_PER_NODE]:
            sub_type = _type_name(sub)
            child: dict = {
                "name": sanitize_key(str(k)),
                "type": sub_type,
                "value": _walk(sub, depth + 1) if sub_type in ("object", "array") else None,
            }
            if sub_type == "str":
                tags, length, ph_label = _classify_str_status(sub)
                child["status"] = tags
                child["length"] = length
                if ph_label is not None:
                    child["placeholder"] = ph_label
            children.append(child)
        return {
            "_type": "object",
            "_count": len(items),
            "_truncated": truncated,
            "children": children,
        }
    if t == "array":
        return {
            "_type": "array",
            "_count": len(v),
            "_element_types": sorted({_type_name(x) for x in v[:50]}),
        }
    return {"_type": t}


def redact_jsonlike(text: str) -> dict:
    """JSON テキストから minimal info を抽出する。

    Returns:
        {"format": "json", "root": {...}, "entries": int (top-level count)}
    """
    data = json.loads(text)
    root = _walk(data, 0)
    if root.get("_type") == "object":
        entries = root["_count"]
    elif root.get("_type") == "array":
        entries = root["_count"]
    else:
        entries = 1
    return {"format": "json", "root": root, "entries": entries}


def format_jsonlike(info: dict) -> str:
    lines = [f"format: {info['format']}", f"entries: {info['entries']}"]
    root = info["root"]

    def emit(node: dict, indent: str) -> None:
        t = node.get("_type", "unknown")
        if t == "object":
            count = node.get("_count", 0)
            lines.append(f"{indent}<object, {count} children{' (truncated)' if node.get('_truncated') else ''}>")
            for child in node.get("children", []):
                ct = child["type"]
                line = f"{indent}  {child['name']}  <type={ct}>"
                if ct == "str" and "status" in child:
                    status_part = "  ".join(child["status"])
                    line += f"  {status_part}"
                    if "placeholder" in child:
                        line += f'  matched="{child["placeholder"]}"'
                    if "<empty>" not in child["status"]:
                        line += f"  length={child['length']}"
                lines.append(line)
                if ct in ("object", "array") and child.get("value"):
                    emit(child["value"], indent + "    ")
        elif t == "array":
            count = node.get("_count", 0)
            elem_types = node.get("_element_types", [])
            lines.append(f"{indent}<array, {count} elements, types={elem_types}>")
        else:
            lines.append(f"{indent}<type={t}>")

    emit(root, "")
    lines.append(
        "note: string scalar values are summarized to status tags and length only."
        " array/object counts shown; non-string values removed."
    )
    return "\n".join(lines)
