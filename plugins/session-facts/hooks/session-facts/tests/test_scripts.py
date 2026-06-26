"""likely_commands の runtime 補正 (v0.6): venv/mise prefix の適用、
uv/poetry が自前 env 管理で不変なこと、bare-python (.py 比率) ケース。"""
from __future__ import annotations

import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.scripts import _likely_commands
from core.context import AnalysisConfig, RepoContext


def _ctx(pm=None, runtime=None, stack=()) -> RepoContext:
    ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
    if pm is not None:
        ctx.results["package_manager"] = pm
    if runtime is not None:
        ctx.results["runtime"] = runtime
    ctx.stack = list(stack)
    return ctx


class LikelyCommandsRuntimeTest(unittest.TestCase):
    def test_python_pm_with_venv_prefix(self):
        ctx = _ctx(pm="python", runtime={"venv": ".venv"})
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m pytest", cmds)
        self.assertNotIn("python -m pytest", cmds)  # not the bare form

    def test_python_pm_with_mise_prefix(self):
        ctx = _ctx(pm="python", runtime={"manager": "mise", "tools": {"python": "3.12"}})
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("mise exec -- python -m pytest", cmds)

    def test_python_pm_without_runtime_is_bare(self):
        ctx = _ctx(pm="python")
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("python -m pytest", cmds)

    def test_uv_left_unchanged(self):
        # uv manages its own env: keep ``uv run`` even when a venv is present.
        ctx = _ctx(pm="uv", runtime={"venv": ".venv"})
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("uv run pytest", cmds)
        self.assertNotIn(".venv/bin/python -m pytest", cmds)

    def test_poetry_left_unchanged(self):
        ctx = _ctx(pm="poetry", runtime={"venv": ".venv"})
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("poetry run pytest", cmds)
        self.assertNotIn(".venv/bin/python -m pytest", cmds)

    def test_bare_python_with_venv_gets_pytest(self):
        # pm is None (no lockfile/pyproject) but python is in the stack.
        ctx = _ctx(pm=None, runtime={"venv": ".venv"}, stack=["python"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m pytest", cmds)

    def test_bare_python_without_runtime_emits_no_pytest(self):
        # No concrete runner known -> do not guess a global python.
        ctx = _ctx(pm=None, stack=["python"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c for c in cmds))


if __name__ == "__main__":
    unittest.main()
