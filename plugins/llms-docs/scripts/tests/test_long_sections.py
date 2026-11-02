"""A section too long for one ``content`` call (the env-var table of a CLI, its
"All commands" table): the search shows the lines that match the most words,
each with its own context, and the ``Next:`` line searches inside the page
instead of reading the section. The order of pages and sections stays as it
was: this changes what is shown and suggested, not what ranks first.

The ``Next:`` lines are taken from the output as printed, split by the shell
rules and run, over the cache and ``--file``, ranked on the pages and on the
index, on one source and on a ``members`` alias, with the words the reader may
swap in (``SEARCH_INSIDE_NOTE`` asks for that). No network.
"""

import json
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _commands  # noqa: E402
import _common  # noqa: E402

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
LIMIT = _common.DEFAULT_MAX_CONTENT_CHARS

# Rows cut from code.claude.com's llms-full.txt (env-vars, 2026-10-28; the
# descriptions shortened), in the order they have there.
REAL_ROWS = [
    "| `API_TIMEOUT_MS` | Time in milliseconds that each API request attempt has to receive "
    "its response headers. The default is 600000, or 10 minutes. |",
    "| `BASH_DEFAULT_TIMEOUT_MS` | Default timeout for a foreground Bash or PowerShell tool "
    "command, in milliseconds (default: 120000, or 2 minutes). |",
    "| `BASH_MAX_OUTPUT_LENGTH` | Maximum number of characters of bash output that Claude Code "
    "reads back into a command's result. |",
    "| `BASH_MAX_TIMEOUT_MS` | Maximum timeout the model can set for a foreground Bash or "
    "PowerShell tool command, in milliseconds. |",
    "| `CLAUDE_ENV_FILE` | Path to a shell script whose contents Claude Code runs before each "
    "Bash command in the same shell process. |",
]
FILLER_ROW = "| `FILLER_{n}` | Padding row number {n} of the variable table, with nothing to find. |"
# rows for the words a reader may search or swap in
HOSTILE_ROWS = ["| `--timeoutflag` | a flag written with dashes |",
                "| `-t` | the short flag, zzdash |",
                "| `max\\_tokens` | an escaped name |",
                "| `+*` | symbols only |"]


def table(chars: int = LIMIT + 2000) -> list:
    """A heading-free run of rows over *chars* characters: the real rows after
    the first third of the padding, the hostile rows at the end."""
    rows = []
    n = 0
    while sum(len(r) + 1 for r in rows) < chars // 3:
        rows.append(FILLER_ROW.format(n=n))
        n += 1
    rows += REAL_ROWS
    while sum(len(r) + 1 for r in rows) < chars:
        rows.append(FILLER_ROW.format(n=n))
        n += 1
    return rows + HOSTILE_ROWS


ENV_PAGE = "\n".join([
    "# Environment variables",
    "Source: https://example.com/docs/env-vars",
    "",
    "Variables intro.",
    "",
    "## Set variables",
    "",
    "Export a variable with a timeout before you start.",
    "",
    "## Variables",
    "",
    "A variable with a timeout, or a bash setting, is read at startup.",
    "",
    *table(),
    "",
])
CMD_PAGE = "\n".join([
    "# Commands",
    "Source: https://example.com/docs/commands",
    "",
    "## All commands",
    "",
    *[f"| `/cmd{n}` | Command number {n} of the list, and all it does, zzcmd. |" for n in range(500)],
    "",
])
TWO_LONG_PAGE = "\n".join([
    "# Two",
    "Source: https://example.com/docs/two",
    "",
    "## First long",
    "",
    *[f"row {n} zztwo padding padding padding padding padding padding" for n in range(500)],
    "",
    "## Second long",
    "",
    *[f"other {n} zztwo padding padding padding padding padding padding" for n in range(500)],
    "",
])
SHORT_PAGE = ("# Short\nSource: https://example.com/docs/short\n\n## Small\n\n"
              "A bash timeout in a short section, zzcmd, zztwo.\n")
FULL = ENV_PAGE + CMD_PAGE + TWO_LONG_PAGE + SHORT_PAGE
INDEX = ("- [Environment variables](https://example.com/docs/env-vars.md): variables\n"
         "- [Commands](https://example.com/docs/commands.md): the commands\n"
         "- [Two](https://example.com/docs/two.md): two long sections\n"
         "- [Short](https://example.com/docs/short.md): a short page\n")
