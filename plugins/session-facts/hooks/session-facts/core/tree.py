from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from core.constants import MAX_TREE_CHILDREN, SKIP_TREE_CHILDREN


class Node(dict):
    pass


def _visible_items(node: Node, depth: int) -> List[Tuple[str, "Node"]]:
    """Children of ``node`` in display order. Below the root (``depth > 1``)
    dot-directories (``api/.idea``, ``web/.storybook``) are dropped
    entirely: they are tool noise at every level, while root-level ones
    stay (collapsed) so the reader knows which harness/CI config exists."""
    items = sorted(node.items(), key=lambda kv: kv[0])
    if depth > 1:
        items = [(n, c) for n, c in items if not n.startswith(".")]
    return items


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


def render_tree(
    node: Node,
    prefix: str = "",
    depth_cap: Optional[int] = None,
    compress: bool = True,
    _depth: int = 1,
    collapse: Optional[Set[str]] = None,
) -> List[str]:
    """Render a directory tree.

    - ``depth_cap``: maximum path-part depth to render. ``None`` means render
      everything that ``build_dir_tree`` produced.
    - ``compress``: collapse single-child chains onto one row (``a/b/c/``)
      instead of nesting them across separate rows. The merged chain still
      consumes its real path-part depth, so ``depth_cap`` stays meaningful
      regardless of compression.
    - ``collapse``: directory names rendered as one row with their children
      hidden (``.github/``, ``tests/`` ...). Defaults to SKIP_TREE_CHILDREN;
      pass an empty set to render everything. A collapsed directory is
      marked with a trailing ``…`` only when it actually had children, so
      the reader can tell "hidden" from "empty".
    """
    if collapse is None:
        collapse = SKIP_TREE_CHILDREN
    lines: List[str] = []
    items = _visible_items(node, _depth)
    hidden = 0
    if _depth > 1 and len(items) > MAX_TREE_CHILDREN:
        # A directory with dozens of same-shaped children (locale dirs,
        # per-brand style dirs) would otherwise force the whole tree down
        # to depth 1 under the line budget; show the first few and count
        # the rest, so the *other* top-level directories keep their depth.
        # The root level itself is never capped: hiding a top-level
        # directory hides a whole module (dify's ``web/`` was the victim).
        hidden = len(items) - MAX_TREE_CHILDREN
        items = items[:MAX_TREE_CHILDREN]
    for index, (name, child) in enumerate(items):
        is_last = index == len(items) - 1 and not hidden
        branch = "└── " if is_last else "├── "

        if name in collapse:
            marker = "/…" if child else "/"
            lines.append(prefix + branch + name + marker)
            continue

        chain_name = name
        cur = child
        cur_depth = _depth
        if compress:
            while depth_cap is None or cur_depth < depth_cap:
                visible = _visible_items(cur, cur_depth + 1)
                if len(visible) != 1 or visible[0][0] in collapse:
                    break
                only_name, only_child = visible[0]
                chain_name += "/" + only_name
                cur = only_child
                cur_depth += 1

        lines.append(prefix + branch + chain_name + "/")
        if depth_cap is not None and cur_depth >= depth_cap:
            continue
        extension = "    " if is_last else "│   "
        lines.extend(
            render_tree(cur, prefix + extension, depth_cap, compress, cur_depth + 1, collapse)
        )
    if hidden:
        lines.append(prefix + f"└── … (+{hidden} more dirs)")
    return lines


def truncate_lines(lines: Sequence[str], max_lines: int) -> List[str]:
    if len(lines) <= max_lines:
        return list(lines)
    omitted = len(lines) - max_lines
    return list(lines[:max_lines]) + [f"... ({omitted} more lines omitted)"]


def select_tree_lines(
    paths: Iterable[str],
    max_lines: int,
    *,
    min_depth: int = 1,
    max_depth: int = 5,
    fixed_depth: Optional[int] = None,
    compress: bool = True,
    collapse: Optional[Set[str]] = None,
) -> Tuple[List[str], int]:
    """Build the dir tree once and pick the deepest rendering that still fits.

    Strategy E (build-once, render-many, early-stop): ``build_dir_tree`` walks
    every tracked file and is the expensive step, so it runs a single time at
    ``max_depth``. ``render_tree`` is cheap, so we render at increasing
    ``depth_cap`` values and keep the largest depth whose (compression-aware)
    line count stays at or below ``max_lines``. Because line count is
    monotonically non-decreasing in depth, we can stop at the first overflow.

    When ``fixed_depth`` is given the dynamic search is skipped and that exact
    depth is rendered (used for the CLI ``--tree-depth`` override and the
    subtree-mode top-level-only structure).

    Returns ``(lines, depth_used)``. ``lines`` is truncated as a last resort
    only when even ``min_depth`` (or ``fixed_depth``) overflows ``max_lines``.
    """
    paths = list(paths)

    if fixed_depth is not None:
        tree = build_dir_tree(paths, fixed_depth)
        lines = render_tree(tree, depth_cap=fixed_depth, compress=compress, collapse=collapse)
        return truncate_lines(lines, max_lines), fixed_depth

    if min_depth > max_depth:
        min_depth = max_depth

    tree = build_dir_tree(paths, max_depth)
    chosen: Optional[List[str]] = None
    chosen_depth = min_depth
    for depth in range(min_depth, max_depth + 1):
        lines = render_tree(tree, depth_cap=depth, compress=compress, collapse=collapse)
        if len(lines) <= max_lines:
            chosen = lines
            chosen_depth = depth
        else:
            break

    if chosen is None:
        # Even the shallowest depth overflows; truncate as a last resort.
        lines = render_tree(tree, depth_cap=min_depth, compress=compress, collapse=collapse)
        return truncate_lines(lines, max_lines), min_depth

    return chosen, chosen_depth
