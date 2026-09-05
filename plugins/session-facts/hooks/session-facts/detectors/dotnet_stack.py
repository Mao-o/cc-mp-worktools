from __future__ import annotations

from typing import List

from core.context import RepoContext
from core.dotnet import has_dotnet_project


class DotnetStackDetector:
    name = "dotnet_stack"
    priority = 85

    def detect(self, ctx: RepoContext) -> List[str]:
        if has_dotnet_project(ctx.root):
            return ["dotnet"]
        return []


def register():
    return DotnetStackDetector()
