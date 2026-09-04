from __future__ import annotations

from typing import Optional, TYPE_CHECKING

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
    if _has_dotnet_project(root):
        return "dotnet"
    return None


def _has_dotnet_project(root) -> bool:
    """Root-level ``*.csproj``/``*.sln`` (detectors/dotnet_stack.py duplicates
    this same check, matching the existing gradle-check duplication between
    this module and detectors/java_stack.py -- pm.py and the stack detector
    each own their check rather than sharing a helper across the layer
    boundary).
    """
    try:
        return any(p.suffix in (".csproj", ".sln") for p in root.iterdir() if p.is_file())
    except OSError:
        return False
