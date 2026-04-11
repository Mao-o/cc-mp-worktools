from __future__ import annotations

from typing import List

from core.context import RepoContext


class ReactViteDetector:
    name = "react_vite"
    priority = 25

    def detect(self, ctx: RepoContext) -> List[str]:
        found: List[str] = []
        if "react" in ctx.all_deps:
            found.append("react")
        if any(
            (ctx.root / name).exists()
            for name in ("vite.config.ts", "vite.config.js")
        ):
            found.append("vite")
        return found


def register():
    return ReactViteDetector()
