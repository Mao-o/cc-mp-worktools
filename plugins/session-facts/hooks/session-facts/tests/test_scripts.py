"""likely_commands の runtime 補正 (v0.6): venv/mise prefix の適用、
uv/poetry が自前 env 管理で不変なこと、bare-python (.py 比率) ケース。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.scripts import ScriptsCollector, _likely_commands
from core.context import AnalysisConfig, RepoContext
from core.pm import detect_package_manager
from detectors.dotnet_stack import DotnetStackDetector
from detectors.swift_stack import SwiftStackDetector


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

    def test_dotnet_command_survives_when_npm_is_the_primary_pm(self):
        # merge-review finding: a root with both a higher-priority JS
        # manifest (package-lock.json) and a .NET solution (App.sln) has
        # core/pm.py pick "npm" as the single primary package_manager (see
        # tests/test_pm.py::JsTsPriorityTest), but DotnetStackDetector still
        # reports "dotnet" in ctx.stack independently of that choice.
        # Before this fix, the dotnet branch lived in the pm-exclusive
        # elif chain below the npm `if prefix:` branch and could never run
        # once npm claimed pm, so "dotnet test" never appeared even though
        # the repo also has an App.sln at its root.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package-lock.json").write_text("{}")
            (root / "package.json").write_text('{"scripts": {"build": "webpack"}}')
            (root / "App.sln").write_text("Microsoft Visual Studio Solution File\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["package_manager"] = detect_package_manager(ctx)
            self.assertEqual(ctx.results["package_manager"], "npm")
            ctx.stack = DotnetStackDetector().detect(ctx)
            self.assertEqual(ctx.stack, ["dotnet"])

            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("npm run build", cmds)
            self.assertIn("dotnet test", cmds)

    def test_dotnet_command_survives_truncation_with_many_npm_scripts(self):
        # merge-review finding (round 2): the previous fix above only
        # covers the *presence* of the dotnet branch, not truncation. When
        # the co-located npm project exposes at least max_script_entries
        # scripts, every one of them is appended (as "npm run <name>")
        # before the scala/elixir/swift/dotnet block runs, so the trailing
        # `deduped[:max_items]` slice used to cut "dotnet test" off the end
        # even though it was correctly appended to `commands`.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package-lock.json").write_text("{}")
            npm_scripts = {f"script{i}": f"echo {i}" for i in range(16)}
            (root / "package.json").write_text(
                '{"scripts": %s}'
                % str(npm_scripts).replace("'", '"')
            )
            (root / "App.sln").write_text("Microsoft Visual Studio Solution File\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["package_manager"] = detect_package_manager(ctx)
            self.assertEqual(ctx.results["package_manager"], "npm")
            ctx.stack = DotnetStackDetector().detect(ctx)
            self.assertEqual(ctx.stack, ["dotnet"])

            max_items = AnalysisConfig().max_script_entries
            self.assertEqual(max_items, 16)  # same default the cap in scripts.py uses
            cmds = _likely_commands(ctx, max_items=max_items)
            self.assertIn("dotnet test", cmds)

    def test_existing_stacks_output_is_unchanged_when_pm_and_stack_agree(self):
        # Guards against the refactor (moving scala/elixir/swift/dotnet off
        # the pm-exclusive elif chain onto stack membership) silently
        # changing output for repos where pm and the newly-added stack
        # detector agree, which is still the common case.
        for pm, stack, expected in (
            ("sbt", ["scala"], ["sbt test", "sbt compile"]),
            ("mix", ["elixir"], ["mix test"]),
            ("swift", ["swift"], ["swift build", "swift test"]),
            ("dotnet", ["dotnet"], ["dotnet test"]),
        ):
            with self.subTest(pm=pm):
                ctx = _ctx(pm=pm, stack=stack)
                cmds = _likely_commands(ctx, max_items=16)
                for expected_cmd in expected:
                    self.assertIn(expected_cmd, cmds)

    def test_xcode_only_project_suggests_no_swift_command(self):
        # merge-review finding: detectors/swift_stack.py now also reports
        # "stack: swift" for an Xcode-only app (*.xcodeproj/*.xcworkspace,
        # no Package.swift) -- see SwiftStackDetectorTest in
        # tests/test_new_stack_detectors.py. "swift" in stack alone must NOT
        # be enough to suggest `swift build`/`swift test`: those are SwiftPM
        # commands, and `xcodebuild` (the Xcode-only equivalent) needs an
        # explicit -scheme this collector cannot safely infer.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            ctx = RepoContext(root=root, config=AnalysisConfig())
            # detectors/swift_stack.py's Xcode-only branch now also
            # requires a tracked .swift file as positive evidence
            # (merge-review finding, round 5) -- supply one so this fixture
            # still exercises the "swift" stack + no-swift-command path it
            # is meant to guard.
            ctx.tracked_files = ["App/AppDelegate.swift"]
            ctx.results["package_manager"] = detect_package_manager(ctx)
            self.assertIsNone(ctx.results["package_manager"])
            ctx.stack = SwiftStackDetector().detect(ctx)
            self.assertEqual(ctx.stack, ["swift"])

            cmds = _likely_commands(ctx, max_items=16)
            self.assertNotIn("swift build", cmds)
            self.assertNotIn("swift test", cmds)

    def test_package_swift_project_still_suggests_swift_commands(self):
        # Companion to the Xcode-only case above, exercised through the same
        # real-filesystem detector + package_manager pipeline (rather than
        # the synthetic _ctx() helper used elsewhere in this file) so the
        # two cases are guarded by tests built the same way.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text(
                "// swift-tools-version:5.9\nimport PackageDescription\n"
            )
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["package_manager"] = detect_package_manager(ctx)
            self.assertEqual(ctx.results["package_manager"], "swift")
            ctx.stack = SwiftStackDetector().detect(ctx)
            self.assertEqual(ctx.stack, ["swift"])

            cmds = _likely_commands(ctx, max_items=16)
            self.assertIn("swift build", cmds)
            self.assertIn("swift test", cmds)


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
