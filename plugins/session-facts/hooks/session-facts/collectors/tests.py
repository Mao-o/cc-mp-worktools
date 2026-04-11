from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.constants import CODE_EXTENSIONS, TEST_PATH_MARKERS
from core.context import RepoContext
from core.util import is_code_file, is_test_path


class TestsCollector:
    name = "tests"
    section_title = "## Test Snapshot"
    priority = 60

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        snapshot = _collect_test_snapshot(ctx.tracked_files)
        ctx.results["test_snapshot"] = snapshot
        if not snapshot:
            return None
        lines = [self.section_title]
        ordered_keys = [
            "code_files", "test_files", "test_to_code_ratio",
            "unit_tests", "integration_tests", "e2e_tests",
        ]
        for key in ordered_keys:
            if key in snapshot:
                lines.append(f"- {key}: {snapshot[key]}")
        for test_dir in snapshot.get("test_dirs", []):
            lines.append(f"- test_dir: {test_dir}")
        return "\n".join(lines) if len(lines) > 1 else None


def _collect_test_snapshot(tracked_files: List[str]) -> Dict[str, Any]:
    code_files = 0
    test_files = 0
    unit = 0
    integration = 0
    e2e = 0
    test_dirs: Set[str] = set()

    for path_str in tracked_files:
        p = Path(path_str)
        suffix = p.suffix.lower()
        if suffix not in CODE_EXTENSIONS:
            continue
        lowered = path_str.lower()
        if is_test_path(path_str):
            test_files += 1
            if "integration" in lowered:
                integration += 1
            elif "e2e" in lowered or "cypress" in lowered or "playwright" in lowered:
                e2e += 1
            else:
                unit += 1
            parts = p.parts[:-1]
            for i, part in enumerate(parts):
                low = part.lower()
                if low in TEST_PATH_MARKERS:
                    test_dirs.add("/".join(parts[: i + 1]))
        elif is_code_file(path_str):
            code_files += 1

    snapshot: Dict[str, Any] = {}
    if code_files:
        snapshot["code_files"] = code_files
    if test_files:
        snapshot["test_files"] = test_files
        if code_files:
            snapshot["test_to_code_ratio"] = round(test_files / code_files, 2)
    if unit:
        snapshot["unit_tests"] = unit
    if integration:
        snapshot["integration_tests"] = integration
    if e2e:
        snapshot["e2e_tests"] = e2e
    if test_dirs:
        snapshot["test_dirs"] = sorted(test_dirs)[:10]
    return snapshot


def register():
    return TestsCollector()
