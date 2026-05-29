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
    cwd_rel = ctx.cwd_relative
    if cwd_rel:
        lines.append(f"- cwd: {cwd_rel} (subdirectory of repo_root)")
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

    lines.extend(_render_git_progress(ctx))

    return "\n".join(lines)


def _render_git_progress(ctx: RepoContext) -> List[str]:
    git = ctx.results.get("git_progress") or {}
    lines: List[str] = []

    branch = git.get("branch")
    ahead = git.get("ahead", 0)
    behind = git.get("behind", 0)
    if branch:
        # On the default branch with nothing diverged there is no delta worth
        # reporting, so the branch line is omitted entirely.
        is_default = branch in ("main", "master")
        if not (is_default and not ahead and not behind):
            line = f"- branch: {branch}"
            upstream = git.get("upstream")
            if upstream and (ahead or behind):
                parts = []
                if ahead:
                    parts.append(f"ahead {ahead}")
                if behind:
                    parts.append(f"behind {behind}")
                line += f" ({', '.join(parts)} vs {upstream})"
            lines.append(line)

    commits = git.get("recent_commits") or []
    if commits:
        lines.append("- recent_commits:")
        for commit in commits:
            lines.append(f"  - {commit}")

    return lines
