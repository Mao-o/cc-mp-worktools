"""metrics.py: テキスト → 数値メトリクスのテスト。"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path

import _testutil  # noqa: F401

import metrics


@dataclass
class _FakeLoaded:
    text: str
    lines: list[str]


def _loaded(text: str) -> _FakeLoaded:
    return _FakeLoaded(text=text, lines=text.splitlines())


class TestComputeEmptyFile(unittest.TestCase):
    def test_empty_file_no_zero_division(self):
        result = metrics.compute(_loaded(""), "python", Path("/repo/empty.py"))
        self.assertEqual(result.line_count, 0)
        self.assertEqual(result.def_count, 0)
        self.assertEqual(result.import_category_count, 0)
        self.assertEqual(result.import_categories, ())
        self.assertEqual(result.control_flow_density, 0.0)


class TestCountDefsPython(unittest.TestCase):
    def test_ast_counts_top_level_defs(self):
        text = "def a():\n    pass\n\ndef b():\n    pass\n\nclass C:\n    pass\n"
        self.assertEqual(metrics.count_defs_python(text), 3)

    def test_ast_counts_nested_methods(self):
        text = (
            "class Outer:\n"
            "    def method_a(self):\n"
            "        def inner():\n"
            "            pass\n"
            "        return inner\n"
            "\n"
            "    async def method_b(self):\n"
            "        pass\n"
        )
        # Outer(class) + method_a + inner + method_b = 4
        self.assertEqual(metrics.count_defs_python(text), 4)

    def test_syntax_error_returns_none(self):
        self.assertIsNone(metrics.count_defs_python("def a(:\n  pass"))

    def test_compute_falls_back_to_regex_on_syntax_error(self):
        text = "def a(:\n  pass\ndef b():\n  pass\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/broken.py"))
        # regex フォールバック: 行頭 "def " にマッチする行数
        self.assertEqual(result.def_count, 2)


class TestCountDefsGeneric(unittest.TestCase):
    def test_generic_keywords_counted(self):
        text = (
            "function foo() {}\n"
            "class Bar {}\n"
            "interface Baz {}\n"
            "struct Qux {}\n"
            "enum Quux {}\n"
            "func corge() {}\n"
        )
        result = metrics.compute(_loaded(text), "go", Path("/repo/foo.go"))
        self.assertEqual(result.def_count, 6)


class TestImportCategories(unittest.TestCase):
    def test_multiple_categories_detected(self):
        text = "\n".join(
            [
                "import requests",
                "import logging",
                "from django.contrib.auth import authenticate",
                "import react",
                "x = 1",
            ]
        )
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertEqual(result.import_category_count, 4)
        self.assertEqual(
            set(result.import_categories), {"network", "ui", "logging", "auth"}
        )

    def test_no_import_lines_zero_categories(self):
        text = "x = 1\ny = 2\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertEqual(result.import_category_count, 0)

    def test_login_does_not_false_positive_logging(self):
        text = "from django.contrib.auth import login\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertNotIn("logging", result.import_categories)
        self.assertIn("auth", result.import_categories)


class TestControlFlowDensity(unittest.TestCase):
    def test_known_ratio(self):
        text = "\n".join(
            [
                "if x:",
                "    pass",
                "for y in z:",
                "    pass",
                "a = 1",
                "b = 2",
            ]
        )
        # 6 non-empty lines, 2 with control-flow keywords (if/for)
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertAlmostEqual(result.control_flow_density, 2 / 6)

    def test_blank_lines_excluded_from_denominator(self):
        text = "if x:\n\n\n    pass\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        # non-empty lines: "if x:" と "    pass" の 2 行、うち 1 行が control-flow
        self.assertAlmostEqual(result.control_flow_density, 1 / 2)


if __name__ == "__main__":
    unittest.main()
