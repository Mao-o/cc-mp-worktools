"""``_warn_if_split_suspect``: a page split that looks wrong is said on stderr.

The split rules stay as they are (a boundary inside a code block is not a
boundary, so a frontmatter example in a code sample is not a page). What this
adds is the warning that a file came out wrong, instead of a silent single page.

The motivating file: a Next.js ``llms-full.txt`` fetched on 2026-10-15 whose
closing fences ran into the next line (``` ```## Heading ```), which split into
9 pages of 474 with no warning. The next day's copy was fine, so the file is
rebuilt here from the pages' real frontmatter and the same joined fences.
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
FULL_URL = "https://example.com/llms-full.txt"
INDEX_URL = "https://example.com/llms.txt"


def page(k: int, *body) -> str:
    # the frontmatter of nextjs.org/docs/llms-full.txt (2026-10-28)
    return "\n".join(["---", f"title: Page {k}", f"description: Welcome to page {k}.",
                      f'url: "https://example.com/docs/p{k}"', "docs_index: /docs/llms.txt",
                      "related:", "  - /docs/app/getting-started", "---", "",
                      "> For an index of all documentation, see [/docs/llms.txt](/docs/llms.txt).",
                      "", f"## Heading {k}", "", f"zzq body {k}.", "", *body, ""])


def joined_fences_file(total: int = 474, whole: int = 9) -> str:
    """*whole* pages split as they should; the last of them holds a code block
    whose closer ran into the next line, as does every later page's."""
    good = "".join(page(k) for k in range(whole - 1))
    broken = "".join(page(k, "```ts", "const a = 1", "```## Next heading", "", "More text.")
                     for k in range(whole - 1, total))
    return good + broken


def index_for(total: int) -> str:
    return "".join(f"- [Page {k}](https://example.com/docs/p{k}): page {k}\n" for k in range(total))


FM = {"url": FULL_URL, "split": "frontmatter", "page_url": "frontmatter:url"}
SOURCES = {
    "no-index": FM,
    "title-join": {**FM, "index_url": INDEX_URL},
    "url-join": {**FM, "index_url": INDEX_URL, "rank_from": "index", "index_join": "url",
                 "page_ref_forms": ["int", "url", "slug"]},
    "auto": {"url": FULL_URL, "split": "auto", "split_candidates": [
        {"split": "frontmatter", "page_url": "frontmatter:url"},
        {"split": "h1", "page_url": "line:Source: "}]},
    "h1": {"url": FULL_URL, "split": "h1", "page_url": "line:Source: ", "index_url": INDEX_URL},
    "fm-skip-empty": {**FM, "skip_empty": True},
    "fm-no-url": {"url": FULL_URL, "split": "frontmatter", "page_url": "none"},
    # Cloudflare's shape: the page URL is a link in the body
    "fm-link": {"url": FULL_URL, "split": "frontmatter", "page_url": "link:View as Markdown"},
    "h1-source": {"url": FULL_URL, "split": "h1", "page_url": "line:Source: "},
    "h1-source-skip-empty": {"url": FULL_URL, "split": "h1", "page_url": "line:Source: ", "skip_empty": True},
    "h1-link": {"url": FULL_URL, "split": "h1", "page_url": "h1-link"},
    "h1-no-url": {"url": FULL_URL, "split": "h1"},
    "line": {"url": FULL_URL, "split": "line", "line_prefix": "Source: "},
    # Vercel's shape: a long rule around the frontmatter, the page URL under ``source``
    "fm-vercel": {"url": FULL_URL, "split": "frontmatter", "frontmatter_delimiter": "-" * 80,
                  "page_url": "frontmatter:source"},
    # Render's shape: the full text under /docs/, pages with no URL of their own
    # (the index gives them one by title)
    "render-like": {"url": "https://render.com/docs/llms-full.txt", "split": "h1",
                    "index_url": "https://render.com/docs/llms.txt"},
    # the full text under /docs/, the index a site-wide llms.txt at the root
    "docs-dir": {"url": "https://example.com/docs/llms-full.txt", "split": "frontmatter",
                 "page_url": "frontmatter:url", "index_url": "https://example.com/llms.txt"},
    # a full text on a mirror: no entry of its index is under its directory
    "mirror": {"url": "https://mirror.example/llms-full.txt", "split": "frontmatter",
               "page_url": "frontmatter:url", "index_url": "https://mirror.example/llms.txt"},
}


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llms split ")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = str(Path(self.tmp, "sources.json"))
        Path(self.sources).write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")

    def profile(self, name: str) -> dict:
        return generic._validate_profile(self.sources, name, SOURCES[name])

    def stage(self, name: str, full: str, index: str | None = None) -> None:
        profile = self.profile(name)
        Path(generic._cache_path(self.tmp, profile)).write_text(full, encoding="utf-8")
        if index is not None:
            Path(generic._index_cache_path(self.tmp, profile)).write_text(index, encoding="utf-8")

    def files(self, full: str, index: str | None = None) -> tuple:
        path = Path(self.tmp, "full.txt")
        path.write_text(full, encoding="utf-8")
        opts = ("--file", str(path))
        if index is not None:
            path = Path(self.tmp, "index.txt")
            path.write_text(index, encoding="utf-8")
            opts += ("--index-file", str(path))
        return opts

    def run_g(self, source: str, *argv, corpus: tuple | None = None):
        opts = corpus if corpus is not None else ("--cache-dir", self.tmp)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled")):
            try:
                return _loader.run_cli(generic, [SCRIPT, *argv, "--source", source,
                                                 "--sources-file", self.sources, *opts])
            except Exception as e:  # a crash fails the assertions, not the run
                self.fail(f"{argv} crashed: {e!r}")

    def warnings(self, err: str, what: str) -> list:
        return [ln for ln in err.splitlines() if ln.startswith("WARNING:") and what in ln]

    def warning(self, err: str, what: str) -> str:
        found = self.warnings(err, what)
        self.assertEqual(len(found), 1, err)
        return found[0]


