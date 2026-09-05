from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from core.dotnet import has_dotnet_project

if TYPE_CHECKING:
    from core.context import RepoContext


def detect_package_manager(ctx: "RepoContext") -> Optional[str]:
    root = ctx.root
    # JS/TS
    if (root / "pnpm-lock.yaml").exists() or (root / "pnpm-workspace.yaml").exists():
        return "pnpm"
    if (root / "package-lock.json").exists():
        return "npm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        return "bun"
    if (root / "deno.json").exists() or (root / "deno.jsonc").exists():
        return "deno"
    # Python — uv/poetry take precedence over plain python
    if (root / "uv.lock").exists() or (root / "uv.toml").exists():
        return "uv"
    if (root / "poetry.lock").exists():
        return "poetry"
    if (root / "pyproject.toml").exists():
        return "python"
    # JVM
    if (root / "gradlew").exists() or (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return "gradle"
    if (root / "pom.xml").exists():
        return "maven"
    if (root / "build.sbt").exists():
        return "sbt"
    # Other
    if (root / "go.mod").exists():
        return "go"
    if (root / "Cargo.toml").exists():
        return "cargo"
    if (root / "composer.json").exists():
        return "composer"
    if (root / "mix.exs").exists():
        return "mix"
    if (root / "Package.swift").exists():
        return "swift"
    # has_dotnet_project() is the single source of truth shared with
    # detectors/dotnet_stack.py (unlike the gradle exists() check just
    # above, which stays duplicated between this module and
    # detectors/java_stack.py -- that one is a single trivial exists() call
    # on each side, with no parsing logic worth protecting behind a shared
    # helper; the dotnet check now has to inspect solution file contents,
    # see core/dotnet.py, so a drift between two hand-rolled copies would
    # be much easier to introduce and much harder to notice).
    if has_dotnet_project(root):
        return "dotnet"
    return None
