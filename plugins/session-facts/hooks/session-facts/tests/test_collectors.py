"""collector レベルの統合テスト: structure subtree 圧縮 (#2)、repo_notes api
閾値 (#4)、tests collector の test_dir 集約 (#5)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

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
    def _api_note_present(self, n_api_files: int) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"src/api/handler{i:03d}.py" for i in range(n_api_files)]
            ctx = _ctx(root, tracked)
            out = RepoNotesCollector().collect(ctx)
            return bool(out) and "api-related files are concentrated" in out

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
                "plugins/sensitive-files-guard/hooks/check-sensitive-files/tests/test_a.py",
                "plugins/sensitive-files-guard/hooks/redact-sensitive-reads/tests/test_b.py",
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


if __name__ == "__main__":
    unittest.main()
