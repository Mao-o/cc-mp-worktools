from __future__ import annotations

from typing import List

from core.constants import NEXT_CONFIG_CANDIDATES
from core.context import RepoContext


class NextjsDetector:
    name = "nextjs"
    priority = 20

    def detect(self, ctx: RepoContext) -> List[str]:
        if "next" in ctx.all_deps or any(
            (ctx.root / f).exists() for f in NEXT_CONFIG_CANDIDATES
        ):
            return ["nextjs"]
        return []


def register():
    return NextjsDetector()
