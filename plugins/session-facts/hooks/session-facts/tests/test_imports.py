"""core.imports: JS/Python import 抽出 + 相対 import 解決のユニットテスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from core.imports import (
    extract_js_specifiers,
    extract_python_specifiers,
    resolve_js_import,
    resolve_python_import,
)


class ExtractJsSpecifiersTest(unittest.TestCase):
    def test_named_import(self):
        text = "import { foo } from './utils/foo'\n"
        self.assertEqual(extract_js_specifiers(text), ["./utils/foo"])

    def test_default_and_side_effect_import(self):
        text = "import Foo from '../foo'\nimport './styles.css'\n"
        self.assertEqual(extract_js_specifiers(text), ["../foo", "./styles.css"])

    def test_require_and_dynamic_import(self):
        text = "const x = require('./bar')\nconst y = import('./baz')\n"
        self.assertEqual(extract_js_specifiers(text), ["./bar", "./baz"])

    def test_bare_package_specifier_captured(self):
        # Extraction doesn't filter by origin; resolution decides what's in-repo.
        text = "import React from 'react'\n"
        self.assertEqual(extract_js_specifiers(text), ["react"])


class ExtractPythonSpecifiersTest(unittest.TestCase):
    def test_from_import(self):
        text = "from app.services import order\n"
        self.assertEqual(extract_python_specifiers(text), ["app.services"])

    def test_relative_from_import(self):
        text = "from ..models import User\n"
        self.assertEqual(extract_python_specifiers(text), ["..models"])

    def test_plain_import_multiple_names(self):
        text = "import os, sys\nimport app.utils\n"
        self.assertEqual(extract_python_specifiers(text), ["os", "sys", "app.utils"])


class ResolveJsImportTest(unittest.TestCase):
    def setUp(self):
        self.tracked = {
            "src/utils/foo.ts",
            "src/components/Button.tsx",
            "src/index.ts",
        }

    def test_resolves_sibling_with_extension_appended(self):
        target = resolve_js_import("./foo", "src/utils", self.tracked)
        self.assertEqual(target, "src/utils/foo.ts")

    def test_resolves_parent_relative(self):
        target = resolve_js_import("../utils/foo", "src/components", self.tracked)
        self.assertEqual(target, "src/utils/foo.ts")

    def test_resolves_index_file(self):
        tracked = {"src/lib/index.ts"}
        target = resolve_js_import("./lib", "src", tracked)
        self.assertEqual(target, "src/lib/index.ts")

    def test_bare_package_unresolved(self):
        self.assertIsNone(resolve_js_import("react", "src/components", self.tracked))

    def test_alias_path_unresolved(self):
        self.assertIsNone(resolve_js_import("@/utils/foo", "src/components", self.tracked))

    def test_unmatched_relative_returns_none(self):
        self.assertIsNone(resolve_js_import("./missing", "src", self.tracked))


class ResolvePythonImportTest(unittest.TestCase):
    def setUp(self):
        self.tracked = {
            "app/services/order.py",
            "app/models/__init__.py",
            "app/models/user.py",
        }

    def test_resolves_absolute_module(self):
        target = resolve_python_import("app.services.order", "app/main.py", self.tracked)
        self.assertEqual(target, "app/services/order.py")

    def test_resolves_package_init(self):
        target = resolve_python_import("app.models", "app/main.py", self.tracked)
        self.assertEqual(target, "app/models/__init__.py")

    def test_resolves_single_dot_relative(self):
        # "from . import models" -> single leading dot means "this package".
        target = resolve_python_import(".models", "app/main.py", self.tracked)
        self.assertEqual(target, "app/models/__init__.py")

    def test_resolves_double_dot_relative(self):
        target = resolve_python_import("..models.user", "app/services/order.py", self.tracked)
        self.assertEqual(target, "app/models/user.py")

    def test_stdlib_or_third_party_unresolved(self):
        self.assertIsNone(resolve_python_import("os.path", "app/main.py", self.tracked))


if __name__ == "__main__":
    unittest.main()
