"""Explicit heading ids: ``## Foo {#foo}`` (mdBook / VitePress / Docusaurus /
Pandoc) and ``## Foo [#foo]`` / ``## Foo \\[#foo]`` (fumadocs).

The id is not part of what the page shows, so ``split_heading_id`` takes it
off where the headings are read (``extract_sections``, the index headings,
the page titles): ``sections``, ``Section:`` / ``Next:`` lines, hints and
errors name ``Overview/Checkpoints``. The id becomes the section's anchor,
and ``content`` still opens a section by the old text (``Overview
{#overview}/Checkpoints {#checkpoints}``) and by the id itself. The page body
is printed and searched as written.

The CLI fixtures are cut from real pages (Zed's ``docs/ai/agent-panel.md``
fetched 2026-11-03, with one in-page link to ``#checkpoints`` added; the
Agent Plugins and Zod ``llms-full.txt`` fetched 2026-11-02 and 2026-10-15)
and run through the bundled presets: the Zed page on a mocked network, on
the cache that leaves and on ``--file``, the others on ``--file``. Every
``Next:`` line runs as the shell splits it.
"""

import contextlib
import io
import itertools
import json
import re
import shlex
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import _loader
from test_index_heading_level import VITEST_VI_API_H3
from test_page_fetch import _Net

import _commands
import _common

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"


class SplitHeadingIdTest(unittest.TestCase):
    STRIPPED = {
        "Overview {#overview}": ("Overview", "overview"),
        "Token Usage and Compaction {#token-usage}": ("Token Usage and Compaction", "token-usage"),
        "Foo {#id .c k=v}": ("Foo", "id"),
        'Foo {#id title="a b" .c}': ("Foo", "id"),
        "Foo {.c}": ("Foo", None),
        "Foo {.c #second}": ("Foo", "second"),
        "Foo {#first #second}": ("Foo", "first"),
        "Foo {: #ial-id}": ("Foo", "ial-id"),
        "Foo {:#ial-id}": ("Foo", "ial-id"),
        "Foo\t{#tab}": ("Foo", "tab"),
        "Foo {#id}  ": ("Foo", "id"),
        "Why Agent Plugins? [#why-agent-plugins]": ("Why Agent Plugins?", "why-agent-plugins"),
        "Stringbools \\[#stringbool]": ("Stringbools", "stringbool"),
        "`z.deepPartial()` \\[#deep-partial]": ("`z.deepPartial()`", "deep-partial"),
        "Foo \\[#escaped\\]": ("Foo", "escaped"),
        "エディターやエージェントに接続する [#エディターやエージェントに接続する]":
            ("エディターやエージェントに接続する", "エディターやエージェントに接続する"),
        "見出し {#日本語-id}": ("見出し", "日本語-id"),
        "Terminal: Detect Virtual Environments {#terminal-detect_venv}":
            ("Terminal: Detect Virtual Environments", "terminal-detect_venv"),
        "A {.a} {#b}": ("A {.a}", "b"),
    }
    KEPT = [
        "GET https://example.com/v1/media/{mediaId}",
        "GET {mediaId}",
        "Each blocks {#each items as item}",
        "Foo {#each ...}",
        "Foo {#if x}",
        "Foo {props.name}",
        "Foo {{#each}}",
        "Foo {# c #}",
        "{#only-id}",
        "[#only-id]",
        "Foo{#id}",
        "Foo[#id]",
        "Foo {#x} bar",
        "Issue [#123](https://example.com/issues/123)",
        "Foo {=html}",
        "Foo {}",
        "Foo {#}",
        "Foo {: }",
        "Foo [#]",
        "Foo [#a b]",
        "Foo {.a.b}",
        "Foo {k=}",
        "Foo {#a}}",
        "object {response, tool_calls, usage }",
        "Plain heading",
        "",
    ]

    def test_takes_off_a_trailing_id_block(self):
        for title, want in self.STRIPPED.items():
            with self.subTest(title=title):
                self.assertEqual(_common.split_heading_id(title), want)

    def test_keeps_braces_and_brackets_that_are_not_an_id_block(self):
        for title in self.KEPT:
            with self.subTest(title=title):
                self.assertEqual(_common.split_heading_id(title), (title, None))

    def test_time_grows_linearly(self):
        """Token runs that never close, and blocks that never end the title:
        the time for 4000 units stays within 8x of 1000 (quadratic would be
        16x). Each time is the best of three."""
        units = {"tokens with no closing brace": ("Foo {", "#a .b k=v "),
                 "blocks in the middle": ("Foo", " {#a} x"),
                 "brackets in the middle": ("Foo", " [#a] x"),
                 "spaces and braces": ("Foo", " { "),
                 "key=value runs": ("Foo {#a ", "k=a=a=a "),
                 "quoted values never closed": ("Foo {#a ", 'k="a b '),
                 "one long word of ids": ("Foo {", "#a#a#a#a"),
                 "one long word of brackets": ("Foo [", "#a[#a"),
                 "long gaps between tokens": ("Foo {#a", "      #a"),
                 "a long title before a block": ("", "word ")}
        for label, (head, unit) in units.items():
            with self.subTest(label=label):
                times = []
                for n in (1000, 4000):
                    title = head + unit * n + " {#tail"  # never a block at the end
                    self.assertEqual(_common.split_heading_id(title), (title, None))
                    best = None
                    for _ in range(3):
                        start = time.perf_counter()
                        for _ in range(3):
                            _common.split_heading_id(title)
                        took = time.perf_counter() - start
                        best = took if best is None else min(best, took)
                    times.append(best)
                self.assertLess(times[1], max(times[0], 0.002) * 8, (label, times))


ZED_PAGE = """\
> For the complete documentation index and Markdown links, see [llms.txt](/docs/llms.txt).

# Agent Panel

The Agent Panel is where you interact with AI agents that can read, write, and run code in your project.

## Getting Started {#getting-started}

If you're using the Agent Panel for the first time, configure either a model for the [Zed Agent](./zed-agent.md) or an [External Agent](./external-agents.md).

## Overview {#overview}

With an LLM provider or External Agent configured, type in the message editor and press `enter` to submit.
The sections below cover what you can do from here. To go back to an earlier state, see [Checkpoints](#checkpoints).

> Note that some Agent Panel features may not be available for every External Agent. Restoring threads from history, checkpoints, token usage display, and similar features depend on the agent integration.

### Queueing Messages

Messages sent while the agent is in the generating state get, by default, queued.

### Checkpoints {#checkpoints}

Every time the model performs an edit, you should see a "Restore Checkpoint" button at the top of your message, allowing you to return your code base to the state it was in prior to that message.

The checkpoint button appears even if you interrupt the thread midway through an edit, as this is likely a moment when you've identified that the agent is not heading in the right direction and you want to revert back.

## Token Usage and Compaction {#token-usage}

Zed surfaces how many tokens you are consuming for your currently active thread near the profile selector in the panel's message editor.

Configure automatic compaction with `agent.auto_compact`. See [Agent Settings](./agent-settings.md#automatic-compaction) for options.

## Feedback {#feedback}

You can rate agent responses to help improve Zed's system prompt and tools.
"""
ZED_URL = "https://zed.dev/docs/ai/agent-panel.md"
ZED_INDEX_URL = "https://zed.dev/docs/llms.txt"
ZED_INDEX = f"# Zed\n\n> Docs.\n\n- [Agent Panel]({ZED_URL}): The Agent Panel.\n"
ZED_ROBOTS = "User-agent: *\nAllow: /\n"
ZED_SECTIONS = ["[L2] Getting Started", "[L2] Overview", "  [L3] Overview/Queueing Messages",
                "  [L3] Overview/Checkpoints", "[L2] Token Usage and Compaction", "[L2] Feedback"]
