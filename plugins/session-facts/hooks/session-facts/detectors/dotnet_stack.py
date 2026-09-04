from __future__ import annotations

from pathlib import Path
from typing import List

from core.context import RepoContext

# .csproj/.fsproj/.vbproj are the C#/F#/VB.NET project-file suffixes MSBuild
# recognizes; .sln covers a multi-project solution regardless of which of
# those languages its member projects use. Without .fsproj/.vbproj a
# solutionless F# or VB.NET repo (a conventional layout for small/single-
# project .NET repos) was never recognized as dotnet at all -- this
# detector is presented as ".NET support", not "C#-only support"
# (merge-review finding).
#
# .slnx is the newer XML-based solution format (.NET SDK 9+ / Visual
# Studio 17.10+) that dotnet/msbuild/VS all read the same way a classic
# .sln is read -- a root whose member .csproj/.fsproj/.vbproj live in
# subdirectories and whose only root-level solution file is .slnx was
# otherwise invisible to this detector even though it is exactly the
# ".sln-equivalent" case the tuple already exists to cover (merge-review
# finding, round 5).
_DOTNET_PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx")


def _has_dotnet_project(root: Path) -> bool:
    """Root-level ``*.csproj``/``*.fsproj``/``*.vbproj``/``*.sln``/``*.slnx``
    (core/pm.py duplicates this same check, matching the existing
    gradle-check duplication between that module and this one)."""
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