class JoinedFencesFileTest(_Case):
    """The Next.js file: no index in its preset, so the open fence catches it."""

    FULL = joined_fences_file()

    def test_the_rule_stands_and_the_file_is_caught_by_every_command(self):
        self.assertEqual(len(generic.split_documents(self.FULL.splitlines(keepends=True), self.profile("no-index"))), 9)
        self.stage("no-index", self.FULL)
        for argv in (["sections", "0"], ["content", "0"], ["search", "zzq"], ["search-content", "zzq"],
                     ["search-index", "zzq"], ["fetch-index"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_g("no-index", *argv)
                self.assertEqual(code, 0, err)
                found = self.warnings(err, "never closes")
                self.assertEqual(len(found), 1, err)
                self.assertIn(generic._cache_path(self.tmp, self.profile("no-index")), found[0])

    def test_the_warning_names_where_the_block_opened(self):
        full = self.FULL
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        # the block that never closes is the first one (the first "```ts")
        opened = full.splitlines().index("```ts") + 1
        self.assertIn(f"opened near line {opened} of", self.warning(err, "never closes"))

    def test_with_an_index_the_page_count_is_said_as_well(self):
        code, _out, err = self.run_g("title-join", "sections", "0",
                                     corpus=self.files(self.FULL, index_for(474)))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)
        # the count is of the URLs under the directory of the profile's url
        self.assertIn("split into 9 pages, but its llms.txt lists 474 under https://example.com/. The file may",
                      self.warning(err, "split into"))

    def test_the_same_pages_with_their_fences_whole_say_nothing(self):
        full = self.FULL.replace("```## Next heading", "```\n\n## Next heading")
        self.assertEqual(len(generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))), 474)
        code, _out, err = self.run_g("title-join", "sections", "0", corpus=self.files(full, index_for(474)))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")


class UnclosedFenceTest(_Case):
    def test_a_closed_block_says_nothing(self):
        full = page(0, "```ts", "x", "```") + page(1)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_frontmatter_example_in_a_closed_block_is_still_no_page_and_says_nothing(self):
        full = page(0, "```yaml", "---", "url: https://example.com/not-a-page", "---", "```") + page(1)
        docs = generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))
        self.assertEqual([d["title"] for d in docs], ["Page 0", "Page 1"])
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_left_open_on_the_last_page_is_said(self):
        full = page(0) + page(1, "```ts", "x")
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)

    def test_it_survives_dropping_empty_pages(self):
        """``skip_empty`` removes pages from the list; what the split found
        about the file is not a property of one of them."""
        full = page(0) + "---\ntitle: Empty\nurl: https://example.com/e\n---\n\n" + page(1, "```ts", "x")
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)

    def test_split_auto_reports_only_the_candidate_that_won(self):
        # frontmatter wins (it finds the URLs), and it left a block open
        lost_h1 = page(0) + page(1, "```ts", "x")
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(lost_h1))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)
        # h1 wins (Source: lines); the frontmatter candidate, which never
        # finds a closing block here, has an open fence that is not the file's
        won_h1 = ("# One\nSource: https://example.com/a\n\nbody\n"
                  "# Two\nSource: https://example.com/b\n\n```ts\nx\n"
                  "---\nurl: https://example.com/c\n---\n")
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(won_h1))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "never closes"), [], err)


# Cut from render.com/docs/llms.txt (2026-11-18), lines as written: under /docs/
# a topic bundle, some docs pages and quickstarts (which its llms-full.txt leaves
# out); outside it the changelog, the blog and articles; and list items with a
# bare URL, which are no entries. The whole file has 225 page URLs under /docs/
# and 186 outside it, for 137 pages, which said the file was broken on every read.
RENDER_INDEX = """\
# Render

Render (https://render.com) is a cloud application platform for deploying, securing, and scaling applications, from hundred-line prototypes to complex multi-service architectures.

## Start here

Route according to your task:

- To retrieve documentation for a single topic, use the Documentation index below, search https://render.com/api/agent/docs-search?query={query}, or fetch the relevant page as Markdown from https://render.com/docs/{slug}.md.

## Agent interfaces

- MCP server documentation: https://render.com/docs/mcp-server.md
- Render REST API reference material: https://api-docs.render.com/llms.txt

## Documentation

For the complete documentation corpus, use https://render.com/llms-full.txt only when the task requires broad access to the full Render documentation set.

For complete documentation on a single topic, use a topic bundle:

- [Render Sandboxes — all articles](https://render.com/docs/sandboxes/all)

### Core Platform

#### Start

- [Your First Render Deploy](https://render.com/docs/your-first-deploy.md): Run your web app in minutes.
- [Deploy for Free](https://render.com/docs/free.md): Preview the Render platform with free web services and datastores.
- [Platform Features by Plan](https://render.com/docs/platform-features-by-plan.md): Enable powerful platform capabilities with a Pro, Scale, or Enterprise plan.
- [Using Render with Coding Agents](https://render.com/docs/llm-support.md): Deploy and manage apps using LLM-powered tools.
- [Render FAQ](https://render.com/docs/faq.md)

#### Compute

- [Render Compute Plans](https://render.com/docs/compute-plans.md): Specify your service's available CPU and RAM.

### Quickstarts

- [Deploy Ackee](https://render.com/docs/deploy-ackee.md)
- [Deploy an Actix Web App](https://render.com/docs/deploy-actix-todo.md)
- [Deploy Adminer on Render](https://render.com/docs/deploy-adminer.md)
- [Deploy Astro on Render](https://render.com/docs/deploy-astro.md): Host your site for free in minutes.

## Changelog

https://render.com/changelog.md

Most recent 10 entries:

- [Render Sandboxes now available in beta](https://render.com/changelog/render-sandboxes-now-available-in-beta.md)
- [Reuse a build across multiple services](https://render.com/changelog/reuse-a-build-across-multiple-services.md)

## Blog

https://render.com/blog

Most recent 10 posts:

- [How we built Render OIDC](https://render.com/blog/how-we-built-render-oidc.md)
- [Introducing Render Sandboxes: A computer for every agent](https://render.com/blog/introducing-render-sandboxes.md)

## Articles

https://render.com/articles

- [Render vs DigitalOcean App Platform: Web App Hosting Comparison](https://render.com/articles/render-vs-digitalocean-app-platform.md)
- [Render Hobby vs Pro vs Scale: Which Plan Do You Need?](https://render.com/articles/render-hobby-vs-pro-vs-scale.md)
- [Deploy Directus on Render: A Practical Guide](https://render.com/articles/deploy-directus-render.md)
- [How Much Does It Cost to Host a SaaS Application?](https://render.com/articles/saas-hosting-cost.md)
- [How Much Does It Cost to Host a FastAPI App?](https://render.com/articles/fastapi-hosting-cost.md)
- [How to migrate a production app to a new hosting platform](https://render.com/articles/how-to-migrate-a-production-app-to-a-new-hosting-platform.md)
- [Render Sandboxes vs Vercel Sandboxes](https://render.com/articles/render-sandboxes-vs-vercel-sandboxes.md)
- [Set up self-hosted GitHub Actions runners on Render](https://render.com/articles/set-up-self-hosted-github-actions-runners-on-render.md)
- [SSE vs WebSockets for Streaming Responses](https://render.com/articles/sse-vs-websockets-llm-streaming.md)
- [Managed PostgreSQL Pricing in 2026: Compute, Storage, Backups, and Egress](https://render.com/articles/managed-postgresql-pricing-2026.md)
- [Render Workflows vs Inngest: durable compute vs durable orchestration](https://render.com/articles/render-workflows-vs-inngest.md)
- [Render Workflows vs Trigger.dev: durable steps vs checkpointed tasks](https://render.com/articles/render-workflows-vs-trigger.md)
- [Render Workflows vs Temporal: managed compute vs your own workers](https://render.com/articles/render-workflows-vs-temporal.md)
- [Best durable workflow platforms for AI agents and LLM workloads](https://render.com/articles/best-durable-workflow-platforms-for-ai-agents-and-llm-workloads.md)

## Metadata

### Scope of public content

- https://render.com/docs
- https://render.com/blog
- https://render.com/changelog
- https://render.com/articles
"""

