"""The presets added for small differences of shape (Gemini CLI: an H1 that is
a link; Kilo Code: a relative ``## Source:`` and Markdoc titles) and for the
per-page sites (Zed, Amp). Fixtures are cut from each site's real files
(2026-11-03). Every printed ``Next:`` line is run as the shell splits it.
"""

import json
import os
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader
from test_page_fetch import _Net

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"


def presets() -> dict:
    # read when a test asks: a preset refused by validation fails the tests
    # that use it, not the import of this module
    return g._read_sources_file(g.PRESETS_FILE)

GEMINI = """\
# Gemini CLI Documentation

# [Automation and triage processes](http://geminicli.com/docs/issue-and-pr-automation.md)


This document provides a detailed overview of the automated processes.

## Guiding principle: Issues and pull requests

```bash
# For a stable channel patch
gh workflow run
```

# Code Reviewer Instructions

Review like this.

# [Web search tool (`google_web_search`)](http://geminicli.com/docs/tools/web-search.md)

import { Tabs, TabItem } from '@astrojs/starlight/components';

The search tool.

```ts
import { Tabs, TabItem } from '@astrojs/starlight/components';
```

## Usage

Call it.

# [Not a page](#anchor)

Under the web search page.
"""

KILO = """\
# Kilo Code Documentation

## Page Index

- [Introduction](https://kilocode-docs.vercel.app/api/raw-markdown?path=%2Fgetting-started)

## Source: /ai-providers/alibaba

# Using Alibaba Cloud With Kilo Code

Set the key.

## Source: /automate

---
title: "Automate"
description: "Automate your development workflows with Kilo Code"
---

# {% $markdoc.frontmatter.title %}

{% callout type="generic" %}
Automate repetitive tasks.
{% /callout %}

## Workflows

Run them.

## Source: /customize

\ufeff---
title: "Customize"
description: "Make Kilo Code work your way"
---


{% callout type="generic" %}
# {% $markdoc.frontmatter.title %}
{% /callout %}

## Source: /no-frontmatter

# {% $markdoc.frontmatter.title %}

Text.
"""


def _lines(text: str) -> list:
    return text.splitlines(keepends=True)


class GeminiShapeTest(unittest.TestCase):
    def setUp(self):
        self.docs = g.split_documents(_lines(GEMINI), presets()["gemini-cli"])

    def test_pages_titles_and_urls(self):
        self.assertEqual([(d["title"], d["url"]) for d in self.docs], [
            ("Automation and triage processes", "http://geminicli.com/docs/issue-and-pr-automation.md"),
            ("Web search tool (`google_web_search`)", "http://geminicli.com/docs/tools/web-search.md"),
        ])

    def test_an_h1_with_no_link_stays_a_heading_of_the_page_before(self):
        self.assertIn("# Code Reviewer Instructions\n", self.docs[0]["body_lines"])
        self.assertEqual(self.docs[0]["min_level"], 1)
        self.assertIn("# [Not a page](#anchor)\n", self.docs[1]["body_lines"])

    def test_the_stray_mdx_import_goes_and_the_one_in_a_code_block_stays(self):
        body = self.docs[1]["body_lines"]
        self.assertEqual(body.count("import { Tabs, TabItem } from '@astrojs/starlight/components';\n"), 1)
        self.assertLess(body.index("```ts\n"),
                        body.index("import { Tabs, TabItem } from '@astrojs/starlight/components';\n"))

    def test_the_rule_is_only_for_split_h1(self):
        with self.assertRaises(SystemExit), _loader_quiet():
            g._validate_profile("f", "x", {"url": "https://a.example/llms.txt", "split": "line",
                                           "line_prefix": "Source: ", "page_url": "h1-link"})

    def test_only_an_h1_that_is_one_whole_link_is_read(self):
        cases = {
            "[A](http://x)": ("A", "http://x"),
            "[A](/docs/a.md)": ("A", "/docs/a.md"),
            "[A [b]](http://x)": ("A [b]", "http://x"),
            "[A [b] [c [d]]](http://x)": ("A [b] [c [d]]", "http://x"),
            "[A (b)](http://x/a_(c))": ("A (b)", "http://x/a_(c)"),
            r"[A \] b](http://x)": (r"A \] b", "http://x"),
            "[ A ](http://x)": ("A", "http://x"),
            # not one link
            "[A](http://x) and [B](http://y)": ("[A](http://x) and [B](http://y)", ""),
            "[A](http://x)[B](http://y)": ("[A](http://x)[B](http://y)", ""),
            "[A](http://x) tail": ("[A](http://x) tail", ""),
            "lead [A](http://x)": ("lead [A](http://x)", ""),
            "[A] b](http://x)": ("[A] b](http://x)", ""),
            "[A] /b(c)": ("[A] /b(c)", ""),
            "[A [b](http://x)": ("[A [b](http://x)", ""),
            "[A](http://x": ("[A](http://x", ""),
            "[A](http://x y)": ("[A](http://x y)", ""),
            "[A](#anchor)": ("[A](#anchor)", ""),
            "[A]()": ("[A]()", ""),
            "[](http://x)": ("[](http://x)", ""),
            "[A]": ("[A]", ""),
            "[": ("[", ""),
            "Plain": ("Plain", ""),
        }
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(g._h1_link(title), want)

    def test_without_the_rule_a_link_h1_is_the_title_as_written(self):
        p = g._validate_profile("f", "x", {"url": "https://a.example/llms.txt", "split": "h1"})
        docs = g.split_documents(_lines("# [A](http://a.example/a.md)\n\nx\n"), p)
        self.assertEqual((docs[0]["title"], docs[0]["url"]), ("[A](http://a.example/a.md)", ""))


