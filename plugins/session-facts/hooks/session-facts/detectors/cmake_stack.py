from __future__ import annotations

from typing import List

from core.context import RepoContext


class CmakeStackDetector:
    name = "cmake_stack"
    priority = 91

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "CMakeLists.txt").exists():
            return ["cmake"]
        return []


def register():
    return CmakeStackDetector()
