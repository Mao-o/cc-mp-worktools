from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from core.constants import CODE_EXTENSIONS, SERVICE_DIR_MARKERS
from core.context import RepoContext
from core.util import filter_to_cwd

DEPRIORITY_NAMES = {
    "__init__.py",
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
    "types.ts",
    "types.py",
    "type.ts",
    "mod.rs",
}
PRIORITY_NAMES = {
    "__main__.py",
    "main.py",
    "main.go",
    "main.rs",
    "main.ts",
    "main.js",
    "app.py",
    "server.py",
    "server.ts",
    "server.js",
}


class ServicesCollector:
    name = "services"
    section_title = "## Service Entry Points"
    priority = 20

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_service_entries
        cwd_rel = ctx.cwd_relative

        if not cwd_rel:
            entries = _collect_service_entries(ctx.tracked_files, max_items)
            if not entries:
                return None
            return _format_section(self.section_title, entries)

        cwd_files = filter_to_cwd(ctx.tracked_files, cwd_rel)
        cwd_entries = _collect_service_entries(cwd_files, max_items)
        repo_entries = _collect_service_entries(
            ctx.tracked_files, max(max_items // 2, 4)
        )
        repo_minus_cwd = [p for p in repo_entries if p not in cwd_entries]

        sections = []
        if cwd_entries:
            sections.append(
                _format_section(f"## Service Entry Points (cwd: {cwd_rel})", cwd_entries)
            )
        if repo_minus_cwd:
            sections.append(
                _format_section("## Service Entry Points (repo-wide)", repo_minus_cwd)
            )
        elif not cwd_entries and repo_entries:
            sections.append(_format_section(self.section_title, repo_entries))
        return "\n\n".join(sections) if sections else None


def _format_section(title: str, entries: List[str]) -> str:
    lines = [title]
    for path in entries:
        lines.append(f"- {path}")
    return "\n".join(lines)


def _collect_service_entries(tracked_files: List[str], max_items: int) -> List[str]:
    candidates: List[Tuple[int, str]] = []
    for path_str in tracked_files:
        p = Path(path_str)
        if p.suffix.lower() not in CODE_EXTENSIONS:
            continue
        lowered_parts = [part.lower() for part in p.parts[:-1]]
        score = 0
        for marker in SERVICE_DIR_MARKERS:
            if marker in lowered_parts:
                score += 5
        name = p.name.lower()
        if any(
            token in name
            for token in ("service", "client", "repository", "gateway", "adapter", "usecase", "api")
        ):
            score += 2
        if name in DEPRIORITY_NAMES:
            score -= 3
        if name in PRIORITY_NAMES:
            score += 3
        if score > 0:
            candidates.append((-score, path_str))
    ordered = [
        path
        for _score, path in sorted(set(candidates), key=lambda item: (item[0], item[1]))
    ]
    return ordered[:max_items]


def register():
    return ServicesCollector()
