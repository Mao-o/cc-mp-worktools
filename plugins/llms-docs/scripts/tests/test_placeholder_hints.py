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

The command fragments the hints quote (``Tip:`` / ``Note:`` / ``WARNING:``
and the refusal of a full scan: ``search-content --page-ref=<page_ref> --
"<query>"``, ``sections -- <slug>``, ``sections -- <page_ref>``,
``--page-ref=<page_ref>``) are run the same way (``HintFragmentSwapTest``).
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


# A command a hint quotes with a placeholder ('sections -- <slug>'), and the
# option the refusal of a full scan asks for (--page-ref=<page_ref>). The
# patterns also take the forms before 0.55.0, so a regression is run, not missed.
_QUOTED_HINT_RE = re.compile(r"'([^']*<(?:page_ref|slug)>[^']*)'")
_PAGE_REF_OPTION_RE = re.compile(r"--page-ref[= ]<page_ref>")
# Cut from zed.dev's llms.txt, robots.txt and three of its pages (2026-11):
# the bundled ``zed`` preset fetches each page, reads robots.txt first and
# searches one page at a time (``search_content_needs_page_ref``).
ZED_SERVED = {
    "https://zed.dev/docs/llms.txt": (
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
# claude-code's llms.txt with a page whose slug starts with "-"; its
# llms-full.txt is not cached yet, so search-index lists slugs and says so
BETA_INDEX = INDEX + "- [Beta](https://code.claude.com/docs/en/-beta.md): the beta page\n"
BETA_FULL = DASH_PAGE + "\n" + REAL_PAGE + "\n# Beta\nSource: https://code.claude.com/docs/en/-beta\n\n## B\nbeta body\n"


class HintFragmentSwapTest(unittest.TestCase):
    """The commands the hints quote with a ``<page_ref>`` / ``<slug>``
    placeholder: the zero-hit ``Tip:`` of ``search-index`` / ``search`` on a
    source that searches one page at a time (``search-content
    --page-ref=<page_ref> -- "<query>"``), the ``WARNING:`` of
    ``search-index --show-sections`` on a ``body: "fetch"`` source
    (``sections -- <page_ref>``), the refusal of ``search-content`` without a
    page (``--page-ref=<page_ref>``) and ``search-index``'s ``Note:`` while
    llms-full.txt is not cached (``sections -- <slug>``).

    Each fragment is taken from the output as printed, its placeholders
    replaced by the shell-quoted value, split by the shell rules and run with
    the script name before it and the options of the run that printed it
    right after the subcommand, where the ``Next:`` lines put them (the
    refusal's option goes into the refused command). It reaches the page
    each listed ref names; a word that starts with ``-`` in its place is
    looked up as a ref (its page, or exit 1), not read as an option
    (argparse's exit 2 or its usage), as ``--page-ref <page_ref>`` and
    ``sections <slug>`` were before 0.55.0. The bundled ``zed`` preset runs
    on files cut from the real ones."""

    def setUp(self):
        _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": {"fetched": {
            "body": "fetch", "rank_from": "index", "index_url": FETCH_INDEX_URL,
            "page_ref_forms": ["int", "url", "slug"], "search_content_needs_page_ref": True}}}),
            encoding="utf-8")

    def fetch_corpora(self) -> dict:
        """label -> (options, served URLs, a word every page has, {typed ref: title})."""
        fetched = {FETCH_INDEX_URL: "".join(f"- [{t}]({u}): {t} page\n" for u, (t, _b) in FETCH_PAGES.items()),
                   **{u: body for u, (_t, body) in FETCH_PAGES.items()}}
        return {
            # the slug of docs/-beta.md, typed as it is
            "body fetch": (["--source", "fetched", "--sources-file", self.sources,
                            "--cache-dir", tempfile.mkdtemp(dir=self.tmp)], fetched, "body", {"-beta": "Beta"}),
            "zed, cut from the real files": (["--source", "zed", "--sources-file", self.sources,
                                              "--cache-dir", tempfile.mkdtemp(dir=self.tmp)], ZED_SERVED, "agent", {}),
        }

    def run_words(self, net, argv: list):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            try:
                return _loader.run_cli(generic, argv)
            except Exception as e:  # a crash is a failed check, not an error of the run
                self.fail(f"{shlex.join(argv)} crashed: {e!r}")

    def fragment(self, text: str, pattern) -> str:
        found = pattern.findall(text)
        self.assertEqual(len(found), 1, text)
        return found[0]

    def words(self, fragment: str, subs: dict) -> list:
        for placeholder, value in subs.items():
            self.assertIn(placeholder, fragment)
            fragment = fragment.replace(placeholder, shlex.quote(value))
        try:
            return shlex.split(fragment, comments=True)
        except ValueError as exc:
            self.fail(f"not a shell line ({exc}): {fragment}")

    def run_hint(self, net, fragment: str, options: list, ref: str, query: str | None):
        """*fragment* with *ref* in place of its page (and *query* in place
        of ``"<query>"``) run as the hint says."""
        if fragment.startswith("--"):
            # the option the refused command asks for, before its query
            words = self.words(fragment, {"<page_ref>": ref})
            return self.run_words(net, [SCRIPT, "search-content", *options, *words, "--", query])
        subs = {"<slug>" if "<slug>" in fragment else "<page_ref>": ref}
        if '"<query>"' in fragment:
            subs['"<query>"'] = query
        words = self.words(fragment, subs)
        return self.run_words(net, [SCRIPT, words[0], *options, *words[1:]])

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

    def fetch_hints(self, net, options: list) -> dict:
        """The fragments the four outputs of a page-by-page source print."""
        out = {}
        for cmd in ("search-index", "search"):
            code, text, err = self.run_words(net, [SCRIPT, cmd, *options, "--", "zzznotthere"])
            self.assertEqual(code, 0, err)
            out[f"{cmd} tip"] = self.fragment(text, _QUOTED_HINT_RE)
        code, _text, err = self.run_words(net, [SCRIPT, "search-index", *options, "--show-sections", "--", "agent"])
        self.assertEqual(code, 0, err)
        out["--show-sections warning"] = self.fragment(err, _QUOTED_HINT_RE)
        code, text, err = self.run_words(net, [SCRIPT, "search-content", *options, "--", "agent"])
        self.assertEqual(code, 2, err)
        out["refusal"] = self.fragment(err, _PAGE_REF_OPTION_RE)
        return out

    def assert_reaches(self, net, fragment, options, query, ref, title):
        """*fragment* with *ref* in place of its page reaches that page."""
        code, out, err = self.run_hint(net, fragment, options, ref, query)
        self.assertEqual(code, 0, (fragment, ref, err))
        if fragment.startswith("sections"):
            self.assertRegex(out, rf'^Sections in \[\d+\] "{re.escape(title)}"\n')
        else:
            # search-content's header of the page's hits
            self.assertRegex(out, rf"(?m)^\[\d+\] {re.escape(title)}$")

    def assert_is_a_ref(self, net, fragment, options, query, value):
        """*value* in place of the page is looked up as a ref."""
        code, out, err = self.run_hint(net, fragment, options, value, query)
        self.assertIn(code, (0, 1), (fragment, value, err))
        self.assertNotIn("usage:", out + err)
        if code == 1:
            self.assertTrue(err.startswith("Error: "), err)

    def test_every_hint_puts_its_placeholder_after_double_dash_or_joins_it_to_its_option(self):
        for label, (options, served, _word, _typed) in self.fetch_corpora().items():
            with self.subTest(corpus=label):
                hints = self.fetch_hints(_Net(served), options)
                self.assertEqual(hints, {
                    "search-index tip": 'search-content --page-ref=<page_ref> -- "<query>"',
                    "search tip": 'search-content --page-ref=<page_ref> -- "<query>"',
                    "--show-sections warning": "sections -- <page_ref>",
                    "refusal": "--page-ref=<page_ref>"})
        net = _Net({CODE_FULL_URL: BETA_FULL})
        code, out, err = self.run_words(net, [SCRIPT, "search-index", *self.code_options(), "--", "hooks"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fragment(out, _QUOTED_HINT_RE), "sections -- <slug>")
        self.assertEqual(net.calls, [])

    def test_each_fetch_hint_with_each_listed_ref_and_a_dash_word(self):
        for label, (options, served, word, typed) in self.fetch_corpora().items():
            net = _Net(served)
            refs = self.refs(net, options)
            for hint, fragment in self.fetch_hints(net, options).items():
                for ref, title in {**refs, **typed}.items():
                    with self.subTest(corpus=label, hint=hint, ref=ref):
                        self.assert_reaches(net, fragment, options, word, ref, title)
                for value in HOSTILE_REFS:
                    with self.subTest(corpus=label, hint=hint, value=value):
                        self.assert_is_a_ref(net, fragment, options, word, value)

    def test_the_tip_takes_a_dash_query_in_place_of_its_query(self):
        for label, (options, served, _word, _typed) in self.fetch_corpora().items():
            net = _Net(served)
            fragment = self.fetch_hints(net, options)["search-index tip"]
            for query in HOSTILE_QUERIES:
                with self.subTest(corpus=label, query=query):
                    code, out, err = self.run_hint(net, fragment, options, "0", query)
                    self.assertEqual(code, 0, (fragment, query, err))
                    self.assertIn(f'results for "{query}"', out)

    def code_options(self) -> list:
        """claude-code with only its llms.txt cached."""
        if not hasattr(self, "_code_cache"):
            self._code_cache = tempfile.mkdtemp(dir=self.tmp)
            code = generic._read_sources_file(generic.PRESETS_FILE)["claude-code"]
            Path(generic._index_cache_path(self._code_cache, code)).write_text(BETA_INDEX, encoding="utf-8")
        return ["--source", "claude-code", "--cache-dir", self._code_cache]

    def test_the_note_with_each_listed_slug_and_a_dash_word(self):
        # sections fetches llms-full.txt, as the note says
        net = _Net({CODE_FULL_URL: BETA_FULL})
        options = self.code_options()
        refs = self.refs(net, options)
        self.assertIn("en/-beta", refs)
        code, out, err = self.run_words(net, [SCRIPT, "search-index", *options, "--", "hooks"])
        self.assertEqual(code, 0, err)
        fragment = self.fragment(out, _QUOTED_HINT_RE)
        # the slug as listed, and as typed (-beta)
        for ref, title in {**refs, "-beta": "Beta"}.items():
            with self.subTest(ref=ref):
                self.assert_reaches(net, fragment, options, None, ref, title)
        for value in HOSTILE_REFS:
            with self.subTest(value=value):
                self.assert_is_a_ref(net, fragment, options, None, value)
        self.assertEqual(sorted(set(net.calls)), [CODE_FULL_URL])


if __name__ == "__main__":
    unittest.main()
