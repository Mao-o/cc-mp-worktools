from __future__ import annotations

from typing import List

from core.context import RepoContext


class TaskrunnerDetector:
    name = "taskrunner"
    priority = 92

    def detect(self, ctx: RepoContext) -> List[str]:
        root = ctx.root
        found: List[str] = []
        if (root / "Makefile").exists():
            found.append("makefile")
        if (root / "Justfile").exists() or (root / "justfile").exists():
            found.append("justfile")
        if (root / "Taskfile.yml").exists() or (root / "Taskfile.yaml").exists():
            found.append("taskfile")
        if (root / "nx.json").exists():
            found.append("nx")
        return found


def register():
    return TaskrunnerDetector()
