from __future__ import annotations

from typing import List

from core.context import RepoContext


class DockerDetector:
    name = "docker"
    priority = 95

    def detect(self, ctx: RepoContext) -> List[str]:
        root = ctx.root
        if any(
            (root / f).exists()
            for f in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
        ):
            return ["docker"]
        return []


def register():
    return DockerDetector()
