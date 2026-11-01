"""core/pm.py::detect_package_manager() priority ordering had zero test
coverage (internal backlog). Each case below pins one precedence rule from
the if/elif-style chain (first match wins), including ties between two
markers that could otherwise both apply.

Cases are tables: (label, {file name: content}, expected). The label is the
name the case had as a separate test."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from core.pm import detect_package_manager


def _pm(root: Path) -> object:
    return detect_package_manager(RepoContext(root=root, config=AnalysisConfig()))


Case = Tuple[str, Dict[str, Union[str, bytes]], Optional[str]]


def _run_cases(test: unittest.TestCase, cases: Sequence[Case]) -> None:
    for label, files, expected in cases:
        with test.subTest(label):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name, content in files.items():
                    if isinstance(content, bytes):
                        (root / name).write_bytes(content)
                    else:
                        (root / name).write_text(content)
                test.assertEqual(_pm(root), expected)


# The solution must reference a managed project (round 6 fix, see
# test_new_stack_detectors.py's vcxproj-only case) -- an empty .sln is no
# longer enough on its own.
_MANAGED_SLN = (
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", '
    '"App.csproj", "{GUID}"\nEndProject\n'
)


class NoSignalTest(unittest.TestCase):
    def test_empty_directory_detects_nothing(self):
        _run_cases(self, [("test_empty_directory_detects_nothing", {}, None)])


class JsTsPriorityTest(unittest.TestCase):
    def test_priority_order(self):
        _run_cases(self, [
            ("test_pnpm_lock_wins_over_package_lock_and_yarn_lock",
             {"pnpm-lock.yaml": "", "package-lock.json": "{}", "yarn.lock": ""},
             "pnpm"),
            ("test_pnpm_workspace_alone_also_wins",
             {"pnpm-workspace.yaml": "", "package-lock.json": "{}"}, "pnpm"),
            ("test_npm_wins_over_yarn_and_bun",
             {"package-lock.json": "{}", "yarn.lock": "", "bun.lock": ""}, "npm"),
            ("test_yarn_wins_over_bun",
             {"yarn.lock": "", "bun.lockb": b""}, "yarn"),
            ("test_bun_wins_over_deno",
             {"bun.lock": "", "deno.json": "{}"}, "bun"),
            ("test_deno_wins_over_python_and_jvm_markers",
             {"deno.jsonc": "{}", "pyproject.toml": ""}, "deno"),
        ])


class PythonPriorityTest(unittest.TestCase):
    def test_priority_order(self):
        _run_cases(self, [
            ("test_uv_wins_over_poetry_and_plain_pyproject",
             {"uv.lock": "", "poetry.lock": "", "pyproject.toml": ""}, "uv"),
            ("test_uv_toml_alone_also_wins",
             {"uv.toml": "", "poetry.lock": ""}, "uv"),
            ("test_poetry_wins_over_plain_pyproject",
             {"poetry.lock": "", "pyproject.toml": ""}, "poetry"),
            ("test_bare_pyproject_is_python",
             {"pyproject.toml": ""}, "python"),
            ("test_python_wins_over_jvm_and_other_markers",
             {"pyproject.toml": "", "pom.xml": "", "go.mod": ""}, "python"),
        ])


class JvmPriorityTest(unittest.TestCase):
    def test_priority_order(self):
        _run_cases(self, [
            ("test_gradle_wins_over_maven_and_sbt",
             {"build.gradle": "", "pom.xml": "", "build.sbt": ""}, "gradle"),
            ("test_gradlew_alone_is_gradle", {"gradlew": ""}, "gradle"),
            ("test_gradle_kts_alone_is_gradle", {"build.gradle.kts": ""}, "gradle"),
            ("test_maven_wins_over_sbt",
             {"pom.xml": "", "build.sbt": ""}, "maven"),
            ("test_sbt_wins_over_go_cargo_composer_mix_swift_dotnet",
             {"build.sbt": "", "go.mod": "", "Cargo.toml": ""}, "sbt"),
        ])


class OtherPriorityTest(unittest.TestCase):
    def test_priority_order(self):
        _run_cases(self, [
            ("test_go_wins_over_cargo_composer_mix_swift_dotnet",
             {"go.mod": "", "Cargo.toml": "", "composer.json": "{}"}, "go"),
            ("test_cargo_wins_over_composer_mix_swift_dotnet",
             {"Cargo.toml": "", "composer.json": "{}"}, "cargo"),
            ("test_composer_wins_over_mix_swift_dotnet",
             {"composer.json": "{}", "mix.exs": ""}, "composer"),
            ("test_mix_wins_over_swift_and_dotnet",
             {"mix.exs": "", "Package.swift": "", "App.csproj": ""}, "mix"),
            ("test_swift_wins_over_dotnet",
             {"Package.swift": "", "App.csproj": ""}, "swift"),
            ("test_dotnet_is_the_last_resort_before_none",
             {"App.sln": _MANAGED_SLN}, "dotnet"),
            ("test_cmake_alone_detects_no_package_manager",
             {"CMakeLists.txt": ""}, None),
        ])


if __name__ == "__main__":
    unittest.main()