CHECKPOINTS_BODY = "### Checkpoints {#checkpoints}\n"


class _Corpora(unittest.TestCase):
    """The Zed page on the bundled ``zed`` preset (a mocked network, then the
    cache it left) and on ``--file`` (one H1 page of a ``split: h1``
    profile), with and without ``anchor_style``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = json.loads(Path(g.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]["zed"]
        self.assertEqual(raw["body"], "fetch")
        file_profile = {"url": "https://example.com/llms-full.txt", "split": "h1", "page_url": "line:Source: ",
                        "min_heading_level": 2, "page_ref_forms": raw["page_ref_forms"]}
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": {
            "zeda": {**raw, "anchor_style": "github"},
            "zedfile": file_profile,
            "zedfilea": {**file_profile, "anchor_style": "github"},
        }}), encoding="utf-8")
        self.file = Path(self.tmp, "local-llms-full.txt")
        page = ZED_PAGE.split("\n# Agent Panel\n", 1)[1]
        self.file.write_text(f"# Agent Panel\nSource: {ZED_URL}\n{page}", encoding="utf-8")
        self.served = {ZED_INDEX_URL: ZED_INDEX, "https://zed.dev/robots.txt": ZED_ROBOTS, ZED_URL: ZED_PAGE}

    def corpora(self, anchors: bool = False):
        """label -> (option words, network). "cache" reads what "fetch" left,
        on a network that serves nothing."""
        out = {}
        cache = Path(self.tmp, "cache-a" if anchors else "cache")
        cache.mkdir(exist_ok=True)
        fetch_source = "zeda" if anchors else "zed"
        common = ["--sources-file", str(self.sources), "--cache-dir", str(cache)]
        out["fetch"] = (["--source", fetch_source, *common], _Net(self.served))
        out["cache"] = (["--source", fetch_source, *common], _Net({}))
        out["file"] = (["--source", "zedfilea" if anchors else "zedfile", *common, "--file", str(self.file)],
                       _Net({}))
        return out

    def run_cli(self, net, argv):
        try:
            with mock.patch("urllib.request.urlopen", side_effect=net):
                return _loader.run_cli(g, argv)
        except Exception as e:  # noqa: BLE001  (a broken step fails the test, not errors it)
            self.fail(f"{argv}: {type(e).__name__}: {e}")

    def run_line(self, net, line, replace=None):
        """Run a ``Next:`` line as the shell splits it; *replace* maps a word
        of it to the words that stand in for it."""
        words = shlex.split(line, comments=True)
        self.assertEqual(words[:2], ["Next:", SCRIPT], line)
        argv = []
        for w in words[1:]:
            argv += replace[w] if replace and w in replace else [w]
        return self.run_cli(net, argv)

    @staticmethod
    def next_lines(out):
        return [ln for ln in out.splitlines() if ln.startswith("Next:")]


class ZedFormCliTest(_Corpora):
    def test_sections_name_the_headings_without_their_ids_and_each_row_opens_itself(self):
        for label, (opts, net) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "sections", *opts, "--", "0"])
                self.assertEqual(code, 0, err)
                self.assertEqual([ln for ln in out.splitlines() if re.match(r"^\s*\[L\d\] ", ln)], ZED_SECTIONS)
                self.assertNotIn("{#", out)
                nxt = self.next_lines(out)
                self.assertEqual(len(nxt), 1, out)
                for row in ZED_SECTIONS:
                    path = re.sub(r"^\s*\[L\d\] ", "", row)
                    code, got, err = self.run_line(net, nxt[0], {'<heading_path>': [path]})
                    self.assertEqual(code, 0, f"{path}\n{err}")
                    self.assertIn(f"# heading_path: {path}\n", got)
                    self.assertNotIn("{#", got.split("\n---\n", 1)[0])
            if label == "fetch":
                self.assertEqual(sorted(set(net.calls)), sorted(self.served))

    def test_search_content_names_the_section_and_its_next_line_opens_it(self):
        for label, (opts, net) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "search-content", *opts, "--page-ref", "0", "--",
                                                    "checkpoint"])
                self.assertEqual(code, 0, err)
                self.assertEqual(re.findall(r"(?m)^    Section: (.*?)  \(x\d+\)", out)[:1], ["Overview/Checkpoints"])
                self.assertNotIn("{#", "\n".join(ln for ln in out.splitlines() if not ln.startswith("      ")))
                nxt = self.next_lines(out)
                self.assertTrue(nxt, out)
                self.assertEqual(shlex.split(nxt[0], comments=True)[2:5], ["content", "0", "Overview/Checkpoints"])
                for line in nxt:
                    code, got, err = self.run_line(net, line)
                    self.assertEqual(code, 0, f"{line}\n{err}")
                    words = shlex.split(line, comments=True)
                    self.assertEqual(words[2], "content", line)
                    self.assertIn(f"# heading_path: {words[4]}\n", got)
                code, got, err = self.run_line(net, nxt[0])
                self.assertIn(CHECKPOINTS_BODY, got)

    def test_every_name_of_the_section_opens_it(self):
        names = ["Overview/Checkpoints", "overview/checkpoints", "Checkpoints", "checkpoints",
                 "Overview {#overview}/Checkpoints {#checkpoints}", "Checkpoints {#checkpoints}",
                 "checkpoints {#checkpoints}", "#checkpoints",
                 # a part of the old path (the substring tier falls back to it)
                 "Checkpoints {#check", "Overview {#overview}/Check"]
        for label, (opts, net) in self.corpora().items():
            for name in names:
                with self.subTest(corpus=label, name=name):
                    code, out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0", name])
                    self.assertEqual(code, 0, err)
                    self.assertIn("# heading_path: Overview/Checkpoints\n", out)
                    # the body is printed as written
                    self.assertIn(CHECKPOINTS_BODY, out)
                    self.assertIn("Restore Checkpoint", out)
                    self.assertNotIn("Token Usage", out)

    def test_the_parent_opens_without_ambiguity(self):
        # every name below is also a part of its subsections' paths as written
        for label, (opts, net) in self.corpora().items():
            for name in ("Overview", "overview", "Overview {#overview}", "overview {#overview}", "#overview"):
                with self.subTest(corpus=label, name=name):
                    code, out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0", name])
                    self.assertEqual(code, 0, err)
                    self.assertIn("# heading_path: Overview\n", out)
                    self.assertIn("## Overview {#overview}\n", out)
                    self.assertIn("  - Overview/Checkpoints\n", out)
                    self.assertNotIn("Token Usage", out)

    def test_a_missing_heading_lists_the_text_and_every_command_runs(self):
        for label, (opts, net) in self.corpora().items():
            with self.subTest(corpus=label):
                code, _out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0", "Checkpoint Restore"])
                self.assertEqual(code, 1, err)
                self.assertIn("  - Overview/Checkpoints\n", err)
                self.assertNotIn("{#", err)
                for line in (ln.strip() for ln in err.splitlines() if ln.strip().startswith(SCRIPT)):
                    code, got, err2 = self.run_line(net, "Next: " + line)
                    self.assertEqual(code, 0, f"{line}\n{err2}")

    def test_a_cut_section_offers_its_text_as_the_search_word(self):
        """``--max-chars 200`` cuts the section (it has no subsection): the
        stand-in word is the heading's text, and the line runs as printed and
        with the word replaced (a word that starts with "-", an escape,
        function words only, symbols only)."""
        for label, (opts, net) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--max-chars", "200", "--", "0",
                                                    "Overview/Checkpoints"])
                self.assertEqual(code, 0, err)
                body = out.split("\n---\n", 1)[1]
                self.assertIn(CHECKPOINTS_BODY, body)
                self.assertNotIn("you want to revert back", body)
                self.assertRegex(body, r"\.\.\. \(\d+ chars truncated; this section has no subsections")
                self.assertIn("with 'checkpoints' (the section heading, a stand-in)", out)
                nxt = self.next_lines(out)
                self.assertEqual(len(nxt), 1, out)
                self.assertTrue(nxt[0].endswith(" -- checkpoints"), nxt[0])
                code, got, err = self.run_line(net, nxt[0])
                self.assertEqual(code, 0, err)
                self.assertIn("    Section: Overview/Checkpoints  (x", got)
                for word in ("restore checkpoint", "--resume", "-step", "\\_", "the of", "{#}", "#checkpoints"):
                    code, got, err = self.run_line(net, nxt[0], {"checkpoints": [word]})
                    self.assertEqual(code, 0, f"{word}\n{err}")
                    self.assertIn("Search-content results for ", got)


class ZedFormAnchorTest(_Corpora):
    def test_a_section_with_an_id_links_the_id_and_one_without_a_slug(self):
        cases = (("token usage", "Token Usage and Compaction", "token-usage"),
                 ("restore checkpoint", "Overview/Checkpoints", "checkpoints"),
                 ("queued", "Overview/Queueing Messages", "queueing-messages"))
        # the page's own link to the section is by its id
        self.assertIn("](#checkpoints)", ZED_PAGE)
        for label, (opts, net) in self.corpora(anchors=True).items():
            for query, path, anchor in cases:
                with self.subTest(corpus=label, query=query):
                    code, out, err = self.run_cli(net, [SCRIPT, "search-content", *opts, "--page-ref", "0", "--",
                                                        query])
                    self.assertEqual(code, 0, err)
                    line = next((ln for ln in out.splitlines() if ln.startswith(f"    Section: {path}  ")), "")
                    self.assertTrue(line.endswith(f"  [https://zed.dev/docs/ai/agent-panel#{anchor}]"), out)
                    # the URL opens the page, the fragment the section
                    url, fragment = re.search(r"\[(https://\S+)#(\S+)\]$", line).groups()
                    code, got, err = self.run_cli(net, [SCRIPT, "sections", *opts, "--", f"{url}#{fragment}"])
                    self.assertEqual(code, 0, err)
                    self.assertIn('Sections in [0] "Agent Panel"', got)
                    code, got, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0", f"#{fragment}"])
                    if anchor == "queueing-messages":
                        # a slug is not an id the heading gave itself
                        self.assertEqual(code, 1, got)
                        continue
                    self.assertEqual(code, 0, err)
                    self.assertIn(f"# heading_path: {path}\n", got)

    def test_without_anchor_style_no_anchor(self):
        for label, (opts, net) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "search-content", *opts, "--page-ref", "0", "--",
                                                    "token usage"])
                self.assertEqual(code, 0, err)
                self.assertNotIn("#token-usage]", out)


def _zed_form(body: str) -> str:
    return f"> Docs index.\n\n# Agent Panel\n\nIntro.\n\n{body}"


# a heading whose text is the path of a later heading as written
SHADOWED_TOP = _zed_form("## Default Width {#panel-default-width}\n\nalpha width text.\n\n## Other\n\nother body.\n\n"
                         "### Default Width\n\nbravo width text.\n")
# a heading whose text is an id-less heading of the same level
SHADOWED_BARE = _zed_form("## Setup {#a}\n\nalpha install text.\n\n### Alpha child\n\nalpha child body.\n\n"
                          "## Setup\n\nbravo install text.\n\n## Other\n\nother body.\n")
# two headings whose texts are the same, with subsections of their own
SAME_TEXT = _zed_form("## Setup {#a}\n\nalpha install text.\n\n### Alpha child\n\nalpha child body.\n\n"
                      "## Setup {#b}\n\nbravo install text.\n\n### Bravo child\n\nbravo child body.\n\n"
                      "## Other\n\nother body.\n")
# the same, the second with no subsection
SAME_TEXT_CHILDLESS = _zed_form("## Setup {#a}\n\nalpha install text.\n\n### Alpha child\n\nalpha child body.\n\n"
                                "## Setup {#b}\n\nbravo install text.\n\n## Other\n\nother body.\n")
# the same, with subsections whose texts are the same too
SAME_TEXT_CHILDREN = _zed_form("## Setup {#a}\n\nalpha install text.\n\n### Child\n\nalpha child body.\n\n"
                               "## Setup {#b}\n\nbravo install text.\n\n### Child\n\nbravo child body.\n\n"
                               "## Other\n\nother body.\n")


class SharedTextTest(_Corpora):
    """Pages where a heading's text (its id taken off) is also another
    heading's name (マージ前レビューの指摘): every name printed opens its section,
    or the first with it; ``content`` hints at the subsections of the section
    it printed; a ``Next:`` line and a hint reach the section by its name,
    ``#<id>`` or its path as written."""

    def corpora_of(self, text: str):
        """``corpora`` for a page of *text* (in the form of the Zed page), in
        a directory of its own."""
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        cache = root / "cache"
        cache.mkdir()
        file = root / "local-llms-full.txt"
        file.write_text(f"# Agent Panel\nSource: {ZED_URL}\n{text.split(chr(10) + '# Agent Panel' + chr(10), 1)[1]}",
                        encoding="utf-8")
        common = ["--sources-file", str(self.sources), "--cache-dir", str(cache)]
        return {"fetch": (["--source", "zed", *common], _Net({**self.served, ZED_URL: text})),
                "cache": (["--source", "zed", *common], _Net({})),
                "file": (["--source", "zedfile", *common, "--file", str(file)], _Net({}))}

    def rows(self, net, opts):
        code, out, err = self.run_cli(net, [SCRIPT, "sections", *opts, "--", "0"])
        self.assertEqual(code, 0, err)
        rows = [ln for ln in out.splitlines() if re.match(r"^\s*\[L\d\] ", ln)]
        return rows, self.next_lines(out)[0]

    def opens(self, net, line, body, replace=None):
        """Run the ``Next:`` *line* (a word of it replaced as *replace* says):
        it prints *body*, and the output without its subsection hints."""
        code, got, err = self.run_line(net, line, replace)
        self.assertEqual(code, 0, f"{line}\n{err}")
        self.assertIn(body, got.split("\n---\n", 1)[1], line)
        return got

    def search_next(self, net, opts, query):
        code, out, err = self.run_cli(net, [SCRIPT, "search-content", *opts, "--page-ref", "0", "--", query])
        self.assertEqual(code, 0, err)
        return re.findall(r"(?m)^    Section: (.*?)  \(x\d+\)", out), self.next_lines(out)[0]

    def hints(self, out):
        """The subsection hint blocks of a ``content`` output: ``(label, rows)``."""
        return [(label, re.findall(r"(?m)^  - (.*)$", rows))
                for label, rows in re.findall(r"(?m)^--- (.*) ---\n((?:  - .*\n)*)", out)]

    def test_a_heading_whose_text_is_another_heading_is_named_as_written(self):
        """``## Default Width {#panel-default-width}`` and ``### Default
        Width`` under ``## Other``: ``Default Width`` opens the second (as
        before), so the first is named as written."""
        for label, (opts, net) in self.corpora_of(SHADOWED_TOP).items():
            with self.subTest(corpus=label):
                rows, nxt = self.rows(net, opts)
                self.assertEqual(rows, ["[L2] Default Width {#panel-default-width}", "[L2] Other",
                                        "  [L3] Other/Default Width"])
                for path, body in (("Default Width {#panel-default-width}", "alpha width text."),
                                   ("Other/Default Width", "bravo width text."), ("Default Width", "bravo width")):
                    self.opens(net, nxt, body, {"<heading_path>": [path]})
                for query, section, body in (("alpha", "Default Width {#panel-default-width}", "alpha width text."),
                                             ("bravo", "Other/Default Width", "bravo width text.")):
                    sections, line = self.search_next(net, opts, query)
                    self.assertEqual(sections, [section])
                    self.assertEqual(shlex.split(line, comments=True)[2:5], ["content", "0", section], line)
                    self.opens(net, line, body)

    def test_a_heading_whose_text_is_an_id_less_heading_is_named_as_written(self):
        """``## Setup {#a}`` (with a subsection) and ``## Setup``: ``Setup``
        opens the second, so the first is named ``Setup {#a}``, and each
        lists its own subsections."""
        for label, (opts, net) in self.corpora_of(SHADOWED_BARE).items():
            with self.subTest(corpus=label):
                rows, nxt = self.rows(net, opts)
                self.assertEqual(rows, ["[L2] Setup {#a}", "  [L3] Setup/Alpha child", "[L2] Setup", "[L2] Other"])
                for query, section, body in (("alpha install", "Setup {#a}", "alpha install text."),
                                             ("bravo", "Setup", "bravo install text.")):
                    sections, line = self.search_next(net, opts, query)
                    self.assertEqual(sections, [section])
                    self.assertEqual(shlex.split(line, comments=True)[2:5], ["content", "0", section], line)
                    # one section each: no note of a repeated heading
                    self.assertNotIn("heading appears", line)
                    self.opens(net, line, body)
                got = self.opens(net, nxt, "bravo install text.", {"<heading_path>": ["Setup"]})
                self.assertEqual(self.hints(got), [])
                for name in ("Setup {#a}", "#a", "a"):
                    got = self.opens(net, nxt, "alpha install text.", {"<heading_path>": [name]})
                    self.assertIn("# heading_path: Setup {#a}\n", got)
                    self.assertEqual(self.hints(got), [("Subsections of 'Setup {#a}' (1)", ["Setup/Alpha child"])] * 2)

    def test_the_second_of_two_same_texts_lists_its_own_subsections(self):
        """``## Setup {#a}`` / ``## Setup {#b}``: both are ``Setup``, and the
        second opened by ``#b`` or ``Setup {#b}`` hints at its own subsection
        and decides by it whether it can be narrowed."""
        for label, (opts, net) in self.corpora_of(SAME_TEXT).items():
            with self.subTest(corpus=label):
                rows, nxt = self.rows(net, opts)
                self.assertEqual(rows, ["[L2] Setup", "  [L3] Setup/Alpha child", "[L2] Setup",
                                        "  [L3] Setup/Bravo child", "[L2] Other"])
                for name in ("#b", "Setup {#b}"):
                    got = self.opens(net, nxt, "bravo install text.", {"<heading_path>": [name]})
                    hints = self.hints(got)
                    self.assertEqual(hints, [("Subsections of 'Setup' (1)", ["Setup/Bravo child"])] * 2)
                    hint_next = [ln for ln in self.next_lines(got) if "<heading_path from above>" in ln][0]
                    self.opens(net, hint_next, "bravo child body.", {"<heading_path from above>": hints[0][1]})
        for label, (opts, net) in self.corpora_of(SAME_TEXT_CHILDLESS).items():
            with self.subTest(corpus=label, childless=True):
                for name, heading, has_subsections in (("Setup", "## Setup {#a}\n", True),
                                                       ("#a", "## Setup {#a}\n", True),
                                                       ("#b", "## Setup {#b}\n", False),
                                                       ("Setup {#b}", "## Setup {#b}\n", False)):
                    code, got, err = self.run_cli(net, [SCRIPT, "content", *opts, "--max-chars", "30", "--", "0",
                                                        name])
                    self.assertEqual(code, 0, err)
                    # the body (cut after its heading) is the section's
                    body = got.split("\n---\n", 1)[1]
                    self.assertIn(heading, body, name)
                    self.assertNotIn(heading.replace("{#a}", "{#x}").replace("{#b}", "{#a}").replace("{#x}", "{#b}"),
                                     body, name)
                    self.assertEqual(bool(self.hints(got)), has_subsections, name)
                    self.assertEqual("this section has no subsections to narrow to" in got, not has_subsections, name)
                    self.assertEqual("chars truncated; narrow with " in got, has_subsections, name)
                    if not has_subsections:
                        cut = [ln for ln in self.next_lines(got) if " search-content " in ln][0]
                        self.assertTrue(cut.endswith(" -- setup"), cut)
                        self.assertEqual(self.run_line(net, cut)[0], 0, cut)

    def test_a_hit_in_the_second_of_two_same_texts_reads_it_by_its_id(self):
        """The ``Next:`` line of a hit in the second ``Setup`` is ``content 0
        '#b'`` (0.53.0 printed ``'Setup {#b}'``; both open it), and of the
        first ``Setup`` with the note, as before."""
        for label, (opts, net) in self.corpora_of(SAME_TEXT).items():
            with self.subTest(corpus=label):
                for query, words, note, body in (
                        ("bravo install", ["content", "0", "#b"], False, "bravo install text."),
                        ("alpha install", ["content", "0", "Setup"], True, "alpha install text.")):
                    sections, line = self.search_next(net, opts, query)
                    self.assertEqual(sections, ["Setup"])
                    self.assertEqual(shlex.split(line, comments=True)[2:5], words, line)
                    self.assertEqual(line.endswith("# heading appears 2 times; this reads the first"), note, line)
                    self.opens(net, line, body)

    def test_a_subsection_of_the_second_same_text_is_reached_by_its_path_as_written(self):
        """Both ``Setup`` have a ``### Child``: the second child's ``Next:``
        line and hint are ``Setup {#b}/Child`` (``Setup/Child`` opens the
        first child), the first child's stay ``Setup/Child``."""
        for label, (opts, net) in self.corpora_of(SAME_TEXT_CHILDREN).items():
            with self.subTest(corpus=label):
                rows, nxt = self.rows(net, opts)
                self.assertEqual(rows, ["[L2] Setup", "  [L3] Setup/Child", "[L2] Setup", "  [L3] Setup/Child",
                                        "[L2] Other"])
                sections, line = self.search_next(net, opts, "bravo child")
                self.assertEqual(sections, ["Setup/Child"])
                self.assertEqual(shlex.split(line, comments=True)[2:5], ["content", "0", "Setup {#b}/Child"], line)
                self.assertNotIn("heading appears", line)
                self.opens(net, line, "bravo child body.")
                for name, child, body in (("#b", "Setup {#b}/Child", "bravo child body."),
                                          ("Setup {#b}", "Setup {#b}/Child", "bravo child body."),
                                          ("Setup", "Setup/Child", "alpha child body."),
                                          ("#a", "Setup/Child", "alpha child body.")):
                    got = self.opens(net, nxt, body.split()[0] + " install text.", {"<heading_path>": [name]})
                    self.assertEqual(self.hints(got), [("Subsections of 'Setup' (1)", [child])] * 2, name)
                    hint_next = [ln for ln in self.next_lines(got) if "<heading_path from above>" in ln][0]
                    self.opens(net, hint_next, body, {"<heading_path from above>": [child]})

    def test_the_whole_page_and_its_top(self):
        """``content <page>`` lists the top-level sections, the second
        ``Setup`` as ``#b``; ``(top)`` reads the text above the headings."""
        for label, (opts, net) in self.corpora_of(SAME_TEXT).items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0"])
                self.assertEqual(code, 0, err)
                self.assertIn("bravo child body.", out)
                self.assertEqual(self.hints(out), [("Top-level sections (3)", ["Setup", "#b", "Other"])] * 2)
                hint_next = [ln for ln in self.next_lines(out) if "<heading_path from above>" in ln][0]
                self.opens(net, hint_next, "bravo install text.", {"<heading_path from above>": ["#b"]})
                code, out, err = self.run_cli(net, [SCRIPT, "content", *opts, "--", "0", "(top)"])
                self.assertEqual(code, 0, err)
                self.assertIn("Intro.", out)
                self.assertNotIn("install text.", out)
                self.assertEqual(self.hints(out), [])
                # a hit above the first heading
                sections, line = self.search_next(net, opts, "intro")
                self.assertEqual(sections, ["(top)  [before first heading]"])
                self.assertEqual(shlex.split(line, comments=True)[2:5], ["content", "0", "(top)"], line)
                self.opens(net, line, "Intro.")


