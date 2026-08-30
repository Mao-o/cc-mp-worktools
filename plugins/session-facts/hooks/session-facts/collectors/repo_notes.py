from __future__ import annotations

import re
from typing import List, Optional

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


def _collect_repo_specific_notes(ctx: RepoContext, max_items: int) -> List[str]:
    root = ctx.root
    tracked_files = ctx.tracked_files
    notes: List[str] = []

    def add(note: str) -> None:
        if note not in notes and len(notes) < max_items:
            notes.append(note)

    lowered = [f"/{p.lower()}" for p in tracked_files]

    has_features = any("/features/" in lp for lp in lowered)
    has_components = any("/components/" in lp for lp in lowered)
    if has_features and has_components:
        add("features/ and components/ both exist; feature modules and shared UI appear separated")

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
            add("firebase integration appears substantial")
        elif fb_count >= 3:
            add("firebase integration appears moderate")
        elif fb_count >= 1:
            add("firebase integration appears minimal")

    api_paths = [
        tracked_files[i]
        for i, lp in enumerate(lowered)
        if "/api/" in lp or "api" in lp.rsplit("/", 1)[-1]
    ]
    # Raised from >= 5 to >= 20: at the old threshold this note fired on almost
    # every repo, so it carried no signal. A note that shows up everywhere is
    # noise (see sessionstart-injection rule). 20+ api files is genuinely
    # api-heavy and worth flagging.
    if len(api_paths) >= 20:
        add("api-related files are concentrated; inspect API layer early for behavior changes")

    snapshot = ctx.results.get("test_snapshot", {})
    if snapshot.get("integration_tests", 0) and snapshot.get("integration_tests", 0) >= snapshot.get("unit_tests", 0):
        add("integration tests are as prominent as or more prominent than unit tests")

    return notes[:max_items]


def register():
    return RepoNotesCollector()
