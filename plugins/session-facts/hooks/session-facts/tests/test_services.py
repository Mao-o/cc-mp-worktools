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
        # __init__.py is never an entry; a deep index.ts / types.py has no
        # other signal and the DEPRIORITY_NAMES penalty keeps it out.
        files = ["pkg/__init__.py", "a/b/c/d/index.ts", "src/types.py"]
        self.assertEqual(_collect_service_entries(files, max_items=10), [])

    def test_shallow_index_file_is_an_entry_point(self):
        # src/index.ts (<= 3 path parts) is the conventional package entry.
        self.assertEqual(
            _collect_service_entries(["src/index.ts"], max_items=10), ["src/index.ts"]
        )

    def test_service_dir_marker_alone_is_a_positive_candidate(self):
        files = ["services/user.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["services/user.py"]
        )

    def test_ambiguous_client_dir_needs_a_name_token(self):
        # client/ and api/ (top-level) are too common to score on their own
        # (joa.1); a service-layer token in the file name unlocks them.
        self.assertEqual(_collect_service_entries(["client/foo.py"], max_items=10), [])
        self.assertEqual(
            _collect_service_entries(["clients/user_client.py"], max_items=10),
            ["clients/user_client.py"],
        )

    def test_nested_api_dir_is_a_route_layer(self):
        # functions/src/api/issues.ts: api/ below the top level is where
        # requests enter, so it counts without a name token.
        files = ["functions/src/api/issues.ts", "api/core/thing.py"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["functions/src/api/issues.ts"]
        )

    def test_priority_name_alone_is_a_positive_candidate(self):
        files = ["cmd/main.go"]
        self.assertEqual(_collect_service_entries(files, max_items=10), ["cmd/main.go"])

    def test_entry_points_outrank_the_service_layer(self):
        # Tier 1 (main.go, routes/) is listed before tier 2 (services/)
        # regardless of raw score (joa.1: two-tier layout).
        files = ["services/plain.py", "cmd/main.go", "src/routes/users.ts"]
        ordered = _collect_service_entries(files, max_items=10)
        self.assertEqual(ordered[:2], ["cmd/main.go", "src/routes/users.ts"])
        self.assertEqual(ordered[2], "services/plain.py")

    def test_test_paths_are_excluded(self):
        files = ["services/__tests__/user.test.ts", "tests/services/test_user.py", "services/user.py"]
        self.assertEqual(_collect_service_entries(files, max_items=10), ["services/user.py"])

    def test_noise_dirs_and_view_files_are_excluded(self):
        files = [
            "services/errors/app.py",
            "web/models/app.ts",
            "web/.storybook/main.ts",
            "app/services/d-payment/page.tsx",
            "api/services/account_service.py",
        ]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["api/services/account_service.py"]
        )

    def test_strong_entry_names_do_not_decay_with_depth(self):
        # __main__.py four levels down is still the program entry; app.py
        # that deep is just a module sharing the name.
        files = ["plugins/x/hooks/y/__main__.py", "api/core/plugin/back/app.py", "services/z.py"]
        ordered = _collect_service_entries(files, max_items=10)
        self.assertEqual(ordered[0], "plugins/x/hooks/y/__main__.py")

    def test_service_layer_keeps_reserved_slots(self):
        # 20 route files would fill every slot; the service layer keeps
        # TIER2_RESERVED of them so both layers stay visible.
        files = [f"src/routes/r{i}.ts" for i in range(20)] + ["services/a.py", "services/b.py"]
        ordered = _collect_service_entries(files, max_items=12)
        self.assertEqual(len(ordered), 12)
        self.assertIn("services/a.py", ordered)
        self.assertIn("services/b.py", ordered)

    def test_per_directory_cap_spreads_entries(self):
        files = [f"src/routes/r{i}.ts" for i in range(8)] + [f"src/handlers/h{i}.ts" for i in range(8)]
        ordered = _collect_service_entries(files, max_items=8)
        self.assertEqual(sum(1 for p in ordered if "/routes/" in p), 4)
        self.assertEqual(sum(1 for p in ordered if "/handlers/" in p), 4)

    def test_manifest_entry_points_rank_first(self):
        files = ["bin/cli.js", "src/routes/a.ts"]
        ordered = _collect_service_entries(
            files, max_items=10, manifest_entries={"bin/cli.js": "package.json bin"}
        )
        self.assertEqual(ordered[0], "bin/cli.js")

    def test_deprioritized_name_still_included_when_dir_marker_offsets_it(self):
        files = ["a/b/services/index.ts"]
        self.assertEqual(
            _collect_service_entries(files, max_items=10), ["a/b/services/index.ts"]
        )

    def test_name_token_match_is_a_positive_candidate(self):
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


class ManifestEntryPointsTest(unittest.TestCase):
    def test_package_json_main_and_bin_and_pyproject_scripts(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "main": "./src/index.js", "bin": {"cli": "bin/cli.js"},
            }))
            (root / "api").mkdir()
            (root / "api" / "pyproject.toml").write_text(
                "[project]\nname='x'\n[project.scripts]\nserve = \"app.main:run\"\n"
            )
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = [
                "package.json", "src/index.js", "bin/cli.js", "dist/index.js",
                "api/pyproject.toml", "api/app/main.py",
            ]
            from collectors.services import manifest_entry_points
            entries = manifest_entry_points(ctx)
            self.assertEqual(entries["src/index.js"], "package.json main")
            self.assertEqual(entries["bin/cli.js"], "package.json bin")
            self.assertEqual(entries["api/app/main.py"], "pyproject scripts")
            self.assertNotIn("dist/index.js", entries)


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
