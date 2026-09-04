"""likely_commands の runtime 補正 (v0.6): venv/mise prefix の適用、
uv/poetry が自前 env 管理で不変なこと、bare-python (.py 比率) ケース。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.scripts import ScriptsCollector, _likely_commands
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


class NewStackLikelyCommandsTest(unittest.TestCase):
    """internal backlog: swift/dotnet/scala/elixir detectors added a
    package_manager value with no corresponding _likely_commands branch,
    so a repo with only e.g. build.sbt would get a "stack: scala" header
    but no Likely Commands hint at all."""

    def test_sbt_pm_suggests_sbt_test_and_compile(self):
        ctx = _ctx(pm="sbt", stack=["scala"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("sbt test", cmds)
        self.assertIn("sbt compile", cmds)

    def test_mix_pm_suggests_mix_test(self):
        ctx = _ctx(pm="mix", stack=["elixir"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("mix test", cmds)

    def test_swift_pm_suggests_swift_build_and_test(self):
        ctx = _ctx(pm="swift", stack=["swift"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("swift build", cmds)
        self.assertIn("swift test", cmds)

    def test_dotnet_pm_suggests_dotnet_test(self):
        ctx = _ctx(pm="dotnet", stack=["dotnet"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("dotnet test", cmds)

    def test_cmake_stack_alone_suggests_no_command(self):
        # cmake is deliberately not a package_manager value (see
        # tests/test_new_stack_detectors.py::CmakeStackDetectorTest); a
        # cmake-only repo gets a "stack: cmake" header but no guessed build
        # invocation.
        ctx = _ctx(pm=None, stack=["cmake"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("cmake" in c for c in cmds))


class ScriptCommandLengthCapTest(unittest.TestCase):
    """internal backlog: a single overly-long script command (some
    generators produce one-liners several hundred chars wide, e.g. the
    reported 354-char case) used to render unbounded in ## Scripts."""

    def test_long_command_is_truncated_to_120_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            long_command = "echo " + ("x" * 350)  # 355 chars total
            (root / "package.json").write_text(
                '{"scripts": {"build": "%s"}}' % long_command
            )
            ctx = RepoContext(root=root, config=AnalysisConfig())
            out = ScriptsCollector().collect(ctx)
            self.assertIsNotNone(out)
            line = next(ln for ln in out.splitlines() if ln.startswith("- build:"))
            # "- build: " prefix + the (<=120-char) command.
            self.assertLessEqual(len(line) - len("- build: "), 120)
            self.assertTrue(line.endswith("…"))

    def test_short_command_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts": {"test": "jest --watch"}}')
            ctx = RepoContext(root=root, config=AnalysisConfig())
            out = ScriptsCollector().collect(ctx)
            self.assertIn("- test: jest --watch", out)


if __name__ == "__main__":
    unittest.main()
