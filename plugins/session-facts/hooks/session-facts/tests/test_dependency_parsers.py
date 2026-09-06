"""joa.15: go.mod forms, Gemfile, composer.json."""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.dependencies import parse_composer_require, parse_gemfile, parse_go_mod


class GoModTest(unittest.TestCase):
    def test_single_line_block_and_major_suffix(self):
        text = (
            "module example.com/app\n\ngo 1.22\n\n"
            "require github.com/gin-gonic/gin v1.9.1\n\n"
            "require (\n"
            "\tgithub.com/labstack/echo/v4 v4.11.0\n"
            "\tgolang.org/x/sys v0.15.0 // indirect\n"
            ")\n"
        )
        self.assertEqual(parse_go_mod(text), [("gin", "v1.9.1"), ("echo", "v4.11.0")])

    def test_lines_outside_require_are_ignored(self):
        self.assertEqual(parse_go_mod("module x\ngo 1.22\nreplace a => b\n"), [])


class GemfileTest(unittest.TestCase):
    def test_gem_lines_with_and_without_version(self):
        text = "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\ngem \"rspec\"\n  gem 'pg', '>= 1.1'\n"
        self.assertEqual(parse_gemfile(text), [("rails", "~> 7.1"), ("rspec", ""), ("pg", ">= 1.1")])


class ComposerTest(unittest.TestCase):
    def test_require_and_require_dev(self):
        text = '{"require": {"php": "^8.2", "laravel/framework": "^11.0", "ext-json": "*"}, "require-dev": {"phpunit/phpunit": "^10"}}'
        self.assertEqual(parse_composer_require(text), [("laravel/framework", "^11.0"), ("phpunit/phpunit", "^10")])

    def test_invalid_json_is_empty(self):
        self.assertEqual(parse_composer_require("not json"), [])


if __name__ == "__main__":
    unittest.main()
