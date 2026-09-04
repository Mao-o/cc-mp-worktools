"""Minimal fixture tests for the Swift/.NET/Scala/Elixir/CMake detectors
(internal backlog: repos in these stacks had no detector at all, so header
output degraded to Structure-only with no "stack:" line or Likely Commands,
despite CODE_EXTENSIONS already counting their source files in Test
Snapshot -- an asymmetric state)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List

import _testutil  # noqa: F401  (sys.path 整備)

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


if __name__ == "__main__":
    unittest.main()
