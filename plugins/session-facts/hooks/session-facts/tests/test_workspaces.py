"""joa.2: workspace-aware detection, header line, subtree manifest scoping,
per-workspace commands. Fixtures mirror the dify (api/ + web/) and pnpm
workspace (apps/web) shapes from the audit."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from cli import summarize_repo
from core.context import AnalysisConfig


def _write(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))


DIFY_LIKE = {
    "README.md": "Dify-like is an open-source platform for building things.\n",
    "Makefile": "lint:\n\techo lint\n",
    "api/pyproject.toml": "[project]\nname = \"api\"\ndependencies = [\"fastapi>=0.110\", \"pytest\"]\n",
    "api/uv.lock": "",
    "api/app.py": "app = 1\n",
    "api/tests/test_app.py": "def test_x(): pass\n",
    "web/package.json": {"name": "web", "scripts": {"dev": "next dev", "test": "vitest"},
                          "dependencies": {"next": "15.0.0", "react": "19.0.0"},
                          "devDependencies": {"typescript": "5.0.0"}},
    "web/pnpm-lock.yaml": "",
    "web/next.config.ts": "export default {}\n",
    "web/.env.example": "NEXT_PUBLIC_API=\n",
    "web/app/page.tsx": "export default () => null\n",
    "web/tsconfig.json": "{}",
}


class WorkspaceDetectionTest(unittest.TestCase):
    def _run(self, files, cwd=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, files)
            return summarize_repo(root, AnalysisConfig(), is_git=False, cwd=(root / cwd) if cwd else None)

    def test_stack_and_deps_come_from_workspace_manifests(self):
        out = self._run(DIFY_LIKE)
        header = out.split("\n\n")[0]
        for tag in ("python", "uv", "fastapi", "node", "typescript", "nextjs", "react", "monorepo"):
            self.assertIn(tag, header, header)
        self.assertIn("major_dependencies:", header)
        self.assertIn("next@15", header)
        self.assertIn("fastapi@0.110", header)

    def test_header_lists_workspaces_with_pm_and_tags(self):
        out = self._run(DIFY_LIKE)
        self.assertIn("- workspaces: api (uv: python, fastapi), web (pnpm: node, nextjs, react, typescript)", out)

    def test_env_keys_and_nextjs_facts_read_the_workspace(self):
        out = self._run(DIFY_LIKE)
        self.assertIn("- NEXT_PUBLIC_API", out)
        self.assertIn("- app_dir: web/", out)
        self.assertIn("- config_file: web/next.config.ts", out)

    def test_workspace_commands_are_suggested_from_the_root(self):
        out = self._run(DIFY_LIKE)
        self.assertIn("- cd api && uv run pytest", out)
        self.assertIn("- cd web && pnpm test", out)
        self.assertIn("- cd web && pnpm dev", out)

    def test_subtree_mode_scopes_manifests_to_the_workspace(self):
        out = self._run(DIFY_LIKE, cwd="web")
        self.assertIn("manifests scoped to workspace web/", out)
        self.assertIn("- package_manager: pnpm", out)
        self.assertIn("## Scripts (run: pnpm <name>)", out)
        self.assertIn("- pnpm dev", out)
        # No cd-prefixed workspace commands in subtree mode.
        self.assertNotIn("cd web &&", out)

    def test_purpose_still_describes_the_repo_in_subtree_mode(self):
        out = self._run(DIFY_LIKE, cwd="web")
        self.assertIn("- purpose: Dify-like is an open-source platform for building things.", out)


if __name__ == "__main__":
    unittest.main()
