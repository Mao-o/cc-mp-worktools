from __future__ import annotations

from typing import List, Optional

from core.context import RepoContext
from core.tree import build_dir_tree, render_tree, truncate_lines


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

        depth = ctx.args.tree_depth
        dir_tree = build_dir_tree(cwd_files, depth)
        tree_lines = truncate_lines(render_tree(dir_tree), ctx.args.max_tree_lines)
        if not tree_lines:
            return None

        lines = [f"## Subtree (cwd: {rel}, dirs only, depth={depth})"]
        lines.extend(tree_lines)
        return "\n".join(lines)


def register():
    return CwdSubtreeCollector()
