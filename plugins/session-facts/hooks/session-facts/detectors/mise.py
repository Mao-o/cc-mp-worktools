from __future__ import annotations

from typing import List

from core.context import RepoContext
from core.runtime import has_mise


class MiseDetector:
    name = "mise"
    priority = 5

    def detect(self, ctx: RepoContext) -> List[str]:
        # has_mise also recognises the dotless ``mise.toml`` and
        # ``.config/mise/config.toml`` locations the inline check used to miss.
        return ["mise"] if has_mise(ctx.root) else []


def register():
    return MiseDetector()
