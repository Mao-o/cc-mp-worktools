from __future__ import annotations

from pathlib import Path
from typing import List

from core.context import RepoContext

_DOTNET_PROJECT_SUFFIXES = (".csproj", ".sln")


def _has_dotnet_project(root: Path) -> bool:
    """Root-level ``*.csproj``/``*.sln`` (core/pm.py duplicates this same
    check, matching the existing gradle-check duplication between that
    module and this one)."""
    try:
        return any(p.suffix in _DOTNET_PROJECT_SUFFIXES for p in root.iterdir() if p.is_file())
    except OSError:
        return False


class DotnetStackDetector:
    name = "dotnet_stack"
    priority = 85

    def detect(self, ctx: RepoContext) -> List[str]:
        if _has_dotnet_project(ctx.root):
            return ["dotnet"]
        return []


def register():
    return DotnetStackDetector()