# The pages of render.com/docs/llms-full.txt (2026-11-18) that the cut above
# lists, opening lines as written: H1 pages with no URL, and an H1 in a code block
RENDER_FULL = """\
Generated on: 2026-11-18
Obtain latest version from: https://render.com/llms-full.txt
REST API llms reference: https://api-docs.render.com/llms.txt

Render (https://render.com) is a powerful, flexible cloud application platform that helps teams deploy, secure, and scale everything from hundred-line prototypes to complex multi-service architectures.

- Links use absolute URLs. Fragments refer to headings in the original source article.
- Each h1 designates a new page.

# Glossary

The following terms are used throughout the Render docs:

###### service type

When you deploy code on Render, you select a *service type* based on the capabilities you need.

# Your First Render Deploy

Welcome! Let's get up and running on Render.

## Consider infrastructure as code (IaC)

```yaml
# This is a basic example Blueprint for a Django web service and
# the Render Postgres database it connects to.
services:
  - type: web # A Python web service named django-app running on a free instance
    plan: free
```

# Deploy for Free

zzq free.

# Platform Features by Plan

zzq plans.

# Using Render with Coding Agents

zzq agents.

# Render FAQ

zzq faq.

# Render Compute Plans

zzq compute.
"""

RENDER_TITLES = ["Glossary", "Your First Render Deploy", "Deploy for Free", "Platform Features by Plan",
                 "Using Render with Coding Agents", "Render FAQ", "Render Compute Plans"]


