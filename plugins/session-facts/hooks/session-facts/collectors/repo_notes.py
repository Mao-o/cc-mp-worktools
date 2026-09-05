from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional

from core.context import RepoContext
from core.firebase import has_firebase
from core.util import has_app_router, has_pages_router


class RepoNotesCollector:
    name = "repo_notes"
    section_title = "## Repo-Specific Notes"
    priority = 70

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_notes
        notes = _collect_repo_specific_notes(ctx, max_items)
        if not notes:
            return None
        lines = [self.section_title]
        for note in notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


# A directory-name note is only worth printing when it tells the reader
# something the ## Structure tree does not already show. ``api/`` as a
# top-level directory is visible in the tree; ``functions/src/api`` (41
# files) two levels down is not. Notes therefore name the directories with
# their file counts, and a segment that is a *top-level* directory is
# suppressed when it is the only evidence (internal backlog joa.18).
_MIN_API_FILES = 20
_MAX_DIRS_PER_NOTE = 3


def _dirs_named(tracked_files: List[str], segment: str) -> Dict[str, int]:
    """``{dir path ending in <segment>: file count below it}``, counting each
    file once for the shallowest matching ancestor."""
    counts: Counter = Counter()
    for path in tracked_files:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower() == segment:
                counts["/".join(parts[: i + 1])] += 1
                break
    return dict(counts)


def _format_dirs(dirs: Dict[str, int]) -> str:
    ranked = sorted(dirs.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = [f"{d}/ ({n:,} files)" for d, n in ranked[:_MAX_DIRS_PER_NOTE]]
    if len(ranked) > _MAX_DIRS_PER_NOTE:
        shown.append(f"+{len(ranked) - _MAX_DIRS_PER_NOTE} more")
    return ", ".join(shown)


def _non_top_level(dirs: Dict[str, int]) -> Dict[str, int]:
    return {d: n for d, n in dirs.items() if "/" in d}


def _collect_repo_specific_notes(ctx: RepoContext, max_items: int) -> List[str]:
    root = ctx.root
    tracked_files = ctx.tracked_files
    notes: List[str] = []

    def add(note: str) -> None:
        if note not in notes and len(notes) < max_items:
            notes.append(note)

    lowered = [f"/{p.lower()}" for p in tracked_files]

    feature_dirs = _dirs_named(tracked_files, "features")
    component_dirs = _dirs_named(tracked_files, "components")
    if feature_dirs and component_dirs:
        top_feature = max(feature_dirs.items(), key=lambda kv: (kv[1], -len(kv[0])))
        top_component = max(component_dirs.items(), key=lambda kv: (kv[1], -len(kv[0])))
        add(
            "feature modules and shared UI are separated: "
            f"{top_feature[0]}/ ({top_feature[1]} files) vs "
            f"{top_component[0]}/ ({top_component[1]:,} files)"
        )

    if has_app_router(root) and has_pages_router(root):
        add("app/ and pages/ both exist; router style may be mixed or transitional")

    script_names = list((ctx.package_json.get("scripts") or {}).keys())
    seedish = [
        name
        for name in script_names
        if re.search(r'(seed|sync|migrate|emulator)', name, re.I)
    ]
    if seedish:
        add(f"scripts include seed/sync/migrate/emulator workflows: {', '.join(sorted(seedish)[:4])}")

    context_paths = [
        tracked_files[i]
        for i, lp in enumerate(lowered)
        if "/contexts/" in lp
        or lp.endswith("context.ts")
        or lp.endswith("context.tsx")
    ]
    if context_paths:
        authish = [p for p in context_paths if "auth" in p.lower()]
        if authish and len(authish) >= max(1, len(context_paths) // 2):
            add("context layer appears auth-focused")

    firebase_paths = [
        tracked_files[i]
        for i, lp in enumerate(lowered)
        if "/firebase/" in lp or "firebase" in lp.rsplit("/", 1)[-1]
    ]
    fb_count = len(firebase_paths)
    if has_firebase(ctx):
        if fb_count >= 6:
            add(f"firebase integration appears substantial ({fb_count} firebase-named files)")
        elif fb_count >= 3:
            add(f"firebase integration appears moderate ({fb_count} firebase-named files)")
        elif fb_count >= 1:
            add(f"firebase integration appears minimal ({fb_count} firebase-named file{'s' if fb_count > 1 else ''})")

    # api layer: only directories that the tree does not already show at
    # the top level, and only when they hold a real concentration of files.
    api_dirs = {
        d: n for d, n in _non_top_level(_dirs_named(tracked_files, "api")).items()
        if n >= _MIN_API_FILES
    }
    if api_dirs:
        add(f"api layer: {_format_dirs(api_dirs)}")

    snapshot = ctx.results.get("test_snapshot", {})
    unit = snapshot.get("unit_tests", 0)
    integration = snapshot.get("integration_tests", 0)
    if integration and integration >= unit:
        add(
            "integration tests are as prominent as or more prominent than unit tests "
            f"({integration} integration vs {unit} unit)"
        )

    return notes[:max_items]


def register():
    return RepoNotesCollector()
