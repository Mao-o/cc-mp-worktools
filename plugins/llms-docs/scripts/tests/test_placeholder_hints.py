"""The ``Next:`` lines that carry a placeholder (``"<query>"``,
``"<heading_path>"``, ``"<heading_path from above>"``, ``<page_ref>``) keep
working when the reader puts in its place what the line asks for, a word
that starts with ``-`` included: the options come first and every
positional after ``--``.

Each line is taken from the output as printed, the placeholder replaced by
the shell-quoted word, split by the shell rules and run; it must parse
(argparse exits 2 on a word it reads as an option) and reach the page or
section it names. Both corpus options are run: the cache (``--cache-dir``)
and ``--file`` with ``--index-file``.
"""

import contextlib
import io
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


if __name__ == "__main__":
    unittest.main()
