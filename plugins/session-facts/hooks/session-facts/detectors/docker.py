from __future__ import annotations

from typing import List

from core.constants import COMPOSE_FILE_CANDIDATES
from core.context import RepoContext


class DockerDetector:
    name = "docker"
    priority = 95

    def detect(self, ctx: RepoContext) -> List[str]:
        if ctx.find_in_manifest_dirs("Dockerfile", *COMPOSE_FILE_CANDIDATES) is not None:
            return ["docker"]
        return []


def register():
    return DockerDetector()
