"""The ``Next:`` lines that carry a placeholder (``"<query>"``,
``"<heading_path>"``, ``"<heading_path from above>"``, ``<page_ref>``, and
fetch-index's ``sections <page_ref>`` / ``sections <slug>``) keep working
when the reader puts in its place what the line asks for, a word that starts
with ``-`` included: the options come first and every positional after
``--``.

Each line is taken from the output as printed, the placeholder replaced by
the shell-quoted word, split by the shell rules and run; it must parse
(argparse exits 2 on a word it reads as an option) and reach the page or
section it names. Both corpus options are run: the cache (``--cache-dir``)
and ``--file`` with ``--index-file``.

The whole commands the hints name at the end of their line (``Tip:`` /
``Note:`` / ``WARNING:`` and the refusal of a full scan: ``search-content …
--page-ref=<page_ref> -- "<query>"``, ``search-content … -- "<query>"``,
``sections … -- <slug>``, ``sections … -- <page_ref>``) are run the same way,
with nothing added (``HintCommandSwapTest``).
"""

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _commands  # noqa: E402
import test_pixai_preset as pixai
from test_page_fetch import _Net

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"

# A CLI reference whose headings look like options.
DASH_PAGE = (
    "# Hooks\nSource: https://code.claude.com/docs/en/hooks\n\n"
    "Intro.\n\n"
    "## Hook events\nplain\n"
    "## -x\nxword\n"
    "## --limit\nlimitword\n"
    "## ---\nrulerword\n"
    "## -\nbareword\n"
    "## --help\nhelpword\n"
)
# Cut from code.claude.com's llms-full.txt (2026-10-28): a heading written
# with a Markdown escape (``tool\_use``).
REAL_PAGE = (
    "# Error reference\nSource: https://code.claude.com/docs/en/errors\n\n"
    "Look up Claude Code runtime error messages with what each one means and how to fix it.\n\n"
    "## Find your error\n\nMatch the message you see to a section below.\n\n"
    "### tool\\_use.name over 200 characters\n\n"
    "A tool call in the conversation history carries a name longer than the 200 characters "
    "the API accepts in a request:\n"
)
INDEX = ("- [Hooks](https://code.claude.com/docs/en/hooks.md): hook events\n"
         "- [Error reference](https://code.claude.com/docs/en/errors.md): error messages\n")

DASH_HEADINGS = ["-x", "--limit", "---", "-", "--help"]
REAL_HEADING = "Find your error/tool\\_use.name over 200 characters"
# Words a reader may put in place of "<query>": dash words, an escape, a
# function word only, symbols only.
HOSTILE_QUERIES = ["-x", "--limit", "--help", "-", "---", "\\_", "the", "'", "+*"]


class PlaceholderLineSwapTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        full = Path(self.tmp, "claude-code-llms-full.txt")
        full.write_text(DASH_PAGE + "\n" + REAL_PAGE, encoding="utf-8")
        index = Path(self.tmp, "claude-code-llms.txt")
        index.write_text(INDEX, encoding="utf-8")
        for name in (full.name, index.name):
            Path(self.tmp, name + ".meta.json").write_text("{}", encoding="utf-8")
        # a profile that ranks the pages themselves (rank_from "pages"), as
        # a user's sources.json writes one: search-index prints its own line
        sources = Path(self.tmp, "sources.json")
        sources.write_text('{"sources": {"plain": {"url": "https://example.com/llms-full.txt", '
                           '"split": "h1", "page_url": "line:Source: "}}}', encoding="utf-8")
        self.corpora = {
            "--cache-dir": ["--source", "claude-code", "--cache-dir", self.tmp, "--max-age", "999999999"],
            "--file": ["--source", "claude-code", "--file", str(full), "--index-file", str(index)],
            "pages": ["--source", "plain", "--sources-file", str(sources), "--file", str(full)],
        }

    def run_line(self, line: str):
        try:
            argv = shlex.split(line, comments=True)
        except ValueError as exc:
            self.fail(f"not a shell line ({exc}): {line}")
        self.assertEqual(argv[0], SCRIPT, line)
        return _loader.run_cli(generic, argv)

    def next_lines(self, argv, prefix: str) -> list[str]:
        code, out, err = _loader.run_cli(generic, [SCRIPT, *argv])
        self.assertEqual(code, 0, err)
        lines = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: " + prefix)]
        self.assertTrue(lines, out)
        return lines

    def assert_reads(self, line: str, placeholder: str, heading: str):
        self.assertIn(placeholder, line)
        swapped = line.replace(placeholder, shlex.quote(heading))
        code, out, err = self.run_line(swapped)
        self.assertEqual(code, 0, (swapped, err))
        self.assertIn(f"# heading_path: {heading}\n", out, swapped)

    def test_sections_line_takes_a_dash_heading(self):
        for opt, corpus in self.corpora.items():
            for ref, headings in (("0", DASH_HEADINGS), ("1", [REAL_HEADING])):
                (line,) = self.next_lines(["sections", *corpus, ref], f"{SCRIPT} content ")
                self.assertTrue(line.endswith(f' -- {ref} "<heading_path>"'), line)
                for heading in headings:
                    with self.subTest(opt=opt, heading=heading):
                        self.assert_reads(line, '"<heading_path>"', heading)

    def test_subsection_line_takes_a_dash_heading(self):
        # content of a whole page prints the subsection list and its line
        # before and after the body
        for opt, corpus in self.corpora.items():
            lines = self.next_lines(["content", *corpus, "0"], f"{SCRIPT} content ")
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], lines[1])
            for heading in DASH_HEADINGS:
                with self.subTest(opt=opt, heading=heading):
                    self.assert_reads(lines[0], '"<heading_path from above>"', heading)

    def test_truncation_line_takes_a_dash_heading(self):
        for opt, corpus in self.corpora.items():
            code, out, err = _loader.run_cli(generic, [SCRIPT, "content", *corpus, "0", "--max-chars", "20"])
            self.assertEqual(code, 0, err)
            m = re.search(r"chars truncated; narrow with (.+)\)$", out, re.M)
            self.assertIsNotNone(m, out)
            for heading in DASH_HEADINGS:
                with self.subTest(opt=opt, heading=heading):
                    self.assert_reads(m.group(1), '"<heading_path>"', heading)

    def test_search_line_takes_a_dash_query(self):
        # search-index points at search with a "<query>" placeholder
        for opt, corpus in self.corpora.items():
            (line,) = self.next_lines(["search-index", *corpus, "hooks"], f"{SCRIPT} search ")
            self.assertTrue(line.endswith(' -- "<query>"'), line)
            for word in HOSTILE_QUERIES:
                with self.subTest(opt=opt, word=word):
                    swapped = line.replace('"<query>"', shlex.quote(word))
                    code, out, err = self.run_line(swapped)
                    self.assertIn(code, (0, 1), (swapped, err))
                    if code == 0:
                        self.assertIn(f'results for "{word}"', out, swapped)
            with self.subTest(opt=opt, word="limitword"):
                # a query that hits reaches the page that has it
                code, out, err = self.run_line(line.replace('"<query>"', "limitword"))
                self.assertEqual(code, 0, err)
                self.assertRegex(out, r"(?m)^\[0\] Hooks\b")
                self.assertIn("Section: --limit", out)

    def test_placeholder_content_line_takes_a_ref_and_a_dash_heading(self):
        # no page to name (every candidate failed): the line with both
        # placeholders
        for opt, corpus in self.corpora.items():
            hint = tuple(shlex.quote(a) for a in corpus)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(sys, "argv", [SCRIPT]):
                _commands.render_next_content([], hint_args=hint)
            line = out.getvalue().strip().removeprefix("Next: ")
            self.assertTrue(line.endswith(' -- <page_ref> "<heading_path>"'), line)
            for heading in DASH_HEADINGS:
                with self.subTest(opt=opt, heading=heading):
                    self.assert_reads(line.replace("<page_ref>", "0"), '"<heading_path>"', heading)


# fetch-index's rows: "[ref] Title"
_LISTED_RE = re.compile(r"^\[(.+?)\] (.+)$", re.M)
# Words a reader may put in place of a ref: dash words (``-1`` is a negative
# number to argparse), ``--help``, an escape, a function word, symbols.
HOSTILE_REFS = ["-step", "--x", "-1", "-", "---", "--help", "\\_", "the", "'", "+*"]
CODE_FULL_URL = "https://code.claude.com/docs/llms-full.txt"
# a body "fetch" source: llms.txt lists the pages, each fetched on its own
FETCH_INDEX_URL = "https://docs.example.com/llms.txt"
FETCH_PAGES = {"https://docs.example.com/docs/alpha.md": ("Alpha", "# Alpha\n\n## A one\nalpha body\n"),
               "https://docs.example.com/docs/-beta.md": ("Beta", "# Beta\n\n## B one\nbeta body\n")}
# a page no entry of INDEX names (``--file`` with no entry joined)
OTHER_PAGE = "# Elsewhere\nSource: https://code.claude.com/docs/en/elsewhere\n\n## E\nelsewhere body\n"


