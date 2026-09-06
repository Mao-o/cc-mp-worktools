"""joa.31 / joa.25: nested-marker scan completeness, tracked-file cap,
hub-files skip line."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.hub_files import HubFilesCollector
from core.context import AnalysisConfig, RepoContext
from core.fs import MAX_NESTED_SCAN_ENTRIES, scan_nested_project_markers


class ScanNestedProjectMarkersTest(unittest.TestCase):
    def test_found_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text("{}")
            self.assertEqual(scan_nested_project_markers(root, ("package.json",), ()), (True, True))

    def test_not_found_small_tree_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            self.assertEqual(scan_nested_project_markers(root, ("package.json",), ()), (False, True))

    def test_visit_budget_exhausted_is_incomplete_regardless_of_order(self):
        # The marker sits in one of many sibling dirs; whichever order the
        # OS enumerates them, hitting the budget must report "incomplete"
        # rather than "no markers" (the old bool API could not tell).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(40):
                (root / f"d{i:02d}").mkdir()
            (root / "d39" / "package.json").write_text("{}")
            found, complete = scan_nested_project_markers(root, ("package.json",), (), max_depth=2, max_dirs=8)
            self.assertTrue(found or not complete)

    def test_enumeration_cap_at_exact_limit_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(MAX_NESTED_SCAN_ENTRIES):
                (root / f"f{i:05d}.txt").write_text("x")
            found, complete = scan_nested_project_markers(root, ("package.json",), ())
            self.assertFalse(found)
            self.assertFalse(complete)

    def test_permission_error_is_incomplete_not_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locked").mkdir()

            def failing_scandir(path):
                raise PermissionError(path)

            with mock.patch.object(os, "scandir", failing_scandir):
                found, complete = scan_nested_project_markers(root, ("package.json",), ())
            self.assertFalse(found)
            self.assertFalse(complete)


class HubFilesSkipLineTest(unittest.TestCase):
    def test_over_cap_says_skipped_instead_of_vanishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {f"src/m{i}.py": "import os\n" for i in range(5)}
            for rel, content in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(content)
            cfg = AnalysisConfig(include_hub_files=True, max_hub_scan=3)
            ctx = RepoContext(root=root, config=cfg)
            ctx.tracked_files = list(files)
            out = HubFilesCollector().collect(ctx)
            self.assertEqual(out, "## Hub Files\n- skipped: 5 candidate files > --max-hub-scan 3")


class TrackedFilesCapTest(unittest.TestCase):
    def test_header_notes_truncation(self):
        from cli import summarize_repo
        import cli as cli_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("test:\n\techo\n")
            for i in range(12):
                (root / f"f{i}.py").write_text("x = 1\n")
            with mock.patch.object(cli_module, "MAX_TRACKED_FILES", 10):
                out = summarize_repo(root, AnalysisConfig(), is_git=False)
            self.assertIn("- tracked_files: 10+ (truncated; counts below are lower bounds)", out)


if __name__ == "__main__":
    unittest.main()