FULL_URL = "https://example.com/llms-full.txt"
FULL_URL_2 = "https://example.com/two/llms-full.txt"
PAGES = {"url": FULL_URL, "split": "h1", "page_url": "line:Source: "}
SOURCES = {
    "pages": PAGES,
    "ranked-on-index": {**PAGES, "index_url": "https://example.com/llms.txt",
                        "page_ref_forms": ["int", "url", "slug"], "rank_from": "index", "index_join": "url"},
    "second": {**PAGES, "url": FULL_URL_2},
    "both": {"members": ["pages", "second"]},
}


def body(*rows) -> list:
    return [r if r.endswith("\n") else r + "\n" for r in rows]


def next_lines(out: str) -> list:
    return [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]


class SnippetOfALongSectionTest(unittest.TestCase):
    PAD = [f"padding row {n} " + "x" * 80 for n in range(400)]

    def search(self, rows, query="bash timeout", **kw):
        return _common.search_content_in_body(body("## Big", *rows), query, **kw)

    def test_the_lines_with_the_most_words_are_shown_not_the_first(self):
        rows = ["a bash note", "a timeout note", *self.PAD, "bash timeout row", *self.PAD, "a bash tail"]
        hits = self.search(rows, context_lines=0)
        r = hits["results"][0]
        self.assertTrue(r["long_section"])
        marked = [ln for ln in r["snippet"].splitlines() if ln.startswith("→")]
        self.assertEqual(marked, ["→ a bash note", "→ a timeout note", "→ bash timeout row"])
        # the best line first in the choice: with room for one it is the one
        one = self.search(rows, context_lines=0, long_section_hits=1)["results"][0]
        self.assertEqual([ln for ln in one["snippet"].splitlines() if ln.startswith("→")],
                         ["→ bash timeout row"])
        self.assertIn("(3 more hits in this section)", one["snippet"])

    def test_only_the_lines_around_the_hits_are_shown(self):
        rows = [*self.PAD, "before", "bash timeout one", "after", *self.PAD, "bash timeout two", *self.PAD]
        r = self.search(rows, context_lines=1, max_snippet_chars=None)["results"][0]
        self.assertEqual(r["snippet"].splitlines(),
                         ["  before", "→ bash timeout one", "  after", "  ...",
                          "  " + self.PAD[-1], "→ bash timeout two", "  " + self.PAD[0]])

    def test_windows_that_touch_are_one(self):
        rows = [*self.PAD, "bash timeout one", "between", "bash timeout two", *self.PAD]
        r = self.search(rows, context_lines=1, max_snippet_chars=None)["results"][0]
        self.assertEqual(r["snippet"].splitlines(),
                         ["  " + self.PAD[-1], "→ bash timeout one", "  between", "→ bash timeout two",
                          "  " + self.PAD[0]])

    def test_a_tight_budget_leaves_one_gap_line_per_gap(self):
        ctx = "c" * 60
        rows = [*self.PAD, ctx, "bash timeout one", ctx, *self.PAD, ctx, "bash timeout two", ctx, *self.PAD]
        for budget in range(40, 260, 5):
            with self.subTest(budget=budget):
                lines = self.search(rows, context_lines=1, max_snippet_chars=budget)["results"][0]["snippet"].splitlines()
                self.assertEqual([ln for ln in lines if ln.startswith("→")],
                                 ["→ bash timeout one", "→ bash timeout two"])
                doubled = [a for a, b in zip(lines, lines[1:]) if a == b == "  ..."]
                self.assertEqual(doubled, [], lines)

    def test_a_section_that_content_can_show_is_not_long(self):
        # one line of 1 + its newline, under the heading line "## Big\n"
        head = len("## Big\n")
        hit = "bash timeout\n"
        for size, long_section in ((LIMIT, False), (LIMIT + 1, True)):
            with self.subTest(size=size):
                pad = "x" * (size - head - len(hit) - 1) + "\n"
                r = _common.search_content_in_body(["## Big\n", hit, pad], "bash timeout")["results"][0]
                self.assertEqual(r["long_section"], long_section)

    def test_the_size_is_the_sections_own_text_not_its_subsections(self):
        rows = ["bash timeout in the parent", "## Other", *self.PAD]
        lines = body("## Big", *rows)
        results = _common.search_content_in_body(lines, "bash timeout")["results"]
        self.assertFalse(results[0]["long_section"])

    def test_a_short_section_keeps_its_first_hits_and_the_lines_between(self):
        rows = ["a bash note", "plain", "a timeout note", "plain", "plain", "bash timeout row", "bash timeout last"]
        r = self.search(rows, context_lines=0, max_snippet_chars=None)["results"][0]
        self.assertFalse(r["long_section"])
        self.assertEqual(r["snippet"].splitlines(),
                         ["→ a bash note", "  plain", "→ a timeout note", "  plain", "  plain",
                          "→ bash timeout row", "  ... (1 more hits in this section)"])

    def test_the_text_before_the_first_heading_can_be_long(self):
        lines = body(*self.PAD, "bash timeout", *self.PAD)
        r = _common.search_content_in_body(lines, "bash timeout", context_lines=0)["results"][0]
        self.assertEqual(r["heading_path"], "(top)")
        self.assertTrue(r["long_section"])
        short = _common.search_content_in_body(body("bash timeout", "## Next", *self.PAD),
                                               "bash timeout")["results"][0]
        self.assertFalse(short["long_section"])

    def test_the_order_of_the_sections_is_the_one_a_short_section_would_get(self):
        """Most hit lines first, whatever the size: the long section with more
        hits stays above the short one, and a short one with more hits above
        a long one. The first hit line is the section's position either way."""
        long_first = body("## Long", "bash timeout", "bash timeout", *self.PAD, "## Short", "bash timeout")
        self.assertEqual([r["heading_path"] for r in
                          _common.search_content_in_body(long_first, "bash timeout")["results"]],
                         ["Long", "Short"])
        short_first = body("## Long", "bash timeout", *self.PAD, "## Short", "bash timeout", "bash timeout")
        self.assertEqual([r["heading_path"] for r in
                          _common.search_content_in_body(short_first, "bash timeout")["results"]],
                         ["Short", "Long"])
        # hits past the first ones keep counting, and line_offset is the first
        r = _common.search_content_in_body(body("## Long", "a bash note", *self.PAD, "bash timeout"),
                                           "bash timeout")["results"][0]
        self.assertEqual((r["hit_count"], r["line_offset"]), (2, 1))