class FetchIndexLineSwapTest(unittest.TestCase):
    """fetch-index's ``Next: … sections`` line on each of its paths: llms.txt
    entries joined to their pages (``<page_ref>``: cached, ``--file`` +
    ``--index-file``), not joined (``<slug>``: llms-full.txt not cached yet,
    a ``--file`` no entry matches), a profile ranked on its pages (cache and
    ``--file``; the PixAI files cut from the real ones), and a ``body:
    "fetch"`` source. The line, as printed and with each listed ref in place
    of the placeholder, runs (shell rules) and opens that ref's page; a word
    that starts with ``-`` in its place is looked up as a ref (exit 1), not
    read as an option (argparse's exit 2), which the line did before 0.55.0
    (``sections <page_ref> --source …``)."""

    def setUp(self):
        _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = self.write("full.txt", DASH_PAGE + "\n" + REAL_PAGE)
        self.index = self.write("index.txt", INDEX)
        self.other = self.write("other.txt", OTHER_PAGE)
        self.pixai_full = self.write("pixai-full.txt", pixai.FULL)
        self.pixai_index = self.write("pixai-index.txt", pixai.INDEX)
        self.sources = self.write("sources.json", json.dumps({"sources": {
            "plain": {"url": "https://example.com/llms-full.txt", "split": "h1",
                      "page_url": "line:Source: "},
            "fetched": {"body": "fetch", "rank_from": "index", "index_url": FETCH_INDEX_URL,
                        "page_ref_forms": ["int", "url", "slug"]}}}))

    def write(self, name: str, text: str, directory: str | None = None) -> str:
        path = os.path.join(directory or self.tmp, name)
        Path(path).write_text(text, encoding="utf-8")
        return path

    def profile(self, name: str) -> dict:
        path = generic.PRESETS_FILE if name in ("claude-code", "pixai") else self.sources
        return generic._read_sources_file(path)[name]

    def corpora(self) -> dict:
        """label -> (options, placeholder, served URLs, whether a listed ref
        opens its page). Each corpus has a cache dir of its own."""
        out = {}

        def cache() -> str:
            return tempfile.mkdtemp(dir=self.tmp)

        code = self.profile("claude-code")
        c = cache()
        self.write(os.path.basename(generic._cache_path(c, code)), DASH_PAGE + "\n" + REAL_PAGE, c)
        self.write(os.path.basename(generic._index_cache_path(c, code)), INDEX, c)
        out["index, cached"] = (["--source", "claude-code", "--cache-dir", c, "--max-age", "999999999"],
                                "<page_ref>", {}, True)
        out["index, --file"] = (["--source", "claude-code", "--cache-dir", cache(), "--file", self.full,
                                 "--index-file", self.index], "<page_ref>", {}, True)
        c = cache()
        self.write(os.path.basename(generic._index_cache_path(c, code)), INDEX, c)
        # sections fetches llms-full.txt, as the line's note says
        out["index, llms-full.txt not cached"] = (
            ["--source", "claude-code", "--cache-dir", c],
            "<slug>", {CODE_FULL_URL: DASH_PAGE + "\n" + REAL_PAGE}, True)
        out["index, --file no entry matches"] = (
            ["--source", "claude-code", "--cache-dir", cache(), "--file", self.other,
             "--index-file", self.index], "<slug>", {}, False)
        plain = self.profile("plain")
        c = cache()
        self.write(os.path.basename(generic._cache_path(c, plain)), DASH_PAGE + "\n" + REAL_PAGE, c)
        out["pages, cached"] = (["--source", "plain", "--sources-file", self.sources, "--cache-dir", c],
                                "<page_ref>", {}, True)
        out["pages, --file"] = (["--source", "plain", "--sources-file", self.sources, "--cache-dir", cache(),
                                 "--file", self.full], "<page_ref>", {}, True)
        px = self.profile("pixai")
        c = cache()
        self.write(os.path.basename(generic._cache_path(c, px)), pixai.FULL, c)
        self.write(os.path.basename(generic._index_cache_path(c, px)), pixai.INDEX, c)
        out["pixai, cached"] = (["--source", "pixai", "--cache-dir", c], "<page_ref>", {}, True)
        out["pixai, --file"] = (["--source", "pixai", "--cache-dir", cache(), "--file", self.pixai_full,
                                 "--index-file", self.pixai_index], "<page_ref>", {}, True)
        fetched = self.profile("fetched")
        c = cache()
        self.write(os.path.basename(generic._index_cache_path(c, fetched)),
                   "".join(f"- [{title}]({url}): {title} page\n" for url, (title, _b) in FETCH_PAGES.items()), c)
        for url, (_title, body) in FETCH_PAGES.items():
            page = Path(generic._page_cache_path(c, fetched, url))
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(body, encoding="utf-8")
        out["body fetch, cached"] = (["--source", "fetched", "--sources-file", self.sources, "--cache-dir", c],
                                     "<page_ref>", {}, True)
        return out

    def run_words(self, net, argv: list):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            try:
                return _loader.run_cli(generic, argv)
            except Exception as e:  # a crash is a failed check, not an error of the run
                self.fail(f"{shlex.join(argv)} crashed: {e!r}")

    def run_line(self, net, line: str, placeholder: str, value: str | None):
        if value is not None:
            line = line.replace(placeholder, shlex.quote(value))
        try:
            argv = shlex.split(line, comments=True)
        except ValueError as exc:
            self.fail(f"not a shell line ({exc}): {line}")
        self.assertEqual(argv[:2], [SCRIPT, "sections"], line)
        return self.run_words(net, argv)

    def listing(self, net, options: list, extra=()) -> tuple[dict, str]:
        """{ref: title} and the one ``Next:`` line of fetch-index."""
        code, out, err = self.run_words(net, [SCRIPT, "fetch-index", *options, *extra])
        self.assertEqual(code, 0, err)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertEqual(len(nexts), 1, out)
        refs = {}
        for m in _LISTED_RE.finditer(out):
            ref = shlex.split(m.group(1))
            self.assertEqual(len(ref), 1, m.group(0))
            refs[ref[0]] = m.group(2)
        return refs, nexts[0]

    def test_every_path_prints_the_placeholder_last_after_double_dash(self):
        for label, (options, placeholder, served, _opens) in self.corpora().items():
            net = _Net(served)
            with self.subTest(corpus=label):
                _refs, line = self.listing(net, options)
                self.assertTrue(line.endswith(f" -- {placeholder}"), line)
                argv = shlex.split(line, comments=True)
                self.assertEqual(argv.count(placeholder), 1, line)
                self.assertEqual(argv[-2:], ["--", placeholder], line)
                # the corpus options of this run, before "--" (--file makes
                # --cache-dir / --max-age moot, so they go with it)
                given = dict(zip(options[::2], options[1::2]))
                kept = {"--source", "--sources-file", "--index-file", "--file"}
                if "--file" not in given:
                    kept |= {"--cache-dir", "--max-age"}
                for flag, value in given.items():
                    if flag in kept:
                        self.assertIn(flag, argv[:-2], line)
                        self.assertEqual(argv[argv.index(flag) + 1], value, line)
                # the listing branches print the same line
                for extra in (["--compact"], ["--limit", "1"]):
                    self.assertEqual(self.listing(net, options, extra)[1], line, extra)
                self.assertEqual(net.calls, [])

    def test_line_as_printed_and_with_each_listed_ref(self):
        for label, (options, placeholder, served, opens) in self.corpora().items():
            net = _Net(served)
            refs, line = self.listing(net, options)
            self.assertTrue(refs, label)
            with self.subTest(corpus=label, ref=None):
                # as printed: the placeholder is looked up as a ref and named in the error
                code, out, err = self.run_line(net, line, placeholder, None)
                self.assertEqual(code, 1, (line, err))
                self.assertTrue(err.startswith("Error: ") and placeholder in err, err)
            for ref, title in refs.items():
                with self.subTest(corpus=label, ref=ref):
                    code, out, err = self.run_line(net, line, placeholder, ref)
                    if opens:
                        self.assertEqual(code, 0, (line, ref, err))
                        self.assertRegex(out, rf'^Sections in \[\d+\] "{re.escape(title)}"\n')
                    else:
                        # the note says these refs are slugs of llms.txt, not --file pages
                        self.assertEqual(code, 1, (line, ref, err))
                        self.assertTrue(err.startswith("Error: ") and ref in err, err)
            with self.subTest(corpus=label, network=True):
                self.assertEqual(sorted(set(net.calls)), sorted(served))

    def test_a_ref_that_starts_with_a_dash_opens_its_page(self):
        # the slug of docs/-beta.md: before 0.55.0 the line put it before
        # the options, where argparse read it as one
        options, placeholder, served, _opens = self.corpora()["body fetch, cached"]
        net = _Net(served)
        _refs, line = self.listing(net, options)
        code, out, err = self.run_line(net, line, placeholder, "-beta")
        self.assertEqual(code, 0, (line, err))
        self.assertRegex(out, r'^Sections in \[1\] "Beta"\n')
        self.assertEqual(net.calls, [])

    def test_a_dash_word_in_place_of_the_ref_is_a_ref(self):
        for label, (options, placeholder, served, _opens) in self.corpora().items():
            net = _Net(served)
            _refs, line = self.listing(net, options)
            for value in HOSTILE_REFS:
                with self.subTest(corpus=label, value=value):
                    code, out, err = self.run_line(net, line, placeholder, value)
                    self.assertIn(code, (0, 1), (line, value, err))
                    self.assertNotIn("usage:", err)
                    if code == 0:
                        self.assertTrue(out.startswith("Sections in ["), out)
                    else:
                        self.assertTrue(err.startswith("Error: "), err)


