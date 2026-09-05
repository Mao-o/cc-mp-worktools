"""cli._infer_purpose: manifest priority, README banner skipping, sentence
boundary (internal backlog joa.13)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from cli import _infer_purpose, _readme_purpose_line
from core.context import AnalysisConfig, RepoContext
from core.util import truncate_purpose


def _ctx(root: Path) -> RepoContext:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    ctx.tracked_files = []
    return ctx


class ReadmePurposeLineTest(unittest.TestCase):
    def test_html_banner_and_link_lines_are_skipped(self):
        text = (
            "![cover](./x.png)\n\n"
            "<p align=\"center\">\n"
            "  📌 <a href=\"https://x\">Introducing Foo Workflow File Upload</a>\n"
            "</p>\n\n"
            "<p align=\"center\">\n  <a href=\"https://a\">Cloud</a> · <a href=\"https://b\">Docs</a>\n</p>\n\n"
            "# Foo\n\n"
            "Foo is an open-source platform for developing LLM applications.\n"
        )
        self.assertEqual(
            _readme_purpose_line(text),
            "Foo is an open-source platform for developing LLM applications.",
        )

    def test_symbol_led_and_short_lines_are_skipped(self):
        text = "🚀 Launch!\n- bullet item here\n> quote line here\nToo short\nA real description line.\n"
        self.assertEqual(_readme_purpose_line(text), "A real description line.")

    def test_first_prose_line_before_any_heading_wins(self):
        text = "This is a Next.js project bootstrapped with create-next-app.\n\n## Getting Started\n\nOpen http://localhost:3000 to see it.\n"
        self.assertTrue(_readme_purpose_line(text).startswith("This is a Next.js"))


class SentenceBoundaryTest(unittest.TestCase):
    def test_dotted_names_do_not_end_the_sentence(self):
        self.assertEqual(
            truncate_purpose("This is the Node.js SDK for v2.0 of the API. More text."),
            "This is the Node.js SDK for v2.0 of the API.",
        )

    def test_japanese_full_stop_still_ends(self):
        self.assertEqual(
            truncate_purpose("指田製作所の不良案件ワークフローを管理する DX システム。続きの文がここに来る。"),
            "指田製作所の不良案件ワークフローを管理する DX システム。",
        )


class ManifestPriorityTest(unittest.TestCase):
    def test_pyproject_project_description_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname = \"x\"\ndescription = \"A tiny API server\"\n")
            (root / "README.md").write_text("Some readme line that is long enough.\n")
            self.assertEqual(_infer_purpose(_ctx(root)), "A tiny API server")

    def test_cargo_and_pubspec_descriptions_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("[package]\nname = \"x\"\ndescription = \"A Rust CLI\"\n")
            self.assertEqual(_infer_purpose(_ctx(root)), "A Rust CLI")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pubspec.yaml").write_text("name: app\ndescription: A Flutter app.\n")
            self.assertEqual(_infer_purpose(_ctx(root)), "A Flutter app.")

    def test_workspace_manifest_description_does_not_describe_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sdks" / "node").mkdir(parents=True)
            (root / "sdks" / "node" / "package.json").write_text(json.dumps({"description": "The Node SDK"}))
            (root / "README.md").write_text("The whole platform, described here.\n")
            ctx = _ctx(root)
            ctx.tracked_files = ["sdks/node/package.json"]
            self.assertEqual(_infer_purpose(ctx), "The whole platform, described here.")


if __name__ == "__main__":
    unittest.main()
