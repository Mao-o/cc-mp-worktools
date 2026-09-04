from __future__ import annotations

from typing import List

from core.context import RepoContext


class SwiftStackDetector:
    name = "swift_stack"
    priority = 65

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "Package.swift").exists():
            return ["swift"]
        return []


def register():
    return SwiftStackDetector()
