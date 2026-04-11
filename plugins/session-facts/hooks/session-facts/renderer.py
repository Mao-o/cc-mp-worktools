from __future__ import annotations

from typing import List

from core.context import RepoContext


def render_header(ctx: RepoContext) -> str:
    """Render the ## Project Facts header section."""
    lines: List[str] = []
    lines.append("## Project Facts")

    purpose = ctx.results.get("purpose")
    if purpose:
        lines.append(f"- purpose: {purpose}")
    lines.append(f"- repo_root: {ctx.root}")
    if ctx.results.get("is_git_repo") is False:
        lines.append("- git_repo: false (using filesystem walk)")

    pm = ctx.results.get("package_manager")
    if pm:
        lines.append(f"- package_manager: {pm}")
    if ctx.stack:
        lines.append(f"- stack: {', '.join(ctx.stack)}")

    major_deps = ctx.results.get("major_dependencies")
    if major_deps:
        lines.append(f"- major_dependencies: {', '.join(major_deps)}")

    return "\n".join(lines)
