from __future__ import annotations

from pathlib import Path

# .csproj/.fsproj/.vbproj are the C#/F#/VB.NET project-file suffixes MSBuild
# recognizes directly. A root-level file with one of these suffixes is
# .NET on its own, no further inspection needed.
_DOTNET_MANAGED_PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj")

# .sln (classic) and .slnx (newer XML-based format, .NET SDK 9+ / Visual
# Studio 17.10+) are Visual Studio solution files. Neither format is
# .NET-exclusive on its own: a solution can list only native C++ projects
# (*.vcxproj), so a root-level solution file is not by itself proof of a
# .NET repo (merge-review finding, round 6) -- it must be combined with a
# look at what the solution actually references, see
# _solution_references_managed_project() below.
_DOTNET_SOLUTION_SUFFIXES = (".sln", ".slnx")


def has_dotnet_project(root: Path) -> bool:
    """True when ``root`` is a .NET project/repo root.

    Single source of truth for ".NET here?", shared by
    detectors/dotnet_stack.py (the ``dotnet`` stack tag) and core/pm.py
    (the ``dotnet`` package manager) so the two call sites cannot drift out
    of sync. Before this, both modules separately hand-rolled the same
    root.iterdir() suffix check, and both over-matched: a root-level
    .sln/.slnx alone was treated as proof of .NET even when every member
    project the solution actually listed was native C++ (*.vcxproj), a
    conventional layout for a C++-only Visual Studio solution (merge-review
    finding, round 6).

    A root-level .csproj/.fsproj/.vbproj is always .NET. A root-level
    .sln/.slnx with no such file directly at root is only treated as .NET
    if the solution's own contents reference a managed project: classic
    .sln lists members as
    ``Project(...) = "Name", "path\\App.csproj", "{guid}"`` lines, and
    .slnx lists them as ``<Project Path="src/App.csproj" />`` elements.
    Both are recognized by a case-insensitive substring search for the
    three managed suffixes rather than actually parsing either format --
    sufficient because neither format uses those suffixes anywhere except
    a member project's path. An unreadable or empty solution file is
    treated as not-.NET (fail-closed) rather than guessed either way.

    Unlike this check, the plain gradle exists() check duplicated between
    core/pm.py and detectors/java_stack.py is left duplicated: both sides
    of that one are a single trivial ``exists()`` call with no parsing
    logic worth protecting behind a shared helper.
    """
    try:
        root_files = [p for p in root.iterdir() if p.is_file()]
    except OSError:
        return False

    if any(p.suffix in _DOTNET_MANAGED_PROJECT_SUFFIXES for p in root_files):
        return True

    solutions = (p for p in root_files if p.suffix in _DOTNET_SOLUTION_SUFFIXES)
    return any(_solution_references_managed_project(p) for p in solutions)


def _solution_references_managed_project(solution: Path) -> bool:
    """True when ``solution``'s own text mentions a managed project file."""
    try:
        text = solution.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if not text:
        return False
    lower = text.lower()
    return any(suffix in lower for suffix in _DOTNET_MANAGED_PROJECT_SUFFIXES)