class _CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llms long ")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = str(Path(self.tmp, "sources.json"))
        Path(self.sources).write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
        self.full = str(Path(self.tmp, "full.txt"))
        self.index = str(Path(self.tmp, "index.txt"))
        Path(self.full).write_text(FULL, encoding="utf-8")
        Path(self.index).write_text(INDEX, encoding="utf-8")
        for name, raw in SOURCES.items():
            if "members" in raw:
                continue
            profile = generic._validate_profile(self.sources, name, raw)
            Path(generic._cache_path(self.tmp, profile)).write_text(FULL, encoding="utf-8")
            if raw.get("index_url"):
                Path(generic._index_cache_path(self.tmp, profile)).write_text(INDEX, encoding="utf-8")
        self.corpora = {"--cache-dir": ("--cache-dir", self.tmp),
                        "--file": ("--file", self.full, "--index-file", self.index)}

    def run_argv(self, argv: list):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled")):
            try:
                return _loader.run_cli(generic, list(argv))
            except Exception as e:  # a crash fails the assertions, not the run
                self.fail(f"{argv} crashed: {e!r}")

    def run_cmd(self, command: str, query: str, source: str, corpus: tuple, *more):
        return self.run_argv([SCRIPT, command, *more, "--source", source, "--sources-file", self.sources,
                              *corpus, "--", query])

    def split_line(self, line: str) -> list:
        try:
            return shlex.split(line, comments=True)
        except ValueError as e:
            self.fail(f"not a shell command: {line}: {e}")

    def option(self, argv: list, flag: str) -> str:
        if flag not in argv:
            self.fail(f"{flag} is missing: {argv}")
        return argv[argv.index(flag) + 1]

    def run_line(self, line: str):
        return self.run_argv(self.split_line(line))

    def line_of(self, lines: list, what: str) -> str:
        found = [ln for ln in lines if what in ln]
        self.assertEqual(len(found), 1, lines)
        return found[0]


