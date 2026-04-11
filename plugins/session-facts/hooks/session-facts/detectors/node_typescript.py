from __future__ import annotations

from typing import List

from core.context import RepoContext


class NodeTypescriptDetector:
    name = "node_typescript"
    priority = 10

    def detect(self, ctx: RepoContext) -> List[str]:
        found: List[str] = []
        if (ctx.root / "package.json").exists():
            found.append("node")
        if (
            (ctx.root / "tsconfig.json").exists()
            or (ctx.root / "tsconfig.base.json").exists()
            or "typescript" in ctx.all_deps
        ):
            found.append("typescript")
        return found


def register():
    return NodeTypescriptDetector()
