from __future__ import annotations

from typing import Optional

from core.context import RepoContext
from core.tree import build_dir_tree, render_tree, truncate_lines


class StructureCollector:
    name = "structure"
    section_title = "## Structure (dirs only)"
    priority = 10

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        depth = ctx.args.tree_depth
        dir_tree = build_dir_tree(ctx.tracked_files, depth)
        tree_lines = truncate_lines(render_tree(dir_tree), ctx.args.max_tree_lines)
        if not tree_lines:
            return None
        lines = [f"## Structure (dirs only, depth={depth})"]
        lines.extend(tree_lines)
        return "\n".join(lines)


def register():
    return StructureCollector()
