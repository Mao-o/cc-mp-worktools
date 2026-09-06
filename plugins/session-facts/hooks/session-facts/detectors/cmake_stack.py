from __future__ import annotations

from typing import List

from core.context import RepoContext


class CmakeStackDetector:
    name = "cmake_stack"
    priority = 91

    def detect(self, ctx: RepoContext) -> List[str]:
        if ctx.find_in_manifest_dirs("CMakeLists.txt") is not None:
            return ["cmake"]
        return []


def register():
    return CmakeStackDetector()