class FewPagesThanTheIndexTest(_Case):
    def test_the_ratio_boundary(self):
        """Half the index is not yet "far fewer"; below half is."""
        full = "".join(page(k) for k in range(5))
        for listed, warned in ((10, False), (11, True), (5, False)):
            with self.subTest(listed=listed):
                code, _out, err = self.run_g("title-join", "sections", "0",
                                             corpus=self.files(full, index_for(listed)))
                self.assertEqual(code, 0, err)
                self.assertEqual(bool(self.warnings(err, "split into")), warned, err)

    def test_it_counts_pages_not_entries_that_link_a_place_in_one(self):
        """Zod's index lists one page per anchor: 326 entries, 17 pages."""
        full = page(0)
        index = "".join(f"- [Place {k}](https://example.com/docs/p0?id=place{k}): a place\n" for k in range(50))
        index += "".join(f"- [Other {k}](https://example.com/docs/llms-{k}.txt): another index\n" for k in range(50))
        code, _out, err = self.run_g("title-join", "sections", "0", corpus=self.files(full, index))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "split into"), [], err)

    def test_an_h1_split_is_compared_too(self):
        full = "# One\nSource: https://example.com/a\n\nbody\n"
        code, _out, err = self.run_g("h1", "sections", "0", corpus=self.files(full, index_for(4)))
        self.assertEqual(code, 0, err)
        self.assertIn("split into 1 pages, but its llms.txt lists 4", self.warning(err, "split into"))

    def test_every_way_the_index_is_at_hand(self):
        full = "".join(page(k) for k in range(2))
        index = index_for(10)
        ways = {
            "title join, cached": ("title-join", None, ["sections", "0"]),
            "title join, files": ("title-join", self.files(full, index), ["sections", "0"]),
            "url join, cached": ("url-join", None, ["sections", "0"]),
            "url join, files": ("url-join", self.files(full, index), ["sections", "0"]),
            "listing, cached": ("url-join", None, ["fetch-index"]),
            "listing, files": ("url-join", self.files(full, index), ["fetch-index"]),
            "search index, cached": ("url-join", None, ["search-index", "page"]),
        }
        self.stage("title-join", full, index)
        self.stage("url-join", full, index)
        for what, (source, corpus, argv) in ways.items():
            with self.subTest(what):
                _code, _out, err = self.run_g(source, *argv, corpus=corpus)
                self.assertEqual(len(self.warnings(err, "split into 2 pages")), 1, err)

    def test_a_command_that_needs_no_index_does_not_download_one_to_compare(self):
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full)  # no index in the cache
        calls = []

        def fake(req, *a, **kw):
            calls.append(getattr(req, "full_url", req))
            raise OSError("test: network disabled")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            code, _out, err = _loader.run_cli(generic, [SCRIPT, "sections", "0", "--source", "url-join",
                                                        "--sources-file", self.sources, "--cache-dir", self.tmp])
        self.assertEqual((code, err, calls), (0, "", []))

    def test_a_stale_cached_index_is_not_compared(self):
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full, index_for(10))
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=("--cache-dir", self.tmp, "--max-age", "999999999"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "split into")), 1, err)
        # an index cached for another URL is another site's
        meta = Path(generic._index_cache_path(self.tmp, self.profile("url-join")) + ".meta.json")
        meta.write_text(json.dumps({"url": "https://other.example/llms.txt"}), encoding="utf-8")
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=("--cache-dir", self.tmp, "--max-age", "999999999"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "split into"), [], err)

    def test_file_without_index_file_does_not_compare_with_the_cache(self):
        """``--file`` is another corpus than the cache: its index is not this file's."""
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full, index_for(10))
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=self.files(full) + ("--cache-dir", self.tmp))
        self.assertEqual((code, err), (0, ""))

    def test_a_missing_index_file_is_left_to_the_command_that_needs_it(self):
        full = "".join(page(k) for k in range(2))
        corpus = self.files(full) + ("--index-file", str(Path(self.tmp, "gone.txt")))
        code, _out, err = self.run_g("url-join", "sections", "0", corpus=corpus)
        self.assertEqual((code, err), (0, ""))
        code, _out, err = self.run_g("url-join", "search", "page", corpus=corpus)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def compared(self, source: str, index: str) -> tuple:
        try:
            return generic._compared_index_urls(self.profile(source), index.splitlines(keepends=True))
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"_compared_index_urls crashed: {e!r}")

    def test_an_index_that_lists_more_of_the_site_than_the_full_text(self):
        """Render's llms.txt lists articles, a blog and a changelog next to the
        docs, and its llms-full.txt is the docs: the pages are compared with
        the URLs under the directory of the profile's url (/docs/) only."""
        try:
            docs = generic.split_documents(RENDER_FULL.splitlines(keepends=True), self.profile("render-like"))
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"the split crashed: {e!r}")
        self.assertEqual([d["title"] for d in docs], RENDER_TITLES)
        entries = {generic.normalize_doc_url(e["url"]) for e in generic.parse_llms_index(RENDER_INDEX.splitlines())}
        self.assertEqual(len(entries), 29, sorted(entries))
        listed, directory = self.compared("render-like", RENDER_INDEX)
        self.assertEqual(directory, "https://render.com/docs/")
        self.assertEqual(len(listed), 11, sorted(listed))
        self.assertEqual({u for u in entries if u.startswith("https://render.com/docs/")}, listed)
        self.assertIn("https://render.com/docs/sandboxes/all", listed)
        # 7 pages for 11 URLs: not "far fewer" (before the rule: 29, and every read warned)
        self.stage("render-like", RENDER_FULL, RENDER_INDEX)
        for what, corpus in (("cached", None), ("files", self.files(RENDER_FULL, RENDER_INDEX))):
            for argv in (["fetch-index", "--compact"], ["sections", "0"], ["search", "zzq"]):
                with self.subTest(what=what, argv=argv):
                    code, _out, err = self.run_g("render-like", *argv, corpus=corpus)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(err, "")

    def test_a_cut_copy_of_such_a_file_is_still_said(self):
        cut = RENDER_FULL[:RENDER_FULL.index("# Render FAQ")]
        self.assertEqual(len(generic.split_documents(cut.splitlines(keepends=True), self.profile("render-like"))), 5)
        code, _out, err = self.run_g("render-like", "sections", "0", corpus=self.files(cut, RENDER_INDEX))
        self.assertEqual(code, 0, err)
        found = self.warning(err, "split into")
        self.assertIn("split into 5 pages, but its llms.txt lists 11 under https://render.com/docs/. The file may be "
                      "broken upstream", found)
        # the count can be redone from what the line says: the entries under the
        # directory it names (a URL that is the directory itself included)
        m = re.search(r"lists (\d+) under (\S+)\. ", found)
        self.assertIsNotNone(m, found)
        count, named = int(m.group(1)), m.group(2)
        entries = {generic.normalize_doc_url(e["url"]) for e in generic.parse_llms_index(RENDER_INDEX.splitlines())}
        self.assertEqual(count, len({u for u in entries if u.startswith(named) or u + "/" == named}))

    def test_the_bundled_render_preset_counts_only_its_docs(self):
        """The fix stands on the bundled preset's url being under /docs/. Render
        serves the same two files at the site root too, and the files name the
        root as their home (2026-11-18): a url moved there would count the whole
        site again, and every read would warn as before 0.57.1."""
        try:
            profile = generic._read_sources_file(generic.PRESETS_FILE)["render"]
            listed, directory = generic._compared_index_urls(profile, RENDER_INDEX.splitlines(keepends=True))
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"comparing with the bundled render preset crashed: {e!r}")
        self.assertEqual((directory, len(listed)), ("https://render.com/docs/", 11))

    def test_the_ratio_counts_the_urls_under_the_directory_of_url(self):
        """The boundary of test_the_ratio_boundary with 100 more entries outside
        the directory of the profile's url (``/docs/``). The index is the
        site-wide llms.txt at the root: its own directory is not the one."""
        full = "".join(page(k) for k in range(5))
        outside = "".join(f"- [Post {k}](https://example.com/blog/post-{k}): post {k}\n" for k in range(100))
        for listed, warned in ((10, False), (11, True)):
            with self.subTest(listed=listed):
                code, _out, err = self.run_g("docs-dir", "sections", "0",
                                             corpus=self.files(full, index_for(listed) + outside))
                self.assertEqual(code, 0, err)
                found = self.warnings(err, "split into")
                self.assertEqual(len(found), int(warned), err)
                if warned:
                    self.assertIn(f"split into 5 pages, but its llms.txt lists {listed} under "
                                  "https://example.com/docs/. The file may", found[0])

    def test_what_is_under_the_directory(self):
        """The host is compared ignoring case, the path as written; the
        directory itself is under it, a sibling sharing its prefix is not."""
        anchor = "https://example.com/docs/anchor"
        cases = {
            "https://example.com/docs/a": True,
            "https://example.com/docs/a/b.md": True,
            "https://example.com/docs/-beta": True,
            "https://example.com/docs/\\_x": True,
            "https://example.com/docs/!": True,
            "https://example.com/docs": True,
            "https://example.com/docs/": True,
            "https://example.com/docs.md": True,
            "https://example.com/docs?lang=en": True,
            "https://example.com/docs#top": True,
            "https://EXAMPLE.com/docs/a": True,
            "https://example.com/docsearch/a": False,
            "https://example.com/doc/a": False,
            "https://example.com/Docs/a": False,
            "https://example.com/": False,
            "https://example.com": False,
            "https://example.com/blog/a": False,
            "http://example.com/docs/a": False,
            "https://other.example/docs/a": False,
            "https://docs.example.com/docs/a": False,
            "https://example.com:8443/docs/a": False,
        }
        for url, under in cases.items():
            with self.subTest(url=url):
                listed, directory = self.compared("docs-dir", f"- [Anchor]({anchor})\n- [Case]({url})\n")
                self.assertEqual(directory, "https://example.com/docs/")
                self.assertIn(anchor, listed)
                self.assertEqual(len(listed), 1 + under, sorted(listed))
                self.assertEqual(generic.normalize_doc_url(url) in listed, under, sorted(listed))

    def test_the_directory_of_the_url(self):
        for url, directory in (("https://render.com/docs/llms-full.txt", "https://render.com/docs/"),
                               ("https://zod.dev/llms-full.txt", "https://zod.dev/"),
                               ("https://developers.openai.com/api/docs/llms-full.txt",
                                "https://developers.openai.com/api/docs/"),
                               ("https://example.com/docs/", "https://example.com/docs/"),
                               ("https://example.com", "https://example.com/"),
                               ("https://example.com/docs/llms-full.txt?v=a/b#c/d", "https://example.com/docs/")):
            with self.subTest(url=url):
                try:
                    got = generic._url_directory(url)
                except Exception as e:  # a crash fails the assertions, not the run
                    self.fail(f"{url}: {e!r}")
                self.assertEqual(got, directory)

    def test_an_index_with_nothing_under_the_directory_is_counted_whole(self):
        """A full text on a mirror: no entry is under its directory, so every
        entry counts (as before the directory rule) and the line names no
        directory. Counting none would leave the check unable to fire."""
        listed, directory = self.compared("mirror", index_for(10))
        self.assertEqual((len(listed), directory), (10, None))
        full = "".join(page(k) for k in range(2))
        code, _out, err = self.run_g("mirror", "sections", "0", corpus=self.files(full, index_for(10)))
        self.assertEqual(code, 0, err)
        self.assertIn("split into 2 pages, but its llms.txt lists 10. The file may be broken upstream",
                      self.warning(err, "split into"))
        code, _out, err = self.run_g("mirror", "sections", "0", corpus=self.files(full, index_for(4)))
        self.assertEqual((code, err), (0, ""))


