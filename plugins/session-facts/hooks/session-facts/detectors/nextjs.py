from __future__ import annotations

from typing import List

from core.constants import NEXT_CONFIG_CANDIDATES
from core.context import RepoContext


class NextjsDetector:
    name = "nextjs"
    priority = 20

    def detect(self, ctx: RepoContext) -> List[str]:
        if "next" in ctx.all_deps or ctx.find_in_manifest_dirs(*NEXT_CONFIG_CANDIDATES) is not None:
            return ["nextjs"]
        return []


def register():
    return NextjsDetector()