AGENT_PLUGINS = """\
---
title: Agent Plugins
description: A portable package format for reusable components that extend AI agents.
---

# Agent Plugins

Agent Plugins is an open, vendor-neutral standard for packaging reusable components into portable plugins.

## Why Agent Plugins? [#why-agent-plugins]

AI agent clients have developed their own plugin formats, even when plugins contain the same underlying components.

## The portable package [#the-portable-package]

An Agent Plugin is a directory with a required manifest and optional components in fixed locations.

---
title: Agent Plugins Specification
description: The complete normative contract for portable Agent Plugin packages and conformant clients.
type: reference
---

# Agent Plugins Specification

## Table of contents [#table-of-contents]

1. [Status and version](#1-status-and-version)

## 1. Status and version [#1-status-and-version]

This specification defines version `1.0.0` of the Agent Plugins format.

### 1.1 Governance model [#11-governance-model]

Governance for the Agent Plugins project is defined separately from the portable package format.
"""
ZOD = """\
# Zod

Zod is a TypeScript-first schema validation library with static type inference.

# Defining schemas

To validate data, you must first define a *schema*.

## Stringbools \\[#stringbool]

> Introduced in `zod@4.0`.

In some cases (e.g. parsing environment variables) it's valuable to parse certain string "boolish" values to a plain `boolean` value. Use `z.stringbool()` for this.

## Optionals

To make a schema optional, use `z.optional()`.
"""


