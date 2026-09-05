"""Minimal fixture tests for detectors/ modules that had zero test
coverage (internal backlog: only flutter.py, mise.py (via
tests/test_runtime.py) and firebase.py (tests/test_detectors.py) had any
tests; every other detectors/*.py file -- including several this plugin
relies on for its most common stacks, Node/TypeScript and Next.js among
them -- was completely unverified). Each class below covers one detector
with its no-signal baseline plus each of its own detection branches; the
Swift/.NET/Scala/Elixir/CMake detectors added alongside this batch have
their own tests/test_new_stack_detectors.py instead."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from detectors.claude_plugin import ClaudePluginDetector
from detectors.deno import DenoDetector
from detectors.docker import DockerDetector
from detectors.java_stack import JavaStackDetector
from detectors.nextjs import NextjsDetector
from detectors.node_typescript import NodeTypescriptDetector
from detectors.prisma import PrismaDetector
from detectors.python_stack import PythonStackDetector
from detectors.react_vite import ReactViteDetector
from detectors.taskrunner import TaskrunnerDetector
from detectors.testing import TestingDetector


def _ctx(root: Path, tracked_files: Optional[List[str]] = None) -> RepoContext:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    if tracked_files is not None:
        ctx.tracked_files = tracked_files
    return ctx


def _write_package_json(root: Path, deps: dict) -> None:
    (root / "package.json").write_text(json.dumps({"dependencies": deps}))


class NodeTypescriptDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(NodeTypescriptDetector().detect(_ctx(Path(tmp))), [])

    def test_package_json_alone_detects_node_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}")
            self.assertEqual(NodeTypescriptDetector().detect(_ctx(root)), ["node"])

    def test_tsconfig_without_package_json_detects_typescript_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tsconfig.json").write_text("{}")
            self.assertEqual(NodeTypescriptDetector().detect(_ctx(root)), ["typescript"])

    def test_typescript_dependency_without_tsconfig_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"typescript": "^5.0.0"})
            self.assertEqual(
                NodeTypescriptDetector().detect(_ctx(root)), ["node", "typescript"]
            )


class NextjsDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(NextjsDetector().detect(_ctx(Path(tmp))), [])

    def test_next_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"next": "^14.0.0"})
            self.assertEqual(NextjsDetector().detect(_ctx(root)), ["nextjs"])

    def test_next_config_file_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "next.config.js").write_text("module.exports = {}\n")
            self.assertEqual(NextjsDetector().detect(_ctx(root)), ["nextjs"])


class PythonStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(PythonStackDetector().detect(_ctx(Path(tmp))), [])

    def test_pyproject_alone_detects_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname = "x"\n')
            self.assertEqual(PythonStackDetector().detect(_ctx(root)), ["python"])

    def test_pyproject_framework_mentions_are_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["fastapi", "pytest"]\n'
            )
            self.assertEqual(
                PythonStackDetector().detect(_ctx(root)),
                ["python", "fastapi", "pytest"],
            )

    def test_uv_lock_without_pyproject_or_py_files_is_not_detected(self):
        # uv/poetry tags are only appended once a "python" tag was already
        # found (pyproject.toml, or the .py-ratio fallback); a bare uv.lock
        # with neither present detects nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uv.lock").write_text("")
            self.assertEqual(PythonStackDetector().detect(_ctx(root)), [])

    def test_py_ratio_fallback_without_pyproject_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = [f"m{i}.py" for i in range(10)] + ["README.md"]
            self.assertEqual(
                PythonStackDetector().detect(_ctx(root, tracked_files=tracked)),
                ["python"],
            )

    def test_py_ratio_below_threshold_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracked = ["m0.py"] + [f"f{i}.md" for i in range(20)]
            self.assertEqual(
                PythonStackDetector().detect(_ctx(root, tracked_files=tracked)), []
            )


class JavaStackDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(JavaStackDetector().detect(_ctx(Path(tmp))), [])

    def test_gradlew_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gradlew").write_text("#!/bin/sh\n")
            self.assertEqual(JavaStackDetector().detect(_ctx(root)), ["java", "gradle"])

    def test_pom_xml_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text("<project></project>\n")
            self.assertEqual(JavaStackDetector().detect(_ctx(root)), ["java", "maven"])

    def test_both_gradle_and_maven_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build.gradle").write_text("")
            (root / "pom.xml").write_text("<project></project>\n")
            self.assertEqual(
                JavaStackDetector().detect(_ctx(root)), ["java", "gradle", "maven"]
            )


class DockerDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(DockerDetector().detect(_ctx(Path(tmp))), [])

    def test_dockerfile_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Dockerfile").write_text("FROM scratch\n")
            self.assertEqual(DockerDetector().detect(_ctx(root)), ["docker"])

    def test_compose_file_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text("services: {}\n")
            self.assertEqual(DockerDetector().detect(_ctx(root)), ["docker"])


class ClaudePluginDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ClaudePluginDetector().detect(_ctx(Path(tmp))), [])

    def test_root_marketplace_json_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "marketplace.json").write_text("{}")
            self.assertEqual(
                ClaudePluginDetector().detect(_ctx(root)), ["claude-code-marketplace"]
            )

    def test_nested_marketplace_json_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "marketplace.json").write_text("{}")
            self.assertEqual(
                ClaudePluginDetector().detect(_ctx(root)), ["claude-code-marketplace"]
            )

    def test_plugin_json_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text("{}")
            self.assertEqual(
                ClaudePluginDetector().detect(_ctx(root)), ["claude-code-plugin"]
            )

    def test_component_subdirectories_are_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text("{}")
            (root / "hooks").mkdir()
            (root / "skills").mkdir()
            self.assertEqual(
                ClaudePluginDetector().detect(_ctx(root)),
                ["claude-code-plugin", "hooks", "skills"],
            )


class DenoDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(DenoDetector().detect(_ctx(Path(tmp))), [])

    def test_deno_json_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deno.json").write_text("{}")
            self.assertEqual(DenoDetector().detect(_ctx(root)), ["deno"])

    def test_deno_jsonc_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deno.jsonc").write_text("{}")
            self.assertEqual(DenoDetector().detect(_ctx(root)), ["deno"])


class ReactViteDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ReactViteDetector().detect(_ctx(Path(tmp))), [])

    def test_react_dependency_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"react": "^18.0.0"})
            self.assertEqual(ReactViteDetector().detect(_ctx(root)), ["react"])

    def test_vite_config_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vite.config.ts").write_text("export default {}\n")
            self.assertEqual(ReactViteDetector().detect(_ctx(root)), ["vite"])

    def test_both_are_reported_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"react": "^18.0.0"})
            (root / "vite.config.js").write_text("export default {}\n")
            self.assertEqual(
                ReactViteDetector().detect(_ctx(root)), ["react", "vite"]
            )


class TestingDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(TestingDetector().detect(_ctx(Path(tmp))), [])

    def test_each_dependency_flag_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(
                root,
                {
                    "zod": "^3",
                    "vitest": "^1",
                    "jest": "^29",
                    "@playwright/test": "^1",
                    "cypress": "^13",
                },
            )
            self.assertEqual(
                TestingDetector().detect(_ctx(root)),
                ["zod", "vitest", "jest", "playwright", "cypress"],
            )

    def test_playwright_config_without_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "playwright.config.ts").write_text("export default {}\n")
            self.assertEqual(TestingDetector().detect(_ctx(root)), ["playwright"])

    def test_cypress_config_without_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cypress.config.ts").write_text("export default {}\n")
            self.assertEqual(TestingDetector().detect(_ctx(root)), ["cypress"])

    def test_pnpm_workspace_alone_is_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n")
            self.assertEqual(TestingDetector().detect(_ctx(root)), ["monorepo"])

    def test_turbo_json_alone_is_monorepo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turbo.json").write_text("{}")
            self.assertEqual(TestingDetector().detect(_ctx(root)), ["monorepo"])


class PrismaDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(PrismaDetector().detect(_ctx(Path(tmp))), [])

    def test_prisma_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"prisma": "^5.0.0"})
            self.assertEqual(PrismaDetector().detect(_ctx(root)), ["prisma"])

    def test_prisma_client_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_package_json(root, {"@prisma/client": "^5.0.0"})
            self.assertEqual(PrismaDetector().detect(_ctx(root)), ["prisma"])

    def test_prisma_directory_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prisma").mkdir()
            self.assertEqual(PrismaDetector().detect(_ctx(root)), ["prisma"])


class TaskrunnerDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(TaskrunnerDetector().detect(_ctx(Path(tmp))), [])

    def test_makefile_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("test:\n\techo hi\n")
            self.assertEqual(TaskrunnerDetector().detect(_ctx(root)), ["makefile"])

    def test_justfile_lowercase_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "justfile").write_text("test:\n\techo hi\n")
            self.assertEqual(TaskrunnerDetector().detect(_ctx(root)), ["justfile"])

    def test_taskfile_yml_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Taskfile.yml").write_text("version: '3'\n")
            self.assertEqual(TaskrunnerDetector().detect(_ctx(root)), ["taskfile"])

    def test_nx_json_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nx.json").write_text("{}")
            self.assertEqual(TaskrunnerDetector().detect(_ctx(root)), ["nx"])

    def test_multiple_task_runners_are_all_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("test:\n\techo hi\n")
            (root / "nx.json").write_text("{}")
            self.assertEqual(
                TaskrunnerDetector().detect(_ctx(root)), ["makefile", "nx"]
            )


if __name__ == "__main__":
    unittest.main()
