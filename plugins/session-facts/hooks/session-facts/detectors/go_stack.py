from __future__ import annotations

from typing import List

from core.context import RepoContext


class GoStackDetector:
    name = "go_stack"
    priority = 60

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "go.mod").exists():
            return ["go"]
        return []


def register():
    return GoStackDetector()