class FrontmatterCutOffTest(_Case):
    L = generic._FM_LOOKAHEAD

    def full(self, list_lines: int, *, closed: bool = True, after: int = 20) -> str:
        """A page, then a block with a list of *list_lines* items."""
        block = ["---", "title: Long", 'url: "https://example.com/docs/long"', "supportedModels:",
                 *[f"  - model-{n}" for n in range(list_lines)]]
        tail = (["---"] if closed else []) + ["", "body of the long page", *["more text"] * after, ""]
        return page(0) + "\n".join(block + tail)

    def test_the_boundary_of_the_bound(self):
        # the closing line is the block's line 3 + list_lines + 1
        fits = self.L - 5
        for list_lines, warned, pages in ((fits, False, 2), (fits + 1, True, 1), (fits + 200, True, 1)):
            with self.subTest(list_lines=list_lines):
                full = self.full(list_lines)
                docs = generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))
                self.assertEqual(len(docs), pages)
                code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                found = self.warnings(err, "did not close within")
                self.assertEqual(bool(found), warned, err)
                if warned:
                    opened = full.splitlines(keepends=True).index("title: Long\n")
                    self.assertIn("1 frontmatter block(s) of", found[0])
                    self.assertIn(f"{self.L} lines (first near line {opened}) ", found[0])

    def test_a_block_cut_by_the_end_of_the_file_is_not_cut_by_the_bound(self):
        """It ran out of file, not of lookahead: raising the bound changes
        nothing, and a file ending in a rule and a list is common."""
        for list_lines in (self.L - 6, self.L - 5, self.L - 4):
            with self.subTest(list_lines=list_lines):
                full = page(0) + "\n".join(["---", "title: Long", 'url: "https://example.com/l"', "items:",
                                            *[f"  - m{n}" for n in range(list_lines)]]) + "\n"
                total = len(full.splitlines()) - len(page(0).splitlines())
                code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                self.assertEqual(bool(self.warnings(err, "did not close within")), total > self.L, (total, err))

    def test_a_rule_followed_by_a_long_list_is_no_cut_block(self):
        """A horizontal rule and a list with no required key is plain text."""
        rule = "\n".join(["---", "", *[f"- item {n}" for n in range(self.L + 50)], "", "text", ""])
        full = page(0) + rule
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_that_stops_being_yaml_is_no_cut_block(self):
        rule = "\n".join(["---", "title: A note", "", "# Not yaml", *["more text"] * (self.L + 50), ""])
        full = page(0) + rule
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_in_a_code_fence_is_not_looked_at(self):
        block = ["---", "title: Long", 'url: "https://example.com/l"', "items:",
                 *[f"  - m{n}" for n in range(self.L + 20)]]
        full = page(0, "```yaml", *block, "```") + page(1)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))