# The whole command a hint names, at the end of its line after ": " (the
# zero-hit Tip:, the Note: while llms-full.txt is not cached, the WARNING: of
# --show-sections and the refusal of a full scan). A hint that quotes a
# fragment instead (the form before) has none, so a regression is caught.
_HINT_COMMAND_RE = re.compile(rf"(?m)^(?:Tip|Note|WARNING|Error): .*?: ({re.escape(SCRIPT)} .*)$")
# Cut from zed.dev's llms.txt, robots.txt and three of its pages (2026-11):
# the bundled ``zed`` preset fetches each page, reads robots.txt first and
# searches one page at a time (``search_content_needs_page_ref``).
ZED_INDEX_URL = "https://zed.dev/docs/llms.txt"
ZED_SERVED = {
    ZED_INDEX_URL: (
        "# Zed Docs\n\n> Official Zed documentation index with links to Markdown versions of each docs page.\n\n"
        "## AI\n\n"
        "- [Agents](https://zed.dev/docs/ai/agents.md): Compare Zed Agent, External Agents, and Terminal Threads.\n"
        "- [Tools](https://zed.dev/docs/ai/tools.md): Built-in tools for Zed's AI agent including file editing, "
        "code search, terminal commands, web search, skills, and diagnostics.\n"
        "- [Tool Permissions](https://zed.dev/docs/ai/tool-permissions.md)\n"),
    "https://zed.dev/robots.txt": ("# robots.txt for zed.dev\n\nUser-agent: *\nAllow: /\n\n"
                                   "# Prevent crawling Next.js build artifacts\nDisallow: /_next/static/chunks/app/\n"),
    "https://zed.dev/docs/ai/agents.md": (
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Agents\n\nZed supports three agent paths. Choose the path based on how you want agentic work to run.\n\n"
        "## Thread Types {#thread-types}\n\nThreads are the units shown in the "
        "[Threads Sidebar](./parallel-agents.md#threads-sidebar). Thread types include:\n"),
    "https://zed.dev/docs/ai/tools.md": (
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Tools\n\nZed's built-in agent has access to these tools for reading, searching, and editing your "
        "codebase.\n\n## Read & Search Tools\n\n### `diagnostics`\n"),
    "https://zed.dev/docs/ai/tool-permissions.md": (
        "> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).\n\n"
        "# Tool Permissions\n\nConfigure which [Agent Panel](./agent-panel.md) tools run automatically and "
        "which require your approval.\nFor a list of available tools, [see the Tools page](./tools.md).\n"),
}
# Cut from firebase.google.com's llms.txt, robots.txt and three of its pages
# (2026-11): the bundled ``firebase`` preset fetches each page, reads robots.txt
# first, and its search-content scans every page (no
# ``search_content_needs_page_ref``).
FIREBASE_SERVED = {
    "https://firebase.google.com/robots.txt": ("User-agent: *\nDisallow: /*?*authuser=\nDisallow: /byptwd/\n"
                                               "Sitemap: https://firebase.google.com/sitemap.xml\n"),
    "https://firebase.google.com/docs/llms.txt": (
        "# Documentation\n\nDeveloper documentation for Firebase\n\n## Docs\n\n"
        "- [Create Custom Tokens](https://firebase.google.com/docs/auth/admin/create-custom-tokens.md.txt): "
        "A guide on creating and using custom JSON Web Tokens (JWTs) with the Firebase Admin SDK or a "
        "third-party JWT library to authenticate users or devices in Firebase.\n"
        "- [Deploy targets](https://firebase.google.com/docs/cli/targets.md.txt): A guide to using deploy "
        "targets for managing and deploying to specific Firebase resources, including Hosting sites, "
        "Storage buckets, and Realtime Database instances.\n"
        "- [Users in Firebase Projects](https://firebase.google.com/docs/auth/users.md.txt): A conceptual "
        "overview of users in Firebase projects, including how they are managed and authenticated.\n"),
    "https://firebase.google.com/docs/auth/admin/create-custom-tokens.md.txt": (
        "Firebase gives you complete control over authentication by allowing you to\n"
        "authenticate users or devices using secure JSON Web Tokens (JWTs). You generate\n"
        "these tokens on your server, pass them back to a client device, and then use\n"
        "them to authenticate via the `signInWithCustomToken()` method.\n\n"
        "## Before you begin\n\n"
        "Custom tokens are signed JWTs where the private key used for signing belongs to\n"
        "a Google service account.\n"),
    "https://firebase.google.com/docs/cli/targets.md.txt": (
        "***Deploy targets*** are short-name identifiers (that you define yourself) for\n"
        "Firebase resources in your Firebase project.\n\n"
        "## Set up deploy targets for your Firebase resources\n\n"
        "Using the Firebase CLI, apply a `TARGET_NAME` (short-name identifier that\n"
        "you define yourself) to a Firebase resource or group of Firebase resources.\n"),
    "https://firebase.google.com/docs/auth/users.md.txt": (
        "The Firebase Authentication **user** object represents a user account that has signed\n"
        "up for an app in your\n\nproject. Apps usually have many registered users, and every app in a\n\n"
        "## User properties\n\n"
        "Authentication users have a fixed set of basic properties---a unique\n"
        "ID, a primary email address, a name and a photo URL.\n"),
}
# claude-code's llms.txt with a page whose slug starts with "-"; its
# llms-full.txt is not cached yet, so search-index lists slugs and says so
BETA_INDEX = INDEX + "- [Beta](https://code.claude.com/docs/en/-beta.md): the beta page\n"
BETA_FULL = DASH_PAGE + "\n" + REAL_PAGE + "\n# Beta\nSource: https://code.claude.com/docs/en/-beta\n\n## B\nbeta body\n"


