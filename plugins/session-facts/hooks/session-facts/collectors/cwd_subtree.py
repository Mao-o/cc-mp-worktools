from __future__ import annotations

from typing import List, Optional

from core.context import RepoContext
from core.tree import select_tree_lines


class CwdSubtreeCollector:
    name = "cwd_subtree"
    section_title = "## Subtree (cwd-scoped, dirs only)"
    priority = 15

    def should_run(self, ctx: RepoContext) -> bool:
        if not ctx.tracked_files:
            return False
        rel = ctx.cwd_relative
        if not rel:
            return False
        prefix = rel + "/"
        return any(p.startswith(prefix) for p in ctx.tracked_files)

    def collect(self, ctx: RepoContext) -> Optional[str]:
        rel = ctx.cwd_relative
        if not rel:
            return None
        prefix = rel + "/"
        cwd_files: List[str] = [
            p[len(prefix):]
            for p in ctx.tracked_files
            if p.startswith(prefix)
        ]
        if not cwd_files:
            return None

        cfg = ctx.config
        tree_lines, depth = select_tree_lines(
            cwd_files,
            cfg.max_tree_lines,
            min_depth=cfg.min_tree_depth,
            max_depth=cfg.max_tree_depth,
            fixed_depth=cfg.tree_depth,
        )
        if not tree_lines:
            return None

        lines = [f"## Subtree (cwd: {rel}, dirs only, depth={depth})"]
        lines.extend(tree_lines)
        return "\n".join(lines)


def register():
    return CwdSubtreeCollector()
