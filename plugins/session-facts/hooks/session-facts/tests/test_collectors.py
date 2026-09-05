"""collector レベルの統合テスト: structure subtree 圧縮 (#2)、repo_notes api
閾値 (#4)、tests collector の test_dir 集約 (#5)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.cwd_subtree import CwdSubtreeCollector
from collectors.nextjs_facts import NextjsFactsCollector
from collectors.repo_notes import RepoNotesCollector
from collectors.structure import StructureCollector
from collectors.tests import TestsCollector
from core.context import AnalysisConfig, RepoContext


def _ctx(root: Path, tracked, cwd=None) -> RepoContext:
    ctx = RepoContext(root=root, config=AnalysisConfig(), cwd=cwd)
    ctx.tracked_files = list(tracked)
    return ctx


class StructureSubtreeTest(unittest.TestCase):
    def test_subtree_mode_compresses_repo_structure_to_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "plugins" / "verify-cloud-account"
            sub.mkdir(parents=True)
            tracked = [
                "plugins/verify-cloud-account/hooks/x/core/a.py",
                "plugins/verify-cloud-account/hooks/x/services/b.py",
                "plugins/other-plugin/hooks/y/c.py",
                "infra/deploy/d.py",
            ]
            ctx = _ctx(root, tracked, cwd=sub)
            out = StructureCollector().collect(ctx)
            self.assertIsNotNone(out)
            lines = out.splitlines()
            # depth=1 header + only top-level dir names, no deep nesting.
            self.assertEqual(lines[0], "## Structure (dirs only, depth=1)")
            body = lines[1:]
            self.assertEqual(body, ["├── infra/", "└── plugins/"])

    def test_repo_root_mode_uses_dynamic_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "src/api/routers/a.py",
                "src/api/routers/b.py",
                "src/lib/c.py",
            ]
            ctx = _ctx(root, tracked, cwd=None)  # cwd == root -> not subtree
            out = StructureCollector().collect(ctx)
            self.assertIsNotNone(out)
            header = out.splitlines()[0]
            # Dynamic search expands beyond depth 1 when there is room.
            self.assertRegex(header, r"## Structure \(dirs only, depth=[2-5]\)")


class RepoNotesApiThresholdTest(unittest.TestCase):
    def _api_note_present(self, n_api_files: int, prefix: str = "src/api") -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"{prefix}/handler{i:03d}.py" for i in range(n_api_files)]
            ctx = _ctx(root, tracked)
            out = RepoNotesCollector().collect(ctx)
            return bool(out) and "api layer:" in out

    def test_note_names_the_directory_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"functions/src/api/h{i:03d}.ts" for i in range(41)]
            out = RepoNotesCollector().collect(_ctx(root, tracked))
            self.assertIn("api layer: functions/src/api/ (41 files)", out)

    def test_top_level_api_dir_is_not_noted(self):
        # A top-level api/ is already visible in ## Structure (joa.18).
        self.assertFalse(self._api_note_present(200, prefix="api"))

    def test_below_threshold_no_note(self):
        # patentai-mini / session-facts scale: should NOT fire.
        self.assertFalse(self._api_note_present(19))

    def test_at_threshold_fires(self):
        self.assertTrue(self._api_note_present(20))

    def test_well_above_threshold_fires(self):
        # affiliate01 / dify scale.
        self.assertTrue(self._api_note_present(40))

    def test_old_threshold_no_longer_fires(self):
        # 5 files used to fire under the old >= 5 rule; must be silent now.
        self.assertFalse(self._api_note_present(5))


class TestsCollectorAggregationTest(unittest.TestCase):
    def test_test_dirs_are_aggregated_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "plugins/sensitive-files-guardrail/hooks/check-sensitive-files/tests/test_a.py",
                "plugins/sensitive-files-guardrail/hooks/redact-sensitive-reads/tests/test_b.py",
                "plugins/verify-cloud-account/hooks/verify-cloud-account/tests/test_c.py",
                "plugins/a/src/impl.py",
                "plugins/b/src/impl.py",
            ]
            ctx = _ctx(root, tracked)
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            test_dir_lines = [ln for ln in out.splitlines() if ln.startswith("- test_dir:")]
            self.assertEqual(test_dir_lines, ["- test_dir: plugins/*/hooks/*/tests"])

    def test_code_without_tests_says_none_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = ["src/impl.py", "src/other.py"]
            ctx = _ctx(root, tracked)
            out = TestsCollector().collect(ctx)
            self.assertEqual(out, "## Test Snapshot\n- tests: none detected")

    def test_no_code_files_emits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = ["docs/readme.md"]
            ctx = _ctx(root, tracked)
            out = TestsCollector().collect(ctx)
            self.assertIsNone(out)

    def test_test_dir_lines_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"m{i}/src/tests/test_x.py" for i in range(8)]
            tracked += [f"m{i}/src/deep/tests/test_y.py" for i in range(8)]
            tracked += [f"m{i}/src/deep/er/tests/test_z.py" for i in range(8)]
            tracked += [f"m{i}/src/deep/er/est/tests/test_w.py" for i in range(8)]
            out = TestsCollector().collect(_ctx(root, tracked))
            test_dir_lines = [ln for ln in out.splitlines() if ln.startswith("- test_dir:")]
            self.assertLessEqual(len(test_dir_lines), 6)
            self.assertTrue(test_dir_lines[-1].startswith("- test_dir: … (+"))

    def test_single_test_dir_is_not_abstracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "app/tests/test_x.py",
                "app/main.py",
            ]
            ctx = _ctx(root, tracked)
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            test_dir_lines = [ln for ln in out.splitlines() if ln.startswith("- test_dir:")]
            self.assertEqual(test_dir_lines, ["- test_dir: app/tests"])


class RepoNotesRouterMixNoteTest(unittest.TestCase):
    """core.util.has_app_router()/has_pages_router() consolidation: this note
    and nextjs_facts.py's app_router/pages_router lines used to each
    re-derive the same app/-vs-pages/ boolean independently. Previously
    untested."""

    def test_app_and_pages_both_present_fires_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "pages").mkdir()
            ctx = _ctx(root, ["app/layout.tsx", "pages/index.tsx"])
            out = RepoNotesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("app/ and pages/ both exist", out)

    def test_src_app_and_src_pages_both_present_fires_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "app").mkdir(parents=True)
            (root / "src" / "pages").mkdir(parents=True)
            ctx = _ctx(root, ["src/app/layout.tsx", "src/pages/index.tsx"])
            out = RepoNotesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("app/ and pages/ both exist", out)

    def test_app_only_does_not_fire_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            ctx = _ctx(root, ["app/layout.tsx"])
            out = RepoNotesCollector().collect(ctx)
            self.assertTrue(out is None or "app/ and pages/ both exist" not in out)


class RepoNotesFirebaseSignalTest(unittest.TestCase):
    """core.firebase.has_firebase() consolidation: this note used to have its
    own broader-than-the-detector check (config file, npm deps, pyproject)
    but still missed tracked requirements*.txt files. A bare "firebase"
    substring in a path, with no real signal backing it, must not fire."""

    def test_firebase_json_with_enough_paths_fires_moderate_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "firebase.json").write_text("{}")
            tracked = [f"functions/firebase{i}.ts" for i in range(3)]
            ctx = _ctx(root, tracked)
            out = RepoNotesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("firebase integration appears moderate", out)

    def test_python_requirements_only_firebase_signal_fires_note(self):
        # internal backlog: previously this note only checked pyproject.toml
        # for Python firebase-admin, missing requirements.txt entirely.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("firebase-admin==6.5.0\n")
            tracked = ["requirements.txt"] + [f"app/firebase_{i}.py" for i in range(3)]
            ctx = _ctx(root, tracked)
            out = RepoNotesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("firebase integration appears moderate", out)

    def test_firebase_named_paths_without_any_real_signal_do_not_fire(self):
        # fb_count is only a path-name heuristic for how deep the
        # integration is; has_firebase() must gate whether it fires at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"src/firebase{i}.ts" for i in range(6)]
            ctx = _ctx(root, tracked)
            out = RepoNotesCollector().collect(ctx)
            self.assertIsNone(out)


class NextjsFactsRouterTest(unittest.TestCase):
    """core.util.has_app_router()/has_pages_router() consolidation.
    Previously untested."""

    def test_app_router_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            ctx = _ctx(root, ["app/layout.tsx"])
            ctx.stack.append("nextjs")
            out = NextjsFactsCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("- app_router: yes", out)
            self.assertNotIn("- pages_router: yes", out)

    def test_pages_router_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pages").mkdir()
            ctx = _ctx(root, ["pages/index.tsx"])
            ctx.stack.append("nextjs")
            out = NextjsFactsCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("- pages_router: yes", out)
            self.assertNotIn("- app_router: yes", out)

    def test_src_app_router_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "app").mkdir(parents=True)
            ctx = _ctx(root, ["src/app/layout.tsx"])
            ctx.stack.append("nextjs")
            out = NextjsFactsCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("- app_router: yes", out)

    def test_neither_router_dir_present_and_no_other_signal_emits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _ctx(root, [])
            ctx.stack.append("nextjs")
            out = NextjsFactsCollector().collect(ctx)
            self.assertIsNone(out)


class CwdSubtreeFilterTest(unittest.TestCase):
    """core.util.filter_to_cwd() consolidation: collect() now filters
    through the shared helper (which returns root-relative paths, like its
    other callers services.py/tests.py need) then strips the cwd prefix
    locally, since the tree must be rooted at cwd. Previously untested."""

    def test_subtree_lists_only_cwd_files_with_prefix_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "plugins" / "verify-cloud-account"
            sub.mkdir(parents=True)
            tracked = [
                "plugins/verify-cloud-account/hooks/x/core/a.py",
                "plugins/verify-cloud-account/hooks/x/services/b.py",
                "plugins/other-plugin/hooks/y/c.py",
            ]
            ctx = _ctx(root, tracked, cwd=sub)
            out = CwdSubtreeCollector().collect(ctx)
            self.assertEqual(
                out,
                "## Subtree (cwd: plugins/verify-cloud-account, dirs only, depth=5)\n"
                "└── hooks/x/\n"
                "    ├── core/\n"
                "    └── services/",
            )

    def test_no_cwd_scope_emits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _ctx(root, ["src/a.py"], cwd=None)
            self.assertFalse(CwdSubtreeCollector().should_run(ctx))
            self.assertIsNone(CwdSubtreeCollector().collect(ctx))


if __name__ == "__main__":
    unittest.main()