class FumadocsFormTest(unittest.TestCase):
    """``[#id]`` (Agent Plugins) and ``\\[#id]`` (Zod) on the bundled presets
    with ``--file``: the same reading as ``{#id}``."""

    CASES = {
        # source: (text, page, query, the section, its id, its old path)
        "agent-plugins": (AGENT_PLUGINS, 0, "underlying components", "Agent Plugins/Why Agent Plugins?",
                          "why-agent-plugins", "Agent Plugins/Why Agent Plugins? [#why-agent-plugins]"),
        "agent-plugins-h3": (AGENT_PLUGINS, 1, "governance", "Agent Plugins Specification/1. Status and version/"
                             "1.1 Governance model", "11-governance-model",
                             "Agent Plugins Specification/1. Status and version [#1-status-and-version]/"
                             "1.1 Governance model [#11-governance-model]"),
        "zod": (ZOD, 1, "boolish", "Stringbools", "stringbool", "Stringbools \\[#stringbool]"),
    }

    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def opts(self, key):
        text = self.CASES[key][0]
        f = Path(self.tmp, f"{key}.txt")
        f.write_text(text, encoding="utf-8")
        return ["--source", key.split("-h3")[0], "--sources-file", self.sources, "--cache-dir", self.tmp,
                "--file", str(f)]

    def test_the_section_reads_as_shown_and_opens_by_every_name(self):
        for key, (_text, page, query, path, anchor_id, old_path) in self.CASES.items():
            with self.subTest(case=key):
                opts = self.opts(key)
                code, out, err = _loader.run_cli(g, [SCRIPT, "sections", *opts, "--", str(page)])
                self.assertEqual(code, 0, err)
                self.assertIn(f"] {path}\n", out)
                self.assertNotIn("[#", out.split("=" * 60, 1)[1])
                code, out, err = _loader.run_cli(g, [SCRIPT, "search-content", *opts, "--page-ref", str(page),
                                                     "--", query])
                self.assertEqual(code, 0, err)
                self.assertIn(f"    Section: {path}  (x", out)
                nxt = [ln for ln in out.splitlines() if ln.startswith("Next:")]
                self.assertTrue(nxt, out)
                code, got, err = _loader.run_cli(g, shlex.split(nxt[0], comments=True)[1:])
                self.assertEqual(code, 0, err)
                self.assertIn(f"# heading_path: {path}\n", got)
                for name in (path, old_path, old_path.rsplit("/", 1)[-1], anchor_id, "#" + anchor_id):
                    code, got, err = _loader.run_cli(g, [SCRIPT, "content", *opts, "--", str(page), name])
                    self.assertEqual(code, 0, f"{name}\n{err}")
                    self.assertIn(f"# heading_path: {path}\n", got)
                    # the heading line of the body as written
                    self.assertIn(old_path.rsplit("/", 1)[-1] + "\n", got)

    def test_the_index_headings_have_no_id(self):
        code, out, err = _loader.run_cli(g, [SCRIPT, "search-index", *self.opts("agent-plugins"), "--show-sections",
                                             "--", "portable package"])
        self.assertEqual(code, 0, err)
        rows = re.findall(r"(?m)^      - (.+)$", out)
        self.assertIn("The portable package", rows)
        self.assertFalse([r for r in rows if "[#" in r], rows)


