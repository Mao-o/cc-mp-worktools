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
from typing import List

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


def _ctx(root: Path) -> RepoContext:
    return RepoContext(root=root, config=AnalysisConfig())


def _detect(detector, root: Path) -> List[str]:
    return detector.detect(_ctx(root))


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
