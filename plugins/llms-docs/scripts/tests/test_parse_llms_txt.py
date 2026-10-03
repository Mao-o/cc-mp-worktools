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
import _common  # noqa: E402

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

MINTLIFY = """\
# Bytecode Caching
Source: https://example.com/docs/bytecode

Intro.

# ESM (an H1 inside the page body, not a page)

```bash
# a shell comment
```

## Usage

# CSS
Source: https://example.com/docs/css

Styles.
"""

VERCEL_LIKE = """\
# Site documentation

Preamble.

DELIM
title: "Access tokens"
description: "Create and scope tokens"
source: "https://example.com/docs/access-tokens"
DELIM

Body A.

DELIM
title: "Forbidden properties"
description: "Learn how to disallow reading from,
writing to, and/or calling one or more properties"
source: "https://example.com/docs/forbidden"
DELIM

**Important:** a bold line that is not a YAML key.

DELIM

Prose between two long rules is not a page.

DELIM
""".replace("DELIM", "-" * 80)

CODEX_LIKE = """\
# Site — full documentation

> preamble

# Administration

---

# Agent approvals

Body.

---

# Configuration

---

# Config basics

Body.
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

    def test_h1_after_a_code_block_in_an_mdx_comment(self):
        # "``` */}" closes the block opened inside the comment
        text = (
            "# Unions\n\n{/* For convenience:\n\n  ```ts\n"
            "  const either = z.string().or(z.number());\n  ``` */}\n\n# Mini\n\nbody\n"
        )
        docs = generic.split_documents(_lines(text), _profile(split="h1"))
        self.assertEqual([d["title"] for d in docs], ["Unions", "Mini"])

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

    def test_h1_needs_url_folds_body_h1_into_the_page(self):
        profile = _profile(split="h1", page_url="line:Source: ", h1_needs_url=True)
        docs = generic.split_documents(_lines(MINTLIFY), profile)
        self.assertEqual([d["title"] for d in docs], ["Bytecode Caching", "CSS"])
        self.assertEqual([d["url"] for d in docs], ["https://example.com/docs/bytecode", "https://example.com/docs/css"])
        body = "".join(docs[0]["body_lines"])
        self.assertIn("# ESM (an H1 inside the page body, not a page)", body)
        self.assertIn("## Usage", body)
        # the folded H1 is a section of its own, with its subsections under it
        self.assertEqual(docs[0]["min_level"], 1)
        paths = [sec["heading_path"] for sec in _common.extract_sections(docs[0]["body_lines"], min_level=1)]
        self.assertIn("ESM (an H1 inside the page body, not a page)/Usage", paths)
        # without h1_needs_url the body H1 is a (URL-less) page of its own
        plain = generic.split_documents(_lines(MINTLIFY), _profile(split="h1", page_url="line:Source: "))
        self.assertEqual(len(plain), 3)

    def test_frontmatter_delimiter_and_multiline_quoted_value(self):
        profile = _profile(split="frontmatter", frontmatter_delimiter="-" * 80, page_url="frontmatter:source")
        docs = generic.split_documents(_lines(VERCEL_LIKE), profile)
        self.assertEqual([d["title"] for d in docs], ["Access tokens", "Forbidden properties"])
        self.assertEqual(docs[1]["url"], "https://example.com/docs/forbidden")
        self.assertEqual(docs[1]["description"],
                         "Learn how to disallow reading from, writing to, and/or calling one or more properties")
        # the long rules around plain prose stay inside the second page
        self.assertIn("Prose between two long rules is not a page.\n", docs[1]["body_lines"])

    def test_unclosed_quote_does_not_drop_the_page(self):
        text = (
            "---\ntitle: A\ndescription: \"never closed\n---\n\nBody A.\n\n"
            "---\ntitle: B\n---\n\nBody B.\n"
        )
        docs = generic.split_documents(_lines(text), _profile(split="frontmatter"))
        self.assertEqual([d["title"] for d in docs], ["A", "B"])
        self.assertIn("Body A.\n", docs[0]["body_lines"])

    def test_unquoted_value_continued_on_an_unindented_line(self):
        # Cloudflare (Browser Rendering) runs a description on to the next line
        text = (
            "---\ntitle: Quick Actions timeouts\ndescription: Browser Rendering uses several timers\n"
            "If any of these timers exceed their limit, the request fails.\nimage: https://example.com/a.png\n---\n\n"
            "# Quick Actions timeouts\n\nBody.\n"
        )
        docs = generic.split_documents(_lines(text), _profile(split="frontmatter"))
        self.assertEqual([d["title"] for d in docs], ["Quick Actions timeouts"])
        self.assertEqual(docs[0]["description"],
                         "Browser Rendering uses several timers If any of these timers exceed their limit, the request fails.")

    def test_markdown_after_a_rule_is_still_not_frontmatter(self):
        # a rule, a key-shaped line, then Markdown: the continuation rule must
        # not swallow a heading or a list into a "frontmatter" block
        # ("- item" is not here: a YAML list line has always been accepted, for "tags:")
        for tail in ("# Heading\n", "> quote\n", "[link](https://example.com)\n", "| a | b |\n"):
            with self.subTest(tail=tail):
                text = "---\ntitle: Page\n---\n\nBody.\n\n---\ntitle: looks like a key\n" + tail + "---\n"
                docs = generic.split_documents(_lines(text), _profile(split="frontmatter"))
                self.assertEqual([d["title"] for d in docs], ["Page"])

    def test_closes_double_quote_respects_escapes(self):
        self.assertTrue(generic._closes_double_quote('end"'))
        self.assertFalse(generic._closes_double_quote('end\\"'))
        self.assertTrue(generic._closes_double_quote('end\\\\"'))
        self.assertFalse(generic._closes_double_quote("no quote"))

    def test_skip_empty_treats_a_rule_only_body_as_empty(self):
        docs = generic.split_documents(_lines(CODEX_LIKE), _profile(split="h1", skip_empty=True))
        self.assertEqual([d["title"] for d in docs],
                         ["Site — full documentation", "Agent approvals", "Config basics"])


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
            "non-http index_url": {"sources": {"x": {**ok, "index_url": "llms.txt"}}},
            "h1_needs_url without page_url": {"sources": {"x": {**ok, "h1_needs_url": True}}},
            "h1_needs_url not bool": {"sources": {"x": {**ok, "page_url": "line:Source: ", "h1_needs_url": 1}}},
            "h1_needs_url on frontmatter": {"sources": {"x": {**ok, "split": "frontmatter", "page_url": "link:x",
                                                              "h1_needs_url": True}}},
            "frontmatter_delimiter on h1": {"sources": {"x": {**ok, "frontmatter_delimiter": "---"}}},
            "bad frontmatter_delimiter": {"sources": {"x": {**ok, "split": "frontmatter", "frontmatter_delimiter": "=="}}},
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
        # agent-plugins.org has no llms-full.txt (404): its llms.txt is the
        # whole text (frontmatter per page, no links), so it is the one preset
        # that points at an llms.txt
        whole_text_llms_txt = {"agent-plugins"}
        for name, p in presets.items():
            with self.subTest(name):
                self.assertTrue(p["url"].startswith("https://"), p["url"])
                tail = "/llms.txt" if name in whole_text_llms_txt else "/llms-full.txt"
                self.assertTrue(p["url"].endswith(tail), p["url"])
                if name in whole_text_llms_txt:
                    # the url already is the llms.txt; an index_url would only
                    # point at the same file again
                    self.assertIsNone(p["index_url"])
                self.assertTrue(p["description"])

    def test_a_preset_index_url_is_the_llms_txt_next_to_its_llms_full_txt(self):
        # index_url joins page URLs by title, so it must be the llms.txt of the
        # same docs set as url: not a site root (a 2-level index), not another
        # host. A site that moves is easy to half-update, and an old host that
        # redirects hides it.
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        with_index = {name: p for name, p in presets.items() if p["index_url"]}
        self.assertTrue(with_index)
        for name, p in with_index.items():
            with self.subTest(name):
                self.assertTrue(p["index_url"].endswith("/llms.txt"), p["index_url"])
                self.assertEqual(p["index_url"].rsplit("/", 1)[0], p["url"].rsplit("/", 1)[0])

    def test_browser_rendering_preset_follows_the_product_to_browser_run(self):
        # The product was renamed and /browser-rendering/llms.txt redirects to
        # /browser-run/. The old llms-full.txt is still served, but it is an
        # older snapshot (47 pages, no URL inside the pages), so the preset
        # (whose name users already use) must read the new location. The new
        # file carries a URL in every page that has one, so it needs no
        # index_url (an old one left behind would be a 301 to keep checking).
        p = generic._read_sources_file(generic.PRESETS_FILE)["cloudflare-browser-rendering"]
        self.assertEqual(p["url"], "https://developers.cloudflare.com/browser-run/llms-full.txt")
        self.assertIsNone(p["index_url"])
        # the current template carries each page's own URL
        docs = generic.split_documents(_lines(CLOUDFLARE_LIKE), p)
        self.assertEqual(docs[0]["url"], "https://example.com/product/index.md")

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

    def test_readme_host_list_matches_the_presets(self):
        # The README lists every host the presets fetch from (one per line in
        # a text block); the root README's Privacy table points at it, so a
        # preset added or moved without the list misleads whoever decides the
        # network egress.
        from urllib.parse import urlsplit
        readme = Path(generic.PRESETS_FILE).parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        m = re.search(r"\*\*取得先のホスト\*\*(?:(?!```).)*```text\n(.*?)```", text, re.S)
        self.assertIsNotNone(m, "README lost the machine-readable host list")
        listed = [line.strip() for line in m.group(1).splitlines() if line.strip()]
        self.assertEqual(len(listed), len(set(listed)), "a host is listed twice")
        with open(generic.PRESETS_FILE, encoding="utf-8") as f:
            raw = json.load(f)["sources"]
        fetched = set()
        for p in raw.values():
            for key in ("url", "index_url"):
                if p.get(key):
                    fetched.add(urlsplit(p[key]).hostname)
        self.assertTrue(fetched)
        self.assertEqual(fetched - set(listed), set(), "hosts the presets fetch from but the README omits")
        self.assertEqual(set(listed) - fetched, set(), "hosts the README lists but no preset fetches from")

    def test_skill_description_stays_under_the_listing_cap(self):
        # Claude Code truncates description + when_to_use at 1,536 characters
        # in the skill listing, silently cutting the Triggers at the end
        skill = Path(generic.PRESETS_FILE).parents[1] / "skills" / "researching-library-docs" / "SKILL.md"
        front = skill.read_text(encoding="utf-8").split("\n---\n", 1)[0]
        desc, rest = front.split("description: |\n", 1)[1].split("\nwhen_to_use: |\n", 1)
        when = rest.split("\nargument-hint:", 1)[0]

        def flat(block):
            return " ".join(line.strip() for line in block.splitlines())
        self.assertLess(len(flat(desc)) + len(flat(when)), 1536 - 50, "leave room for one more preset")

    def test_preset_shapes_split_their_fixtures(self):
        # one fixture per shape, read through the shipped preset
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        cases = {
            "nextjs": (FRONTMATTER_TITLE, ["Getting Started", "Install"]),
            "drizzle": (LINE_SOURCE, ["Page A", "b"]),
            "zod": (H1_NO_URL, ["Site", "Schemas", "Errors"]),
            "hono": (H1_BANNER, ["Site", "Routing"]),
            "cloudflare-d1": (CLOUDFLARE_LIKE, ["Product", "Getting started"]),
            "bun": (MINTLIFY, ["Bytecode Caching", "CSS"]),
            "vercel": (VERCEL_LIKE, ["Access tokens", "Forbidden properties"]),
            "codex": (CODEX_LIKE, ["Site — full documentation", "Agent approvals", "Config basics"]),
        }
        for name, (text, titles) in cases.items():
            with self.subTest(name):
                docs = generic.split_documents(_lines(text), presets[name])
                self.assertEqual([d["title"] for d in docs], titles)
        old_template = (
            "---\ntitle: Old\n---\n\n[Skip to content](#%5Ftop) \n\nWas this helpful?\n\nYesNo\n\n"
            "[ Edit page ](https://github.com/example/edit/x.mdx) [ Report issue ](https://example.com)\n\nCopy page\n\n"
            "# Old\n\nBody text.\n\n```sh\nCopy page\n```\n"
        )
        body = "".join(generic.split_documents(_lines(old_template), presets["cloudflare-browser-rendering"])[0]["body_lines"])
        for noise in ("Skip to content", "Was this helpful", "YesNo", "Edit page"):
            self.assertNotIn(noise, body)
        self.assertIn("```sh\nCopy page\n```", body)  # inside a fence: content
        self.assertEqual(body.count("Copy page"), 1)
        cf = generic.split_documents(_lines(CLOUDFLARE_LIKE), presets["cloudflare-workers"])
        self.assertEqual(cf[0]["url"], "https://example.com/product/index.md")
        self.assertFalse(any(re.match(r"^Last updated", line) for line in cf[0]["body_lines"]))


INDEX = """\
# Site

