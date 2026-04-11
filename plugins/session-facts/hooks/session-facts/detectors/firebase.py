from __future__ import annotations

from typing import List

from core.context import RepoContext


class FirebaseDetector:
    name = "firebase"
    priority = 30

    def detect(self, ctx: RepoContext) -> List[str]:
        deps = ctx.all_deps
        if "firebase" in deps or any(
            name.startswith("@firebase/") for name in deps
        ):
            return ["firebase"]
        return []


def register():
    return FirebaseDetector()