class KiloShapeTest(unittest.TestCase):
    def setUp(self):
        self.docs = g.split_documents(_lines(KILO), presets()["kilo-code"])

    def test_urls_keep_the_docs_path_of_the_prefix(self):
        self.assertEqual([d["url"] for d in self.docs], [
            "https://kilo.ai/docs/ai-providers/alibaba", "https://kilo.ai/docs/automate",
            "https://kilo.ai/docs/customize", "https://kilo.ai/docs/no-frontmatter"])

    def test_a_template_title_comes_from_the_frontmatter(self):
        self.assertEqual([d["title"] for d in self.docs][:3],
                         ["Using Alibaba Cloud With Kilo Code", "Automate", "Customize"])

    def test_a_template_title_with_no_frontmatter_is_left_as_it_is(self):
        self.assertEqual(self.docs[3]["title"], "{% $markdoc.frontmatter.title %}")

    def test_the_template_heading_is_not_a_section(self):
        self.assertNotIn("# {% $markdoc.frontmatter.title %}\n", self.docs[1]["body_lines"])

    def test_the_index_part_before_the_first_source_is_no_page(self):
        self.assertFalse(any("Page Index" in "".join(d["body_lines"]) for d in self.docs))

    def test_a_double_brace_template_counts_too(self):
        body = _lines('---\ntitle: "T"\n---\n')
        self.assertEqual(g._untemplated_title("{{ title }}", body), "T")
        self.assertEqual(g._untemplated_title("Plain", body), "Plain")

    def test_url_prefix_validation_and_forms(self):
        ok = {"url": "https://a.example/llms-full.txt", "split": "line", "line_prefix": "Source: "}
        for raw in ({**ok, "url_prefix": "nope"}, {**ok, "url_prefix": 3},
                    {**ok, "url_prefix": "https://a.example", "url_base": "https://a.example"}):
            with self.subTest(raw=raw), self.assertRaises(SystemExit), _loader_quiet():
                g._validate_profile("f", "x", raw)
        p = g._validate_profile("f", "x", {**ok, "url_prefix": "https://a.example/docs/"})
        text = "Source: /a\n\n# A\n\nSource: b\n\n# not a delimiter\n\nSource: https://b.example/c\n\n# C\n"
        self.assertEqual([d["url"] for d in g.split_documents(_lines(text), p)],
                         ["https://a.example/docs/a", "https://b.example/c"])

    def test_url_base_still_joins_as_a_url(self):
        p = g._validate_profile("f", "x", {"url": "https://a.example/llms-full.txt", "split": "line",
                                           "line_prefix": "Source: ", "url_base": "https://a.example/docs"})
        self.assertEqual(g.split_documents(_lines("Source: /a\n\n# A\n"), p)[0]["url"], "https://a.example/a")


