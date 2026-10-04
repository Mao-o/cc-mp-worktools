"""What the four ``parse-*.py`` scripts print after an error or an empty result.

An ambiguous page reference, a heading path that does not resolve, a search
whose ``Next:`` line was a placeholder, and a search with no hits all used to
leave the caller to rebuild the next command by hand (a copied heading with a
typo, a reworded query that cannot help). Each script is checked for the same
four behaviours against a small local corpus (no network): the shared tests
live in ``_GuidanceTests`` and every script only supplies its fixture.

Plus the rule that is specific to URL slugs (``hooks`` -> ``/en/hooks`` rather
than ``/en/agent-sdk/hooks``), which only claude-docs has.
"""

import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _common
import _commands

claude = _loader.load_script("parse-claude-docs.py")
ai_sdk = _loader.load_script("parse-ai-sdk.py")
firebase = _loader.load_script("parse-firebase.py")
generic = _loader.load_script("parse-llms-txt.py")

# Three terms that live in three different sections of the first page, so a
# query naming all of them has every term in the corpus and no section with
# two of them (nothing matches).
HOOKS_BODY = (
    "## Hook events\n"
    "### PreToolUse\n"
    "Runs before a tool call. AlphaTerm\n"
    "## Configuration\n"
    "betaterm\n"
    "## Output\n"
    "gammaterm\n"
    # A nested heading whose title is the full heading_path of a later
    # top-level one: a command naming "Error handling" must read the later.
    "## Tools\n"
    "### Error handling\n"
    "nestedonly\n"
    "## Error handling\n"
    "toplevelword\n"
    # The same heading_path twice: commands can only reach the first.
    "## Repeated\n"
    "### Item\n"
    "dupword first\n"
    "## Repeated\n"
    "### Item\n"
    "dupword second\n"
)
OTHER_BODY = "## Overview\nNothing relevant here.\n"

# What a command line offers to run: a ``Next:`` line, an indented candidate
# (ambiguous page / heading, ``Closest sections:``), or the ``For [i]:`` tail
# of the slug ``Note:``. A trailing duplicate note is a shell comment, so the
# line is run exactly as printed.
DUP_NOTE_RE = re.compile(r"\s+# heading appears (\d+) times; this reads the first$")
NOTE_CMD_RE = re.compile(r"For \[\d+\]: (.+)$")