class VitestTitleTest(unittest.TestCase):
    """Vitest gives its page's H1 an id (``# Migrating to Vitest 5.0
    {#vitest-5}``): the page title is the text, in every listing; a title
    ``page_ref`` still finds the page by the old title."""

    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        f = Path(self.tmp, "vitest.txt")
        f.write_text(VITEST_VI_API_H3, encoding="utf-8")
        self.opts = ["--source", "vitest", "--sources-file", self.sources, "--cache-dir", self.tmp, "--file", str(f)]

    def test_no_listing_shows_the_id(self):
        for argv in (["fetch-index"], ["search-index", "--show-sections", "--", "hoisted"],
                     ["search", "--", "hoisted"], ["search-content", "--", "hoisted"], ["sections", "--", "0"]):
            with self.subTest(command=argv[0]):
                code, out, err = _loader.run_cli(g, [SCRIPT, argv[0], *self.opts, *argv[1:]])
                self.assertEqual(code, 0, err)
                self.assertIn("Migrating to Vitest 5.0", out)
                self.assertNotIn("{#", "\n".join(ln for ln in out.splitlines() if not ln.startswith("      ")))
        code, out, err = _loader.run_cli(g, [SCRIPT, "fetch-index", *self.opts])
        self.assertIn("[0] Migrating to Vitest 5.0\n", out)

    def test_a_title_page_ref_finds_the_page_by_either_title(self):
        for ref in ("Migrating to Vitest 5.0 {#vitest-5}", "Migrating to Vitest 5.0", "vitest 5.0 {#vitest",
                    "vitest-5"):
            with self.subTest(ref=ref):
                code, out, err = _loader.run_cli(g, [SCRIPT, "sections", *self.opts, "--", ref])
                self.assertEqual(code, 0, err)
                self.assertIn('Sections in [0] "Migrating to Vitest 5.0"\n', out)
                self.assertIn("[L1] Migrating to Vitest 5.0\n", out)