def options_of(argv: list) -> list:
    """The options of a command after its script and subcommand, up to
    ``--``. A positional before them is skipped: a ``Next:`` line writes a
    query that does not start with "-" first (``search-index <query> --source
    …``)."""
    words = argv[2:argv.index("--")] if "--" in argv else argv[2:]
    while words and not words[0].startswith("--"):
        words = words[1:]
    return words


class HintCommandSwapTest(unittest.TestCase):
    """The commands the hints name in their text: the zero-hit ``Tip:`` of
    ``search-index`` / ``search`` on a ``body: "fetch"`` source
    (``search-content … --page-ref=<page_ref> -- "<query>"`` where the source
    searches one page at a time, else ``search-content … -- "<query>"``), the
    ``WARNING:`` of ``search-index --show-sections`` there (``sections … --
    <page_ref>``), the refusal of ``search-content`` without a page
    (``search-content … --page-ref=<page_ref> -- <the query>``) and
    ``search-index``'s ``Note:`` while llms-full.txt is not cached
    (``sections … -- <slug>``).

    Each is a whole command, put together like the ``Next:`` line of its
    output: the script, the subcommand, the same options right after it, the
    placeholders after ``--``. It is taken from the line as printed, only its
    placeholders replaced by the shell-quoted value, split by the shell rules
    and run, nothing added. It reaches the page each listed ref names; a word
    that starts with ``-`` in its place is looked up as a ref (its page, or
    exit 1), not read as an option (argparse's exit 2 or its usage). Before
    0.55.0 the hints quoted fragments (``sections <slug>``), and a fragment
    with ``--`` (``sections -- <slug>``) took no option after it: the options
    a reader adds land after ``--`` (exit 2). The bundled ``zed`` and
    ``firebase`` presets run on files cut from the real ones."""

    def setUp(self):
        _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {"fetched": {
            "body": "fetch", "rank_from": "index", "index_url": FETCH_INDEX_URL,
            "page_ref_forms": ["int", "url", "slug"], "search_content_needs_page_ref": True}}}),
            encoding="utf-8")
        self.zed_index = os.path.join(self.tmp, "zed-llms.txt")
        Path(self.zed_index).write_text(ZED_SERVED[ZED_INDEX_URL], encoding="utf-8")

    def cache(self) -> str:
        return tempfile.mkdtemp(dir=self.tmp)

    def fetch_corpora(self) -> dict:
        """label -> the page-by-page sources: their options (each its own
        cache dir; ``--sources-file``, ``--max-age`` and ``--index-file`` are
        each given to one at least, none the default), served URLs, a word
        every page has, {typed ref: title} and whether ``search-content``
        needs a page."""
        fetched = {FETCH_INDEX_URL: "".join(f"- [{t}]({u}): {t} page\n" for u, (t, _b) in FETCH_PAGES.items()),
                   **{u: body for u, (_t, body) in FETCH_PAGES.items()}}
        return {
            # the slug of docs/-beta.md, typed as it is
            "body fetch": {"options": ["--source", "fetched", "--sources-file", self.sources,
                                       "--cache-dir", self.cache()],
                           "served": fetched, "word": "body", "typed": {"-beta": "Beta"}, "one_page": True},
            "zed, cut from the real files": {
                "options": ["--source", "zed", "--sources-file", self.sources, "--cache-dir", self.cache(),
                            "--max-age", "999999999"],
                "served": ZED_SERVED, "word": "agent", "typed": {}, "one_page": True},
            "zed, --index-file": {
                "options": ["--source", "zed", "--cache-dir", self.cache(), "--index-file", self.zed_index],
                "served": {u: t for u, t in ZED_SERVED.items() if u != ZED_INDEX_URL},
                "word": "agent", "typed": {}, "one_page": True},
            "firebase, cut from the real files": {
                "options": ["--source", "firebase", "--cache-dir", self.cache()],
                "served": FIREBASE_SERVED, "word": "firebase", "typed": {}, "one_page": False},
        }

    def code_corpora(self) -> dict:
        """label -> options of claude-code with only its llms.txt: cached
        (with ``--max-age``), and as ``--index-file`` (each its own cache dir)."""
        code = generic._read_sources_file(generic.PRESETS_FILE)["claude-code"]
        cached = self.cache()
        Path(generic._index_cache_path(cached, code)).write_text(BETA_INDEX, encoding="utf-8")
        index = os.path.join(self.tmp, "claude-code-llms.txt")
        Path(index).write_text(BETA_INDEX, encoding="utf-8")
        return {"llms.txt cached": ["--source", "claude-code", "--cache-dir", cached, "--max-age", "999999999"],
                "--index-file": ["--source", "claude-code", "--cache-dir", self.cache(), "--index-file", index]}

    def run_words(self, net, argv: list):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            try:
                return _loader.run_cli(generic, argv)
            except Exception as e:  # a crash is a failed check, not an error of the run
                self.fail(f"{shlex.join(argv)} crashed: {e!r}")

    def command(self, text: str) -> str:
        """The one command a hint of *text* names, as printed."""
        found = _HINT_COMMAND_RE.findall(text)
        self.assertEqual(len(found), 1, text)
        return found[0]

    def next_options(self, text: str) -> list:
        """The options of the ``Next:`` lines of *text* (the same on each)."""
        found = [options_of(shlex.split(ln[len("Next: "):], comments=True))
                 for ln in text.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(found, text)
        for options in found[1:]:
            self.assertEqual(options, found[0], text)
        return found[0]

    def hints(self, net, corpus: dict) -> dict:
        """hint -> (command, the options of the ``Next:`` lines of its output):
        the zero-hit Tip of search-index and of search, the WARNING of
        search-index --show-sections and, where search-content needs a page,
        the refusal without one (whose output has no ``Next:`` line: those of
        search-index on the same options)."""
        options, word = corpus["options"], corpus["word"]
        out = {}
        for cmd in ("search-index", "search"):
            code, text, err = self.run_words(net, [SCRIPT, cmd, *options, "--", "zzznotthere"])
            self.assertEqual(code, 0, err)
            out[f"{cmd} tip"] = (self.command(text), self.next_options(text))
        code, text, err = self.run_words(net, [SCRIPT, "search-index", *options, "--show-sections", "--", word])
        self.assertEqual(code, 0, err)
        out["--show-sections warning"] = (self.command(err), self.next_options(text))
        if corpus["one_page"]:
            code, _text, err = self.run_words(net, [SCRIPT, "search-content", *options, "--", word])
            self.assertEqual(code, 2, err)
            out["refusal"] = (self.command(err), out["search-index tip"][1])
        return out

    @staticmethod
    def subs(command: str, ref: str, query: str | None) -> dict:
        """The placeholders of *command* and the values that go in their place."""
        out = {holder: ref for holder in ("<page_ref>", "<slug>") if holder in command}
        if query is not None and '"<query>"' in command:
            out['"<query>"'] = query
        return out

    def run_hint(self, net, command: str, subs: dict):
        """*command* as printed, each placeholder of *subs* replaced by the
        shell-quoted value, split by the shell rules and run: nothing added
        (its script name is this script, as on the ``Next:`` lines)."""
        for placeholder, value in subs.items():
            self.assertIn(placeholder, command)
            command = command.replace(placeholder, shlex.quote(value))
        try:
            argv = shlex.split(command, comments=True)
        except ValueError as exc:
            self.fail(f"not a shell line ({exc}): {command}")
        self.assertEqual(argv[0], SCRIPT, command)
        return self.run_words(net, argv)

    def refs(self, net, options: list) -> dict:
        """{ref: title} as fetch-index lists them."""
        code, out, err = self.run_words(net, [SCRIPT, "fetch-index", *options])
        self.assertEqual(code, 0, err)
        refs = {}
        for m in _LISTED_RE.finditer(out):
            ref = shlex.split(m.group(1))
            self.assertEqual(len(ref), 1, m.group(0))
            refs[ref[0]] = m.group(2)
        self.assertTrue(refs, out)
        return refs

    def assert_reaches(self, net, command: str, ref: str, query: str | None, title: str):
        """*command* with *ref* in place of its page reaches that page."""
        code, out, err = self.run_hint(net, command, self.subs(command, ref, query))
        self.assertEqual(code, 0, (command, ref, err))
        if shlex.split(command)[1] == "sections":
            self.assertRegex(out, rf'^Sections in \[\d+\] "{re.escape(title)}"\n')
        else:
            # search-content's header of the page's hits
            self.assertRegex(out, rf"(?m)^\[\d+\] {re.escape(title)}$")

    def assert_is_a_ref(self, net, command: str, value: str, query: str | None):
        """*value* in place of the page is looked up as a ref."""
        code, out, err = self.run_hint(net, command, self.subs(command, value, query))
        self.assertIn(code, (0, 1), (command, value, err))
        self.assertNotIn("usage:", out + err)
        if code == 1:
            self.assertTrue(err.startswith("Error: "), err)

    def assert_corpus_options(self, options: list, hinted: list):
        """Each option of the run (none the default) is on the line, with
        its value."""
        for flag, value in zip(options[::2], options[1::2]):
            self.assertIn(flag, hinted)
            self.assertEqual(hinted[hinted.index(flag) + 1], value, flag)

    def test_each_hint_is_a_whole_command_with_the_options_of_its_next_line(self):
        for label, corpus in self.fetch_corpora().items():
            tip = (["--page-ref=<page_ref>"] if corpus["one_page"] else [], ["<query>"])
            expected = {"search-index tip": ("search-content", *tip), "search tip": ("search-content", *tip),
                        "--show-sections warning": ("sections", [], ["<page_ref>"])}
            if corpus["one_page"]:
                # the query refused, put in already
                expected["refusal"] = ("search-content", ["--page-ref=<page_ref>"], [corpus["word"]])
            hints = self.hints(_Net(corpus["served"]), corpus)
            self.assertEqual(sorted(hints), sorted(expected), label)
            for name, (command, next_options) in hints.items():
                with self.subTest(corpus=label, hint=name):
                    subcommand, extra, after = expected[name]
                    self.assertEqual(shlex.split(command, comments=True),
                                     [SCRIPT, subcommand, *next_options, *extra, "--", *after])
                    self.assert_corpus_options(corpus["options"], next_options)
        for label, options in self.code_corpora().items():
            net = _Net({CODE_FULL_URL: BETA_FULL})
            code, out, err = self.run_words(net, [SCRIPT, "search-index", *options, "--", "hooks"])
            self.assertEqual(code, 0, err)
            with self.subTest(corpus=label, hint="note"):
                next_options = self.next_options(out)
                self.assertEqual(shlex.split(self.command(out), comments=True),
                                 [SCRIPT, "sections", *next_options, "--", "<slug>"])
                self.assert_corpus_options(options, next_options)
            self.assertEqual(net.calls, [])

    def test_the_command_names_the_script_as_the_next_line_does(self):
        # run by its path: both name the file the reader ran (its basename)
        corpus = self.fetch_corpora()["zed, cut from the real files"]
        path = os.path.join(self.tmp, "bin", SCRIPT)
        code, out, err = self.run_words(_Net(corpus["served"]),
                                        [path, "search-index", *corpus["options"], "--", "zzznotthere"])
        self.assertEqual(code, 0, err)
        nexts = [ln for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        for line in nexts:
            self.assertTrue(line.startswith(f"Next: {SCRIPT} search "), line)
        self.assertTrue(self.command(out).startswith(f"{SCRIPT} search-content "), out)

    def test_each_hint_with_each_listed_ref_and_a_dash_word(self):
        for label, corpus in self.fetch_corpora().items():
            net = _Net(corpus["served"])
            refs = self.refs(net, corpus["options"])
            for name, (command, _options) in self.hints(net, corpus).items():
                if "<page_ref>" not in command:
                    continue  # the Tip of a source that scans every page names no page
                for ref, title in {**refs, **corpus["typed"]}.items():
                    with self.subTest(corpus=label, hint=name, ref=ref):
                        self.assert_reaches(net, command, ref, corpus["word"], title)
                for value in HOSTILE_REFS:
                    with self.subTest(corpus=label, hint=name, value=value):
                        self.assert_is_a_ref(net, command, value, corpus["word"])

    def test_the_tips_take_a_dash_query_in_place_of_their_query(self):
        for label, corpus in self.fetch_corpora().items():
            net = _Net(corpus["served"])
            titles = self.refs(net, corpus["options"]).values()
            hints = self.hints(net, corpus)
            for name in ("search-index tip", "search tip"):
                command = hints[name][0]
                if "<page_ref>" not in command:
                    with self.subTest(corpus=label, hint=name, query=corpus["word"]):
                        # over every page: each has the word
                        code, out, err = self.run_hint(net, command, {'"<query>"': corpus["word"]})
                        self.assertEqual(code, 0, err)
                        for title in titles:
                            self.assertRegex(out, rf"(?m)^\[\d+\] {re.escape(title)}$")
                for query in HOSTILE_QUERIES:
                    with self.subTest(corpus=label, hint=name, query=query):
                        code, out, err = self.run_hint(net, command, self.subs(command, "0", query))
                        self.assertEqual(code, 0, (command, query, err))
                        self.assertIn(f'results for "{query}"', out)

    def test_the_refusal_names_the_refused_query(self):
        # only the page is left to put in: the query is the one refused, as
        # the shell gave it (quoted, after "--")
        for label, corpus in self.fetch_corpora().items():
            if not corpus["one_page"]:
                continue
            net = _Net(corpus["served"])
            word = corpus["word"]
            for query in (word, f"it's an {word}", "--install", "-x", "\\_"):
                with self.subTest(corpus=label, query=query):
                    code, _out, err = self.run_words(net, [SCRIPT, "search-content", *corpus["options"], "--", query])
                    self.assertEqual(code, 2, err)
                    code, out, err = self.run_hint(net, self.command(err), {"<page_ref>": "0"})
                    self.assertEqual(code, 0, err)
                    self.assertIn(f'results for "{query}"', out)

    def test_a_refused_query_with_a_newline_stays_a_placeholder(self):
        # the quoted query would split the line the command is copied from
        # ("-- 'agent" on one line, "x'" on the next)
        for label, corpus in self.fetch_corpora().items():
            if not corpus["one_page"]:
                continue
            net = _Net(corpus["served"])
            word = corpus["word"]
            with self.subTest(corpus=label):
                code, _out, err = self.run_words(net, [SCRIPT, "search-content", *corpus["options"], "--", f"{word}\nx"])
                self.assertEqual(code, 2, err)
                refusal = [ln for ln in err.splitlines() if ln.startswith("Error: ")]
                self.assertEqual(len(refusal), 1, err)
                self.assertTrue(refusal[0].endswith(' -- "<query>"'), err)
                code, out, err = self.run_hint(net, self.command(err), {"<page_ref>": "0", '"<query>"': word})
                self.assertEqual(code, 0, err)
                self.assertIn(f'results for "{word}"', out)

    def test_the_note_with_each_listed_slug_and_a_dash_word(self):
        for label, options in self.code_corpora().items():
            # sections fetches llms-full.txt, as the note says
            net = _Net({CODE_FULL_URL: BETA_FULL})
            refs = self.refs(net, options)
            self.assertIn("en/-beta", refs)
            code, out, err = self.run_words(net, [SCRIPT, "search-index", *options, "--", "hooks"])
            self.assertEqual(code, 0, err)
            command = self.command(out)
            # the slug as listed, and as typed (-beta)
            for ref, title in {**refs, "-beta": "Beta"}.items():
                with self.subTest(corpus=label, ref=ref):
                    self.assert_reaches(net, command, ref, None, title)
            for value in HOSTILE_REFS:
                with self.subTest(corpus=label, value=value):
                    self.assert_is_a_ref(net, command, value, None)
            self.assertEqual(sorted(set(net.calls)), [CODE_FULL_URL])


if __name__ == "__main__":
    unittest.main()
