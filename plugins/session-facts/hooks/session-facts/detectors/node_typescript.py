from __future__ import annotations

from typing import List

from core.context import RepoContext


class NodeTypescriptDetector:
    name = "node_typescript"
    priority = 10

    def detect(self, ctx: RepoContext) -> List[str]:
        found: List[str] = []
        # Any tracked package.json (root or workspace) makes this a Node
        # repo; a monorepo whose root holds only pnpm-workspace.yaml and
        # whose apps live under apps/* used to get no "node" tag (joa.2).
        if ctx.find_in_manifest_dirs("package.json") is not None:
            found.append("node")
        if (
            ctx.find_in_manifest_dirs("tsconfig.json", "tsconfig.base.json") is not None
            or "typescript" in ctx.all_deps
        ):
            found.append("typescript")
        return found


def register():
    return NodeTypescriptDetector()
