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

from collectors.scripts import ScriptsCollector, _likely_commands, _test_tool_name
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


class PytestDeclaredOutsidePyprojectTest(unittest.TestCase):
    """Isolated-review P2-c (PR #67): _test_tool_name() only ever recognised
    pytest via `"pytest" in ctx.stack`, and detectors/python_stack.py only
    adds that stack entry when pyproject.toml exists and mentions pytest.
    A project declaring pytest solely via requirements*.txt / Pipfile /
    setup.cfg was invisible to that check, so a root tests/ dir made this
    fall back to `unittest discover tests` even though the tests are
    pytest-style bare functions that plain unittest discovery collects as
    zero tests. These use real files on disk (unlike test_scripts.py's
    fake-root `_ctx()` helper) because the fix reads them via
    collectors.dependencies.is_python_dependency_declared()."""

    def test_pytest_in_requirements_dev_txt_wins_over_unittest_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements-dev.txt").write_text("pytest==7.4.0\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["requirements-dev.txt", "tests/test_a.py"]
            ctx.stack = []  # no pyproject.toml -> python_stack.py never adds "pytest"
            ctx.results["test_snapshot"] = {"test_files": 1}
            self.assertEqual(_test_tool_name(ctx), "pytest")

    def test_no_pytest_declared_anywhere_still_falls_back_to_unittest(self):
        # Sanity check the fix doesn't make the fallback fire unconditionally.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["tests/test_a.py"]
            ctx.stack = []
            ctx.results["test_snapshot"] = {"test_files": 1}
            self.assertEqual(_test_tool_name(ctx), "unittest discover tests")

    def test_pyproject_present_but_silent_on_pytest_still_finds_it_in_requirements(self):
        # The realistic shape: a project mid-migration that already has a
        # pyproject.toml (so pm.py resolves pm="python" and python_stack.py
        # runs its pyproject-substring check) but keeps pytest declared in
        # requirements-dev.txt rather than pyproject.toml itself. End-to-end
        # through _likely_commands()'s pm="python" branch, which the other
        # two tests in this class do not exercise.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\ndependencies = ["flask==3.0"]\n')
            (root / "requirements-dev.txt").write_text("pytest==7.4.0\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["pyproject.toml", "requirements-dev.txt", "tests/test_a.py"]
            ctx.results["package_manager"] = "python"
            ctx.stack = []  # pyproject.toml text has no "pytest" substring
            ctx.results["test_snapshot"] = {"test_files": 1}
            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("python -m pytest", cmds)
            self.assertNotIn("python3 -m unittest discover tests", cmds)


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