# A Markdown example of a code block, written with fences of one length: the
# inner closer closes the outer block, and the last line opens a new one,
# which the next block of the file closes
NESTED_SAMPLE = ("A Markdown sample that shows a code block:", "", "```md", "```js", "console.log('hello')",
                 "```", "```")
SAMPLE = "\n".join(NESTED_SAMPLE)

# The three files of the report, as written there (frontmatter / H1 + Source: / Source: lines)
REPRO = {
    "nextjs": (f"""\
---
title: Page 0
url: "https://example.com/docs/p0"
---

## Heading 0

body 0.

---
title: Page 1
url: "https://example.com/docs/p1"
---

## Heading 1

{SAMPLE}

---
title: Page 2
url: "https://example.com/docs/p2"
---

## Heading 2

body 2.

---
title: Page 3
url: "https://example.com/docs/p3"
---

## Heading 3

```ts
const a = 1
```
""", ["Page 0", "Page 1"], 2, 25, 23),
    "bun": (f"""\
# Page 0
Source: https://example.com/docs/p0

body 0.

# Page 1
Source: https://example.com/docs/p1

{SAMPLE}

# Page 2
Source: https://example.com/docs/p2

body 2.

# Page 3
Source: https://example.com/docs/p3

```ts
const a = 1
```
""", ["Page 0", "Page 1"], 2, 17, 15),
    "drizzle": ("""\
Source: https://example.com/docs/p0

# Page 0

body 0.

Source: https://example.com/docs/p1

# Page 1

```md
```js
console.log('hello')
```
```

Source: https://example.com/docs/p2

# Page 2

```ts
const b = 2
```

Source: https://example.com/docs/p3

# Page 3

body 3.
""", ["Page 0", "Page 1", "Page 3"], 1, 17, 15),
}


def link_page(k, *body) -> str:
    """A page of Cloudflare's shape: the URL is the target of a link under the frontmatter."""
    return "\n".join(["---", f"title: Page {k}", "---", "", f"[View as Markdown](https://example.com/docs/p{k}/index.md)",
                      "", f"zzq body {k}.", "", *body, ""])


def vercel_page(k, *body) -> str:
    """A page of Vercel's shape (the frontmatter of vercel.com/docs/llms-full.txt, 2026-10-22):
    an 80-character rule around it, quoted values, the URL under ``source``."""
    rule = "-" * 80
    return "\n".join([rule, f'title: "Page {k}"', f'description: "Welcome to page {k}."',
                      'last_updated: "2026-10-21T19:33:30.737Z"', f'source: "https://example.com/docs/p{k}"', rule,
                      "", f"# Page {k}", "", f"zzq body {k}.", "", *body, ""])