class ResolveOrderTest(unittest.TestCase):
    """``extract_content`` with ids: what each argument opens."""

    def opened(self, body, arg):
        """``(heading_path, first line)`` of the section *arg* opens; a
        not-found or ambiguous exit fails the test."""
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                content, path = _common.extract_content(body.splitlines(keepends=True), arg)
        except SystemExit:
            self.fail(f"{arg!r} opened no section:\n{err.getvalue()}")
        except Exception as e:  # noqa: BLE001  (a broken step fails the test, not errors it)
            self.fail(f"{arg!r}: {type(e).__name__}: {e}")
        return path, content.splitlines()[0]

    def test_an_exact_title_comes_before_a_case_folded_path(self):
        body = "## setup\na\n## Parent\n### Setup\nb\n"
        self.assertEqual(self.opened(body, "Setup"), ("Parent/Setup", "### Setup"))
        self.assertEqual(self.opened(body, "setup"), ("setup", "## setup"))

    def test_a_bare_title_keeps_opening_the_heading_it_named(self):
        # the text "Default Width" is the leaf of both; as written only the
        # second is "Default Width" (Zed's all-settings page)
        body = ("## Agent Panel {#agent-panel}\n### Default Width {#agent-panel-default-width}\na\n"
                "## Project Panel\n### Default Width\nb\n")
        self.assertEqual(self.opened(body, "Default Width"), ("Project Panel/Default Width", "### Default Width"))
        for arg in ("Agent Panel/Default Width", "Default Width {#agent-panel-default-width}",
                    "agent-panel-default-width", "#agent-panel-default-width",
                    "Agent Panel {#agent-panel}/Default Width {#agent-panel-default-width}"):
            with self.subTest(arg=arg):
                self.assertEqual(self.opened(body, arg),
                                 ("Agent Panel/Default Width", "### Default Width {#agent-panel-default-width}"))

    def test_an_old_name_of_a_section_with_subsections_opens_it(self):
        # each argument is also a part of the subsection's path as written
        body = ("## Agent Panel {#agent-panel}\na\n### Default Width {#agent-panel-default-width}\nb\n"
                "#### Note\nc\n## Other\nd\n")
        for arg in ("Agent Panel {#agent-panel}/Default Width {#agent-panel-default-width}",
                    "agent panel {#agent-panel}/default width {#agent-panel-default-width}",
                    "Default Width {#agent-panel-default-width}", "default width {#agent-panel-default-width}",
                    "agent panel/default width"):
            with self.subTest(arg=arg):
                self.assertEqual(self.opened(body, arg),
                                 ("Agent Panel/Default Width", "### Default Width {#agent-panel-default-width}"))

    def test_the_heading_as_written_comes_before_the_text(self):
        # "Setup" names the second heading as written, the first by its text.
        # The first is named as written ("Setup {#a}"): named by its text it
        # would print "Setup", which opens the second (this test expected that
        # name before; マージ前レビューの指摘)
        body = "## Setup {#a}\none\n## Setup\ntwo\n"
        self.assertEqual(self.opened(body, "Setup"), ("Setup", "## Setup"))
        self.assertEqual(self.opened(body, "Setup {#a}"), ("Setup {#a}", "## Setup {#a}"))
        self.assertEqual(self.opened(body, "a"), ("Setup {#a}", "## Setup {#a}"))

    def test_an_id_comes_before_a_substring(self):
        # "usage" is a part of both headings' text (ambiguous as a substring)
        body = "## Token Usage and Compaction {#usage}\na\n## Usage limits\nb\n"
        self.assertEqual(self.opened(body, "usage"),
                         ("Token Usage and Compaction", "## Token Usage and Compaction {#usage}"))
        # "limits" is a part of the other heading's text
        body = "## Token Usage {#limits}\na\n## Usage limits\nb\n"
        self.assertEqual(self.opened(body, "limits"), ("Token Usage", "## Token Usage {#limits}"))
        self.assertEqual(self.opened(body, "#limits"), ("Token Usage", "## Token Usage {#limits}"))
        # a text the page shows comes before an id
        body = "## Limits\na\n## Token Usage {#limits}\nb\n"
        self.assertEqual(self.opened(body, "limits"), ("Limits", "## Limits"))
        self.assertEqual(self.opened(body, "#limits"), ("Token Usage", "## Token Usage {#limits}"))

    def test_the_substring_tier_reads_the_text_then_the_heading_as_written(self):
        body = ("## Overview {#overview}\na\n### Checkpoints {#checkpoints}\nb\n## Setup {#install}\nc\n"
                "## Install steps\nd\n")
        self.assertEqual(self.opened(body, "Overview/Check"),
                         ("Overview/Checkpoints", "### Checkpoints {#checkpoints}"))
        # as written, "instal" is a part of two headings; in the text, of one
        self.assertEqual(self.opened(body, "instal"), ("Install steps", "## Install steps"))
        # a part of the heading as written only
        self.assertEqual(self.opened(body, "Checkpoints {#check"),
                         ("Overview/Checkpoints", "### Checkpoints {#checkpoints}"))
        self.assertEqual(self.opened(body, "Overview {#overview}/Check"),
                         ("Overview/Checkpoints", "### Checkpoints {#checkpoints}"))
        self.assertEqual(self.opened(body, "setup {#inst"), ("Setup", "## Setup {#install}"))
        # a part of two headings' text: ambiguous
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            _common.extract_content(body.splitlines(keepends=True), "erview")

    def test_the_substring_tier_reads_the_text_of_a_section_named_as_written(self):
        # the first heading is named as written (its text opens the third),
        # and the substring tier still reads its text: "panel" is a part of
        # "Panel settings" only, as it was before it was named so
        body = ("## Default Width {#panel-default-width}\na\n## Other\nb\n### Default Width\nc\n"
                "## Panel settings\nd\n")
        self.assertEqual([s["heading_path"] for s in _common.extract_sections(body.splitlines(keepends=True))],
                         ["Default Width {#panel-default-width}", "Other", "Other/Default Width", "Panel settings"])
        self.assertEqual(self.opened(body, "panel"), ("Panel settings", "## Panel settings"))
        self.assertEqual(self.opened(body, "default width {#panel"),
                         ("Default Width {#panel-default-width}", "## Default Width {#panel-default-width}"))

    def test_two_headings_whose_text_is_the_same(self):
        """``## Setup {#a}`` / ``## Setup {#b}``: one path, listed with the
        note of a repeated heading; the second opens by its old text and id,
        and the ``Next:`` line of a hit in it reads it by its id."""
        body = "## Setup {#a}\nalpha install\n## Setup {#b}\nbravo install\n"
        lines = body.splitlines(keepends=True)
        self.assertEqual(self.opened(body, "Setup"), ("Setup", "## Setup {#a}"))
        for arg in ("Setup {#b}", "b", "#b"):
            with self.subTest(arg=arg):
                self.assertEqual(self.opened(body, arg), ("Setup", "## Setup {#b}"))
        for word, line in (("alpha", " content 0 Setup # heading appears 2 times; this reads the first\n"),
                           ("bravo", " content 0 '#b'\n")):
            with self.subTest(word=word):
                hits = _common.search_content_in_body(lines, word)
                self.assertEqual([(r["heading_path"], r["heading_count"]) for r in hits["results"]],
                                 [("Setup", 2)])
                out = _run_printing(_commands.render_next_content, _commands.hit_candidates([(0, hits, ())]),
                                    hint_args=())
                self.assertTrue(out.endswith(line), out)


def _run_printing(fn, *args, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kw)
    return buf.getvalue()


# The heading parts of the synthetic pages: names that differ only by an id,
# by an attribute without an id, by case, and by level.
_PARTS = ("## Setup", "## setup", "## Setup {#a}", "## Setup {#b}", "## Setup {.c}", "## Other",
          "### Setup", "### Setup {#c}", "### Child")
_WORDS = ("alpha", "bravo", "charlie", "delta")
# a long line in every section: --max-chars _CUT keeps a section's heading and
# its word line and cuts the rest (the subsections come after it)
_PAD = "padding " * 25 + "\n"
_CUT = 60


