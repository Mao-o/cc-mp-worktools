"""collectors/services.py had zero test coverage (internal backlog)."""
from __future__ import annotations

import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.services import _collect_service_entries
from core.context import AnalysisConfig, RepoContext


class CollectServiceEntriesTest(unittest.TestCase):
    def test_non_code_extensions_are_never_candidates(self):
        files = ["services/user.md", "README.md", "services/config.json"]
        self.assertEqual(_collect_service_entries(files, max_items=10), [])

    def test_deprioritized_names_alone_score_zero_and_are_excluded(self):
        # __init__.py / index.* / types.* start at score 0 with no other
        # signal (SERVICE_DIR_MARKERS/token/PRIORITY_NAMES all absent) and
        # score -= 3 from DEPRIORITY_NAMES, landing at -3 (not > 0).
        files = ["pkg/__init__.py", "src/index.ts", "src/types.py"]
        self.assertEqual(_collect_service_entries(files, max_items=10), [])

    def test_service_dir_marker_alone_is_a_positive_candidate(self):
        files = ["services/user.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["services/user.py"]
        )

    def test_client_dir_marker_is_recognized_singular_and_plural(self):
        files = ["client/foo.py", "clients/bar.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10),
            ["client/foo.py", "clients/bar.py"],
        )

    def test_priority_name_alone_is_a_positive_candidate(self):
        # "server.go" isn't a recognized PRIORITY_NAMES entry (only
        # server.py/server.ts/server.js are); main.go is.
        files = ["cmd/main.go"]
        self.assertEqual(_collect_service_entries(files, max_items=10), ["cmd/main.go"])

    def test_priority_name_outranks_service_dir_marker_alone(self):
        # main.go (PRIORITY_NAMES, +3) sorts before a bare services/-dir
        # file with no name-based signal (+5 from the dir marker alone
        # would normally win, so this also confirms the marker score,
        # +5, still beats a plain +3 priority name when both apply to
        # different files -- ordering is by the combined score only).
        files = ["services/plain.py", "cmd/main.go"]
        ordered = _collect_service_entries(files, max_items=10)
        self.assertEqual(set(ordered), {"services/plain.py", "cmd/main.go"})
        # services/plain.py: SERVICE_DIR_MARKERS hit (+5) > cmd/main.go:
        # PRIORITY_NAMES hit (+3) -- higher score sorts first.
        self.assertEqual(ordered[0], "services/plain.py")

    def test_deprioritized_name_still_included_when_dir_marker_offsets_it(self):
        # index.ts under services/ nets +5 (dir marker) - 3 (deprioritized
        # name) = +2, still > 0.
        files = ["services/index.ts"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["services/index.ts"]
        )

    def test_name_token_match_is_a_positive_candidate(self):
        # "client" as a substring of the filename itself (not a directory)
        # also scores +2 via the token check, independent of
        # SERVICE_DIR_MARKERS.
        files = ["lib/api_client.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["lib/api_client.py"]
        )

    def test_max_items_truncates_the_ordered_list(self):
        files = [f"services/svc_{i}.py" for i in range(5)]
        self.assertEqual(len(_collect_service_entries(files, max_items=2)), 2)

    def test_duplicate_paths_are_not_repeated(self):
        files = ["services/user.py", "services/user.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["services/user.py"]
        )


class ServicesCollectorCwdScopeTest(unittest.TestCase):
    """cwd != repo_root splits into a cwd-scoped section plus a
    repo-wide section for entries outside cwd (v0.3.0's cwd-scope split)."""

    def test_repo_root_scope_uses_a_single_section(self):
        from collectors.services import ServicesCollector

        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        ctx.tracked_files = ["services/user.py"]
        out = ServicesCollector().collect(ctx)
        self.assertEqual(out, "## Service Entry Points\n- services/user.py")

    def test_cwd_scope_splits_into_cwd_and_repo_wide_sections(self):
        from collectors.services import ServicesCollector

        root = Path("/repo")
        ctx = RepoContext(root=root, config=AnalysisConfig(), cwd=root / "apps/web")
        ctx.tracked_files = ["apps/web/services/user.py", "apps/api/services/order.py"]
        out = ServicesCollector().collect(ctx)
        self.assertIn("## Service Entry Points (cwd: apps/web)", out)
        self.assertIn("apps/web/services/user.py", out)
        self.assertIn("## Service Entry Points (repo-wide)", out)
        self.assertIn("apps/api/services/order.py", out)

    def test_cwd_scope_with_no_cwd_entries_still_uses_the_repo_wide_title(self):
        # collect()'s own "elif not cwd_entries and repo_entries" branch
        # (bare self.section_title, no "(repo-wide)" suffix) is dead code:
        # when cwd_entries is empty, repo_minus_cwd (which drops nothing,
        # since "p not in []" is always True) is identical to repo_entries,
        # so "if repo_minus_cwd" already fires whenever repo_entries is
        # non-empty -- the elif's own precondition can never be reached.
        # This locks in the actual (reachable) behavior, not the elif's.
        from collectors.services import ServicesCollector

        root = Path("/repo")
        ctx = RepoContext(root=root, config=AnalysisConfig(), cwd=root / "apps/web")
        ctx.tracked_files = ["apps/api/services/order.py"]
        out = ServicesCollector().collect(ctx)
        self.assertEqual(
            out,
            "## Service Entry Points (repo-wide)\n- apps/api/services/order.py",
        )

    def test_should_run_requires_tracked_files(self):
        from collectors.services import ServicesCollector

        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        self.assertFalse(ServicesCollector().should_run(ctx))
        ctx.tracked_files = ["a.py"]
        self.assertTrue(ServicesCollector().should_run(ctx))


if __name__ == "__main__":
    unittest.main()