class NextLineOfALongSectionTest(_CliCase):
    QUERY = "bash timeout environment variable"

    def test_the_line_searches_the_page_and_reaches_the_matching_row(self):
        for source in ("pages", "ranked-on-index"):
            for name, corpus in self.corpora.items():
                for command in ("search", "search-content"):
                    with self.subTest(source=source, corpus=name, command=command):
                        code, out, err = self.run_cmd(command, self.QUERY, source, corpus)
                        self.assertEqual(code, 0, err)
                        lines = next_lines(out)
                        line = self.line_of(lines, "--page-ref 0 ")
                        self.assertNotIn("content 0 Variables", " ".join(lines))
                        self.assertTrue(line.endswith(_commands.SEARCH_INSIDE_NOTE), line)
                        code, followed, err = self.run_line(line)
                        self.assertEqual(code, 0, (line, err))
                        self.assertRegex(followed, r"(?m)^\[0\] Environment variables")
                        self.assertIn("→ | `BASH_DEFAULT_TIMEOUT_MS`", followed)
                        self.assertIn("→ | `BASH_MAX_TIMEOUT_MS`", followed)

    def test_the_search_itself_shows_the_rows_not_the_first_hits(self):
        code, out, err = self.run_cmd("search", "bash timeout", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertIn("→ | `BASH_DEFAULT_TIMEOUT_MS`", out)
        self.assertNotIn("→ ## Variables", out)

    def test_following_the_line_lists_more_rows_than_the_first_search(self):
        """It must not show the same three lines again: the words here are on
        every padding row."""
        code, out, err = self.run_cmd("search-content", "variable table", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(ln.lstrip().startswith("→") for ln in out.splitlines()), 3, out)
        line = self.line_of(next_lines(out), "--page-ref 0 ")
        code, followed, err = self.run_line(line)
        self.assertEqual(code, 0, (line, err))
        marked = [ln for ln in followed.splitlines() if ln.lstrip().startswith("→")]
        self.assertEqual(len(marked), _common.LONG_SECTION_HITS, followed)
        self.assertRegex(followed, r"\(\d+ more hits in this section\)")

    def test_following_the_line_shows_more_around_a_single_hit(self):
        """One matching row is already on screen after the first search: the
        line is worth following for the rows around it (three on each side)."""
        code, out, err = self.run_cmd("search-content", "BASH_MAX_OUTPUT_LENGTH", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("| `FILLER_", out)
        code, followed, err = self.run_line(self.line_of(next_lines(out), "--page-ref 0 "))
        self.assertEqual(code, 0, err)
        rows = [ln.strip() for ln in followed.splitlines() if "| `" in ln]
        self.assertEqual(len(rows), 7, followed)
        self.assertIn("`FILLER_", rows[0])
        self.assertIn("`FILLER_", rows[-1])

    def test_a_search_of_one_page_offers_to_read_the_section_and_does_not_repeat_itself(self):
        for name, corpus in self.corpora.items():
            with self.subTest(corpus=name):
                code, out, err = self.run_cmd("search-content", self.QUERY, "pages", corpus, "--page-ref", "0")
                self.assertEqual(code, 0, err)
                lines = next_lines(out)
                self.assertFalse([ln for ln in lines if "search-content" in ln], lines)
                code, read, err = self.run_line(self.line_of(lines, "content 0 Variables"))
                self.assertEqual(code, 0, err)
                self.assertIn("## Variables", read)

    def test_a_section_whose_headings_name_the_words_is_read(self):
        """The query is about the section: there is nothing to narrow to."""
        for query in ("all commands",):
            code, out, err = self.run_cmd("search", query, "pages", self.corpora["--file"])
            self.assertEqual(code, 0, err)
            line = self.line_of(next_lines(out), "content 1 ")
            self.assertIn("'All commands'", line)
            self.assertNotIn("search-content", line)

    def test_a_long_section_the_words_only_appear_in_is_searched_on_one_word_too(self):
        code, out, err = self.run_cmd("search", "zzcmd", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.line_of(next_lines(out), "--page-ref 1 ")

    def test_two_long_sections_of_a_page_give_one_line(self):
        code, out, err = self.run_cmd("search-content", "zztwo", "pages", self.corpora["--file"], "--limit", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("Section: "), 2, out)
        self.assertEqual(len(next_lines(out)), 1, out)

    def test_a_short_section_still_gets_content(self):
        code, out, err = self.run_cmd("search-content", "zzcmd", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertIn("content 3 Small", self.line_of(next_lines(out), "content 3 "))

    def test_hostile_queries_run_as_printed_and_with_the_terms_swapped(self):
        queries = ["--timeoutflag", "-t", "\\_", "+*", "the", "'", "bash 'timeout", "$HOME timeout"]
        swaps = ["--help", "-x", "\\_tokens", "the", "+*", "'"]
        reached = 0
        for name, corpus in self.corpora.items():
            for query in queries:
                with self.subTest(corpus=name, query=query):
                    code, out, err = self.run_cmd("search-content", query, "pages", corpus)
                    self.assertEqual(code, 0, err)
                    for line in next_lines(out):
                        if "--page-ref" not in line:
                            continue
                        reached += 1
                        code, followed, err = self.run_line(line)
                        self.assertEqual(code, 0, (line, err))
                        self.assertIn(f'results for "{query}"', followed)
                        self.assertNotIn("No matching content", followed)
                        for word in swaps:
                            argv = self.split_line(line)
                            self.assertEqual(argv[-2], "--", argv)
                            argv = [*argv[:-1], word]
                            code, swapped, err = self.run_argv(argv)
                            self.assertEqual(code, 0, (argv, err))
                            self.assertIn(f'results for "{word}"', swapped)
        self.assertGreater(reached, len(queries))

    def test_a_query_with_a_function_word_is_repeated_as_typed(self):
        code, out, err = self.run_cmd("search-content", "the bash timeout", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertIn("-- 'the bash timeout' #", self.line_of(next_lines(out), "--page-ref 0 "))

    def test_corpus_options_are_kept(self):
        code, out, _err = self.run_cmd("search", "bash timeout", "pages", self.corpora["--cache-dir"])
        self.assertEqual(code, 0)
        argv = self.split_line(self.line_of(next_lines(out), "--page-ref 0 "))
        self.assertEqual(self.option(argv, "--cache-dir"), self.tmp)
        self.assertEqual(self.option(argv, "--source"), "pages")

    def test_a_members_alias_gives_the_pages_own_source(self):
        code, out, err = self.run_argv([SCRIPT, "search", "--source", "both", "--sources-file", self.sources,
                                        "--cache-dir", self.tmp, "--", self.QUERY])
        self.assertEqual(code, 0, err)
        found = [ln for ln in next_lines(out) if "--page-ref" in ln]
        self.assertTrue(found, out)
        sources = set()
        for line in found:
            sources.add(self.option(self.split_line(line), "--source"))
            code, followed, err = self.run_line(line)
            self.assertEqual(code, 0, (line, err))
            self.assertIn("→ | `BASH_DEFAULT_TIMEOUT_MS`", followed)
        self.assertLessEqual(sources, {"pages", "second"})

    def test_the_page_fetching_engine_too(self):
        """``body: "fetch"``: page numbers follow llms.txt, and ``search`` and
        ``search-content`` both reach the same page from the page cache."""
        sources = {"fetched": {"index_url": "https://example.com/llms.txt", "body": "fetch", "rank_from": "index",
                                 "page_ref_forms": ["int", "url", "slug"]}}
        path = Path(self.tmp, "fetch-sources.json")
        path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
        profile = generic._validate_profile(str(path), "fetched", sources["fetched"])
        index = "- [Environment variables](https://example.com/docs/env-vars.md): variables\n"
        Path(generic._index_cache_path(self.tmp, profile)).write_text(index, encoding="utf-8")
        page = Path(generic._page_cache_path(self.tmp, profile, "https://example.com/docs/env-vars.md"))
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(ENV_PAGE, encoding="utf-8")
        for command in ("search", "search-content"):
            with self.subTest(command=command):
                code, out, err = self.run_argv([SCRIPT, command, "--source", "fetched", "--sources-file", str(path),
                                                "--cache-dir", self.tmp, "--", "bash timeout variables"])
                self.assertEqual(code, 0, err)
                code, followed, err = self.run_line(self.line_of(next_lines(out), "--page-ref 0 "))
                self.assertEqual(code, 0, err)
                self.assertIn("→ | `BASH_DEFAULT_TIMEOUT_MS`", followed)
                # the search of one page does not point at itself
                self.assertFalse([ln for ln in next_lines(followed) if "search-content" in ln], followed)

    def test_the_order_of_the_pages_does_not_depend_on_the_length(self):
        """The longest section's page stays first for a query it matches most."""
        code, out, err = self.run_cmd("search-content", "bash timeout", "pages", self.corpora["--file"])
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^\[(\d+)\] ", out), ["0", "3"])


if __name__ == "__main__":
    unittest.main()
