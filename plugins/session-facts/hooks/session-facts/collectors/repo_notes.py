from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence

from core.context import RepoContext


class RepoNotesCollector:
    name = "repo_notes"
    section_title = "## Repo-Specific Notes"
    priority = 70

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_notes
        notes = _collect_repo_specific_notes(ctx, max_items)
        if not notes:
            return None
        lines = [self.section_title]
        for note in notes:
            lines.append(f"- {note}")
        return "\n".join(lines)


def _collect_repo_specific_notes(ctx: RepoContext, max_items: int) -> List[str]:
    root = ctx.root
    tracked_files = ctx.tracked_files
    notes: List[str] = []

    def add(note: str) -> None:
        if note not in notes and len(notes) < max_items:
            notes.append(note)

    has_features = any("/features/" in f"/{p.lower()}" for p in tracked_files)
    has_components = any("/components/" in f"/{p.lower()}" for p in tracked_files)
    if has_features and has_components:
        add("features/ and components/ both exist; feature modules and shared UI appear separated")

    has_app = (root / "app").is_dir() or (root / "src" / "app").is_dir()
    has_pages = (root / "pages").is_dir() or (root / "src" / "pages").is_dir()
    if has_app and has_pages:
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
        p
        for p in tracked_files
        if "/contexts/" in f"/{p.lower()}"
        or p.lower().endswith("context.ts")
        or p.lower().endswith("context.tsx")
    ]
    if context_paths:
        authish = [p for p in context_paths if "auth" in p.lower()]
        if authish and len(authish) >= max(1, len(context_paths) // 2):
            add("context layer appears auth-focused")

    firebase_paths = [
        p
        for p in tracked_files
        if "/firebase/" in f"/{p.lower()}" or "firebase" in Path(p).name.lower()
    ]
    if len(firebase_paths) >= 3:
        add("firebase-related integration surface appears substantial")

    api_paths = [
        p
        for p in tracked_files
        if "/api/" in f"/{p.lower()}" or "api" in Path(p).name.lower()
    ]
    if len(api_paths) >= 5:
        add("api-related files are concentrated; inspect API layer early for behavior changes")

    snapshot = ctx.results.get("test_snapshot", {})
    if snapshot.get("integration_tests", 0) and snapshot.get("integration_tests", 0) >= snapshot.get("unit_tests", 0):
        add("integration tests are as prominent as or more prominent than unit tests")

    return notes[:max_items]


def register():
    return RepoNotesCollector()
