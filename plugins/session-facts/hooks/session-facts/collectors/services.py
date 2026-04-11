from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from core.constants import CODE_EXTENSIONS, SERVICE_DIR_MARKERS
from core.context import RepoContext


class ServicesCollector:
    name = "services"
    section_title = "## Service Entry Points"
    priority = 20

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_service_entries
        entries = _collect_service_entries(ctx.tracked_files, max_items)
        if not entries:
            return None
        lines = [self.section_title]
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
        if score > 0:
            candidates.append((-score, path_str))
    ordered = [
        path
        for _score, path in sorted(set(candidates), key=lambda item: (item[0], item[1]))
    ]
    return ordered[:max_items]


def register():
    return ServicesCollector()
