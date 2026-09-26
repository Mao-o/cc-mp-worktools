"""Fixture-based tests for scripts/parse-llms-txt.py (profile-driven loader).

Each fixture reproduces one page shape measured on a real site
(docs/generic-llms-txt-source.md): frontmatter pages with a ``title:``
(Next.js) or only a ``url:`` (Vite / Vitest), ``Source: <url>`` delimited
pages (Drizzle), and H1 pages without URLs (Zod). No network access: every
CLI test passes ``--file``.
"""

import json
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _profile(**raw) -> dict:
    return generic._validate_profile("sources.json", "site", {"url": "https://example.com/llms-full.txt", **raw})


FRONTMATTER_TITLE = """\
# Site Documentation

> preamble before the first page

---
title: Getting Started
description: First steps.
url: "https://example.com/docs/start"
---

# Getting Started

Intro.

---

Note: a horizontal rule followed by prose is not a page boundary.

```yaml
---
title: Example frontmatter inside a code sample
---
```

---
title: Install
url: "https://example.com/docs/install"
---

# Install

## Requirements
"""

FRONTMATTER_URL_ONLY = """\
---
url: /guide.md
---
# Guide

## Overview

---

Prose after a rule.

---
url: /guide/why.md
---
# Why
"""

LINE_SOURCE = """\
# Site

> intro

Source: https://example.com/docs/a

# Page A

Source: see the table below for details.

```ts
const x = 1;
```

Source: https://example.com/docs/b

import Thing from '@mdx/Thing';

no heading here
"""

H1_NO_URL = """\
# Site

intro

# Schemas

## Strings

```bash
# a shell comment is not a page
npm install site
```

# Errors

## Formatting
"""


class SplitTest(unittest.TestCase):
    def test_frontmatter_with_title(self):
        docs = generic.split_documents(_lines(FRONTMATTER_TITLE), _profile(split="frontmatter", page_url="frontmatter:url"))
        self.assertEqual([d["title"] for d in docs], ["Getting Started", "Install"])
        self.assertEqual(docs[0]["url"], "https://example.com/docs/start")
        self.assertEqual(docs[0]["description"], "First steps.")
        # the rule + prose and the fenced example stay inside the first page
        self.assertIn("Note: a horizontal rule followed by prose is not a page boundary.\n", docs[0]["body_lines"])

    def test_frontmatter_with_url_only_and_relative_urls(self):
        profile = _profile(split="frontmatter", frontmatter_key="url", page_url="frontmatter:url",
                           url_base="https://example.com")
        docs = generic.split_documents(_lines(FRONTMATTER_URL_ONLY), profile)
        self.assertEqual([d["title"] for d in docs], ["Guide", "Why"])  # title from the first heading
        self.assertEqual([d["url"] for d in docs], ["https://example.com/guide.md", "https://example.com/guide/why.md"])

    def test_line_prefix_pages(self):
        docs = generic.split_documents(_lines(LINE_SOURCE), _profile(split="line", line_prefix="Source: "))
        self.assertEqual([d["url"] for d in docs], ["https://example.com/docs/a", "https://example.com/docs/b"])
        # a prefix line without a URL is prose, not a boundary
        self.assertIn("Source: see the table below for details.\n", docs[0]["body_lines"])
        self.assertEqual(docs[0]["title"], "Page A")
        self.assertEqual(docs[1]["title"], "b")  # no heading: last URL segment

    def test_h1_pages_without_urls(self):
        docs = generic.split_documents(_lines(H1_NO_URL), _profile(split="h1"))
        self.assertEqual([d["title"] for d in docs], ["Site", "Schemas", "Errors"])
        self.assertEqual({d["url"] for d in docs}, {""})


class ProfileValidationTest(unittest.TestCase):
    def load(self, data) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sources.json")
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
            code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", str(path)])
            return code, err

    def test_rejects_bad_profiles(self):
        ok = {"url": "https://example.com/llms-full.txt", "split": "h1"}
        cases = {
            "path-like name": {"sources": {"../x": ok}},
            "unknown key": {"sources": {"x": {**ok, "typo": 1}}},
            "bad split": {"sources": {"x": {**ok, "split": "h2"}}},
            "non-http url": {"sources": {"x": {**ok, "url": "file:///etc/passwd"}}},
            "line without prefix": {"sources": {"x": {**ok, "split": "line"}}},
            "frontmatter_key on h1": {"sources": {"x": {**ok, "frontmatter_key": "url"}}},
            "bad page_url": {"sources": {"x": {**ok, "page_url": "header:url"}}},
            "unknown top level": {"sources": {"x": ok}, "extra": 1},
            "not json": "{",
        }
        for label, data in cases.items():
            with self.subTest(label):
                code, err = self.load(data)
                self.assertEqual(code, 1)
                self.assertIn("README", err)

    def test_missing_file_points_to_the_readme(self):
        code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", "/nonexistent/sources.json"])
        self.assertEqual(code, 1)
        self.assertIn("README", err)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.sources = tmp / "sources.json"
        self.sources.write_text(json.dumps({"sources": {
            "fm": {"url": "https://example.com/llms-full.txt", "split": "frontmatter", "page_url": "frontmatter:url"},
            "plain": {"url": "https://example.com/llms-full.txt", "split": "h1"},
        }}), encoding="utf-8")
        self.fm_file = tmp / "fm.txt"
        self.fm_file.write_text(FRONTMATTER_TITLE, encoding="utf-8")
        self.h1_file = tmp / "h1.txt"
        self.h1_file.write_text(H1_NO_URL, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cmd(self, *argv) -> tuple[int, str, str]:
        return _loader.run_cli(generic, ["parse-llms-txt.py", *argv, "--sources-file", str(self.sources)])

    def test_hints_keep_source_and_corpus(self):
        code, out, _ = self.run_cmd("sections", "install", "--source", "fm", "--file", str(self.fm_file))
        self.assertEqual(code, 0)
        hint = out.strip().splitlines()[-1]
        self.assertIn("--source fm", hint)
        self.assertIn("--sources-file", hint)
        self.assertIn("--file", hint)
        self.assertIn("URL: https://example.com/docs/install", out)

    def test_pages_without_url_print_no_url_lines(self):
        code, out, _ = self.run_cmd("content", "schemas", "--source", "plain", "--file", str(self.h1_file))
        self.assertEqual(code, 0)
        self.assertNotIn("# source:", out)
        self.assertNotIn("URL:", out)
        self.assertIn("## Strings", out)

    def test_search_finds_body_terms(self):
        code, out, _ = self.run_cmd("search", "requirements", "--source", "fm", "--file", str(self.fm_file))
        self.assertEqual(code, 0)
        self.assertIn("[1] Install", out)

    def test_unknown_source_lists_configured_ones(self):
        code, _out, err = self.run_cmd("fetch-index", "--source", "nope", "--file", str(self.fm_file))
        self.assertEqual(code, 1)
        self.assertIn("fm, plain", err)


if __name__ == "__main__":
    unittest.main()
