"""JSON (および JSON 類似の構造データ) の minimal-info 化。

保持: 鍵名の階層構造、各鍵の型クラス、配列長/オブジェクト子要素数。
破棄: 値そのもの (str/num/bool/null 全て)。
"""
from __future__ import annotations

import json
from typing import Any

from .sanitize import sanitize_key

MAX_DEPTH = 8
MAX_KEYS_PER_NODE = 200


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
            children.append({
                "name": sanitize_key(str(k)),
                "type": _type_name(sub),
                "value": _walk(sub, depth + 1) if _type_name(sub) in ("object", "array") else None,
            })
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
    lines = [f"format: json", f"entries: {info['entries']}"]
    root = info["root"]

    def emit(node: dict, indent: str) -> None:
        t = node.get("_type", "unknown")
        if t == "object":
            count = node.get("_count", 0)
            lines.append(f"{indent}<object, {count} children{' (truncated)' if node.get('_truncated') else ''}>")
            for child in node.get("children", []):
                ct = child["type"]
                lines.append(f"{indent}  {child['name']}  <type={ct}>")
                if ct in ("object", "array") and child.get("value"):
                    emit(child["value"], indent + "    ")
        elif t == "array":
            count = node.get("_count", 0)
            elem_types = node.get("_element_types", [])
            lines.append(f"{indent}<array, {count} elements, types={elem_types}>")
        else:
            lines.append(f"{indent}<type={t}>")

    emit(root, "")
    lines.append("note: values removed. array/object counts shown only.")
    return "\n".join(lines)
