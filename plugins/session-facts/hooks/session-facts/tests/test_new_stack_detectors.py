"""Minimal fixture tests for the Swift/.NET/Scala/Elixir/CMake detectors
(internal backlog: repos in these stacks had no detector at all, so header
output degraded to Structure-only with no "stack:" line or Likely Commands).

Swift/.NET/Scala source extensions (``.swift``/``.cs``/``.scala``) were
already registered in ``core/constants.py::CODE_EXTENSIONS`` before their
detectors existed, so those three stacks got a working Test Snapshot /
Service Entry Points for free. Elixir (``.ex``/``.exs``) and CMake's
underlying C/C++ sources (``.c``/``.cc``/``.cpp``/``.h``/``.hpp``, plus the
``.cxx``/``.hxx``/``.hh`` suffixes a later merge-review pass added) were not
registered -- a merge-review finding caught this for Elixir specifically
(``stack: elixir`` and ``mix test`` would be reported while the repo's own
``lib/*.ex`` and ``test/*_test.exs`` were silently dropped from Test
Snapshot / Service Entry Points); the same gap existed for CMake's C/C++
sources and is closed alongside it. ``ExtensionRegistrationTest`` below
pins that both are now covered.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.services import ServicesCollector
from collectors.tests import TestsCollector
from core.constants import CODE_EXTENSIONS
from core.context import AnalysisConfig, RepoContext
from core.pm import detect_package_manager
from detectors.cmake_stack import CmakeStackDetector
from detectors.dotnet_stack import DotnetStackDetector
from detectors.elixir_stack import ElixirStackDetector
from detectors.scala_stack import ScalaStackDetector
from detectors.swift_stack import SwiftStackDetector


def _ctx(root: Path, tracked: Optional[List[str]] = None) -> RepoContext:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    if tracked is not None:
        ctx.tracked_files = tracked
    return ctx


def _detect(detector, root: Path, tracked: Optional[List[str]] = None) -> List[str]:
    return detector.detect(_ctx(root, tracked))


class SwiftStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(SwiftStackDetector(), Path(tmp)), [])

    def test_package_swift_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text(
                '// swift-tools-version:5.9\nimport PackageDescription\n'
            )
            self.assertEqual(_detect(SwiftStackDetector(), root), ["swift"])

    def test_package_swift_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("// swift-tools-version:5.9\n")
            self.assertEqual(detect_package_manager(_ctx(root)), "swift")

    def test_xcodeproj_alone_is_detected(self):
        # merge-review finding: an Xcode-only app (no Package.swift) has no
        # SwiftPM manifest at all, but *.xcodeproj is still Swift's own
        # project marker -- without this, such a repo got no "stack: swift"
        # and (via PROJECT_MARKERS) was rejected outright by the non-git
        # marker gate. A tracked .swift file is required alongside the
        # bundle (see ObjcOnlyXcodeProjectTest below) -- this fixture
        # supplies one so it still reports "swift".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            tracked = ["App/AppDelegate.swift"]
            self.assertEqual(_detect(SwiftStackDetector(), root, tracked), ["swift"])

    def test_xcworkspace_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcworkspace").mkdir()
            tracked = ["App/AppDelegate.swift"]
            self.assertEqual(_detect(SwiftStackDetector(), root, tracked), ["swift"])

    def test_xcodeproj_without_any_swift_source_is_not_detected(self):
        # merge-review finding (round 5): *.xcodeproj/*.xcworkspace are not
        # Swift-specific -- a legacy Objective-C-only app (.m/.mm sources,
        # no .swift anywhere) still ships an Xcode project/workspace, and
        # the bundle-presence check alone wrongly tagged it "stack: swift".
        # Require at least one tracked .swift file as positive evidence
        # before applying the Swift tag to the Xcode-only branch.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            tracked = ["App/AppDelegate.m", "App/Helper.mm"]
            self.assertEqual(_detect(SwiftStackDetector(), root, tracked), [])

    def test_xcworkspace_without_any_swift_source_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcworkspace").mkdir()
            tracked = ["App/AppDelegate.m"]
            self.assertEqual(_detect(SwiftStackDetector(), root, tracked), [])

    def test_xcodeproj_with_no_tracked_files_at_all_is_not_detected(self):
        # Same gap, degenerate case: no tracked files recorded at all (the
        # bundle directory itself carries no source signal).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            self.assertEqual(_detect(SwiftStackDetector(), root), [])

    def test_xcodeproj_is_not_the_package_manager(self):
        # Xcode-only projects have no SwiftPM manifest to run `swift build`/
        # `swift test` against, so core/pm.py must not report "swift" here
        # (collectors/scripts.py relies on this to withhold those two
        # commands -- see tests/test_scripts.py::
        # NewStackLikelyCommandsTest.test_xcode_only_project_suggests_no_swift_command).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.xcodeproj").mkdir()
            self.assertIsNone(detect_package_manager(_ctx(root)))


class DotnetStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(DotnetStackDetector(), Path(tmp)), [])

    def test_root_csproj_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

    def test_root_sln_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.sln").write_text("Microsoft Visual Studio Solution File\n")
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

    def test_root_slnx_is_detected(self):
        # merge-review finding (round 5): .slnx (the newer XML-based
        # solution format, .NET SDK 9+/VS 17.10+) was not recognized at
        # all -- only the classic .sln suffix was checked.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.slnx").write_text("<Solution />\n")
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

    def test_root_fsproj_is_detected(self):
        # merge-review finding: a solutionless F# repo (no .sln, just an
        # F# project file) was never recognized as dotnet at all -- only
        # .csproj/.sln were checked.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.fsproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

    def test_root_vbproj_is_detected(self):
        # Same gap, VB.NET side.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.vbproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

    def test_nested_csproj_is_not_detected(self):
        # Root-level check only, matching the majority convention among
        # this plugin's other single-marker detectors (go/rust/ruby/php/
        # java all check ctx.root directly, not the whole tree).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(_detect(DotnetStackDetector(), root), [])

    def test_csproj_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(detect_package_manager(_ctx(root)), "dotnet")

    def test_fsproj_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.fsproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(detect_package_manager(_ctx(root)), "dotnet")

    def test_vbproj_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.vbproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
            )
            self.assertEqual(detect_package_manager(_ctx(root)), "dotnet")

    def test_slnx_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.slnx").write_text("<Solution />\n")
            self.assertEqual(detect_package_manager(_ctx(root)), "dotnet")


class ScalaStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(ScalaStackDetector(), Path(tmp)), [])

    def test_build_sbt_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.sbt").write_text('name := "app"\nversion := "0.1.0"\n')
            self.assertEqual(_detect(ScalaStackDetector(), root), ["scala"])

    def test_build_sbt_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.sbt").write_text('name := "app"\n')
            self.assertEqual(detect_package_manager(_ctx(root)), "sbt")


class ElixirStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(ElixirStackDetector(), Path(tmp)), [])

    def test_mix_exs_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mix.exs").write_text(
                "defmodule App.MixProject do\n  use Mix.Project\nend\n"
            )
            self.assertEqual(_detect(ElixirStackDetector(), root), ["elixir"])

    def test_mix_exs_is_the_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
            self.assertEqual(detect_package_manager(_ctx(root)), "mix")


class CmakeStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(CmakeStackDetector(), Path(tmp)), [])

    def test_cmakelists_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.20)\nproject(app)\n"
            )
            self.assertEqual(_detect(CmakeStackDetector(), root), ["cmake"])

    def test_cmakelists_is_not_a_package_manager(self):
        # Deliberately not wired into core/pm.py or Likely Commands: unlike
        # the other four, cmake's actual invocation (build dir, generator)
        # is not standardized enough to guess a single command safely.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CMakeLists.txt").write_text("project(app)\n")
            self.assertIsNone(detect_package_manager(_ctx(root)))


class ExtensionRegistrationTest(unittest.TestCase):
    """Guards the merge-review finding: a stack detector reporting
    ``stack: <x>`` is not enough on its own -- CODE_EXTENSIONS must also
    list that stack's source suffixes, or Test Snapshot / Service Entry
    Points silently drop every file the detector's own stack claims to
    cover."""

    def test_elixir_extensions_are_registered(self):
        for ext in (".ex", ".exs"):
            self.assertIn(ext, CODE_EXTENSIONS)

    def test_cmake_c_family_extensions_are_registered(self):
        for ext in (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".hh"):
            self.assertIn(ext, CODE_EXTENSIONS)

    def test_dotnet_fsharp_vb_extensions_are_registered(self):
        # merge-review finding (round 2): dotnet_stack.py now also detects
        # *.fsproj/*.vbproj repos, but F#'s/VB.NET's own source suffixes
        # (.fs/.fsx/.vb) were not in CODE_EXTENSIONS -- same gap as
        # Elixir's/CMake's above, one stack later.
        for ext in (".fs", ".fsx", ".vb"):
            self.assertIn(ext, CODE_EXTENSIONS)

    def test_fsharp_signature_file_extension_is_registered(self):
        # merge-review finding (round 4): the fix above registered .fs/.fsx
        # but missed .fsi, F#'s signature-file suffix -- an .fsi-heavy
        # project (public API declarations split out from their .fs
        # implementation) undercounted Test Snapshot/Service Entry Points
        # the same way the original Elixir/CMake gap did.
        self.assertIn(".fsi", CODE_EXTENSIONS)

    def test_fsharp_test_snapshot_counts_signature_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "App.fsproj",
                "Library.fsi",
                "Library.fs",
                "Tests/LibraryTests.fs",
            ]
            for name in tracked:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            # Library.fsi + Library.fs are both counted as code_files.
            self.assertIn("code_files: 2", out)
            self.assertIn("test_files: 1", out)

    def test_fsharp_test_snapshot_counts_solutionless_layout(self):
        # A conventional solutionless F# repo: an .fsproj at the root, no
        # .sln. Before dotnet_stack.py recognized *.fsproj and
        # CODE_EXTENSIONS registered .fs/.fsx, this repo got neither a
        # "stack: dotnet" header line nor any Test Snapshot counts.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "App.fsproj",
                "Program.fs",
                "Library.fs",
                "Tests/ProgramTests.fs",
            ]
            for name in tracked:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("")
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            # App.fsproj itself is not a registered source suffix (a build
            # manifest, not source, mirroring CMakeLists.txt's treatment
            # above), so code_files only counts Program.fs/Library.fs.
            self.assertIn("code_files: 2", out)
            self.assertIn("test_files: 1", out)

    def test_dotnet_test_snapshot_counts_slnx_solution_layout(self):
        # merge-review finding (round 5): a repo whose only root-level
        # solution file is the newer .slnx format (member .csproj files in
        # subdirectories) is exactly the ".sln-equivalent" case
        # dotnet_stack.py's suffix tuple already exists to cover.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "App.slnx",
                "src/App/Program.cs",
                "Tests/ProgramTests.cs",
            ]
            for name in tracked:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("")
            self.assertEqual(_detect(DotnetStackDetector(), root), ["dotnet"])

            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("code_files: 1", out)
            self.assertIn("test_files: 1", out)

    def test_scala_script_extension_is_registered(self):
        # merge-review round-5 stack-extension inventory: .sc is Scala's
        # script/worksheet suffix (`scala script.sc`, Ammonite, scala-cli
        # scripts) -- .scala alone covers ordinary sources but silently
        # dropped .sc scripts from Test Snapshot/Service Entry Points, the
        # same shape as every other CODE_EXTENSIONS gap above.
        self.assertIn(".sc", CODE_EXTENSIONS)

    def test_scala_test_snapshot_counts_sc_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "build.sbt",
                "scripts/Migrate.sc",
                "src/main/scala/App.scala",
                "src/test/scala/AppTest.scala",
            ]
            for name in tracked:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            # Migrate.sc + App.scala are both counted as code_files.
            self.assertIn("code_files: 2", out)
            self.assertIn("test_files: 1", out)

    def test_elixir_test_snapshot_counts_conventional_mix_layout(self):
        # A conventional Mix project: lib/*.ex sources, test/*_test.exs
        # tests. Before CODE_EXTENSIONS registered .ex/.exs, every one of
        # these files was invisible to the Test Snapshot collector even
        # though the elixir detector had already reported "stack: elixir"
        # and "mix test" for the very same repo.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "mix.exs",
                "lib/app.ex",
                "lib/app/worker.ex",
                "test/app_test.exs",
                "test/app/worker_test.exs",
            ]
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            # mix.exs itself is also a registered .exs file, so code_files
            # includes it alongside the two lib/*.ex sources.
            self.assertIn("code_files: 3", out)
            self.assertIn("test_files: 2", out)

    def test_cmake_test_snapshot_counts_conventional_c_layout(self):
        # A conventional CMake project: C/C++ sources/headers plus a
        # tests/ dir of *_test.cpp files. cmake_stack.py has no source
        # extensions of its own (CMakeLists.txt drives a build, it is not
        # a source file), so this is the same gap as Elixir's, just one
        # level removed: the language CMake builds, not CMake itself.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "CMakeLists.txt",
                "src/app.cpp",
                "src/app.h",
                "tests/app_test.cpp",
            ]
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = TestsCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("code_files: 2", out)
            self.assertIn("test_files: 1", out)

    def test_elixir_service_entry_points_surfaces_ex_files(self):
        # The review also named collectors/services.py: an .ex file under a
        # SERVICE_DIR_MARKERS-matching directory (e.g. lib/app/services/)
        # scores like any other language and should be surfaced. A bare
        # lib/app.ex still won't surface -- "lib" itself is not a
        # SERVICE_DIR_MARKERS entry, which this fix does not change.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [
                "mix.exs",
                "lib/app.ex",
                "lib/app/services/mailer.ex",
            ]
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = tracked
            out = ServicesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("lib/app/services/mailer.ex", out)
            self.assertNotIn("lib/app.ex", out)


if __name__ == "__main__":
    unittest.main()