class OtherPresetsTest(unittest.TestCase):
    def test_continue_reads_h1_and_source_lines(self):
        text = ("# Configuration\nSource: https://continue-docs.mintlify.app/cli/configuration\n\nBody.\n\n"
                "# Inner heading\n\nMore.\n\n# Headless Mode\nSource: https://continue-docs.mintlify.app/cli/headless-mode\n\nB.\n")
        docs = g.split_documents(_lines(text), presets()["continue"])
        self.assertEqual([d["title"] for d in docs], ["Configuration", "Headless Mode"])
        self.assertTrue(all(d["url"].startswith("https://continue-docs.mintlify.app/") for d in docs))

    def test_agents_js_reads_h1_pages_with_no_url(self):
        text = ("<SYSTEM>This is the full developer documentation</SYSTEM>\n\n# OpenAI Agents SDK TypeScript\n\n> Intro\n\n"
                "# ShellAction\n\nOne.\n\n# ShellAction\n\nTwo.\n")
        docs = g.split_documents(_lines(text), presets()["openai-agents-js"])
        self.assertEqual([d["title"] for d in docs], ["OpenAI Agents SDK TypeScript", "ShellAction", "ShellAction"])
        self.assertEqual({d["url"] for d in docs}, {""})


def _loader_quiet():
    import contextlib
    import io
    return contextlib.redirect_stderr(io.StringIO())


