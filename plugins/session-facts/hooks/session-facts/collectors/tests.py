from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Set

from core.constants import CODE_EXTENSIONS, TEST_PATH_MARKERS
from core.context import RepoContext, TestSnapshot
from core.util import aggregate_paths, filter_to_cwd, is_code_file, is_test_path


# Rendered ``- test_dir:`` lines after aggregation. A deep monorepo yields
# one aggregated pattern per depth (``src/*/__tests__``, ``src/*/*/__tests__``,
# ...) and five of those say no more than two do.
MAX_TEST_DIR_LINES = 5


class TestsCollector:
    name = "tests"
    section_title = "## Test Snapshot"
    priority = 60

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        repo_snapshot = _collect_test_snapshot(ctx.tracked_files)
        ctx.results["test_snapshot"] = repo_snapshot

        cwd_rel = ctx.cwd_relative
        if not cwd_rel:
            return _render_snapshot(self.section_title, repo_snapshot)

        cwd_files = filter_to_cwd(ctx.tracked_files, cwd_rel)
        cwd_snapshot = _collect_test_snapshot(cwd_files)
        if not cwd_snapshot:
            return _render_snapshot(self.section_title, repo_snapshot)
        return _render_snapshot(
            f"## Test Snapshot (cwd: {cwd_rel})", cwd_snapshot
        )


def _render_snapshot(title: str, snapshot: TestSnapshot) -> Optional[str]:
    if not snapshot:
        return None
    if "test_files" not in snapshot:
        # Code without tests: a bare code_files count under a "Test Snapshot"
        # heading misleads; saying so directly is the actionable fact.
        return f"{title}\n- tests: none detected"
    lines = [title]
    ordered_keys = [
        "code_files", "test_files", "test_to_code_ratio",
        "unit_tests", "integration_tests", "e2e_tests",
    ]
    for key in ordered_keys:
        if key in snapshot:
            lines.append(f"- {key}: {snapshot[key]}")
    test_dirs = aggregate_paths(snapshot.get("test_dirs", []))
    for test_dir in test_dirs[:MAX_TEST_DIR_LINES]:
        lines.append(f"- test_dir: {test_dir}")
    if len(test_dirs) > MAX_TEST_DIR_LINES:
        lines.append(f"- test_dir: … (+{len(test_dirs) - MAX_TEST_DIR_LINES} more)")
    return "\n".join(lines) if len(lines) > 1 else None


def _collect_test_snapshot(tracked_files: List[str]) -> TestSnapshot:
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

    snapshot: TestSnapshot = {}
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
        # Keep the raw set generous: aggregate_paths() needs to see every
        # sibling to recognise a shared shape and fold it into one pattern
        # line, so truncating tighter here would risk cutting a shape down
        # before it can even be recognised. aggregate_paths() itself now
        # also caps its own rendered output per degenerate group (P2-3:
        # core/util.py's max_listed), so a large raw set here no longer
        # risks an unbounded number of *rendered* lines downstream -- this
        # 50 only needs to be generous enough for the folding step, not for
        # the final line count.
        snapshot["test_dirs"] = sorted(test_dirs)[:50]
    return snapshot


def register():
    return TestsCollector()
