"""Per-workspace summary for monorepo / "api/ + web/" layouts (internal
backlog joa.2).

The root-level stack line is a union over every manifest the repo tracks;
this module produces the per-directory breakdown the header shows as
``- workspaces: api (uv: python, fastapi), web (pnpm: nextjs, react)`` so an
agent knows *which* sub-project each stack tag came from. Deliberately a
small, fixed vocabulary rather than re-running every detector per
directory: the point is orientation, not completeness.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from core.pm import detect_package_manager_at

if TYPE_CHECKING:
    from core.context import RepoContext

MAX_WORKSPACES_IN_HEADER = 8
MAX_TAGS_PER_WORKSPACE = 4

# npm dependency name -> tag, in display priority order.
_JS_DEP_TAGS = (
    ("next", "nextjs"),
    ("nuxt", "nuxt"),
    ("@remix-run/react", "remix"),
    ("astro", "astro"),
    ("react", "react"),
    ("vue", "vue"),
    ("svelte", "svelte"),
    ("express", "express"),
    ("hono", "hono"),
    ("fastify", "fastify"),
    ("@nestjs/core", "nestjs"),
    ("firebase-functions", "firebase-functions"),
    ("firebase", "firebase"),
    ("vite", "vite"),
    ("typescript", "typescript"),
)
_PY_TEXT_TAGS = (
    ("fastapi", "fastapi"),
    ("django", "django"),
    ("flask", "flask"),
)


def describe_workspace(ctx: "RepoContext", rel_dir: str) -> Dict[str, object]:
    """``{"dir", "pm", "tags"}`` for one workspace directory."""
    root = ctx.root / rel_dir
    tags: List[str] = []
    pkg: Optional[dict] = None
    for d, data in ctx.package_json_manifests():
        if d == rel_dir:
            pkg = data
            break
    if pkg is not None:
        deps: Dict[str, str] = {}
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            section_deps = pkg.get(section)
            if isinstance(section_deps, dict):
                deps.update(section_deps)
        tags.append("node")
        for dep, tag in _JS_DEP_TAGS:
            if dep in deps and tag not in tags:
                tags.append(tag)
    pyproject = ""
    for d, text in ctx.pyproject_manifests():
        if d == rel_dir:
            pyproject = text
            break
    if pyproject:
        tags.append("python")
        lowered = pyproject.lower()
        for needle, tag in _PY_TEXT_TAGS:
            if needle in lowered:
                tags.append(tag)
    pm = detect_package_manager_at(root)
    tags = [t for t in tags if t != pm]
    return {
        "dir": rel_dir,
        "pm": pm,
        "tags": tags[:MAX_TAGS_PER_WORKSPACE],
    }


def summarize_workspaces(ctx: "RepoContext") -> List[Dict[str, object]]:
    return [describe_workspace(ctx, d) for d in ctx.workspace_dirs[:MAX_WORKSPACES_IN_HEADER]]


def render_workspaces_line(workspaces: List[Dict[str, object]], total: int) -> Optional[str]:
    """``- workspaces: api (uv: python, fastapi), web (pnpm: nextjs, react)``
    or None when there are no workspaces."""
    if not workspaces:
        return None
    parts: List[str] = []
    for ws in workspaces:
        inner: List[str] = []
        pm = ws.get("pm")
        tags = list(ws.get("tags") or [])
        if pm and tags:
            inner.append(f"{pm}: {', '.join(tags)}")
        elif pm:
            inner.append(str(pm))
        elif tags:
            inner.append(", ".join(tags))
        parts.append(f"{ws['dir']} ({'; '.join(inner)})" if inner else str(ws["dir"]))
    line = f"- workspaces: {', '.join(parts)}"
    if total > len(workspaces):
        line += f" (+{total - len(workspaces)} more)"
    return line