ZED_INDEX = "https://zed.dev/docs/llms.txt"
ZED_ROBOTS = "https://zed.dev/robots.txt"
ZED_PAGES = {
    "https://zed.dev/docs/getting-started.md": (
        "Getting Started", "Get started with Zed.",
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Getting Started\n\nZed is an open-source code editor.\n\n## Quick Start\n\nOpen a project.\n"),
    "https://zed.dev/docs/ai/overview.md": (
        "Overview", "AI overview.",
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Overview\n\n## Agent Panel (& more)\n\nThe agent panel lives here.\n"),
    "https://zed.dev/docs/windows/overview.md": (
        "Overview", "Windows overview.",
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Overview\n\n## --install flags\n\nThe agent installer.\n"),
}
AMP_INDEX = "https://ampcode.com/llms.txt"
AMP_ROBOTS = "https://ampcode.com/robots.txt"
AMP_PAGES = {
    "https://ampcode.com/docs/markdown": (
        "Introduction", "Amp is a coding agent.",
        "---\nlastModified: 2026-09-02\n---\n\n<script>\n  import DocsQuickstart from '#lib/components'\n</script>\n\n"
        "# Introduction\n\nAmp agent intro.\n\n## Quickstart\n\nInstall the agent.\n"),
    "https://ampcode.com/docs/markdown/prompting": (
        "Prompting", "Write prompts.",
        "---\nlastModified: 2026-11-02\n---\n\n<script>\n\timport Mic from '~icons/lucide/mic'\n"
        "\timport { route } from '#lib/route-helpers.js'\n</script>\n\n# Prompting\n\n"
        '<h2 id="how-to-prompt">How to Prompt</h2>\n\nPrompt the agent well.\n\n```svelte\n<script>\nimport X from \'y\'\n</script>\n```\n\n'
        '<h2 id="finding-threads">Finding Threads</h2>\n\nFind them.\n'),
}


def _index_text(pages: dict) -> str:
    return "# Docs\n\n> A real index has prose too.\n\n" + "".join(
        f"- [{t}]({u}): {d}\n" for u, (t, d, _b) in pages.items())


class PerPagePresetsTest(unittest.TestCase):
    """The bundled ``zed`` / ``amp`` profiles on a mocked network."""

    CASES = {
        "zed": (ZED_INDEX, ZED_ROBOTS, ZED_PAGES,
                "# robots.txt\nUser-agent: *\nAllow: /\nDisallow: /_next/static/chunks/app/\n", "overview"),
        "amp": (AMP_INDEX, AMP_ROBOTS, AMP_PAGES,
                "User-agent: *\nAllow: /\nDisallow: /auth/\nDisallow: /api/\n", "agent"),
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {}}), encoding="utf-8")

    def net(self, name) -> _Net:
        index_url, robots_url, pages, robots, _word = self.CASES[name]
        return _Net({index_url: _index_text(pages), robots_url: robots, **{u: b for u, (_t, _d, b) in pages.items()}})

    def run_words(self, net, words):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            return _loader.run_cli(g, words)

    def run_g(self, net, name, *argv):
        return self.run_words(net, [SCRIPT, *argv, "--source", name, "--sources-file", self.sources,
                                    "--cache-dir", self.tmp])

    def run_next(self, net, line: str, **subs):
        words = shlex.split(line, comments=True)
        self.assertEqual(words[:2], ["Next:", SCRIPT], line)
        return self.run_words(net, [subs.get(w.strip("<>"), w) for w in words[1:]])

    def test_the_presets_ask_for_what_the_decision_says(self):
        for name in self.CASES:
            with self.subTest(name), _loader_quiet():
                p = presets()[name]
                self.assertEqual(p["body"], "fetch")
                self.assertEqual(p["page_fetch_workers"], 1)
                self.assertTrue(p["respect_robots"])
                self.assertTrue(p["search_content_needs_page_ref"])

    def test_listing_reads_only_the_index_and_robots(self):
        for name in self.CASES:
            with self.subTest(name):
                net = self.net(name)
                index_url, robots_url, pages, _r, _w = self.CASES[name]
                code, out, err = self.run_g(net, name, "fetch-index")
                self.assertEqual(code, 0, err)
                self.assertEqual(net.calls, [robots_url, index_url])
                self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("[")), len(pages))
                code, out, err = self.run_g(net, name, "search-index", "overview")
                self.assertEqual(code, 0, err)
                self.assertEqual(net.calls, [robots_url, index_url])

    def test_search_fetches_one_at_a_time_and_every_next_line_runs(self):
        for name in self.CASES:
            with self.subTest(name):
                net = self.net(name)
                code, out, err = self.run_g(net, name, "search", self.CASES[name][4], "--top-n", "2")
                self.assertEqual(code, 0, err)
                self.assertEqual(net.max_active, 1)
                nexts = [line for line in out.splitlines() if line.startswith("Next:")]
                self.assertTrue(nexts, out)
                titles = [t for t, _d, _b in self.CASES[name][2].values()]
                for line in nexts:
                    code2, out2, err2 = self.run_next(net, line)
                    self.assertEqual(code2, 0, f"{line}\n{err2}")
                    # the page number on the line opens that entry's page
                    self.assertIn(f"# doc_title: {titles[int(shlex.split(line)[3])]}\n", out2, line)

    def test_a_full_scan_is_refused_without_a_request_for_any_page(self):
        for name in self.CASES:
            with self.subTest(name):
                net = self.net(name)
                index_url, robots_url, _p, _r, _w = self.CASES[name]
                code, _out, err = self.run_g(net, name, "search-content", "agent")
                self.assertEqual(code, 2, err)
                self.assertIn("--page-ref", err)
                self.assertEqual(net.calls, [robots_url, index_url])

    def test_one_page_search_content_runs_and_its_zero_hit_report_offers_no_full_scan(self):
        for name in self.CASES:
            with self.subTest(name):
                net = self.net(name)
                code, out, err = self.run_g(net, name, "search-content", "agent", "--page-ref", "1")
                self.assertEqual(code, 0, err)
                self.assertIn("agent", out.lower())
                code, out, err = self.run_g(net, name, "search-content", "zzznotthere", "--page-ref", "1")
                self.assertEqual(code, 0, err)
                self.assertNotIn("Drop --page-ref", out)
                self.assertIn("other pages were not fetched", out)

    def test_the_zero_hit_tips_do_not_point_at_the_refused_scan(self):
        for name in self.CASES:
            for cmd in ("search", "search-index"):
                with self.subTest(name=name, cmd=cmd):
                    net = self.net(name)
                    code, out, err = self.run_g(net, name, cmd, "zzznotthere")
                    self.assertEqual(code, 0, err)
                    self.assertNotIn("omit --page-ref", out)
                    # joined with "=": a ref put in its place may start with
                    # "-" (test_placeholder_hints.HintCommandSwapTest runs it)
                    self.assertIn("--page-ref=<page_ref>", out)
                    for line in (ln for ln in out.splitlines() if ln.startswith("Next:")):
                        code2, _o, err2 = self.run_next(net, line)
                        self.assertEqual(code2, 0, f"{line}\n{err2}")

    def test_the_tip_with_the_page_ref_put_in_runs(self):
        # the tip tells the reader to put a page and a query in: done as it says
        # (a query that starts with "-" included)
        net = self.net("zed")
        for query in ("agent", "--install"):
            with self.subTest(query=query):
                code, out, err = self.run_words(net, [
                    SCRIPT, "search-content", "--page-ref", "2", "--source", "zed", "--sources-file", self.sources,
                    "--cache-dir", self.tmp, "--", query])
                self.assertEqual(code, 0, err)
                self.assertIn("installer" if query.startswith("-") else "agent", out.lower())

    def test_zed_boilerplate_line_is_not_searched(self):
        net = self.net("zed")
        code, out, err = self.run_g(net, "zed", "search-content", "complete documentation index", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("No matching content found", out)
        code, out, err = self.run_g(net, "zed", "content", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("complete documentation index", out)
        self.assertIn("Quick Start", out)

    def test_the_zero_hit_counts_do_not_read_the_dropped_line(self):
        net = self.net("zed")
        code, out, err = self.run_g(net, "zed", "search-content", "llms.txt", "--page-ref", "0")
        self.assertEqual(code, 0, err)
        self.assertIn('"llms.txt": 0 of 1', out)

    def test_search_reads_the_page_without_the_dropped_line(self):
        net = self.net("zed")
        code, out, err = self.run_g(net, "zed", "search", "overview complete documentation index")
        self.assertEqual(code, 0, err)
        self.assertNotIn("For the complete documentation index", out)

    def test_amp_html_headings_are_sections_and_the_svelte_header_is_gone(self):
        net = self.net("amp")
        code, out, err = self.run_g(net, "amp", "sections", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("How to Prompt", out)
        self.assertIn("Finding Threads", out)
        code, out, err = self.run_g(net, "amp", "content", "1", "Finding Threads")
        self.assertEqual(code, 0, err)
        self.assertIn("Find them.", out)
        self.assertNotIn("Prompt the agent well", out)
        code, out, err = self.run_g(net, "amp", "content", "1")
        self.assertEqual(code, 0, err)
        self.assertNotIn("lastModified", out)
        self.assertNotIn("~icons/lucide/mic", out)
        # the fixture's only "---" lines are the frontmatter's, so one in the output is left of it
        raw = AMP_PAGES["https://ampcode.com/docs/markdown/prompting"][2].splitlines()
        self.assertEqual(raw.count("---"), 2)
        self.assertLess(raw.index("lastModified: 2026-11-02"), raw.index("# Prompting"))
        self.assertEqual(out.splitlines().count("---"), 1, "the one under the page's header")
        # the same lines inside a code block are the page's own text
        self.assertIn("import X from 'y'", out)

    def test_page_refs_by_url_and_slug_reach_their_page(self):
        net = self.net("amp")
        for ref, want in (("https://ampcode.com/docs/markdown/prompting", "Prompting"),
                          ("https://ampcode.com/docs/markdown", "Introduction"),
                          ("prompting", "Prompting"), ("markdown", "Introduction")):
            with self.subTest(ref=ref):
                code, out, err = self.run_g(net, "amp", "sections", ref)
                self.assertEqual(code, 0, err)
                self.assertIn(f'"{want}"', out.splitlines()[0])

    def test_two_entries_with_one_title_open_by_number_and_a_title_is_no_ref(self):
        # Zed lists "Overview" more than once: page numbers and slugs tell them apart
        net = self.net("zed")
        code, out, err = self.run_g(net, "zed", "content", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("agent panel lives here", out)
        code, out, err = self.run_g(net, "zed", "content", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("agent installer", out)
        code, _out, _err = self.run_g(net, "zed", "content", "Overview")
        self.assertEqual(code, 1)


class SessionStartNoticeTest(unittest.TestCase):
    """A bundled source the skill description does not list is reached through
    this notice, so a preset is named here or is left out on purpose."""

    # preset -> the words the notice uses for it (cloudflare-*: "Cloudflare 製品")
    NAMED = {
        "claude-code": "Claude Code", "ai-sdk": "AI SDK", "firebase": "Firebase",
        "nextjs": "Next.js", "vite": "Vite", "vitest": "Vitest", "drizzle": "Drizzle", "zod": "Zod", "hono": "Hono",
        "bun": "Bun", "vercel": "Vercel", "render": "Render", "mcp": "MCP", "codex": "Codex", "ollama": "Ollama",
        "pixai": "PixAI", "agentskills": "Agent Skills", "agent-plugins": "Agent Plugins", "openai-plugins": "OpenAI Plugins",
        "openai-api-docs": "OpenAI Plugins/API", "acp": "ACP", "cline": "Cline", "factory": "Factory",
        "devin": "Devin", "gemini-cli": "Gemini CLI", "kilo-code": "Kilo Code",
        "openai-agents-js": "OpenAI Agents SDK (TS)", "continue": "Continue", "zed": "Zed", "amp": "Amp",
    }
    # left out on purpose: the Claude docs skill the notice names reads them too
    LEFT_OUT = {"claude-platform", "claude"}

    def notice(self) -> str:
        hooks = json.loads((Path(g.PRESETS_FILE).parent.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        return hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    def test_every_preset_is_named_or_left_out_on_purpose(self):
        notice = self.notice()
        names = json.loads(Path(g.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]
        for name in names:
            with self.subTest(name):
                if name.startswith("cloudflare-"):
                    self.assertIn("Cloudflare 製品", notice)
                elif name in self.LEFT_OUT:
                    self.assertIn("researching-claude-docs", notice)
                else:
                    self.assertIn(name, self.NAMED, "a new preset: add it to the notice and to NAMED, or to LEFT_OUT")
                    self.assertIn(self.NAMED[name], notice)

    def test_the_lists_hold_no_preset_that_is_gone(self):
        names = set(json.loads(Path(g.PRESETS_FILE).read_text(encoding="utf-8"))["sources"])
        self.assertEqual((set(self.NAMED) | self.LEFT_OUT) - names, set())


class FetchKeysValidationTest(unittest.TestCase):
    FETCH = {"body": "fetch", "rank_from": "index", "index_url": "https://a.example/llms.txt",
             "page_ref_forms": ["int", "url", "slug"]}
    FULL = {"url": "https://a.example/llms-full.txt", "split": "h1"}

    def refused(self, raw, text):
        import contextlib
        import io
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            g._validate_profile("f", "x", raw)
        self.assertIn(text, err.getvalue())

    def test_h1_needs_url_takes_the_rule(self):
        raw = {"url": "https://a.example/llms.txt", "split": "h1", "page_url": "h1-link", "h1_needs_url": True}
        try:
            with _loader_quiet():
                p = g._validate_profile("f", "x", raw)
        except SystemExit:
            self.fail("h1_needs_url refused page_url h1-link")
        self.assertTrue(p["h1_needs_url"])

    def test_only_with_body_fetch(self):
        for key in ("respect_robots", "search_content_needs_page_ref"):
            with self.subTest(key):
                self.refused({**self.FULL, key: True}, 'only valid with body "fetch"')

    def test_must_be_booleans(self):
        for key in ("respect_robots", "search_content_needs_page_ref"):
            with self.subTest(key):
                self.refused({**self.FETCH, key: "yes"}, "must be true or false")

    def test_off_by_default(self):
        p = g._validate_profile("f", "x", self.FETCH)
        self.assertFalse(p["respect_robots"])
        self.assertFalse(p["search_content_needs_page_ref"])
        self.assertFalse(presets()["firebase"]["search_content_needs_page_ref"])

    def test_drop_lines_and_heading_pattern_are_checked_on_a_fetch_profile(self):
        self.refused({**self.FETCH, "drop_lines": ["("]}, "invalid regular expression")
        self.refused({**self.FETCH, "heading_line_pattern": "no-group"}, "exactly one group")

    def test_every_bundled_source_that_fetches_page_by_page_reads_robots_txt(self):
        """The policy: a bundled ``body: "fetch"`` preset walks its llms.txt
        page by page, so it obeys robots.txt. A ``body: "full"`` one fetches
        the one or two files its profile names, and has no such key."""
        with _loader_quiet():
            fetching = {name: p for name, p in presets().items() if p.get("body") == "fetch"}
        self.assertGreater(len(fetching), 0)
        for name, p in fetching.items():
            with self.subTest(name):
                self.assertTrue(p["respect_robots"])


FIREBASE_ROBOTS = "https://firebase.google.com/robots.txt"
FIREBASE_INDEX = "https://firebase.google.com/docs/llms.txt"
FIREBASE_PAGES = {
    "https://firebase.google.com/docs/cli/targets.md.txt": ("Deploy targets", "Short names for resources.",
                                                            "## Set up deploy targets\n\nApply a target name.\n"),
    "https://firebase.google.com/docs/auth/users.md.txt": ("Users in Firebase Projects", "Users.",
                                                           "## User properties\n\nA user has properties.\n"),
}


class FirebaseRobotsTest(unittest.TestCase):
    """The bundled ``firebase`` preset reads robots.txt before the first file
    the cache does not serve, once, and keeps it in the page cache."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {}}), encoding="utf-8")

    def run_g(self, net, *argv):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            try:
                return _loader.run_cli(g, [SCRIPT, *argv, "--source", "firebase", "--sources-file", self.sources,
                                           "--cache-dir", self.tmp])
            except Exception as e:  # a crash fails the assertions, not the run
                self.fail(f"{argv} crashed: {e!r}")

    def test_robots_txt_is_read_once_and_then_from_the_cache(self):
        net = _Net({FIREBASE_ROBOTS: "User-agent: *\nDisallow: /*?*authuser=\nDisallow: /byptwd/\n",
                    FIREBASE_INDEX: _index_text(FIREBASE_PAGES), **{u: b for u, (_t, _d, b) in FIREBASE_PAGES.items()}})
        first, second = FIREBASE_PAGES
        code, out, err = self.run_g(net, "content", "0")
        self.assertEqual(code, 0, err)
        self.assertIn("Apply a target name.", out)
        self.assertEqual(net.calls, [FIREBASE_ROBOTS, FIREBASE_INDEX, first])
        net.calls.clear()
        code, out, err = self.run_g(net, "content", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("A user has properties.", out)
        self.assertEqual(net.calls, [second])
        net.calls.clear()
        code, _out, err = self.run_g(net, "content", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(net.calls, [])

    def test_a_robots_txt_that_cannot_be_read_stops_the_first_fetch(self):
        net = _Net({FIREBASE_INDEX: _index_text(FIREBASE_PAGES)})  # robots.txt fails
        code, out, err = self.run_g(net, "fetch-index")
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        self.assertEqual(net.calls, [FIREBASE_ROBOTS])
        self.assertIn(f"could not read {FIREBASE_ROBOTS} (mock network: no {FIREBASE_ROBOTS})", err)
        self.assertNotIn("disallows", err)


# llms.txt entries whose host urllib.parse.urlsplit rejects (ValueError): a
# bracketed host that is not an IPv6 address (Python 3.11.4 and later), an
# unclosed "[" and a lone "]" (every version), and a host with a character
# that NFKC turns into "/". Each host holds the marker "unreadable".
UNREADABLE = {
    "bracketed host": "https://[unreadable].example.com/docs/remote",
    "unclosed [": "https://[unreadable.example.com/docs/remote",
    "lone ]": "https://unreadable].example.com/docs/remote",
    "NFKC slash": "https://unreadable\uff0fexample.com/docs/remote",  # U+FF0F FULLWIDTH SOLIDUS
}
UNREADABLE_REASON = "the host of the URL cannot be read ("


def _rejected(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        urlsplit(url)
    except ValueError:
        return True
    return False


class UnreadableHostTest(unittest.TestCase):
    """A ``respect_robots`` preset whose ``llms.txt`` lists a URL that
    ``urlsplit`` cannot read: that page is one failed fetch (skipped by
    ``search``, a failed fetch for ``sections``), never a traceback, and
    neither the page nor a ``robots.txt`` for its host is requested."""

    # (index, robots, pages, robots.txt, a word in the titles of the bad entry
    # and of at least one good page)
    CASES = {
        "zed": (ZED_INDEX, ZED_ROBOTS, ZED_PAGES, "User-agent: *\nAllow: /\n", "overview"),
        "amp": (AMP_INDEX, AMP_ROBOTS, AMP_PAGES, "User-agent: *\nAllow: /\nDisallow: /auth/\n", "prompting"),
        "firebase": (FIREBASE_INDEX, FIREBASE_ROBOTS, FIREBASE_PAGES, "User-agent: *\nDisallow: /byptwd/\n", "users"),
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {}}), encoding="utf-8")

    def net(self, name: str, bad: str) -> _Net:
        """The entry with *bad* first (page 0), then the preset's pages, in a
        new cache (the cached index of another case would answer instead)."""
        index_url, robots_url, pages, robots, word = self.CASES[name]
        index = _index_text(pages).replace("\n- [", f"\n- [Your instance {word}]({bad}): remote {word}\n- [", 1)
        self.assertIn(bad, index)
        self.cache = tempfile.mkdtemp(dir=self.tmp)
        return _Net({index_url: index, robots_url: robots, **{u: b for u, (_t, _d, b) in pages.items()}})

    def run_g(self, net, name, *argv):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            try:
                return _loader.run_cli(g, [SCRIPT, *argv, "--source", name, "--sources-file", self.sources,
                                           "--cache-dir", self.cache])
            except Exception as e:  # a crash fails the assertions, not the run
                self.fail(f"{name} {argv} crashed: {e!r}")

    def assert_refused(self, net, bad: str, err: str) -> None:
        self.assertNotIn(bad, net.calls)  # the page is never requested
        if _rejected(bad):
            self.assertIn(UNREADABLE_REASON, err)
            # nor a robots.txt for a host it could not name
            self.assertEqual([c for c in net.calls if "unreadable" in c], [])

    def test_the_cases_cover_every_bundled_source_that_reads_robots_txt(self):
        with _loader_quiet():
            polite = {name for name, p in presets().items() if p.get("respect_robots")}
        self.assertEqual(polite, set(self.CASES))
        # the forms every supported Python rejects run everywhere
        self.assertTrue(_rejected(UNREADABLE["unclosed ["]))
        self.assertTrue(_rejected(UNREADABLE["lone ]"]))

    def test_search_skips_the_unreadable_candidate_and_keeps_the_others(self):
        for name, (_i, _r, pages, _robots, word) in self.CASES.items():
            for what, bad in UNREADABLE.items():
                with self.subTest(name=name, url=what):
                    net = self.net(name, bad)
                    code, out, err = self.run_g(net, name, "search", word)
                    self.assertEqual(code, 0, err)
                    self.assertIn(f"(skip: fetch failed {bad}: ", err)
                    self.assertIn("(1 documents skipped", out)
                    self.assert_refused(net, bad, err)
                    # the other candidates are fetched and searched
                    good = [u for u, (t, _d, _b) in pages.items() if word in t.lower()]
                    self.assertTrue(good, word)
                    for url in good:
                        self.assertIn(url, net.calls)
                        self.assertIn(url, out)

    def test_sections_of_the_unreadable_page_is_a_failed_fetch_and_the_others_open(self):
        for name, (_i, _r, pages, _robots, _w) in self.CASES.items():
            first = next(iter(pages))
            for what, bad in UNREADABLE.items():
                with self.subTest(name=name, url=what):
                    net = self.net(name, bad)
                    for cmd in ("sections", "content"):
                        code, out, err = self.run_g(net, name, cmd, "0")
                        self.assertEqual(code, 1, err)
                        self.assertEqual(out, "")
                        self.assertIn(f"Error: Failed to fetch {bad}: ", err)
                        self.assert_refused(net, bad, err)
                    code, out, err = self.run_g(net, name, "sections", "1")
                    self.assertEqual(code, 0, err)
                    self.assertIn(f"Sections in [1] \"{pages[first][0]}\"\n  URL: {first}\n", out)
                    self.assertIn(first, net.calls)

    def test_search_content_on_the_unreadable_page_is_a_failed_fetch(self):
        for name in self.CASES:
            for what, bad in UNREADABLE.items():
                with self.subTest(name=name, url=what):
                    net = self.net(name, bad)
                    code, _out, err = self.run_g(net, name, "search-content", "remote", "--page-ref", "0")
                    self.assertEqual(code, 1, err)
                    self.assertIn(f"(skip: fetch failed {bad}: ", err)
                    self.assertIn("could not fetch the --page-ref 0 page", err)
                    self.assert_refused(net, bad, err)

    def test_a_full_search_content_skips_the_unreadable_page(self):
        # firebase scans every page (Zed and Amp ask for --page-ref)
        _i, _r, pages, _robots, _w = self.CASES["firebase"]
        for what, bad in UNREADABLE.items():
            with self.subTest(url=what):
                net = self.net("firebase", bad)
                code, out, err = self.run_g(net, "firebase", "search-content", "properties")
                self.assertEqual(code, 0, err)
                self.assertIn(f"(skip: fetch failed {bad}: ", err)
                self.assertIn("(1 documents skipped", out)
                self.assertIn("A user has properties.", out)
                self.assert_refused(net, bad, err)
                for url in pages:
                    self.assertIn(url, net.calls)


if __name__ == "__main__":
    unittest.main()