def offered_commands(text: str, script: str) -> list:
    """``[(command, heading_count), ...]`` for every runnable command in *text*."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Next: "):
            line = line[len("Next: "):]
        elif line.startswith("Note:"):
            m = NOTE_CMD_RE.search(line)
            line = m.group(1) if m else ""
        if not line.startswith(script + " ") or "<" in line:
            continue  # not a command, or a placeholder to fill in by hand
        m = DUP_NOTE_RE.search(line)
        found.append((line, int(m.group(1)) if m else 1))
    return found


class _GuidanceTests:
    """Shared tests; a subclass sets ``module`` / ``script`` and ``write_corpus``.

    ``write_corpus`` leaves two pages: page 0 holds ``HOOKS_BODY`` and a bare
    ``hooks`` reference is ambiguous between pages 0 and 1.
    ``corpus_args`` are the options every command needs; ``tail`` is how the
    scripts echo them in a ``Next:`` line.
    """

    module = None
    script = None
    corpus_args: list
    tail: str
    # AI SDK's heading paths start with the page's H1
    path_prefix = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.write_corpus()

    def run_cmd(self, *args):
        return _loader.run_cli(self.module, [self.script, *args, *self.corpus_args])

    def next_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("Next:")]

    def run_line(self, line):
        """Run a printed command as a shell would split it."""
        return _loader.run_cli(self.module, shlex.split(line, comments=True))

    def assert_commands_run(self, text, *, at_least=1):
        """Run every command *text* offers; each must exit 0, and a ``content``
        command must read exactly the heading_path it names."""
        commands = offered_commands(text, self.script)
        self.assertGreaterEqual(len(commands), at_least, text)
        for line, _count in commands:
            with self.subTest(command=line):
                # the corpus options are on every command, before "--" (after
                # it they are positionals); without them the command reads
                # (or fetches) the default corpus instead
                argv = shlex.split(line, comments=True)
                tail = shlex.split(self.tail)
                end = argv.index("--") if "--" in argv else len(argv)
                self.assertTrue(
                    any(argv[i:i + len(tail)] == tail
                        for i in range(end - len(tail) + 1)), line)
                code, out, err = self.run_line(line)
                self.assertEqual(code, 0, err)
                if "--" in argv:  # every positional is after "--": <page> [heading]
                    heading = argv[argv.index("--") + 1:][1:]
                    heading = heading[0] if heading else None
                else:
                    heading = argv[3] if len(argv) > 3 and not argv[3].startswith("--") else None
                if argv[1] == "content" and heading is not None:
                    self.assertIn(f"# heading_path: {heading}\n", out)
        return commands

    # 1. ambiguous page reference

    def test_ambiguous_reference_prints_a_runnable_command_per_candidate(self):
        code, out, err = self.run_cmd("content", "hooks", "Hook events")
        self.assertEqual(code, 1)
        self.assertIn("Ambiguous", err)
        for idx in (0, 1):
            self.assertRegex(
                err,
                rf"{re.escape(self.script)} content {re.escape(self.tail)} -- {idx} 'Hook events'",
            )

    def test_ambiguous_reference_keeps_the_query_of_search_content(self):
        code, out, err = self.run_cmd("search-content", "alphaterm beta", "--page-ref", "hooks")
        self.assertEqual(code, 1)
        for idx in (0, 1):
            self.assertIn(
                f"{self.script} search-content --page-ref {idx} {self.tail} -- 'alphaterm beta'", err)

    def test_ambiguous_reference_in_sections_has_no_heading(self):
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.script} sections {self.tail} -- 0", err)

    # 2. heading not found

    def test_missing_heading_lists_close_sections_as_commands_first(self):
        code, out, err = self.run_cmd("content", "0", "Hook events/Pre Tool Use")
        self.assertEqual(code, 1)
        self.assertIn("Closest sections:", err)
        self.assertIn("Available sections:", err)
        self.assertLess(err.index("Closest sections:"), err.index("Available sections:"))
        closest = err[err.index("Closest sections:"):err.index("Available sections:")]
        self.assertRegex(
            closest,
            rf"{re.escape(self.script)} content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}",
        )

    # 3. Next: filled in from the search results

    def test_search_next_line_names_the_top_hit_and_its_heading(self):
        code, out, err = self.run_cmd("search", "pretooluse")
        self.assertEqual(code, 0, err)
        lines = self.next_lines(out)
        self.assertTrue(lines, out)
        self.assertRegex(
            lines[0],
            rf"^Next: {re.escape(self.script)} content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}$",
        )
        self.assertFalse([ln for ln in lines if "<heading_path>" in ln or "<page_ref>" in ln], lines)

    def test_search_content_next_line_is_filled_in_too(self):
        code, out, err = self.run_cmd("search-content", "pretooluse")
        self.assertEqual(code, 0, err)
        lines = self.next_lines(out)
        self.assertRegex(lines[0], rf"content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}$")
        self.assertLessEqual(len(lines), 3)

    # 4. no hits

    def test_zero_hits_term_not_in_corpus(self):
        code, out, err = self.run_cmd("search-content", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn("not in this corpus", out)
        self.assertIn("rewording", out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)

    def test_zero_hits_some_terms_present_drops_the_absent_ones(self):
        code, out, err = self.run_cmd("search-content", "alphaterm zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 1 of', out)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn(f"Next: {self.script} search-content alphaterm {self.tail}", out)

    def test_zero_hits_terms_never_together_say_so(self):
        code, out, err = self.run_cmd("search-content", "alphaterm betaterm gammaterm")
        self.assertEqual(code, 0, err)
        self.assertIn("No matching content found", out)
        self.assertIn("not together in one section", out)
        self.assertNotIn("not in this corpus", out)
        self.assertIn(f"Next: {self.script} search-content alphaterm {self.tail}", out)

    def test_zero_hits_with_page_ref_says_the_page_cut_it_out(self):
        code, out, err = self.run_cmd("search-content", "alphaterm", "--page-ref", "1")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 1 of', out)
        self.assertIn("limited the search to one page", out)

    def test_smart_search_with_no_result_gets_the_same_diagnosis(self):
        code, out, err = self.run_cmd("search", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)

    # 5. every printed command runs as printed (round trip)

    def test_search_content_next_reads_the_section_that_hit(self):
        # The hit is the top-level "Error handling"; an earlier nested
        # "Tools/Error handling" has the same title and must not be read.
        code, out, err = self.run_cmd("search-content", "toplevelword")
        self.assertEqual(code, 0, err)
        commands = self.assert_commands_run(out)
        self.assertEqual(shlex.split(commands[0][0])[3], self.path_prefix + "Error handling")
        code, body, err = self.run_line(commands[0][0])
        self.assertIn("toplevelword", body)
        self.assertNotIn("nestedonly", body)

    def test_search_next_runs_as_printed(self):
        code, out, err = self.run_cmd("search", "pretooluse")
        self.assertEqual(code, 0, err)
        self.assert_commands_run(out)

    def test_duplicate_heading_path_is_flagged_on_the_command(self):
        code, out, err = self.run_cmd("search-content", "dupword")
        self.assertEqual(code, 0, err)
        commands = self.assert_commands_run(out)
        self.assertEqual(shlex.split(commands[0][0])[3], self.path_prefix + "Repeated/Item")
        self.assertEqual(commands[0][1], 2, out)
        # both copies hit, but the same command is offered once
        self.assertEqual(len(commands), len(set(commands)), out)
        code, body, err = self.run_line(commands[0][0])
        self.assertIn("dupword first", body)
        # a heading_path found once carries no note
        code, out, err = self.run_cmd("search-content", "toplevelword")
        self.assertNotIn("heading appears", out)

    def test_ambiguous_heading_candidates_run_as_printed(self):
        code, out, err = self.run_cmd("content", "0", "error handl")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous heading", err)
        commands = self.assert_commands_run(err, at_least=2)
        self.assertEqual({shlex.split(c)[3] for c, _n in commands},
                         {self.path_prefix + "Tools/Error handling", self.path_prefix + "Error handling"})

    def test_ambiguous_heading_lists_a_repeated_path_once_with_its_count(self):
        code, out, err = self.run_cmd("content", "0", "repeated/it")
        self.assertEqual(code, 1)
        commands = self.assert_commands_run(err, at_least=1)
        self.assertEqual(commands, [(commands[0][0], 2)])

    def test_closest_sections_run_as_printed(self):
        code, out, err = self.run_cmd("content", "0", "Eror handling")
        self.assertEqual(code, 1)
        closest = err[err.index("Closest sections:"):err.index("Available sections:")]
        commands = self.assert_commands_run(closest, at_least=2)
        self.assertEqual({shlex.split(c)[3] for c, _n in commands},
                         {self.path_prefix + "Tools/Error handling", self.path_prefix + "Error handling"})

    def test_ambiguous_page_candidates_run_as_printed(self):
        # sections: a content retry keeps the heading, which only page 0 has
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 1)
        self.assert_commands_run(err, at_least=2)

    # Words a caller types, or a printed line says to swap in, that look like
    # options or carry no word: every positional of a printed retry follows "--".
    HOSTILE = ["-x", "--limit", "\\_", "the", "---", "'", "-"]

    def ambiguous_retries(self, *argv):
        """The retry lines of an ambiguous page ref, split as a shell splits them."""
        code, out, err = _loader.run_cli(
            self.module, [self.script, *argv])
        self.assertEqual(code, 1, (out, err))
        self.assertIn("Ambiguous", err)
        lines = [ln.strip() for ln in err.splitlines()
                 if ln.strip().startswith(self.script + " ")]
        self.assertEqual(len(lines), 2, err)
        split = []
        for ln in lines:
            try:
                split.append(shlex.split(ln, comments=True))
            except ValueError as exc:  # an unquoted word the shell cannot split
                self.fail(f"not a shell line ({exc}): {ln}")
        return split

    def assert_parsed_and_reached(self, argv, page, code, err):
        """Exit 2 is argparse refusing the line; the page must be the one named."""
        self.assertNotEqual(code, 2, (argv, err))
        self.assertIn(str(page), argv)

    def test_retry_lines_keep_a_dash_query_or_heading_a_value(self):
        for word in self.HOSTILE:
            with self.subTest(sub="search-content", word=word):
                retries = self.ambiguous_retries(
                    "search-content", "--page-ref", "hooks", *self.corpus_args, "--", word)
                for idx, argv in enumerate(retries):
                    self.assertEqual(argv[-2:], ["--", word])
                    self.assertNotIn(word, argv[:-2] if word.startswith("-") else [])
                    code, out, err = _loader.run_cli(self.module, argv)
                    self.assert_parsed_and_reached(argv, idx, code, err)
                    # the printed value is a swap-in: another dash word still parses
                    swapped = argv[:-1] + ["--resume"]
                    code, out, err = _loader.run_cli(self.module, swapped)
                    self.assertNotEqual(code, 2, (swapped, err))
            with self.subTest(sub="content", word=word):
                retries = self.ambiguous_retries(
                    "content", *self.corpus_args, "--", "hooks", word)
                for idx, argv in enumerate(retries):
                    self.assertEqual(argv[-3:], ["--", str(idx), word])
                    code, out, err = _loader.run_cli(self.module, argv)
                    self.assertEqual(code, 1, (argv, err))  # reads the page, no such heading
                    self.assertIn("not found", err)
                    swapped = argv[:-1] + ["--resume"]
                    code, out, err = _loader.run_cli(self.module, swapped)
                    self.assertEqual(code, 1, (swapped, err))
                    self.assertIn("not found", err)

    def test_retry_lines_keep_the_options_before_the_dashes(self):
        retries = self.ambiguous_retries(
            "search-content", "--page-ref", "hooks", "--limit", "3", "--context", "1",
            *self.corpus_args, "--", "-x")
        for idx, argv in enumerate(retries):
            self.assertEqual(argv[:4], [self.script, "search-content", "--page-ref", str(idx)])
            self.assertEqual(argv[4:8], ["--limit", "3", "--context", "1"])
            code, out, err = _loader.run_cli(self.module, argv)
            self.assertNotEqual(code, 2, err)
        retries = self.ambiguous_retries(
            "content", "--max-chars", "100", *self.corpus_args, "--", "hooks", "Hook events")
        for idx, argv in enumerate(retries):
            self.assertEqual(argv[:4], [self.script, "content", "--max-chars", "100"])
            self.assertEqual(argv[-3:], ["--", str(idx), "Hook events"])
        retries = self.ambiguous_retries("sections", *self.corpus_args, "--", "hooks")
        for idx, argv in enumerate(retries):
            self.assertEqual(argv[-2:], ["--", str(idx)])
            code, out, err = _loader.run_cli(self.module, argv)
            self.assertEqual(code, 0, err)
            self.assertIn(f"Sections in [{idx}]", out)

    def test_zero_hit_next_lines_run_as_printed(self):
        for sub in ("search-content", "search"):
            for query in ("zzzmissing qqqmissing", "alphaterm zzzmissing",
                          "alphaterm betaterm gammaterm"):
                with self.subTest(sub=sub, query=query):
                    code, out, err = self.run_cmd(sub, query)
                    self.assertEqual(code, 0, err)
                    # may offer nothing (claude-docs --file: no search-index)
                    self.assert_commands_run(out, at_least=0)

    def test_retries_keep_the_other_options(self):
        code, out, err = self.run_cmd("content", "hooks", "Hook events", "--max-chars", "100")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.script} content --max-chars 100 {self.tail} -- 0 'Hook events'", err)
        code, out, err = self.run_cmd("search-content", "alphaterm", "--page-ref", "hooks",
                                      "--limit", "3", "--context", "1")
        self.assertEqual(code, 1)
        self.assertIn(f"search-content --page-ref 0 --limit 3 --context 1 {self.tail} -- alphaterm", err)
        code, out, err = self.run_cmd("content", "0", "Eror handling", "--max-chars", "50")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.script} content 0 '{self.path_prefix}Error handling' --max-chars 50 ", err)
        self.assert_commands_run(err[:err.index("Available sections:")], at_least=2)
        # a default value is not echoed
        code, out, err = self.run_cmd("content", "hooks", "Hook events", "--max-chars", "24000")
        self.assertNotIn("--max-chars", err)


class ClaudeDocsGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = claude
    script = "parse-claude-docs.py"

    def write_corpus(self):
        pages = [
            ("Hooks", "https://example.com/en/cli/hooks", HOOKS_BODY),
            ("Agent SDK Hooks", "https://example.com/en/agent-sdk/hooks", OTHER_BODY),
        ]
        # both sources: a 0-hit search offers the other one, and the round
        # trip runs that command too (it must not reach the network)
        for prefix in ("claude-code", "claude-platform"):
            Path(self.tmp, f"{prefix}-llms.txt").write_text(
                "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")
            Path(self.tmp, f"{prefix}-llms-full.txt").write_text(
                "".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in pages), encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"


class ClaudeDocsFileGuidanceTest(ClaudeDocsGuidanceTest):
    """The same with ``--file``: claude-docs' ``search-index`` has no
    ``--file``, so no hint may send the reader there with it."""

    def write_corpus(self):
        super().write_corpus()
        # search ranks on the llms.txt index, which --file does not replace;
        # keep it in the temporary dir instead of the real cache
        patcher = mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        full = str(Path(self.tmp, "claude-code-llms-full.txt"))
        self.corpus_args = ["--file", full]
        self.tail = f"--file {full}"

    def test_zero_hits_term_not_in_corpus(self):
        code, out, err = self.run_cmd("search-content", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn("not in this corpus", out)
        self.assertNotIn("search-index", "".join(self.next_lines(out)))

    def test_smart_search_with_no_result_gets_the_same_diagnosis(self):
        code, out, err = self.run_cmd("search", "frobnicator quuxify")
        self.assertEqual(code, 0, err)
        self.assertIn('"frobnicator": 0 of', out)
        self.assertNotIn("search-index", "".join(self.next_lines(out)))


class AiSdkGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = ai_sdk
    script = "parse-ai-sdk.py"
    path_prefix = "Hooks/"

    def write_corpus(self):
        pages = [("Hooks", HOOKS_BODY), ("Agent Hooks", OTHER_BODY)]
        Path(self.tmp, "ai-sdk-llms-full.txt").write_text(
            "".join(f"---\ntitle: {t}\ndescription: about {t}\n---\n\n# {t}\n\n{b}\n" for t, b in pages),
            encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"


class AiSdkFileGuidanceTest(AiSdkGuidanceTest):
    """The same with ``--file`` (AI SDK's ``search-index`` takes it too)."""

    def write_corpus(self):
        super().write_corpus()
        full = str(Path(self.tmp, "ai-sdk-llms-full.txt"))
        self.corpus_args = ["--file", full]
        self.tail = f"--file {full}"


class FirebaseGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = firebase
    script = "parse-firebase.py"

    def write_corpus(self):
        pages = [
            ("Hooks", "https://firebase.google.com/docs/functions/hooks.md.txt", HOOKS_BODY),
            ("Auth Hooks", "https://firebase.google.com/docs/auth/hooks.md.txt", OTHER_BODY),
        ]
        # the index (all Firebase's smart search ranks on) must name the term
        Path(self.tmp, "firebase-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}, pretooluse\n" for t, u, _b in pages), encoding="utf-8")
        pages_dir = Path(self.tmp, "firebase-docs")
        pages_dir.mkdir()
        for t, u, b in pages:
            (pages_dir / firebase._url_to_cache_filename(u)).write_text(f"# {t}\n\n{b}\n", encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"

    def test_zero_hits_with_page_ref_says_the_page_cut_it_out(self):
        # Pages are fetched lazily: only the --page-ref page is counted.
        code, out, err = self.run_cmd("search-content", "alphaterm", "--page-ref", "1")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 0 of 1', out)
        self.assertIn("counts cover only the --page-ref 1 page", out)
        self.assertNotIn("not anywhere", out)

    def test_smart_search_with_no_result_gets_the_same_diagnosis(self):
        # Firebase's smart search ranks on the index alone, so the counts
        # cover title/description, and the page bodies are not fetched.
        code, out, err = self.run_cmd("search", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn("index entries (title/description)", out)
        self.assertIn('"zzzmissing": 0 of 2', out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)


class GenericGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = generic
    script = "parse-llms-txt.py"

    def write_corpus(self):
        sources = Path(self.tmp, "sources.json")
        sources.write_text(json.dumps({"sources": {"site": {
            "url": "https://example.com/llms-full.txt", "split": "h1"}}}), encoding="utf-8")
        corpus = Path(self.tmp, "full.txt")
        corpus.write_text(f"# Hooks\n\n{HOOKS_BODY}\n# Agent Hooks\n\n{OTHER_BODY}", encoding="utf-8")
        self.corpus_args = ["--source", "site", "--sources-file", str(sources), "--file", str(corpus)]
        self.tail = f"--source site --sources-file {sources} --file {corpus}"


class ClaudeDocsSourceBothTest(unittest.TestCase):
    """Only ``search`` takes ``--source both``. Any other command that got it
    used to die with argparse's ``invalid choice`` and no way forward; it now
    exits 2 and prints the same command once per source (plus ``search`` for
    a command with a query), and every printed line runs as it stands."""

    module = claude
    script = "parse-claude-docs.py"
    setUp = _GuidanceTests.setUp
    write_corpus = ClaudeDocsGuidanceTest.write_corpus
    run_cmd = _GuidanceTests.run_cmd
    run_line = _GuidanceTests.run_line
    assert_commands_run = _GuidanceTests.assert_commands_run

    def test_each_single_source_command_answers_with_runnable_commands(self):
        cases = {
            "search-content": ["search-content", "alphaterm"],
            "search-index": ["search-index", "hooks"],
            "content": ["content", "0"],
            "sections": ["sections", "0"],
            "fetch-index": ["fetch-index"],
        }
        for name, argv in cases.items():
            with self.subTest(command=name):
                code, out, err = self.run_cmd(*argv, "--source", "both")
                self.assertEqual(code, 2, err)
                self.assertNotIn("invalid choice", err)
                self.assertIn("--source both", err)
                commands = self.assert_commands_run(err, at_least=2)
                lines = [c for c, _n in commands]
                self.assertTrue(any("--source code" in c for c in lines), err)
                self.assertTrue(any("--source platform" in c for c in lines), err)
                # a line that still says both must be the search one
                for c in lines:
                    if "both" in c:
                        self.assertIn(f"{self.script} search ", c)

    def test_a_command_with_a_query_also_offers_search_for_both(self):
        for argv in (["search-content", "alphaterm"], ["search-index", "hooks"]):
            with self.subTest(command=argv[0]):
                code, out, err = self.run_cmd(*argv, "--source", "both")
                self.assertEqual(code, 2, err)
                self.assertIn(f"{self.script} search {argv[1]} --source both", err)
        # content / sections have no query to search for
        code, out, err = self.run_cmd("content", "0", "--source", "both")
        self.assertNotIn(f"{self.script} search ", err)

    def test_the_other_options_survive_in_the_printed_commands(self):
        code, out, err = self.run_cmd("search-content", "alphaterm", "--limit", "3",
                                      "--source=both")
        self.assertEqual(code, 2, err)
        self.assertIn("--limit 3 --source=code", err)
        self.assertIn("--limit 3 --source=platform", err)
        self.assert_commands_run(err, at_least=3)

    def test_search_itself_still_takes_both(self):
        code, out, err = self.run_cmd("search", "alphaterm", "--source", "both")
        self.assertEqual(code, 0, err)

    # -- round trip: every printed line, split as a shell would, must run ------

    def run_printed(self, argv, *, expect_lines=2):
        """Run *argv* (exit 2), then every command it printed, unmodified."""
        code, out, err = _loader.run_cli(self.module, [self.script, *argv])
        self.assertEqual(code, 2, err)
        lines = [ln.strip() for ln in err.splitlines()
                 if ln.strip().startswith(self.script + " ")]
        self.assertGreaterEqual(len(lines), expect_lines, err)
        for line in lines:
            with self.subTest(argv=argv, line=line):
                code, out2, err2 = self.run_line(line)
                self.assertEqual(code, 0, f"{line}\n{err2}")
        return lines, err

    def use_file(self):
        patcher = mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_without_file(self):
        c = ["--cache-dir", self.tmp]
        self.run_printed(["content", "0", "--source", "both", *c], expect_lines=2)
        self.run_printed(["search-content", "alphaterm", "--source=both", *c], expect_lines=3)

    def test_round_trip_with_file_keeps_only_the_snapshots_own_source(self):
        self.use_file()
        full = str(Path(self.tmp, "claude-code-llms-full.txt"))
        for argv in (["content", "0"], ["sections", "0"], ["search-content", "alphaterm"]):
            with self.subTest(argv=argv):
                lines, err = self.run_printed([*argv, "--source", "both", "--file", full],
                                              expect_lines=1)
                self.assertTrue(all("--source code" in ln for ln in lines), lines)
                self.assertFalse(any("--source platform" in ln for ln in lines), lines)
        # the --source=both spelling takes the same path
        lines, err = self.run_printed(["content", "0", "--source=both", "--file", full],
                                      expect_lines=1)
        self.assertEqual(len(lines), 1, lines)

    def test_file_that_names_no_source_says_to_keep_one_line(self):
        self.use_file()
        snap = str(Path(self.tmp, "snap.txt"))
        shutil.copy(Path(self.tmp, "claude-code-llms-full.txt"), snap)
        code, out, err = _loader.run_cli(
            self.module, [self.script, "content", "0", "--source", "both", "--file", snap])
        self.assertEqual(code, 2, err)
        self.assertIn("keep only the line", err)

    def test_abbreviated_option_still_gets_a_source_per_line(self):
        c = ["--cache-dir", self.tmp]
        lines, err = self.run_printed(["search-content", "alphaterm", "--sour", "both", *c],
                                      expect_lines=3)
        singles = [ln for ln in lines if " search " not in ln]
        self.assertTrue(any(ln.endswith("--source code") or "--source code " in ln
                            for ln in singles), singles)
        self.assertTrue(any("--source platform" in ln for ln in singles), singles)
        # with "--", the added --source goes before it, not into the positionals
        lines, err = self.run_printed(["search-content", "--sour", "both", *c, "--", "-foo"],
                                      expect_lines=3)
        for ln in lines:
            if " search " not in ln:
                self.assertLess(ln.index("--source "), ln.index("-- -foo"), ln)

    def test_a_query_starting_with_a_dash_gets_double_dash_in_search(self):
        c = ["--cache-dir", self.tmp]
        lines, err = self.run_printed(
            ["search-content", "--source", "both", *c, "--", "-foo"], expect_lines=3)
        searches = [ln for ln in lines if f"{self.script} search " in ln]
        self.assertEqual(len(searches), 1, lines)
        self.assertIn("-- -foo", searches[0])
        self.assertLess(searches[0].index("--source"), searches[0].index("-- -foo"))
        # the per-source lines keep the "--" the user typed and gain no
        # --source after it
        for ln in lines:
            if ln not in searches:
                self.assertLess(ln.index("--source"), ln.index("-- -foo"), ln)

    def test_help_does_not_advertise_both_as_a_general_choice(self):
        code, out, err = _loader.run_cli(self.module, [self.script, "content", "-h"])
        self.assertIn("only for search", " ".join((out + err).split()))


class ClaudeDocsSlugTest(unittest.TestCase):
    """``hooks`` is ``/en/hooks``, not ``/en/agent-sdk/hooks``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        pages = [
            ("Agent SDK Hooks", "https://example.com/docs/en/agent-sdk/hooks", OTHER_BODY),
            ("Hooks", "https://example.com/docs/en/hooks", HOOKS_BODY),
        ]
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in pages), encoding="utf-8")

    def run_cmd(self, *args):
        return _loader.run_cli(claude, ["parse-claude-docs.py", *args, "--cache-dir", self.tmp])

    def test_bare_slug_resolves_to_the_exact_lang_page_and_names_the_others(self):
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 0, err)
        self.assertIn('Sections in [1] "Hooks"', out)
        self.assertIn("[0] https://example.com/docs/en/agent-sdk/hooks", err)

    def test_note_ends_with_a_command_for_the_other_page(self):
        code, out, err = self.run_cmd("content", "hooks", "Overview", "--max-chars", "100")
        self.assertEqual(code, 1)  # page 1 has no "Overview"; the Note comes first
        self.assertIn(f"For [0]: parse-claude-docs.py content --max-chars 100 "
                      f"--cache-dir {self.tmp} -- 0 Overview", err)
        code, out, err = self.run_cmd("sections", "hooks")
        commands = offered_commands(err, "parse-claude-docs.py")
        self.assertEqual(commands, [(f"parse-claude-docs.py sections --cache-dir {self.tmp} -- 0", 1)])
        code, out, err = _loader.run_cli(claude, shlex.split(commands[0][0], comments=True))
        self.assertEqual(code, 0, err)
        self.assertIn('Sections in [0] "Agent SDK Hooks"', out)

    def test_longer_slug_still_reaches_the_other_page(self):
        code, out, err = self.run_cmd("sections", "agent-sdk/hooks")
        self.assertEqual(code, 0, err)
        self.assertIn('Sections in [0] "Agent SDK Hooks"', out)

    def test_two_languages_of_the_same_slug_stay_ambiguous(self):
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "# Hooks\nSource: https://example.com/docs/en/hooks\n\nx\n"
            "# Hooks ja\nSource: https://example.com/docs/ja/hooks\n\nx\n", encoding="utf-8")
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 1)
        self.assertIn("Ambiguous slug 'hooks'", err)
        self.assertIn("parse-claude-docs.py sections --cache-dir", err)
        self.assertIn(" -- 1", err)


SKILLS_DIR = Path(_loader.SCRIPTS_DIR).parent / "skills"
SKILL_NAMES = ["researching-claude-docs", "researching-ai-sdk", "researching-firebase",
               "researching-library-docs"]


class SkillGuidanceTest(unittest.TestCase):
    """The four SKILL.md say the same thing about how many calls to spend and
    how to read the new error output; only the per-source details differ."""

    def text(self, name):
        return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    def test_every_skill_carries_the_same_rules(self):
        shared = [
            "## 調査の進め方 (手数の目安)",
            "**`search` 1 回 → `content` 1〜2 回**",
            "同じ論点で `search` を **3 回外したら**",
            "**`--max-chars 0`** は、出力に `... (N chars truncated; ...)` が出て",
            "`| head` / `| grep` で出力を切らず",
            "`already executing in this forked",
            "## Skill を呼べない文脈 (subagent など)",
            "WebFetch する前に、",
            "`${CLAUDE_PLUGIN_ROOT}/scripts/",
        ]
        for name in SKILL_NAMES:
            text = self.text(name)
            for needle in shared:
                with self.subTest(skill=name, needle=needle):
                    self.assertIn(needle, text)

    def test_failure_table_names_what_the_scripts_print(self):
        # The strings the table quotes must be strings the scripts still print.
        printed = "".join(
            (Path(_loader.SCRIPTS_DIR) / f).read_text(encoding="utf-8")
            for f in ("_common.py", "_commands.py", "parse-claude-docs.py", "parse-ai-sdk.py",
                      "parse-firebase.py", "parse-llms-txt.py"))
        for quoted in ("Why nothing matched", "Closest sections:", "Available sections:",
                       "Ambiguous {kind}", "Note: '{page_ref}' also matches"):
            self.assertIn(quoted, printed)
        for name in SKILL_NAMES:
            text = self.text(name)
            for needle in ("Why nothing matched:", "Closest sections:", "Available sections:",
                           "Ambiguous"):
                with self.subTest(skill=name, needle=needle):
                    self.assertIn(needle, text)


class NearHeadingsTest(unittest.TestCase):
    SECTIONS = [
        {"title": t, "heading_path": p, "level": 2}
        for t, p in [
            ("Hook events", "Hook events"),
            ("PreToolUse", "Hook events/PreToolUse"),
            ("PostToolUse", "Hook events/PostToolUse"),
            ("Configuration", "Configuration"),
        ]
    ]

    def paths(self, query):
        return [s["heading_path"] for s in _common.near_headings(query, self.SECTIONS)]

    def test_last_element_is_matched_ignoring_case_and_spacing(self):
        self.assertEqual(self.paths("Events/pre tool use")[0], "Hook events/PreToolUse")
        self.assertEqual(self.paths("CONFIGURATION")[0], "Configuration")

    def test_title_containing_the_guess_ranks_before_a_path_match(self):
        self.assertEqual(self.paths("tooluse")[:2],
                         ["Hook events/PreToolUse", "Hook events/PostToolUse"])

    def test_a_longer_guess_still_finds_the_shorter_title(self):
        self.assertEqual(self.paths("Hook events/PreToolUse hook handler configuration")[:1],
                         ["Hook events/PreToolUse"])

    def test_typo_falls_back_to_closeness(self):
        self.assertEqual(self.paths("Configuraton"), ["Configuration"])

    def test_closeness_needs_more_than_a_loose_resemblance(self):
        # difflib puts these two at exactly 0.6
        sections = [{"title": "Change a setting", "heading_path": "Change a setting", "level": 2}]
        self.assertEqual(_common.near_headings("nonexistent thing", sections), [])
        self.assertEqual(len(_common.near_headings("Change a settnig", sections)), 1)

    def test_a_repeated_path_is_offered_once(self):
        twice = self.SECTIONS + [dict(self.SECTIONS[1])]
        paths = [s["heading_path"] for s in _common.near_headings("PreToolUse", twice)]
        self.assertEqual(paths, self.paths("PreToolUse"))
        self.assertEqual(paths.count("Hook events/PreToolUse"), 1)

    def test_nothing_close_gives_nothing(self):
        self.assertEqual(self.paths("zzzzzz"), [])

    def test_at_most_five(self):
        many = [{"title": f"Tool {i}", "heading_path": f"Tool {i}", "level": 2} for i in range(9)]
        self.assertEqual(len(_common.near_headings("tool", many)), 5)


class ExtractContentOrderTest(unittest.TestCase):
    """A full heading_path beats an earlier section whose bare title is the same."""

    BODY = ["## Tools\n", "### Error handling\n", "A\n", "## Error handling\n", "B\n"]

    def test_full_path_wins_over_an_earlier_title(self):
        content, resolved = _common.extract_content(self.BODY, "Error handling")
        self.assertEqual(resolved, "Error handling")
        self.assertIn("B", content)
        self.assertNotIn("A\n", content)

    def test_same_order_when_case_differs(self):
        content, resolved = _common.extract_content(self.BODY, "error handling")
        self.assertEqual(resolved, "Error handling")
        self.assertNotIn("A\n", content)

    def test_a_bare_title_still_reaches_the_nested_section(self):
        body = ["## Tools\n", "### Retries\n", "A\n", "## Other\n", "B\n"]
        content, resolved = _common.extract_content(body, "Retries")
        self.assertEqual(resolved, "Tools/Retries")


class PreferLangExactTest(unittest.TestCase):
    def test_only_a_lang_segment_counts_as_exact(self):
        cands = [(0, "https://x.example/docs/en/agent-sdk/hooks"), (1, "https://x.example/docs/en/hooks")]
        self.assertEqual(_common.prefer_lang_exact(cands, "hooks"), [cands[1]])
        # no language segment: a longer path is not "more exact"
        plain = [(0, "https://x.example/a/hooks"), (1, "https://x.example/b/c/hooks")]
        self.assertEqual(_common.prefer_lang_exact(plain, "hooks"), plain)
        # a directory that merely looks like a two-letter code is not one
        # when it is not followed by exactly the slug
        deeper = [(0, "https://x.example/ai/tools/hooks"), (1, "https://x.example/ai/hooks")]
        self.assertEqual(_common.prefer_lang_exact(deeper, "hooks"), [deeper[1]])


class HitCandidatesTest(unittest.TestCase):
    @staticmethod
    def hits(*paths):
        return {"results": [{"heading_path": p} for p in paths]}

    def test_best_section_of_each_top_page_first(self):
        ranked = [(3, self.hits("A", "A2"), ()), (7, self.hits("B"), ()), (9, self.hits("C"), ())]
        self.assertEqual([(r, h) for r, h, _e, _n in _commands.hit_candidates(ranked)],
                         [(3, "A"), (7, "B"), (9, "C")])

    def test_single_page_fills_with_its_next_sections(self):
        ranked = [(3, self.hits("A", "A2", "A3", "A4"), ())]
        self.assertEqual([h for _r, h, _e, _n in _commands.hit_candidates(ranked)], ["A", "A2", "A3"])

    def test_index_only_page_has_no_heading(self):
        self.assertEqual(_commands.hit_candidates([(4, {"results": []}, ())]), [(4, None, (), 1)])

    def test_index_only_pages_are_dropped_when_a_page_has_body_hits(self):
        ranked = [(4, {"results": []}, ()), (5, self.hits("A"), ())]
        self.assertEqual(_commands.hit_candidates(ranked), [(5, "A", (), 1)])

    def test_per_page_source_is_kept(self):
        ranked = [(1, self.hits("A"), ("--source", "platform"))]
        self.assertEqual(_commands.hit_candidates(ranked), [(1, "A", ("--source", "platform"), 1)])

    def test_keep_takes_the_last_line_when_pages_above_fill_every_line(self):
        ranked = [(1, self.hits("A"), ()), (2, self.hits("B"), ()), (3, self.hits("C"), ()),
                  (7, self.hits("K"), ())]
        self.assertEqual([r for r, _h, _e, _n in _commands.hit_candidates(ranked, keep=(7, ()))],
                         [1, 2, 7])

    def test_keep_already_picked_changes_nothing(self):
        ranked = [(1, self.hits("A", "A2"), ()), (7, self.hits("K"), ())]
        self.assertEqual(_commands.hit_candidates(ranked, keep=(7, ())),
                         _commands.hit_candidates(ranked))

    def test_keep_matches_the_per_page_source_too(self):
        ranked = [(1, self.hits("A"), ("--source", "code")), (2, self.hits("B"), ()),
                  (3, self.hits("C"), ()), (1, self.hits("P"), ("--source", "platform"))]
        got = _commands.hit_candidates(ranked, keep=(1, ("--source", "platform")))
        self.assertEqual(got[-1], (1, "P", ("--source", "platform"), 1))

    def test_keep_without_body_hits_is_not_offered(self):
        ranked = [(1, self.hits("A"), ()), (2, self.hits("B"), ()), (3, self.hits("C"), ()),
                  (7, {"results": []}, ())]
        self.assertEqual([r for r, _h, _e, _n in _commands.hit_candidates(ranked, keep=(7, ()))],
                         [1, 2, 3])

    def test_heading_count_is_carried(self):
        ranked = [(2, {"results": [{"heading_path": "R/I", "heading_count": 2}]}, ())]
        self.assertEqual(_commands.hit_candidates(ranked), [(2, "R/I", (), 2)])


# A page whose headings look like options (a CLI reference: "## --help"),
# next to an ordinary one.
DASH_BODY = (
    "## Hook events\nplain\n"
    "## -x\nxword\n"
    "## --limit\nlimitword\n"
    "## ---\nrulerword\n"
    "## -\nbareword\n"
    "## --help\nhelpword\n"
    "## --help-all\nhelpallword\n"
)


class ContentCommandDashHeadingTest(unittest.TestCase):
    """``content_command`` lines for a heading that starts with ``-``: every
    positional goes after one ``--``, and a line run exactly as printed
    reaches that section. Other headings keep the old form byte for byte."""

    DASH_HEADINGS = ["-x", "--limit", "---", "-", "--help", "--help-all"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        pages = [("Hooks", "https://example.com/en/cli/hooks", DASH_BODY)]
        for prefix in ("claude-code", "claude-platform"):
            Path(self.tmp, f"{prefix}-llms.txt").write_text(
                "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")
            Path(self.tmp, f"{prefix}-llms-full.txt").write_text(
                "".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in pages), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        full = str(Path(self.tmp, "claude-code-llms-full.txt"))
        self.corpus = {"--cache-dir": ["--cache-dir", self.tmp], "--file": ["--file", full]}

    def assert_reads(self, line, heading):
        argv = shlex.split(line, comments=True)
        code, out, err = _loader.run_cli(claude, argv)
        self.assertEqual(code, 0, (line, err))
        self.assertIn(f"# heading_path: {heading}\n", out, line)

    def test_every_dash_heading_round_trips_for_both_corpus_options(self):
        for opt, args in self.corpus.items():
            hint = tuple(shlex.quote(a) for a in args)
            for heading in self.DASH_HEADINGS:
                with self.subTest(opt=opt, heading=heading):
                    line = _common.content_command("parse-claude-docs.py", 0, heading, hint)
                    words = shlex.split(line)
                    self.assertEqual(words[:2], ["parse-claude-docs.py", "content"])
                    self.assertEqual(words[-3:], ["--", "0", heading], line)
                    self.assert_reads(line, heading)

    def test_search_next_line_for_a_dash_heading_runs_as_printed(self):
        # the Next: lines of search / search-content are built separately
        # from content_command; they need the same form
        cases = [("search-content", "helpword"), ("search-content", "xword"),
                 ("search", "limitword")]
        for opt, args in self.corpus.items():
            for sub, word in cases:
                with self.subTest(opt=opt, sub=sub, word=word):
                    code, out, err = _loader.run_cli(
                        claude, ["parse-claude-docs.py", sub, *args, "--", word])
                    self.assertEqual(code, 0, err)
                    nexts = [ln[len("Next: "):] for ln in out.splitlines()
                             if ln.startswith("Next: parse-claude-docs.py content ")]
                    self.assertTrue(nexts, out)
                    argv = shlex.split(nexts[0], comments=True)
                    self.assertIn("--", argv, nexts[0])
                    code, body, err = _loader.run_cli(claude, argv)
                    self.assertEqual(code, 0, (nexts[0], err))
                    self.assertIn(word, body, nexts[0])

    def test_options_stay_before_the_dashes(self):
        line = _common.content_command(
            "s.py", 3, "--a b", ("--max-chars", "100", "--cache-dir", "'/a b'"))
        self.assertEqual(
            shlex.split(line),
            ["s.py", "content", "--max-chars", "100", "--cache-dir", "/a b", "--", "3", "--a b"])

    def test_printed_candidates_run_as_printed(self):
        # an ambiguous heading ("help" matches two), and typos with the
        # nearest heading offered: each printed command reads its section
        cases = [("help", {"--help", "--help-all"}), ("--limt", {"--limit"}),
                 ("-x-", {"-x"}), ("--helpp", {"--help"})]
        for opt, args in self.corpus.items():
            for query, expected in cases:
                with self.subTest(opt=opt, query=query):
                    code, out, err = _loader.run_cli(
                        claude, ["parse-claude-docs.py", "content", *args, "--", "0", query])
                    self.assertEqual(code, 1, err)
                    # the candidates printed before "Available sections:"
                    offered = [c for c, _n in offered_commands(
                        err.split("Available sections:")[0], "parse-claude-docs.py")]
                    got = set()
                    for line in offered:
                        argv = shlex.split(line, comments=True)
                        self.assertEqual(argv[-3:-1], ["--", "0"], line)
                        self.assert_reads(line, argv[-1])
                        got.add(argv[-1])
                    self.assertEqual(got, expected, err)

    def test_ordinary_headings_keep_the_plain_form(self):
        cases = [
            ((0, "Hook events", ("--cache-dir", "/c")),
             "s.py content 0 'Hook events' --cache-dir /c"),
            ((2, "Tools/Error handling", ()), "s.py content 2 'Tools/Error handling'"),
            ((1, "a-b", ("--file", "/f")), "s.py content 1 a-b --file /f"),
            ((1, "Opts/--x", ()), "s.py content 1 Opts/--x"),
            ((4, None, ("--file", "/f")), "s.py content 4 --file /f"),
        ]
        for (ref, heading, hint), expected in cases:
            with self.subTest(heading=heading):
                self.assertEqual(
                    _common.content_command("s.py", ref, heading, hint), expected)


if __name__ == "__main__":
    unittest.main()
