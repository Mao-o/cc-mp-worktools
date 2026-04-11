from __future__ import annotations

from typing import List

from core.context import RepoContext


class PhpStackDetector:
    name = "php_stack"
    priority = 90

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "composer.json").exists():
            return ["php"]
        return []


def register():
    return PhpStackDetector()
