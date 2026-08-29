"""likely_commands の runtime 補正 (v0.6): venv/mise prefix の適用、
uv/poetry が自前 env 管理で不変なこと、bare-python (.py 比率) ケース。

internal backlog (2026-08 精査) で追加: 根拠の無い Likely Commands 抑止。
docker compose up は compose ファイル存在時のみ (Dockerfile 単体は docker
build .)、mise install は mise config 存在時のみ (.tool-versions 単体は
asdf install)、pytest/unittest は test_files が実在する時のみ (かつ
unittest discover tests は root 直下の tests/ に test_*.py がある時のみ)。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.scripts import _likely_commands
from core.context import AnalysisConfig, RepoContext


def _ctx(pm=None, runtime=None, stack=(), test_files=1, tracked_files=()) -> RepoContext:
    ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
    if pm is not None:
        ctx.results["package_manager"] = pm
    if runtime is not None:
        ctx.results["runtime"] = runtime
    ctx.stack = list(stack)
    ctx.tracked_files = list(tracked_files)
    # Every test below is about runtime-prefix / pm-command selection, a
    # concern orthogonal to "are there test files at all" -- default this on
    # so that dimension doesn't need restating in each test. Tests that
    # specifically exercise the test_files gate override it explicitly.
    if test_files:
        ctx.results["test_snapshot"] = {"test_files": test_files}
    return ctx


class LikelyCommandsRuntimeTest(unittest.TestCase):
    def test_python_pm_with_venv_prefix(self):
        ctx = _ctx(pm="python", runtime={"venv": ".venv"}, stack=["pytest"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m pytest", cmds)
        self.assertNotIn("python -m pytest", cmds)  # not the bare form

    def test_python_pm_with_mise_prefix(self):
        ctx = _ctx(
            pm="python",
            runtime={"manager": "mise", "tools": {"python": "3.12"}},
            stack=["pytest"],
        )
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("mise exec -- python -m pytest", cmds)

    def test_python_pm_without_runtime_is_bare(self):
        ctx = _ctx(pm="python", stack=["pytest"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("python -m pytest", cmds)

    def test_uv_left_unchanged(self):
        # uv manages its own env: keep ``uv run`` even when a venv is present.
        ctx = _ctx(pm="uv", runtime={"venv": ".venv"}, stack=["pytest"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("uv run pytest", cmds)
        self.assertNotIn(".venv/bin/python -m pytest", cmds)

    def test_poetry_left_unchanged(self):
        ctx = _ctx(pm="poetry", runtime={"venv": ".venv"}, stack=["pytest"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("poetry run pytest", cmds)
        self.assertNotIn(".venv/bin/python -m pytest", cmds)

    def test_bare_python_with_venv_gets_pytest(self):
        # pm is None (no lockfile/pyproject) but python is in the stack.
        ctx = _ctx(pm=None, runtime={"venv": ".venv"}, stack=["python", "pytest"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m pytest", cmds)

    def test_bare_python_without_runtime_emits_no_pytest(self):
        # No concrete runner known -> do not guess a global python.
        ctx = _ctx(pm=None, stack=["python"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c for c in cmds))


class TestFilesGateTest(unittest.TestCase):
    """internal backlog: pyproject/uv/poetry used to suggest a pytest line
    regardless of whether any test files were detected at all."""

    def test_no_test_files_suppresses_pytest_for_python_pm(self):
        ctx = _ctx(pm="python", stack=["pytest"], test_files=0)
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c or "unittest" in c for c in cmds))

    def test_no_test_files_suppresses_pytest_for_uv(self):
        ctx = _ctx(pm="uv", stack=["pytest"], test_files=0)
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c or "unittest" in c for c in cmds))

    def test_no_test_files_suppresses_pytest_for_poetry(self):
        ctx = _ctx(pm="poetry", stack=["pytest"], test_files=0)
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c or "unittest" in c for c in cmds))

    def test_no_test_files_suppresses_bare_python(self):
        ctx = _ctx(pm=None, runtime={"venv": ".venv"}, stack=["python", "pytest"], test_files=0)
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c or "unittest" in c for c in cmds))

    def test_test_files_present_still_emits_pytest(self):
        # Sanity check the gate isn't inverted.
        ctx = _ctx(pm="python", stack=["pytest"], test_files=3)
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("python -m pytest", cmds)


class UnittestFallbackTest(unittest.TestCase):
    """internal backlog: no pytest dependency + a root tests/ directory
    (this plugin's own layout convention) should surface `unittest discover
    tests` instead of silently emitting no test command at all."""

    def test_python_pm_no_pytest_dep_with_root_tests_dir(self):
        ctx = _ctx(
            pm="python",
            stack=[],  # no "pytest" detected in pyproject.toml
            tracked_files=["tests/test_cli.py", "src/main.py"],
        )
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("python3 -m unittest discover tests", cmds)
        self.assertFalse(any("pytest" in c for c in cmds))

    def test_python_pm_no_pytest_dep_with_venv_prefix(self):
        ctx = _ctx(
            pm="python",
            runtime={"venv": ".venv"},
            stack=[],
            tracked_files=["tests/test_cli.py"],
        )
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m unittest discover tests", cmds)

    def test_uv_no_pytest_dep_with_root_tests_dir(self):
        ctx = _ctx(pm="uv", stack=[], tracked_files=["tests/test_a.py"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("uv run python -m unittest discover tests", cmds)

    def test_poetry_no_pytest_dep_with_root_tests_dir(self):
        ctx = _ctx(pm="poetry", stack=[], tracked_files=["tests/test_a.py"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("poetry run python -m unittest discover tests", cmds)

    def test_bare_python_no_pytest_dep_with_root_tests_dir(self):
        ctx = _ctx(
            pm=None,
            runtime={"venv": ".venv"},
            stack=["python"],
            tracked_files=["tests/test_a.py"],
        )
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn(".venv/bin/python -m unittest discover tests", cmds)

    def test_no_pytest_dep_and_no_root_tests_dir_emits_nothing(self):
        # Test files exist (e.g. under spec/) but not in a root tests/ dir --
        # `unittest discover tests` would find nothing there, so neither
        # command should be suggested (worktools' own layout,
        # plugins/*/hooks/*/tests, is exactly this case: no root tests/).
        ctx = _ctx(
            pm="python",
            stack=[],
            tracked_files=["spec/foo_spec.py", "plugins/x/hooks/y/tests/test_z.py"],
        )
        cmds = _likely_commands(ctx, max_items=16)
        self.assertFalse(any("pytest" in c or "unittest" in c for c in cmds))

    def test_nested_test_file_under_root_tests_dir_still_counts(self):
        # unittest discover recurses by default, so a nested test_*.py under
        # a root tests/ dir still grounds the command.
        ctx = _ctx(pm="python", stack=[], tracked_files=["tests/sub/test_nested.py"])
        cmds = _likely_commands(ctx, max_items=16)
        self.assertIn("python3 -m unittest discover tests", cmds)


class DockerCommandGroundingTest(unittest.TestCase):
    """internal backlog: docker compose up used to fire on a bare
    Dockerfile (detectors/docker.py's "docker" stack entry covers both)."""

    def test_dockerfile_only_suggests_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text("FROM scratch\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["docker"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("docker build .", cmds)
            self.assertNotIn("docker compose up", cmds)

    def test_compose_file_suggests_compose_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text("FROM scratch\n")
            (root / "docker-compose.yml").write_text("services: {}\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["docker"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("docker compose up", cmds)
            self.assertNotIn("docker build .", cmds)

    def test_bare_compose_yml_without_dockerfile_suggests_compose_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "compose.yaml").write_text("services: {}\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["docker"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("docker compose up", cmds)


class MiseCommandGroundingTest(unittest.TestCase):
    """internal backlog: mise install used to fire on a bare .tool-versions
    (asdf-style) file too, since has_mise() treats both as "mise"."""

    def test_tool_versions_only_suggests_asdf_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".tool-versions").write_text("python 3.12.0\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["mise"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("asdf install", cmds)
            self.assertNotIn("mise install", cmds)

    def test_mise_toml_suggests_mise_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mise.toml").write_text('[tools]\npython = "3.12"\n')
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["mise"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("mise install", cmds)
            self.assertNotIn("asdf install", cmds)

    def test_both_present_prefers_mise_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mise.toml").write_text('[tools]\npython = "3.12"\n')
            (root / ".tool-versions").write_text("python 3.12.0\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.stack = ["mise"]
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("mise install", cmds)
            self.assertNotIn("asdf install", cmds)


if __name__ == "__main__":
    unittest.main()
