"""core/makefile.py の target 抽出と Likely Commands への反映 (#8)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.scripts import _make_commands
from core.context import AnalysisConfig, RepoContext
from core.makefile import extract_targets

_MAKEFILE = """\
.PHONY: test build dev clean lint migrate help internal-thing
CC := gcc
PREFIX = /usr/local

test:
\tpytest
build: deps
\tdocker build .
dev:
\tflask run
lint:
\truff check .
migrate:
\talembic upgrade head
clean:
\trm -rf dist
internal-thing:
\techo internal
help:
\t@echo help
"""


class ExtractTargetsTest(unittest.TestCase):
    def test_extracts_targets_in_order(self):
        self.assertEqual(
            extract_targets(_MAKEFILE),
            ["test", "build", "dev", "lint", "migrate", "clean", "internal-thing", "help"],
        )

    def test_excludes_variable_assignments(self):
        self.assertNotIn("CC", extract_targets(_MAKEFILE))
        self.assertNotIn("PREFIX", extract_targets(_MAKEFILE))

    def test_excludes_dot_phony_and_recipe_lines(self):
        targets = extract_targets(_MAKEFILE)
        self.assertNotIn(".PHONY", targets)
        self.assertNotIn("pytest", targets)  # recipe line (tab-indented)

    def test_double_colon_rule(self):
        self.assertEqual(extract_targets("all:: build\n\techo hi\n"), ["all"])

    def test_dedup(self):
        self.assertEqual(extract_targets("test:\n\ta\ntest:\n\tb\n"), ["test"])

    def test_empty(self):
        self.assertEqual(extract_targets(""), [])


class MakeCommandsTest(unittest.TestCase):
    def _ctx_with_makefile(self, tmp, content):
        root = Path(tmp)
        (root / "Makefile").write_text(content)
        ctx = RepoContext(root=root, config=AnalysisConfig())
        return ctx

    def test_prioritized_targets_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx_with_makefile(tmp, _MAKEFILE)
            cmds = _make_commands(ctx, max_items=16)
            # priority order: dev, build, test, lint, migrate; non-conventional dropped.
            self.assertEqual(cmds, ["make dev", "make build", "make test", "make lint", "make migrate"])
            self.assertNotIn("make clean", cmds)
            self.assertNotIn("make help", cmds)
            self.assertNotIn("make internal-thing", cmds)

    def test_no_makefile_falls_back_to_bare_make(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            self.assertEqual(_make_commands(ctx, max_items=16), ["make"])

    def test_makefile_without_conventional_targets_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx_with_makefile(tmp, "weirdtarget:\n\techo hi\n")
            self.assertEqual(_make_commands(ctx, max_items=16), ["make"])

    def test_respects_max_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx_with_makefile(tmp, _MAKEFILE)
            cmds = _make_commands(ctx, max_items=2)
            self.assertEqual(cmds, ["make dev", "make build"])


if __name__ == "__main__":
    unittest.main()