- [Schemas](https://example.com/docs/schemas): Defining schemas
- [**Errors**](https://example.com/docs/errors)
- [Site](https://example.com/a)
- [Site](https://example.com/b)
- [Basic Auth](https://example.com/docs/basic-auth)
"""

# a site root whose llms.txt mostly links one llms.txt per product (a 2-level
# index), plus a learning-track page that is a page after all
TWO_LEVEL_INDEX = """\
# Example Developers

> Each product below links to its own llms.txt.

## Documentation sets
- [Sign in with Example](https://example.com/siwc/llms.txt): Quickstart and integration.
- [Example API guides](https://example.com/api/llms.txt): Guides and endpoint reference.

## Learning tracks
- [Model optimization](https://example.com/tracks/model-optimization.md): Fine-tune and optimize models.
"""


class IndexJoinTest(unittest.TestCase):
    """index_url: page URLs from llms.txt by exact title."""

    def docs(self):
        return generic.split_documents(_lines(H1_NO_URL + "\n# Basic Auth Middleware\n\nx\n"), _profile(split="h1"))

    def test_exact_unique_titles_get_urls(self):
        docs = self.docs()
        joined = generic.join_index_urls(docs, _lines(INDEX))
        urls = {d["title"]: d["url"] for d in docs}
        self.assertEqual(urls["Schemas"], "https://example.com/docs/schemas")
        self.assertEqual(urls["Errors"], "https://example.com/docs/errors")  # ** marks ignored
        self.assertEqual(urls["Site"], "")  # two entries share the title: no guess
        self.assertEqual(urls["Basic Auth Middleware"], "")  # near match is not a match
        self.assertEqual(joined, 2)

    def test_a_page_that_has_a_url_keeps_it(self):
        docs = self.docs()
        docs[1]["url"] = "https://example.com/own"
        generic.join_index_urls(docs, _lines(INDEX))
        self.assertEqual(docs[1]["url"], "https://example.com/own")

    def test_a_title_shared_by_two_pages_gets_no_url(self):
        # Zod: the site banner and the packages/zod page are both "Zod"
        index = _lines("- [Zod](https://zod.dev/packages/zod): The zod package\n")
        corpus = "# Zod\n\nZod is a TypeScript-first schema library.\n\n# Zod\n\nThe zod package.\n"
        docs = generic.split_documents(_lines(corpus), _profile(split="h1"))
        self.assertEqual(generic.join_index_urls(docs, index), 0)
        self.assertEqual([d["url"] for d in docs], ["", ""])
        # one of the two already has a URL: the index entry may be that page's
        docs[0]["url"] = "https://zod.dev/"
        self.assertEqual(generic.join_index_urls(docs, index), 0)
        self.assertEqual(docs[1]["url"], "")

    # Entries that link an llms.txt-family file (another index, a full-text
    # export) are not pages: a page of the same title must not get that URL.
    INDEX_FILES = {
        "Per-product index": "https://example.com/siwc/llms.txt",
        "Full-text export": "https://example.com/docs/llms-full.txt",
        "Small export": "https://example.com/llms-small.txt",
        "Context export": "https://example.com/llms-ctx-full.txt",
        "Upper case": "https://example.com/docs/LLMS.TXT",
        "With query": "https://example.com/docs/llms.txt?lang=en",
        "With fragment": "https://example.com/docs/llms.txt#top",
        "Trailing slash": "https://example.com/docs/llms.txt/",
    }
    # URLs that only look like an index file are pages and keep joining
    PAGES_LIKE_INDEX_FILES = {
        "Markdown twin": "https://example.com/docs/llms.txt.md",
        "Other file name": "https://example.com/docs/not-llms.txt",
        "No extension": "https://example.com/docs/llms",
        "Directory": "https://example.com/llms.txt/intro",
        "Host only": "https://llms.txt",
    }

    def join_by_title(self, url_by_title: dict) -> dict:
        """Join an index of one entry per title against one page per title."""
        index = _lines("".join(f"- [{title}]({url})\n" for title, url in url_by_title.items()))
        corpus = "".join(f"# {title}\n\nBody of {title}.\n\n" for title in url_by_title)
        docs = generic.split_documents(_lines(corpus), _profile(split="h1"))
        generic.join_index_urls(docs, index)
        return {d["title"]: d["url"] for d in docs}

    def test_entries_linking_an_llms_txt_file_are_not_pages(self):
        got = self.join_by_title(self.INDEX_FILES)
        for title in self.INDEX_FILES:
            with self.subTest(title):
                self.assertEqual(got[title], "")

    def test_urls_that_only_look_like_an_index_file_still_join(self):
        got = self.join_by_title(self.PAGES_LIKE_INDEX_FILES)
        for title, url in self.PAGES_LIKE_INDEX_FILES.items():
            with self.subTest(title):
                self.assertEqual(got[title], url)

    def test_two_level_index_gives_no_llms_txt_url(self):
        # index_url pointed at a site root whose llms.txt links other llms.txt
        # files: no page gets one of those URLs. A learning-track entry that
        # shares a title with a page still joins; that cannot be told apart
        # from a real match, which is why the README says not to use a
        # 2-level index as index_url.
        corpus = "# Sign in with Example\n\nx\n\n# Example API guides\n\nx\n\n# Model optimization\n\nx\n"
        docs = generic.split_documents(_lines(corpus), _profile(split="h1"))
        joined = generic.join_index_urls(docs, _lines(TWO_LEVEL_INDEX))
        self.assertEqual({d["title"]: d["url"] for d in docs}, {
            "Sign in with Example": "",
            "Example API guides": "",
            "Model optimization": "https://example.com/tracks/model-optimization.md",
        })
        self.assertEqual(joined, 1)

    def test_an_index_file_entry_does_not_make_a_page_title_ambiguous(self):
        # the entry for the guide's own llms.txt is not a second candidate for
        # the page "Guide": the page entry is the only one left
        index = _lines(
            "- [Guide](https://example.com/guide/llms.txt): the guide's own index\n"
            "- [Guide](https://example.com/docs/guide.md): the guide page\n"
        )
        docs = generic.split_documents(_lines("# Guide\n\nx\n"), _profile(split="h1"))
        self.assertEqual(generic.join_index_urls(docs, index), 1)
        self.assertEqual(docs[0]["url"], "https://example.com/docs/guide.md")

    def cli(self, *extra, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp, "s.json")
            sources.write_text(json.dumps({"sources": {"site": {
                "url": "https://example.com/llms-full.txt", "split": "h1",
                "index_url": "https://example.com/llms.txt"}}}), encoding="utf-8")
            corpus = Path(tmp, "full.txt")
            corpus.write_text(H1_NO_URL, encoding="utf-8")
            index = Path(tmp, "llms.txt")
            index.write_text(INDEX, encoding="utf-8")
            argv = ["parse-llms-txt.py", "fetch-index", "--source", "site", "--sources-file", str(sources),
                    "--cache-dir", tmp]
            argv += [a.replace("{corpus}", str(corpus)).replace("{index}", str(index)) for a in extra]
            return _loader.run_cli(generic, argv)

    def test_local_corpus_without_index_file_does_not_fetch(self):
        with mock.patch.object(generic, "fetch_url", side_effect=AssertionError("fetched")):
            code, out, err = self.cli("--file", "{corpus}")
        self.assertEqual(code, 0, err)
        self.assertNotIn("url:", out)

    def test_index_file_gives_urls_and_stays_in_the_hint(self):
        code, out, err = self.cli("--file", "{corpus}", "--index-file", "{index}")
        self.assertEqual(code, 0, err)
        self.assertIn("url: https://example.com/docs/schemas", out)
        self.assertIn("--index-file", out.strip().splitlines()[-1])

    def test_failed_index_fetch_only_warns(self):
        def fake_fetch(url, cache_path, **kw):
            if url.endswith("/llms.txt"):
                raise generic.FetchError(url, OSError("down"))
            Path(cache_path).write_text(H1_NO_URL, encoding="utf-8")
            return cache_path
        with mock.patch.object(generic, "fetch_url", side_effect=fake_fetch):
            code, out, err = self.cli()
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING", err)
        self.assertIn("documents total", out)


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
