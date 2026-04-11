from __future__ import annotations

from typing import List

from core.context import RepoContext


class MiseDetector:
    name = "mise"
    priority = 5

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / ".mise.toml").exists() or (ctx.root / ".tool-versions").exists():
            return ["mise"]
        return []


def register():
    return MiseDetector()
