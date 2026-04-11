from __future__ import annotations

from typing import List

from core.context import RepoContext


class PrismaDetector:
    name = "prisma"
    priority = 35

    def detect(self, ctx: RepoContext) -> List[str]:
        deps = ctx.all_deps
        if "prisma" in deps or "@prisma/client" in deps or (ctx.root / "prisma").exists():
            return ["prisma"]
        return []


def register():
    return PrismaDetector()
