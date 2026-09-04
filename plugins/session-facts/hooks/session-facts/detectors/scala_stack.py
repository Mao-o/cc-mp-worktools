from __future__ import annotations

from typing import List

from core.context import RepoContext


class ScalaStackDetector:
    name = "scala_stack"
    priority = 57

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "build.sbt").exists():
            return ["scala"]
        return []


def register():
    return ScalaStackDetector()
