from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Sequence


class Node(dict):
    pass


def build_dir_tree(paths: Iterable[str], max_depth: int) -> Node:
    root: Node = Node()
    for path in paths:
        parts = Path(path).parts[:-1]
        if not parts:
            continue
        current = root
        for part in parts[:max_depth]:
            current = current.setdefault(part, Node())
    return root


def render_tree(node: Node, prefix: str = "") -> List[str]:
    lines: List[str] = []
    items = sorted(node.items(), key=lambda kv: kv[0])
    for index, (name, child) in enumerate(items):
        is_last = index == len(items) - 1
        branch = "└── " if is_last else "├── "
        lines.append(prefix + branch + name + "/")
        extension = "    " if is_last else "│   "
        lines.extend(render_tree(child, prefix + extension))
    return lines


def truncate_lines(lines: Sequence[str], max_lines: int) -> List[str]:
    if len(lines) <= max_lines:
        return list(lines)
    omitted = len(lines) - max_lines
    return list(lines[:max_lines]) + [f"... ({omitted} more lines omitted)"]