class _Page:
    """One synthetic page: every heading is followed by a line with its own
    word (``wordalpha text.``) that tells which section a text starts with."""

    def __init__(self, parts):
        self.lines = []
        for i, part in enumerate(parts):
            self.lines += [f"{part}\n", f"word{_WORDS[i]} text.\n", _PAD]
        # the same headings without the long lines, for what only the headings decide
        self.short = [ln for ln in self.lines if ln is not _PAD]
        self.secs = _common.extract_sections(self.lines)
        self._opened = {}

    @staticmethod
    def section_of(text):
        m = re.search(r"(?m)^word(\w+) text\.$", text)
        return _WORDS.index(m.group(1)) if m else None

    def open(self, arg):
        """The section ``content`` opens for *arg*; ``None`` when it exits."""
        if arg not in self._opened:
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    content, _path = _common.extract_content(self.short, arg)
                self._opened[arg] = self.section_of(content)
            except SystemExit:
                self._opened[arg] = None
        return self._opened[arg]

    def first(self, key, i):
        """The first section whose *key* is the one of section *i*."""
        return next(k for k, s in enumerate(self.secs) if key(s) == key(self.secs[i]))

    def children(self, i):
        """The direct subsections of section *i*: the shallowest of those in its block."""
        block = []
        for s in self.secs[i + 1:]:
            if s["level"] <= self.secs[i]["level"]:
                break
            block.append(s)
        level = min((s["level"] for s in block), default=None)
        return [self.secs.index(s) for s in block if s["level"] == level]

    def ref(self, i):
        """The name a line that leads to section *i* uses: the first of its
        name, ``#<its id>`` and its path as written that opens it, else its
        name (no name reaches it)."""
        s = self.secs[i]
        names = [s["heading_path"]]
        if s.get("anchor_id") is not None:
            names.append("#" + s["anchor_id"])
        if "raw_heading_path" in s:
            names.append(s["raw_heading_path"])
        return next((n for n in names if self.open(n) == i), s["heading_path"])


def _name(s):
    return s["heading_path"]


def _raw_path(s):
    return s.get("raw_heading_path", s["heading_path"])


def _old_open(secs, arg):
    """What 0.53.0 opened for *arg* by an exact match: the sections as written,
    path then title, then the same case-folded. ``None`` when no exact match."""
    for key, fold in ((_raw_path, False), (lambda s: s.get("raw_title", s["title"]), False),
                      (_raw_path, True), (lambda s: s.get("raw_title", s["title"]), True)):
        want = arg.lower() if fold else arg
        for k, s in enumerate(secs):
            if (key(s).lower() if fold else key(s)) == want:
                return k
    return None


