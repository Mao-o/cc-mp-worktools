"""core/pm.py::detect_package_manager() priority ordering had zero test
coverage (internal backlog). Each test below pins one precedence rule from
the if/elif-style chain (first match wins), including ties between two
markers that could otherwise both apply."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from core.pm import detect_package_manager


def _pm(root: Path) -> object:
    return detect_package_manager(RepoContext(root=root, config=AnalysisConfig()))


class NoSignalTest(unittest.TestCase):
    def test_empty_directory_detects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_pm(Path(tmp)))


class JsTsPriorityTest(unittest.TestCase):
    def test_pnpm_lock_wins_over_package_lock_and_yarn_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-lock.yaml").write_text("")
            (root / "package-lock.json").write_text("{}")
            (root / "yarn.lock").write_text("")
            self.assertEqual(_pm(root), "pnpm")

    def test_pnpm_workspace_alone_also_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-workspace.yaml").write_text("")
            (root / "package-lock.json").write_text("{}")
            self.assertEqual(_pm(root), "pnpm")

    def test_npm_wins_over_yarn_and_bun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package-lock.json").write_text("{}")
            (root / "yarn.lock").write_text("")
            (root / "bun.lock").write_text("")
            self.assertEqual(_pm(root), "npm")

    def test_yarn_wins_over_bun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "yarn.lock").write_text("")
            (root / "bun.lockb").write_bytes(b"")
            self.assertEqual(_pm(root), "yarn")

    def test_bun_wins_over_deno(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bun.lock").write_text("")
            (root / "deno.json").write_text("{}")
            self.assertEqual(_pm(root), "bun")

    def test_deno_wins_over_python_and_jvm_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deno.jsonc").write_text("{}")
            (root / "pyproject.toml").write_text("")
            self.assertEqual(_pm(root), "deno")


class PythonPriorityTest(unittest.TestCase):
    def test_uv_wins_over_poetry_and_plain_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uv.lock").write_text("")
            (root / "poetry.lock").write_text("")
            (root / "pyproject.toml").write_text("")
            self.assertEqual(_pm(root), "uv")

    def test_uv_toml_alone_also_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uv.toml").write_text("")
            (root / "poetry.lock").write_text("")
            self.assertEqual(_pm(root), "uv")

    def test_poetry_wins_over_plain_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "poetry.lock").write_text("")
            (root / "pyproject.toml").write_text("")
            self.assertEqual(_pm(root), "poetry")

    def test_bare_pyproject_is_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("")
            self.assertEqual(_pm(root), "python")

    def test_python_wins_over_jvm_and_other_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("")
            (root / "pom.xml").write_text("")
            (root / "go.mod").write_text("")
            self.assertEqual(_pm(root), "python")


class JvmPriorityTest(unittest.TestCase):
    def test_gradle_wins_over_maven_and_sbt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle").write_text("")
            (root / "pom.xml").write_text("")
            (root / "build.sbt").write_text("")
            self.assertEqual(_pm(root), "gradle")

    def test_gradlew_alone_is_gradle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gradlew").write_text("")
            self.assertEqual(_pm(root), "gradle")

    def test_gradle_kts_alone_is_gradle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle.kts").write_text("")
            self.assertEqual(_pm(root), "gradle")

    def test_maven_wins_over_sbt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text("")
            (root / "build.sbt").write_text("")
            self.assertEqual(_pm(root), "maven")

    def test_sbt_wins_over_go_cargo_composer_mix_swift_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.sbt").write_text("")
            (root / "go.mod").write_text("")
            (root / "Cargo.toml").write_text("")
            self.assertEqual(_pm(root), "sbt")


class OtherPriorityTest(unittest.TestCase):
    def test_go_wins_over_cargo_composer_mix_swift_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("")
            (root / "Cargo.toml").write_text("")
            (root / "composer.json").write_text("{}")
            self.assertEqual(_pm(root), "go")

    def test_cargo_wins_over_composer_mix_swift_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("")
            (root / "composer.json").write_text("{}")
            self.assertEqual(_pm(root), "cargo")

    def test_composer_wins_over_mix_swift_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "composer.json").write_text("{}")
            (root / "mix.exs").write_text("")
            self.assertEqual(_pm(root), "composer")

    def test_mix_wins_over_swift_and_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mix.exs").write_text("")
            (root / "Package.swift").write_text("")
            (root / "App.csproj").write_text("")
            self.assertEqual(_pm(root), "mix")

    def test_swift_wins_over_dotnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("")
            (root / "App.csproj").write_text("")
            self.assertEqual(_pm(root), "swift")

    def test_dotnet_is_the_last_resort_before_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The solution must reference a managed project (round 6 fix,
            # see test_new_stack_detectors.py's vcxproj-only case) -- an
            # empty .sln is no longer enough on its own.
            (root / "App.sln").write_text(
                'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", '
                '"App.csproj", "{GUID}"\nEndProject\n'
            )
            self.assertEqual(_pm(root), "dotnet")

    def test_cmake_alone_detects_no_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CMakeLists.txt").write_text("")
            self.assertIsNone(_pm(root))


if __name__ == "__main__":
    unittest.main()
