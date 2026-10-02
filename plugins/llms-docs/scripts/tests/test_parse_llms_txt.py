"""Fixture-based tests for scripts/parse-llms-txt.py (profile-driven loader).

Each fixture reproduces one page shape measured on a real site
(docs/generic-llms-txt-source.md): frontmatter pages with a ``title:``
(Next.js) or only a ``url:`` (Vite / Vitest), ``Source: <url>`` delimited
pages (Drizzle), H1 pages without URLs (Zod), H1 pages behind an empty banner
heading (Hono) and frontmatter pages whose URL is a Markdown link amid
boilerplate lines (Cloudflare per-product files). No network access: every
CLI test passes ``--file``.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

LINE_FENCED = """\
Source: https://example.com/docs/a

# Page A

```ts
const x = 1;
```ts
Source: https://example.com/not-a-page
```

```md
    Source: https://example.com/indented-closer-case
    ```

Source: https://example.com/docs/b

# Page B
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

H1_BANNER = """\
<SYSTEM>This is the full developer documentation for Site.</SYSTEM>

# Start of Site documentation

# Site

Intro.

```ts
# not a page
```

# Routing

## Basics
"""

CLOUDFLARE_LIKE = """\
---
title: Product
image: https://example.com/product/og.png
---

[Skip to content](#main-content)

> Documentation Index
> Fetch the complete documentation index at: https://example.com/product/llms.txt
> Use this file to discover all available pages before exploring further.

# Product

Last updated Apr 30, 2026|Copy as Markdown| [View as Markdown](https://example.com/product/index.md)| [Agent setup](https://example.com/agent-setup/)

Product intro.

```md
> Documentation Index
```

---
title: Getting started
---

[Skip to content](#main-content)

# Getting started

Last updated Aug 25, 2026|Copy as Markdown| [View as Markdown](https://example.com/product/get-started/index.md)| [Agent setup](https://example.com/agent-setup/)

Steps.
"""

CLOUDFLARE_DROP = [
    r"^\[Skip to content\]\(#main-content\)$",
    r"^> (Documentation Index|Fetch the complete documentation index at:|Use this file to discover all available pages)",
    r"^Last updated .*\|Copy as Markdown\|",
]


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

    def test_line_split_skips_fenced_examples(self):
        # a fenced example of the delimiter is not a page; ```ts inside a fence
        # does not close it, and an indented ``` does
        docs = generic.split_documents(_lines(LINE_FENCED), _profile(split="line", line_prefix="Source: "))
        self.assertEqual([d["url"] for d in docs], ["https://example.com/docs/a", "https://example.com/docs/b"])

    def test_unclosed_fence_does_not_swallow_later_pages(self):
        text = (
            "Source: https://example.com/docs/a\n\n# A\n\n```ts\nconst broken = 1;\n\n"
            "Source: https://example.com/docs/b\n\n# B\n\nSource: https://example.com/docs/c\n\n# C\n"
        )
        docs = generic.split_documents(_lines(text), _profile(split="line", line_prefix="Source: "))
        self.assertEqual([d["title"] for d in docs], ["A", "B", "C"])

    def test_line_title_skips_fenced_headings(self):
        # ```ts inside a fence does not close it, so "# Example" stays code
        text = (
            "Source: https://example.com/docs/a\n\n```md\n```ts\n# Example\n```\n\n# Real title\n"
        )
        docs = generic.split_documents(_lines(text), _profile(split="line", line_prefix="Source: "))
        self.assertEqual(docs[0]["title"], "Real title")

    def test_line_split_honours_page_url(self):
        # the delimiter still splits pages, but "none" publishes no URL
        docs = generic.split_documents(_lines(LINE_SOURCE), _profile(split="line", line_prefix="Source: ", page_url="none"))
        self.assertEqual(len(docs), 2)
        self.assertEqual({d["url"] for d in docs}, {""})

    def test_h1_pages_without_urls(self):
        docs = generic.split_documents(_lines(H1_NO_URL), _profile(split="h1"))
        self.assertEqual([d["title"] for d in docs], ["Site", "Schemas", "Errors"])
        self.assertEqual({d["url"] for d in docs}, {""})

    def test_skip_empty_drops_banner_headings(self):
        docs = generic.split_documents(_lines(H1_BANNER), _profile(split="h1", skip_empty=True))
        self.assertEqual([d["title"] for d in docs], ["Site", "Routing"])
        # without skip_empty the banner is kept as an (empty) page
        kept = generic.split_documents(_lines(H1_BANNER), _profile(split="h1"))
        self.assertEqual(kept[0]["title"], "Start of Site documentation")

    def test_link_page_url_and_drop_lines(self):
        profile = _profile(split="frontmatter", page_url="link:View as Markdown", drop_lines=CLOUDFLARE_DROP)
        docs = generic.split_documents(_lines(CLOUDFLARE_LIKE), profile)
        self.assertEqual([d["title"] for d in docs], ["Product", "Getting started"])
        # the URL is read from a line that drop_lines then removes
        self.assertEqual([d["url"] for d in docs],
                         ["https://example.com/product/index.md", "https://example.com/product/get-started/index.md"])
        body = "".join(docs[0]["body_lines"])
        self.assertNotIn("Skip to content", body)
        self.assertNotIn("Fetch the complete documentation index", body)
        self.assertNotIn("Copy as Markdown", body)
        self.assertIn("Product intro.", body)
        # a matching line inside a code fence is content, not boilerplate
        self.assertIn("> Documentation Index\n", docs[0]["body_lines"])

    def test_link_page_url_ignores_links_with_spaces_and_other_text(self):
        body = _lines("[Edit](https://example.com/e)\n[View as Markdown](not a url)\n[View as Markdown](https://example.com/p.md)\n")
        self.assertEqual(generic._link_url(body, "View as Markdown"), "https://example.com/p.md")
        self.assertEqual(generic._link_url(body, "Missing"), "")


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
            "non-string description": {"sources": {"x": {**ok, "description": 3}}},
            "frontmatter page_url on h1": {"sources": {"x": {**ok, "page_url": "frontmatter:url"}}},
            "unknown top level": {"sources": {"x": ok}, "extra": 1},
            "empty link page_url": {"sources": {"x": {**ok, "page_url": "link: "}}},
            "drop_lines not a list": {"sources": {"x": {**ok, "drop_lines": "^x"}}},
            "drop_lines bad regex": {"sources": {"x": {**ok, "drop_lines": ["("]}}},
            "skip_empty not bool": {"sources": {"x": {**ok, "skip_empty": "yes"}}},
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


