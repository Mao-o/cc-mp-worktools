"""runtime/venv 検出 (v0.6): core/runtime.py の純関数、runtime_env collector、
mise detector の config 認識、_render_runtime の header 描画。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.runtime_env import RuntimeEnvCollector
from core.context import AnalysisConfig, RepoContext
from core.runtime import (
    build_runtime_info,
    detect_venv,
    first_version,
    parse_mise_tools,
    parse_tool_versions,
    read_python_version,
    read_venv_python,
    runner_prefix,
)
from detectors.mise import MiseDetector
from renderer import render_header


class FirstVersionTest(unittest.TestCase):
    def test_scalar_array_inline_table(self):
        self.assertEqual(first_version('"3.12"'), "3.12")
        self.assertEqual(first_version("['26', '25']"), "26")  # array -> first
        self.assertEqual(first_version('{version = "3.12", virtualenv = ".venv"}'), "3.12")
        self.assertEqual(first_version('{git = "https://x", branch = "main"}'), "")  # no version


class ParseMiseToolsTest(unittest.TestCase):
    def test_scalar_array_inline_and_table_gating(self):
        text = """\
[tools]
node = "20"
python = {version = "3.12", virtualenv = ".venv"}
erlang = ["26", "25"]

[env]
FOO = "bar"
"""
        tools = parse_mise_tools(text)
        self.assertEqual(tools["node"], "20")
        self.assertEqual(tools["python"], "3.12")  # inline-table version field
        self.assertEqual(tools["erlang"], "26")  # array -> first
        self.assertNotIn("FOO", tools)  # [env] assignments are not tools


class ParseToolVersionsTest(unittest.TestCase):
    def test_first_version_and_comments(self):
        tools = parse_tool_versions("python 3.12.0 3.11.9\nnodejs 20.11.0\n# c\n")
        self.assertEqual(tools["python"], "3.12.0")  # only the first version
        self.assertEqual(tools["nodejs"], "20.11.0")


class ReadPythonVersionTest(unittest.TestCase):
    def test_first_non_comment_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".python-version").write_text("# pin\n3.12.3\n3.11.0\n")
            self.assertEqual(read_python_version(root), "3.12.3")

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_python_version(Path(tmp)))


class DetectVenvTest(unittest.TestCase):
    def test_requires_pyvenv_cfg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # a source dir literally named venv/, NOT a virtualenv
            (root / "venv").mkdir()
            (root / "venv" / "main.py").write_text("x = 1\n")
            self.assertIsNone(detect_venv(root))
            # promote it to a real venv
            (root / "venv" / "pyvenv.cfg").write_text("version = 3.12.3\n")
            self.assertEqual(detect_venv(root), "venv")

    def test_prefers_dotvenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv").mkdir()
            (root / ".venv" / "pyvenv.cfg").write_text("version = 3.11.0\n")
            self.assertEqual(detect_venv(root), ".venv")


class ReadVenvPythonTest(unittest.TestCase):
    def _venv(self, root: Path, body: str) -> None:
        (root / ".venv").mkdir()
        (root / ".venv" / "pyvenv.cfg").write_text(body)

    def test_version_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._venv(root, "home = /usr/bin\nversion = 3.12.3\n")
            self.assertEqual(read_venv_python(root, ".venv"), "3.12.3")

    def test_version_info_fallback_trims_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._venv(root, "home = /usr/bin\nversion_info = 3.12.3.final.0\n")
            self.assertEqual(read_venv_python(root, ".venv"), "3.12.3")


class BuildRuntimeInfoTest(unittest.TestCase):
    def test_mise_and_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mise.toml").write_text('[tools]\npython = "3.12"\n')
            (root / ".venv").mkdir()
            (root / ".venv" / "pyvenv.cfg").write_text("version = 3.12.3\n")
            info = build_runtime_info(root)
            self.assertEqual(info["manager"], "mise")
            self.assertEqual(info["tools"], {"python": "3.12"})
            self.assertEqual(info["venv"], ".venv")
            self.assertEqual(info["venv_python"], "3.12.3")

    def test_asdf_label_from_tool_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".tool-versions").write_text("python 3.12.0\n")
            info = build_runtime_info(root)
            self.assertEqual(info["manager"], "asdf")
            self.assertEqual(info["tools"], {"python": "3.12.0"})

    def test_python_version_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".python-version").write_text("3.12.3\n")
            self.assertEqual(build_runtime_info(root), {"python_version": "3.12.3"})

    def test_empty_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build_runtime_info(Path(tmp)), {})


class RunnerPrefixTest(unittest.TestCase):
    def test_venv_wins_over_mise(self):
        info = {"venv": ".venv", "manager": "mise", "tools": {"python": "3.12"}}
        self.assertEqual(runner_prefix(info), ".venv/bin/")

    def test_mise_python(self):
        self.assertEqual(
            runner_prefix({"manager": "mise", "tools": {"python": "3.12"}}),
            "mise exec -- ",
        )

    def test_mise_without_python_is_none(self):
        self.assertIsNone(runner_prefix({"manager": "mise", "tools": {"node": "20"}}))

    def test_empty_is_none(self):
        self.assertIsNone(runner_prefix({}))


class RenderRuntimeTest(unittest.TestCase):
    def _render(self, runtime) -> str:
        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        ctx.results["runtime"] = runtime
        return render_header(ctx)

    def test_mise_and_venv(self):
        out = self._render({
            "manager": "mise", "tools": {"python": "3.12"},
            "venv": ".venv", "venv_python": "3.12.3",
        })
        self.assertIn(
            "- runtime: mise (python 3.12); venv .venv present (python 3.12.3); "
            "run tools via .venv/bin/",
            out,
        )

    def test_venv_only(self):
        out = self._render({"venv": ".venv", "venv_python": "3.11.0"})
        self.assertIn(
            "- runtime: venv .venv present (python 3.11.0); run tools via .venv/bin/",
            out,
        )

    def test_mise_python_hint(self):
        out = self._render({"manager": "mise", "tools": {"python": "3.12"}})
        self.assertIn("- runtime: mise (python 3.12); run tools via mise exec --", out)

    def test_pyenv_only_no_hint(self):
        out = self._render({"python_version": "3.12.3"})
        self.assertIn("- runtime: python 3.12.3 (.python-version)", out)
        self.assertNotIn("run tools via", out)

    def test_no_runtime_no_line(self):
        ctx = RepoContext(root=Path("/repo"), config=AnalysisConfig())
        self.assertNotIn("- runtime:", render_header(ctx))


class RuntimeEnvCollectorTest(unittest.TestCase):
    def test_writes_runtime_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mise.toml").write_text('[tools]\npython = "3.12"\n')
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertIsNone(RuntimeEnvCollector().collect(ctx))  # header-only
            info = ctx.results.get("runtime")
            self.assertEqual(info["manager"], "mise")
            self.assertEqual(info["tools"]["python"], "3.12")

    def test_writes_nothing_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            RuntimeEnvCollector().collect(ctx)
            self.assertNotIn("runtime", ctx.results)


class MiseDetectorConfigTest(unittest.TestCase):
    def test_fires_on_dotless_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mise.toml").write_text('[tools]\nnode = "20"\n')
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertEqual(MiseDetector().detect(ctx), ["mise"])

    def test_fires_on_config_mise_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / ".config" / "mise"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text('[tools]\npython = "3.12"\n')
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertEqual(MiseDetector().detect(ctx), ["mise"])

    def test_silent_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            self.assertEqual(MiseDetector().detect(ctx), [])


if __name__ == "__main__":
    unittest.main()