class SyntheticPagesTest(unittest.TestCase):
    """Every page of 3 or 4 of ``_PARTS`` (every order, with repeats), every
    section, through the functions the commands print with:

    1. its name (``sections`` / ``Section:``) opens it, or the first section
       of that name when several share it
    2. what opened a section in 0.53.0 (the path and title as written, also
       case-folded) opens it still, and ``#<id>`` the first section with the id
    3. ``content`` lists the subsections, and decides whether there are any
       to narrow to, of the section whose text it printed, by any name of it
    4. a ``Next:`` line and a subsection hint name the section by what opens
       it (``_Page.ref``); a name that opens it keeps the line as it was
       (with the note of a repeated heading), and a line that reached it in
       0.53.0 reaches it still
    """

    def check(self, parts, bad):
        page = _Page(parts)
        secs, counts = page.secs, _common.heading_path_counts(page.secs)
        label = " / ".join(parts)
        forms = []
        for i, s in enumerate(secs):
            # 1. the name
            if page.open(s["heading_path"]) != page.first(_name, i):
                bad[1].append(f"{label}: {s['heading_path']!r} opens {page.open(s['heading_path'])}, "
                              f"not {page.first(_name, i)}")
            # 2. the names of 0.53.0
            raw_title = s.get("raw_title", s["title"])
            for arg in dict.fromkeys((_raw_path(s), raw_title, _raw_path(s).lower(), raw_title.lower())):
                want = _old_open(secs, arg)
                if want is not None and page.open(arg) != want:
                    bad[2].append(f"{label}: {arg!r} opens {page.open(arg)}, 0.53.0 {want}")
            if s.get("anchor_id") is not None:
                want = page.first(lambda x: x.get("anchor_id"), i)
                if page.open("#" + s["anchor_id"]) != want:
                    bad[2].append(f"{label}: '#{s['anchor_id']}' opens {page.open('#' + s['anchor_id'])}, not {want}")
            forms += [s["heading_path"], _raw_path(s)] + (["#" + s["anchor_id"]] if s.get("anchor_id") else [])
        # 4. the Next: line of a hit in each section (every word line is a hit
        # of "word": one search, one result per section)
        hits = _common.search_content_in_body(page.lines, "word", max_matches_per_doc=0)
        results = {r["line_offset"] // 3: r for r in hits["results"]}
        self.assertEqual(sorted(results), list(range(len(secs))), label)
        for i, s in enumerate(secs):
            self.assertEqual(results[i]["heading_path"], s["heading_path"], label)
            out = _run_printing(_commands.render_next_content,
                                _commands.hit_candidates([(0, {**hits, "results": [results[i]]}, ())]),
                                hint_args=())
            line = out.strip().removeprefix(f"Next: {SCRIPT} ")
            ref = page.ref(i)
            note = _common.duplicate_heading_note(counts[ref]).strip() if ref == s["heading_path"] else ""
            if line != " ".join(["content", "0", shlex.quote(ref)] + ([note] if note else [])):
                bad[4].append(f"{label}: Next: for {i} is {line!r}, not {ref!r}")
            elif page.first(_raw_path, i) == i and page.open(ref) != i:
                bad[4].append(f"{label}: Next: for {i} ({ref!r}) reached it in 0.53.0, not now")
        # 3. (and 4. for the hints) content by every name. A page of 4 parts
        # renders only a section that a name of another opens first or that
        # has a subsection named otherwise; the pages of 3 parts render all
        for arg in dict.fromkeys(forms):
            t = page.open(arg)
            if (len(parts) > 3 and t is not None and page.first(_name, t) == t
                    and all(page.ref(k) == secs[k]["heading_path"] for k in page.children(t))):
                continue
            args = types.SimpleNamespace(heading_path=arg, max_chars=_CUT, no_subsection_hints=False)
            page_view = _commands.PageView(idx=0, title="Page", body_lines=page.lines)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    out = _run_printing(_commands.render_content, page_view, args, script=SCRIPT, hint_args=())
            except SystemExit:
                bad[3].append(f"{label}: content {arg!r} exits")
                continue
            t = page.section_of(out.split("\n---\n", 1)[1])
            kids = page.children(t)
            blocks = re.findall(r"(?m)^--- Subsections of '.*' \((\d+)\) ---\n((?:  - .*\n)*)", out)
            rows = [re.findall(r"(?m)^  - (.*?)(?: \[code\])?$", b) for _n, b in blocks]
            want_blocks = 2 if kids else 0
            if len(blocks) != want_blocks or any(int(n) != len(kids) or len(r) != len(kids)
                                                 for (n, _b), r in zip(blocks, rows)):
                bad[3].append(f"{label}: content {arg!r} opens {t} and lists {rows}, its subsections are {kids}")
            else:
                for r in rows:
                    for k, row in zip(kids, r):
                        if row not in (secs[k]["heading_path"], page.ref(k)):
                            bad[3].append(f"{label}: content {arg!r} opens {t} and lists {row!r}, not {k}")
                        elif row != page.ref(k):
                            bad[4].append(f"{label}: content {arg!r} hints {row!r} for {k}, not {page.ref(k)!r}")
            childless = "this section has no subsections to narrow to" in out
            keyword = _common.quoted_keyword(_common.search_in_page_keyword(secs[t]["title"]))
            if childless != (not kids) or (childless and f"with {keyword} (the section heading" not in out):
                bad[3].append(f"{label}: content {arg!r} opens {t}, cut as {'childless' if childless else 'with'}"
                              f" subsections; it has {kids}")

    def test_every_section_of_every_page(self):
        bad = {1: [], 2: [], 3: [], 4: []}
        with mock.patch("sys.argv", [SCRIPT]):
            for n in (3, 4):
                for parts in itertools.product(_PARTS, repeat=n):
                    try:
                        self.check(parts, bad)
                    except Exception as e:  # noqa: BLE001  (a broken step fails the test, not errors it)
                        self.fail(f"{' / '.join(parts)}: {type(e).__name__}: {e}")
        # the number of pages that break each property
        failing = {p: len({x.split(": ", 1)[0] for x in v}) for p, v in bad.items() if v}
        self.assertEqual(failing, {}, "\n".join(x for v in bad.values() for x in v[:5]))


class SectionDictTest(unittest.TestCase):
    BASE_KEYS = {"level", "title", "heading_path", "line_start", "line_end", "has_code_blocks"}

    def test_the_extra_keys_appear_only_where_the_text_differs(self):
        body = ["## Overview {#overview}\n", "a\n", "### Queueing Messages\n", "b\n", "## Styled {.wide}\n",
                "c\n", "## Plain\n", "d\n", "### Child\n"]
        secs = {s["heading_path"]: s for s in _common.extract_sections(body)}
        self.assertEqual(list(secs), ["Overview", "Overview/Queueing Messages", "Styled", "Plain", "Plain/Child"])
        self.assertEqual({k: secs["Overview"].get(k, "<missing>")
                          for k in ("title", "raw_title", "anchor_id", "raw_heading_path")},
                         {"title": "Overview", "raw_title": "Overview {#overview}", "anchor_id": "overview",
                          "raw_heading_path": "Overview {#overview}"})
        child = secs["Overview/Queueing Messages"]
        self.assertEqual(set(child), self.BASE_KEYS | {"raw_heading_path"})
        self.assertEqual(child["raw_heading_path"], "Overview {#overview}/Queueing Messages")
        self.assertEqual((secs["Styled"].get("anchor_id", "<missing>"), secs["Styled"].get("raw_title")),
                         (None, "Styled {.wide}"))
        self.assertEqual(set(secs["Plain"]), self.BASE_KEYS)
        self.assertEqual(set(secs["Plain/Child"]), self.BASE_KEYS)

    def test_a_page_without_ids_gets_the_dicts_it_got(self):
        body = ["## A {mediaId}\n", "x\n", "### B {#each items}\n", "y\n", "## C\n"]
        self.assertEqual([set(s) for s in _common.extract_sections(body)], [self.BASE_KEYS] * 3)
        self.assertEqual([s["heading_path"] for s in _common.extract_sections(body)],
                         ["A {mediaId}", "A {mediaId}/B {#each items}", "C"])

    def test_a_hit_result_carries_the_id(self):
        body = ["## Token Usage {#token-usage}\n", "tokens\n", "## Other\n", "tokens\n"]
        hits = _common.search_content_in_body(body, "tokens")
        self.assertEqual([(r["title"], r.get("anchor_id"), "anchor_id" in r) for r in hits["results"]],
                         [("Token Usage", "token-usage", True), ("Other", None, False)])


class AnchorHelperTest(unittest.TestCase):
    def test_section_url_anchor_uses_the_id_as_written(self):
        url = "https://zed.dev/docs/ai/agent-panel"
        self.assertEqual(_common.section_url_anchor(url, "Token Usage and Compaction", anchor_id="token-usage"),
                         f"  [{url}#token-usage]")
        self.assertEqual(_common.section_url_anchor(url, "Token Usage and Compaction"),
                         f"  [{url}#token-usage-and-compaction]")
        self.assertEqual(_common.section_url_anchor(url, "接続する", anchor_id="接続する"), f"  [{url}#接続する]")
        self.assertEqual(_common.section_url_anchor("", "Token Usage", anchor_id="token-usage"), "")
        self.assertEqual(_common.section_url_anchor(url, "(top)", anchor_id="x"), "")

    def test_print_hit_sections_passes_the_id_only_when_there_is_one(self):
        calls = []

        def anchor_for(title, **kw):
            calls.append((title, kw))
            return f" <{title}|{kw.get('anchor_id')}>"

        hits = {"results": [{"heading_path": "A", "title": "A", "hit_count": 1, "matched_keywords": ["x"],
                             "snippet": "x", "anchor_id": "a-id"},
                            {"heading_path": "B", "title": "B", "hit_count": 1, "matched_keywords": ["x"],
                             "snippet": "x"}]}
        out = _run_printing(_commands.print_hit_sections, hits, anchor_for=anchor_for)
        self.assertEqual(calls, [("A", {"anchor_id": "a-id"}), ("B", {})])
        self.assertIn("Section: A  (x1) <A|a-id>", out)
        # a one-argument anchor_for still serves the sections without an id
        out = _run_printing(_commands.print_hit_sections, {"results": hits["results"][1:]},
                            anchor_for=lambda title: f" [#{title}]")
        self.assertIn("Section: B  (x1) [#B]", out)


class PageTitleTest(unittest.TestCase):
    def profile(self, **raw):
        return g._validate_profile("sources.json", "x", {"url": "https://example.com/llms-full.txt", **raw})

    def test_an_h1_title_loses_its_id_and_keeps_it_as_written(self):
        docs = g.split_documents(["# Migrating {#migrating}\n", "a\n", "# Plain\n", "b\n"],
                                 self.profile(split="h1"))
        self.assertEqual([(d["title"], d.get("raw_title")) for d in docs],
                         [("Migrating", "Migrating {#migrating}"), ("Plain", None)])

    def test_a_folded_h1_stays_in_the_body_as_written(self):
        # h1_needs_url: an H1 with no URL under it is a heading of the page before
        docs = g.split_documents(["# Page {#page}\n", "Source: https://example.com/p\n", "a\n",
                                  "# Folded {#folded}\n", "b\n"],
                                 self.profile(split="h1", page_url="line:Source: ", h1_needs_url=True))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["title"], "Page")
        self.assertIn("# Folded {#folded}\n", docs[0]["body_lines"])

    def test_a_frontmatter_title_is_not_a_heading(self):
        text = ["---\n", "title: Using {#each}\n", "---\n", "\n", "# Heading {#h}\n", "a\n",
                "---\n", "url: /b\n", "---\n", "\n", "# Second {#second}\n", "b\n"]
        docs = g.split_documents(text, self.profile(split="frontmatter", frontmatter_key="url"))
        self.assertEqual([d["title"] for d in docs], ["Second"])
        docs = g.split_documents(text[:6], self.profile(split="frontmatter"))
        self.assertEqual([(d["title"], d.get("raw_title")) for d in docs], [("Using {#each}", None)])

    def test_a_line_split_page_title_from_its_heading(self):
        lines = ["## Source: https://example.com/a\n", "# Alpha {#alpha}\n", "a\n",
                 "## Source: https://example.com/b\n", "b\n"]
        docs = g.split_documents(lines, self.profile(split="line", line_prefix="## Source: "))
        self.assertEqual([(d["title"], d.get("raw_title")) for d in docs], [("Alpha", "Alpha {#alpha}"), ("b", None)])
        # a heading that is a template takes the frontmatter's title, as it says
        lines = ["## Source: https://example.com/a\n", "---\n", "title: Using {#each}\n", "---\n",
                 "# {% $markdoc.frontmatter.title %}\n", "a\n"]
        docs = g.split_documents(lines, self.profile(split="line", line_prefix="## Source: "))
        self.assertEqual([(d["title"], d.get("raw_title")) for d in docs], [("Using {#each}", None)])

    def test_the_index_headings_lose_the_id(self):
        self.assertEqual(g._heading_rows(["# T {#t}\n", "## A [#a]\n", "### B {mediaId}\n"], 3),
                         [(1, "T"), (2, "A"), (3, "B {mediaId}")])


if __name__ == "__main__":
    unittest.main()
