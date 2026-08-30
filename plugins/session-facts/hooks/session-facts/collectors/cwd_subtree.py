from __future__ import annotations

from typing import List, Optional

from core.context import RepoContext
from core.tree import select_tree_lines
from core.util import filter_to_cwd


class CwdSubtreeCollector:
    name = "cwd_subtree"
    section_title = "## Subtree (cwd-scoped, dirs only)"
    priority = 15

    def should_run(self, ctx: RepoContext) -> bool:
        if not ctx.tracked_files or not ctx.cwd_relative:
            return False
        return bool(filter_to_cwd(ctx.tracked_files, ctx.cwd_relative))

    def collect(self, ctx: RepoContext) -> Optional[str]:
        rel = ctx.cwd_relative
        if not rel:
            return None
        # filter_to_cwd() only filters (keeps paths relative to repo root, as
        # its other callers -- services.py, tests.py -- need); the tree
        # itself must be rooted at cwd, so the shared prefix is additionally
        # stripped here, which is this collector's own requirement, not a
        # rule filter_to_cwd's other callers share.
        prefix = rel + "/"
        cwd_files: List[str] = [
            p[len(prefix):] for p in filter_to_cwd(ctx.tracked_files, rel)
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