class InsideCodePagesTest(_Case):
    """A page that begins inside a code block (one a nested fence example or a
    fence with no closer left open, until a later block of the file closes
    it) is part of the page before it; the split stands, stderr says how many
    (``_warn_if_split_suspect``). Counted only with the page URL the profile's
    ``page_url`` finds for it, looked for inside the same block."""

    def inside(self, err: str) -> list:
        return self.warnings(err, "begin inside a code block")

    def assert_said(self, err: str, count: int, first: int, opened: int) -> None:
        found = self.inside(err)
        self.assertEqual(len(found), 1, err)
        self.assertIn(f": {count} page(s) of ", found[0])
        self.assertIn(f"(first near line {first}, in a block opened near line {opened}), so each is part of the "
                      "page before it. The file may be broken upstream", found[0])

    def split(self, profile: dict, full: str) -> list:
        try:
            return generic.split_documents(full.splitlines(keepends=True), profile)
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"the split crashed: {e!r}")

    def titles(self, source: str, full: str) -> list:
        return [d["title"] for d in self.split(self.profile(source), full)]

    def counted(self, docs: list) -> list:
        """The page heads the split counted inside code blocks."""
        self.assertTrue(docs)
        self.assertIn("inside_code_pages", docs[0].get("split_notes", {}), docs[0].get("split_notes"))
        return docs[0]["split_notes"]["inside_code_pages"]

    def test_the_three_files_of_the_report(self):
        for source, (full, titles, count, first, opened) in REPRO.items():
            with self.subTest(source=source):
                code, out, err = self.run_g(source, "fetch-index", "--compact", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                self.assertEqual(re.findall(r"^\[\d+\] (.+)$", out, re.M), titles, out)
                self.assertIn(f"({len(titles)} documents total)", out)
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assert_said(err, count, first, opened)
                self.assertIn(str(Path(self.tmp, "full.txt")), self.inside(err)[0])

    def test_a_nested_sample_at_the_end_of_a_page_is_said_by_every_command(self):
        full = page(0) + page(1, *NESTED_SAMPLE) + page(2) + page(3, "```ts", "const a = 1", "```")
        lines = full.splitlines()
        first = lines.index("title: Page 2")  # the line of its "---", counted from 1
        opened = lines.index("console.log('hello')") + 3  # the sample's last fence
        self.assertEqual(self.titles("no-index", full), ["Page 0", "Page 1"])
        self.stage("no-index", full)
        for argv in (["sections", "0"], ["content", "0"], ["search", "zzq"], ["search-content", "zzq"],
                     ["search-index", "zzq"], ["fetch-index"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_g("no-index", *argv)
                self.assertEqual(code, 0, err)
                self.assert_said(err, 2, first, opened)
                self.assertIn(generic._cache_path(self.tmp, self.profile("no-index")), self.inside(err)[0])
                self.assertEqual(self.warnings(err, "never closes"), [], err)

    def test_a_fence_with_no_closer_hides_the_pages_up_to_the_next_block(self):
        full = page(0) + page(1, "```") + page(2) + page(3, "```ts", "x", "```")
        lines = full.splitlines()
        self.assertEqual(self.titles("no-index", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index("title: Page 2"), lines.index("```") + 1)

    def test_a_sample_with_the_key_but_no_page_url_says_nothing(self):
        full = page(0, "```yaml", "---", "title: An example", "description: no url in it", "---", "```") + page(1)
        self.assertEqual(self.titles("no-index", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_whole_page_head_in_a_closed_block_is_counted(self):
        """The cost of the rule: a code block showing a page head with its URL
        (a page about the file format) is said too. None in the bundled
        sources' files."""
        full = page(0, "```yaml", "---", "title: An example", "url: https://example.com/docs/x", "---", "```") + page(1)
        self.assertEqual(self.titles("no-index", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        lines = full.splitlines()
        self.assert_said(err, 1, lines.index("```yaml") + 2, lines.index("```yaml") + 1)

    def test_a_block_is_counted_once_and_its_closer_opens_nothing(self):
        """As outside a block, the line that closes a frontmatter block does
        not open the next one: a second block written right after it is not
        a page head (counting it would say more pages than went missing)."""
        sample = ["```yaml", "---", "title: A", "---", "title: B", "url: https://example.com/docs/b", "---", "```"]
        self.assertEqual(self.titles("no-index", page(0) + "\n".join(sample[1:-1]) + "\n" + page(1)),
                         ["Page 0", "A", "Page 1"])
        full = page(0, *sample) + page(1)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_long_rule_and_another_url_key(self):
        """Vercel's shape: inside a code block too, a frontmatter block is the
        profile's ``frontmatter_delimiter`` (not ``---``) and its URL is under
        the key the profile's ``page_url`` names (not ``url``)."""
        full = vercel_page(0) + vercel_page(1, *NESTED_SAMPLE) + vercel_page(2) + vercel_page(3, "```ts", "x", "```")
        lines = full.splitlines()
        self.assertEqual(self.titles("fm-vercel", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-vercel", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index('title: "Page 2"'), lines.index("console.log('hello')") + 3)

    def test_no_page_url_rule_counts_nothing(self):
        full = page(0) + page(1, *NESTED_SAMPLE) + page(2) + page(3, "```ts", "x", "```")
        self.assertEqual(self.titles("fm-no-url", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-no-url", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_link_rule_looks_for_the_link_in_the_same_block(self):
        # the sample's block closes right after it: the link 7 lines on is the next page's
        outside = link_page(0) + link_page(1, "```md", "---", "title: Example", "---", "```") + link_page(2)
        self.assertEqual(self.titles("fm-link", outside), ["Page 0", "Page 1", "Page 2"])
        code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(outside))
        self.assertEqual((code, err), (0, ""))
        inside = link_page(0) + link_page(1, *NESTED_SAMPLE) + link_page(2) + link_page(3, "```ts", "x", "```")
        lines = inside.splitlines()
        self.assertEqual(self.titles("fm-link", inside), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(inside))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index("title: Page 2"), lines.index("console.log('hello')") + 3)

    def test_a_block_that_closes_the_code_block_has_its_link_outside_it(self):
        """An indented fence line in the frontmatter (a continuation line to
        YAML) closes the code block, as the splitter reads it: the link after
        the frontmatter is no longer in the block, nor is one in a block that
        opens right after it."""
        link = "[View as Markdown](https://example.com/docs/example/index.md)"
        full = link_page(0, "```md", "---", "title: Example", "note: |", "  ```", "---", "", link) + link_page(1)
        self.assertEqual(self.titles("fm-link", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))
        # a new block right after the frontmatter holds a link: not the block the example was in
        full = link_page(0, "```md", "---", "title: Example", "note: |", "  ```", "---", "```bash", link,
                         "```") + link_page(1)
        self.assertEqual(self.titles("fm-link", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))
        # the block closed by a fence of its own after the link: counted
        full = link_page(0, "```md", "---", "title: Example", "note: |", "---", "", link, "```") + link_page(1)
        self.assertEqual(self.titles("fm-link", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.inside(err)), 1, err)
        self.assertEqual(self.warnings(err, "never closes"), [], err)

    def test_the_link_is_looked_for_in_the_lines_a_link_rule_reads(self):
        """``_link_url`` reads 20 lines under the frontmatter: so does the look
        inside the block (the block closes after the last line read)."""
        for gap, counted in ((18, True), (19, True), (20, False)):
            with self.subTest(gap=gap):
                body = ["```md", "---", "title: Example", "---", *["text"] * gap,
                        "[View as Markdown](https://example.com/docs/example/index.md)", "```"]
                full = link_page(0, *body) + link_page(1)
                self.assertEqual(self.titles("fm-link", full), ["Page 0", "Page 1"])
                code, _out, err = self.run_g("fm-link", "sections", "0", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.inside(err)), int(counted), err)

    def test_a_file_that_ends_in_a_code_block_says_only_that(self):
        full = JoinedFencesFileTest.FULL
        # the pages after the open block are counted, and left to the warning that says it
        self.assertEqual(len(self.counted(self.split(self.profile("no-index"), full))), 474 - 9)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)
        self.assertEqual(self.inside(err), [], err)

    def test_with_an_index_one_missing_page_is_said_and_the_ratio_is_not(self):
        full = page(0) + page(1, *NESTED_SAMPLE) + page(2, "```ts", "x", "```") + page(3)
        lines = full.splitlines()
        first, opened = lines.index("title: Page 2"), lines.index("console.log('hello')") + 3
        index = index_for(4)
        self.stage("title-join", full, index)
        self.stage("url-join", full, index)
        ways = {
            "title join, cached": ("title-join", None, ["sections", "0"]),
            "title join, files": ("title-join", self.files(full, index), ["sections", "0"]),
            "url join, cached": ("url-join", None, ["sections", "0"]),
            "listing, cached": ("url-join", None, ["fetch-index"]),
            "listing, files": ("url-join", self.files(full, index), ["fetch-index"]),
            "search index, cached": ("url-join", None, ["search-index", "page"]),
        }
        for what, (source, corpus, argv) in ways.items():
            with self.subTest(what):
                code, _out, err = self.run_g(source, *argv, corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assert_said(err, 1, first, opened)
                self.assertEqual(self.warnings(err, "split into"), [], err)

    def test_it_survives_dropping_empty_pages(self):
        hidden = page(1, *NESTED_SAMPLE) + page(2) + page(3, "```ts", "x", "```")
        full = "---\ntitle: Empty\nurl: https://example.com/e\n---\n\n" + hidden
        self.assertEqual(self.titles("fm-skip-empty", full), ["Page 1"])
        lines = full.splitlines()
        code, _out, err = self.run_g("fm-skip-empty", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index("title: Page 2"), lines.index("console.log('hello')") + 3)
        # an H1 page with no body goes too
        full = ("# Banner\n\n# Page 1\nSource: https://example.com/docs/p1\n\n" + SAMPLE + "\n\n"
                "# Page 2\nSource: https://example.com/docs/p2\n\nbody 2.\n\n```ts\nx\n```\n")
        self.assertEqual(self.titles("h1-source-skip-empty", full), ["Page 1"])
        lines = full.splitlines()
        code, _out, err = self.run_g("h1-source-skip-empty", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 1, lines.index("# Page 2") + 1, lines.index("console.log('hello')") + 3)

    def test_split_auto_reports_only_the_candidate_that_won(self):
        won_fm = page(0) + page(1, *NESTED_SAMPLE) + page(2) + page(3, "```ts", "x", "```")
        lines = won_fm.splitlines()
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(won_fm))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index("title: Page 2"), lines.index("console.log('hello')") + 3)
        # the H1 candidate wins (two Source: lines to one url:); the frontmatter
        # candidate saw a page head with a URL in the yaml block
        won_h1 = ("---\ntitle: Top\nurl: https://example.com/top\n---\n\n"
                  "# One\nSource: https://example.com/a\n\nbody\n\n"
                  "```yaml\n---\ntitle: Example\nurl: https://example.com/x\n---\n```\n\n"
                  "# Two\nSource: https://example.com/b\n\nbody\n")
        candidate = generic._validate_profile(self.sources, "c", {"url": FULL_URL, **SOURCES["auto"]["split_candidates"][0]})
        self.assertEqual(len(self.counted(self.split(candidate, won_h1))), 1)
        try:
            chosen = generic.resolve_split(won_h1.splitlines(keepends=True), self.profile("auto"))[0]
        except Exception as e:  # a crash fails the assertions, not the run
            self.fail(f"the split crashed: {e!r}")
        self.assertEqual(chosen, 1)
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(won_h1))
        self.assertEqual((code, err), (0, ""))

    def test_h1_with_a_source_line(self):
        # the next page's H1 and Source: line inside the block the sample left open
        full = REPRO["bun"][0]
        lines = full.splitlines()
        self.assertEqual(self.titles("h1-source", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("h1-source", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 2, lines.index("# Page 2") + 1, lines.index("console.log('hello')") + 3)
        # a "# comment" in a code block, and the next page's Source: line after the block closed
        full = ("# Page 0\nSource: https://example.com/docs/p0\n\n```powershell\n"
                "# Remove project-specific settings\nRemove-Item x\n```\n\n"
                "# Page 1\nSource: https://example.com/docs/p1\n\nbody\n")
        self.assertEqual(self.titles("h1-source", full), ["Page 0", "Page 1"])
        code, _out, err = self.run_g("h1-source", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_h1_that_is_a_link_and_h1_with_no_page_url(self):
        full = ("# [Page 0](https://example.com/docs/p0)\n\nbody 0.\n\n" + SAMPLE + "\n\n"
                "# [Page 1](https://example.com/docs/p1)\n\nbody 1.\n\n```ts\nx\n```\n")
        lines = full.splitlines()
        self.assertEqual(self.titles("h1-link", full), ["Page 0"])
        code, _out, err = self.run_g("h1-link", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 1, lines.index("# [Page 1](https://example.com/docs/p1)") + 1,
                         lines.index("console.log('hello')") + 3)
        # an H1 in a code block that is no link: a shell comment, as far as the rule can tell
        self.assertEqual(self.titles("h1-no-url", full), ["[Page 0](https://example.com/docs/p0)"])
        code, _out, err = self.run_g("h1-no-url", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_split_line(self):
        full = REPRO["drizzle"][0]
        lines = full.splitlines()
        self.assertEqual(self.titles("line", full), ["Page 0", "Page 1", "Page 3"])
        code, _out, err = self.run_g("line", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assert_said(err, 1, lines.index("Source: https://example.com/docs/p2") + 1,
                         lines.index("console.log('hello')") + 3)
        clean = full.replace("```md\n```js\nconsole.log('hello')\n```\n```\n", "```js\nconsole.log('hello')\n```\n")
        self.assertEqual(self.titles("line", clean), ["Page 0", "Page 1", "Page 2", "Page 3"])
        code, _out, err = self.run_g("line", "sections", "0", corpus=self.files(clean))
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