class PresetsTest(unittest.TestCase):
    """Bundled presets: always available, overridable, and well-formed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name)
        # no $LLMS_DOCS_SOURCES_FILE, and an empty config dir: presets only
        env = {k: v for k, v in os.environ.items() if k != generic.SOURCES_ENV}
        env["XDG_CONFIG_HOME"] = str(self.config)
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_presets_work_without_a_sources_file(self):
        code, out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources"])
        self.assertEqual(code, 0, err)
        self.assertIn("not present, presets only", out)
        self.assertRegex(out, r"(?m)^nextjs  \[preset, frontmatter\]  https://nextjs\.org/docs/llms-full\.txt$")

    def test_user_profile_replaces_the_preset_of_the_same_name(self):
        user = self.config / "llms-docs" / "sources.json"
        user.parent.mkdir(parents=True)
        user.write_text(json.dumps({"sources": {"zod": {"url": "https://mirror.example/llms-full.txt", "split": "h1"}}}),
                        encoding="utf-8")
        code, out, _ = _loader.run_cli(generic, ["parse-llms-txt.py", "sources"])
        self.assertEqual(code, 0)
        self.assertIn("zod  [user, h1]  https://mirror.example/llms-full.txt", out)
        self.assertIn("nextjs  [preset,", out)

    def test_explicit_missing_sources_file_is_an_error(self):
        code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", "/nonexistent/s.json"])
        self.assertEqual(code, 1)
        self.assertIn("/nonexistent/s.json", err)
        with mock.patch.dict(os.environ, {generic.SOURCES_ENV: "/nonexistent/env.json"}):
            code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources"])
        self.assertEqual(code, 1)
        self.assertIn("/nonexistent/env.json", err)

    def test_explicit_file_at_the_default_path_must_exist(self):
        default = str(self.config / "llms-docs" / "sources.json")  # absent
        code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", default])
        self.assertEqual(code, 1)
        self.assertIn(default, err)

    def test_hint_keeps_a_sources_file_given_by_env(self):
        # the env var names the file; the hint must carry it as --sources-file,
        # since a later shell without the env var would resolve "zod" to the preset
        user = self.config / "user.json"
        user.write_text(json.dumps({"sources": {"zod": {"url": "https://mirror.example/llms-full.txt", "split": "h1"}}}),
                        encoding="utf-8")
        corpus = self.config / "zod.txt"
        corpus.write_text(H1_NO_URL, encoding="utf-8")
        with mock.patch.dict(os.environ, {generic.SOURCES_ENV: str(user)}):
            code, out, err = _loader.run_cli(
                generic, ["parse-llms-txt.py", "sections", "schemas", "--source", "zod", "--file", str(corpus)])
        self.assertEqual(code, 0, err)
        hint = out.strip().splitlines()[-1]
        self.assertIn(f"--sources-file {user}", hint)
        # without an explicit file the hint stays short
        code, out, _ = _loader.run_cli(
            generic, ["parse-llms-txt.py", "sections", "schemas", "--source", "zod", "--file", str(corpus)])
        self.assertEqual(code, 0)
        self.assertNotIn("--sources-file", out.strip().splitlines()[-1])

    def test_every_preset_is_valid_and_https(self):
        with open(generic.PRESETS_FILE, encoding="utf-8") as f:
            raw = json.load(f)["sources"]
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        self.assertEqual(set(presets), set(raw))
        for name, p in presets.items():
            with self.subTest(name):
                self.assertTrue(p["url"].startswith("https://"), p["url"])
                self.assertTrue(p["url"].endswith("/llms-full.txt"), p["url"])
                self.assertTrue(p["description"])

    def test_skill_source_table_matches_the_presets(self):
        # researching-library-docs lists the presets by hand (description and
        # the Step 0 table); a preset added or renamed without the skill would
        # never be picked by it
        skill = Path(generic.PRESETS_FILE).parents[1] / "skills" / "researching-library-docs" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        table = text.split("## Step 0", 1)[1].split("\n## ", 1)[0]
        named = set()
        for row in re.findall(r"(?m)^\| [^|]+ \| (.+) \|$", table):
            if row.startswith("`--source`"):
                continue  # header
            m = re.match(r"`cloudflare-<製品>`: (.+)", row)
            if m:
                named |= {f"cloudflare-{slug}" for slug in re.findall(r"`([a-z0-9-]+)`", m.group(1))}
            else:
                named |= set(re.findall(r"`([a-z0-9-]+)`", row))
        presets = set(generic._read_sources_file(generic.PRESETS_FILE))
        self.assertEqual(presets - named, set(), "presets missing from the skill's Step 0 table")
        self.assertEqual(named - presets, set(), "skill names a source that is not a preset")

    def test_preset_shapes_split_their_fixtures(self):
        # one fixture per shape, read through the shipped preset
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        cases = {
            "nextjs": (FRONTMATTER_TITLE, ["Getting Started", "Install"]),
            "drizzle": (LINE_SOURCE, ["Page A", "b"]),
            "zod": (H1_NO_URL, ["Site", "Schemas", "Errors"]),
            "hono": (H1_BANNER, ["Site", "Routing"]),
            "cloudflare-d1": (CLOUDFLARE_LIKE, ["Product", "Getting started"]),
        }
        for name, (text, titles) in cases.items():
            with self.subTest(name):
                docs = generic.split_documents(_lines(text), presets[name])
                self.assertEqual([d["title"] for d in docs], titles)
        cf = generic.split_documents(_lines(CLOUDFLARE_LIKE), presets["cloudflare-workers"])
        self.assertEqual(cf[0]["url"], "https://example.com/product/index.md")
        self.assertFalse(any(re.match(r"^Last updated", line) for line in cf[0]["body_lines"]))


class CacheIdentityTest(unittest.TestCase):
    def test_cache_file_depends_on_the_url(self):
        a = generic._validate_profile("s", "site", {"url": "https://a.example/llms-full.txt", "split": "h1"})
        b = generic._validate_profile("s", "site", {"url": "https://b.example/llms-full.txt", "split": "h1"})
        self.assertNotEqual(generic._cache_path("/c", a), generic._cache_path("/c", b))
        self.assertTrue(generic._cache_path("/c", a).startswith("/c/generic-site-"))


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
        configured = err.split("Configured:", 1)[1]
        self.assertIn(" fm,", configured)
        self.assertIn(" plain,", configured)
        self.assertIn(" nextjs,", configured)  # bundled presets are listed too


if __name__ == "__main__":
    unittest.main()
