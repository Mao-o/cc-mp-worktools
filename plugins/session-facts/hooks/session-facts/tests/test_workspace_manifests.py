"""core/context.py workspace manifest discovery (internal backlog joa.2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext


class WorkspaceDirsTest(unittest.TestCase):
    def test_discovers_manifests_below_root_shallowest_first(self):
        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        ctx.tracked_files = [
            "package.json", "apps/web/package.json", "api/pyproject.toml",
            "node_modules/x/package.json", "a/b/c/d/package.json", "apps/web/src/x.ts",
        ]
        self.assertEqual(ctx.workspace_dirs, ["api", "apps/web"])

    def test_manifests_are_parsed_root_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"name": "root"}))
            (root / "apps" / "web").mkdir(parents=True)
            (root / "apps" / "web" / "package.json").write_text(json.dumps({"name": "web"}))
            (root / "api").mkdir()
            (root / "api" / "pyproject.toml").write_text("[project]\nname = 'api'\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["package.json", "apps/web/package.json", "api/pyproject.toml"]
            self.assertEqual([d for d, _ in ctx.package_json_manifests()], ["", "apps/web"])
            self.assertEqual([d for d, _ in ctx.pyproject_manifests()], ["api"])


if __name__ == "__main__":
    unittest.main()
