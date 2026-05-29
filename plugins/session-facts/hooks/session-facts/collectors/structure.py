from __future__ import annotations

from typing import Optional

from core.context import RepoContext
from core.tree import select_tree_lines


class StructureCollector:
    name = "structure"
    section_title = "## Structure (dirs only)"
    priority = 10

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        cfg = ctx.config
        if ctx.cwd_relative:
            # Subtree mode: the cwd-scoped detail lives in the Subtree section,
            # so the repo-wide map shrinks to top-level dir names only — just
            # enough to know which other modules exist for cross-cutting work.
            tree_lines, depth = select_tree_lines(
                ctx.tracked_files, cfg.max_tree_lines, fixed_depth=1
            )
        else:
            tree_lines, depth = select_tree_lines(
                ctx.tracked_files,
                cfg.max_tree_lines,
                min_depth=cfg.min_tree_depth,
                max_depth=cfg.max_tree_depth,
                fixed_depth=cfg.tree_depth,
            )
        if not tree_lines:
            return None
        lines = [f"## Structure (dirs only, depth={depth})"]
        lines.extend(tree_lines)
        return "\n".join(lines)


def register():
    return StructureCollector()
