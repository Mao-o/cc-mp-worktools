"""The shims at the retired script names (``_shim.py``).

``parse-claude-docs.py`` / ``parse-ai-sdk.py`` / ``parse-firebase.py`` hand
their arguments, translated, to ``parse-llms-txt.py`` with ``os.execv``:

* ``ConvertTest``: the translation itself (``--source`` values and the
  default, ``--index-limit``, abbreviations the old scripts accepted, ``--``)
* ``OldOptionTableTest``: the option table the translation reads is the old
  scripts' (read from their parsers under ``tests/legacy/``), and every
  option in it but ``--index-limit`` is an option of the generic loader
* ``ShimRunTest``: the shim replaces itself with ``sys.executable
  parse-llms-txt.py …`` and, for every parity case, prints what the generic
  loader prints for the preset (same stdout, stderr, exit code, fetches)
* ``ShimNextRoundTripTest``: every command the output recommends names
  ``parse-llms-txt.py`` and, split by the shell's rules and run as printed
  (placeholders replaced as the output says), exits 0
* ``ShimProcessTest``: one real process per shim (the ``os.execv`` path)
"""

import argparse
import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader
import _shim
import test_migration_parity as parity

SHIMS = {"claude-docs": "parse-claude-docs.py", "ai-sdk": "parse-ai-sdk.py", "firebase": "parse-firebase.py"}
GENERIC = os.path.realpath(os.path.join(_loader.SCRIPTS_DIR, "parse-llms-txt.py"))


class ConvertTest(unittest.TestCase):

    CASES = [
        # (label, kind, argv, expected)
        ("claude default source", "claude-docs", ["search", "hook matcher"],
         ["search", "--source", "claude-code", "hook matcher"]),
        ("claude code", "claude-docs", ["search", "q", "--source", "code"],
         ["search", "q", "--source", "claude-code"]),
        ("claude platform", "claude-docs", ["content", "3", "A/B", "--source", "platform"],
         ["content", "3", "A/B", "--source", "claude-platform"]),
        ("claude both", "claude-docs", ["search", "q", "--source", "both"],
         ["search", "q", "--source", "claude"]),
        ("claude both with =", "claude-docs", ["search", "q", "--source=both"],
         ["search", "q", "--source=claude"]),
        ("claude abbreviated source", "claude-docs", ["fetch-index", "--sour", "platform"],
         ["fetch-index", "--source", "claude-platform"]),
        ("claude abbreviated source with =", "claude-docs", ["search", "q", "--so=both"],
         ["search", "q", "--source=claude"]),
        ("claude a preset name is passed on", "claude-docs", ["search", "q", "--source", "claude-platform"],
         ["search", "q", "--source", "claude-platform"]),
        ("claude source given twice: each translated", "claude-docs",
         ["search", "q", "--source", "code", "--source", "platform"],
         ["search", "q", "--source", "claude-code", "--source", "claude-platform"]),
        ("index-limit", "claude-docs", ["search", "q", "--index-limit", "3"],
         ["search", "--source", "claude-code", "q", "--top-n", "3"]),
        ("index-limit with =", "claude-docs", ["search", "q", "--index-limit=3"],
         ["search", "--source", "claude-code", "q", "--top-n=3"]),
        ("index-limit abbreviated", "claude-docs", ["search", "q", "--ind", "3"],
         ["search", "--source", "claude-code", "q", "--top-n", "3"]),
        ("ambiguous in the old script: passed on", "claude-docs", ["search", "q", "--in", "3"],
         ["search", "--source", "claude-code", "q", "--in", "3"]),
        ("a flag abbreviated", "claude-docs", ["content", "3", "--no-l"],
         ["content", "--source", "claude-code", "3", "--no-link-annotations"]),
        ("a value that looks like an option is a value", "claude-docs",
         ["search-content", "q", "--page-ref", "--source"],
         ["search-content", "--source", "claude-code", "q", "--page-ref", "--source"]),
        ("after --: untouched", "claude-docs", ["search", "--source", "platform", "--", "--source", "both"],
         ["search", "--source", "claude-platform", "--", "--source", "both"]),
        ("-- with no source", "claude-docs", ["search", "--", "--index-limit"],
         ["search", "--source", "claude-code", "--", "--index-limit"]),
        ("a value given to a flag stays attached", "ai-sdk", ["fetch-index", "--compact=1"],
         ["fetch-index", "--source", "ai-sdk", "--compact=1"]),
        ("missing value", "claude-docs", ["search", "q", "--source"],
         ["search", "--source", "claude-code", "q", "--source"]),
        ("ai-sdk", "ai-sdk", ["search", "streamText onFinish"],
         ["search", "--source", "ai-sdk", "streamText onFinish"]),
        ("ai-sdk abbreviation unique in the old script", "ai-sdk", ["search-index", "q", "--sh"],
         ["search-index", "--source", "ai-sdk", "q", "--show-sections"]),
        ("ai-sdk compact", "ai-sdk", ["fetch-index", "--comp"],
         ["fetch-index", "--source", "ai-sdk", "--compact"]),
        ("firebase", "firebase", ["fetch-index", "--offset", "100", "--limit", "100"],
         ["fetch-index", "--source", "firebase", "--offset", "100", "--limit", "100"]),
        ("firebase --file is not an old option: passed on", "firebase", ["sections", "1", "--file", "x"],
         ["sections", "--source", "firebase", "1", "--file", "x"]),
        ("a query starting with -", "firebase", ["search", "--", "-x"],
         ["search", "--source", "firebase", "--", "-x"]),
        ("no subcommand: untouched", "claude-docs", [], []),
        ("help: untouched", "claude-docs", ["-h"], ["-h"]),
        ("not an old subcommand: untouched", "ai-sdk", ["sources"], ["sources"]),
        ("help of a subcommand", "firebase", ["search", "-h"], ["search", "--source", "firebase", "-h"]),
    ]

    def test_convert(self):
        for label, kind, argv, expected in self.CASES:
            with self.subTest(label):
                self.assertEqual(_shim.convert(kind, list(argv)), expected)

    def test_generic_loader_reads_the_converted_options_as_the_old_script_did(self):
        """The converted argv parses in the generic loader to the values the
        old script parsed (the page ref, the query, the numbers), with the
        preset in place of the old ``--source``."""
        rows = [
            ("claude-docs", ["search", "q", "--sour", "both", "--ind", "7", "--max-h", "2"],
             {"query": "q", "source": "claude", "top_n": 7, "max_hits": 2}),
            ("claude-docs", ["content", "3", "A/B", "--no-l", "--max-c", "0"],
             {"page_ref": "3", "heading_path": "A/B", "source": "claude-code",
              "no_link_annotations": True, "max_chars": 0}),
            ("ai-sdk", ["search-index", "q", "--sh", "--l", "4"],
             {"query": "q", "source": "ai-sdk", "show_sections": True, "limit": 4}),
            ("firebase", ["fetch-index", "--of", "100", "--li", "5"],
             {"source": "firebase", "offset": 100, "limit": 5}),
            ("firebase", ["search", "--", "-x"], {"query": "-x", "source": "firebase"}),
        ]
        for kind, argv, expected in rows:
            with self.subTest(kind=kind, argv=argv):
                ns = _parse_generic(_shim.convert(kind, argv))
                for key, value in expected.items():
                    self.assertEqual(getattr(ns, key), value, key)


def _generic_parser() -> argparse.ArgumentParser:
    """The generic loader's parser (its ``main`` up to ``parse_args``)."""
    return _parsers_of("parse-llms-txt.py")


def _parsers_of(filename: str) -> argparse.ArgumentParser:
    module = _loader.load_script(filename)

    class _Got(Exception):
        pass

    def grab(self, *a, **k):
        raise _Got(self)

    with mock.patch.object(argparse.ArgumentParser, "parse_args", grab):
        try:
            module.main()
        except _Got as got:
            return got.args[0]
    raise AssertionError(f"{filename}: main() did not parse arguments")


def _parse_generic(argv: list) -> argparse.Namespace:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return _generic_parser().parse_args(argv)
    except SystemExit:
        raise AssertionError(f"the generic loader rejects {argv}: {err.getvalue()}") from None


def _long_options(parser: argparse.ArgumentParser) -> dict:
    """``{subcommand: {--option: takes_value}}`` of *parser*."""
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {name: {o: a.nargs != 0 for a in sp._actions for o in a.option_strings
                   if o.startswith("--") and o != "--help"}
            for name, sp in sub.choices.items()}


class OldOptionTableTest(unittest.TestCase):

    def test_table_is_the_old_scripts_options(self):
        for kind, filename in SHIMS.items():
            with self.subTest(kind=kind):
                self.assertEqual(_shim.OLD_OPTIONS[kind], _long_options(_parsers_of(filename)))

    def test_every_old_option_is_a_generic_option(self):
        generic = _long_options(_generic_parser())
        for kind, commands in _shim.OLD_OPTIONS.items():
            for command, options in commands.items():
                for option, takes_value in options.items():
                    with self.subTest(kind=kind, command=command, option=option):
                        name = _shim.RENAMED.get(option, option)
                        self.assertIn(name, generic[command])
                        self.assertEqual(generic[command][name], takes_value)


class _Exec(Exception):
    pass


def _shim_argv(kind: str, argv: list) -> list:
    """What the shim for *kind* passes to ``os.execv`` when run with *argv*."""
    def fake_execv(path, args):
        raise _Exec(path, args)
    with mock.patch.object(sys, "argv", [SHIMS[kind], *argv]), \
            mock.patch.object(_shim.os, "execv", fake_execv), mock.patch.object(_shim.os, "name", "posix"):
        try:
            _shim.run(kind)
        except _Exec as e:
            path, args = e.args
    return [path, *args]


class _Corpus:
    """A staged copy of a parity fixture with an empty HOME (no user
    sources.json) and the network off; runs the generic loader in-process."""

    def __init__(self, test: unittest.TestCase, corpus: str):
        self.dir = tempfile.mkdtemp(prefix="llms-shim-")
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = os.path.join(self.dir, "cache")
        os.makedirs(self.cache)
        parity.stage_corpus(corpus, self.cache)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)

    def run(self, argv: list) -> tuple:
        module = _loader.load_script("parse-llms-txt.py")
        calls: list = []
        env = {k: v for k, v in os.environ.items()
               if k not in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "LLMS_DOCS_SOURCES_FILE")}
        env["HOME"] = self.home
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=parity._no_network(calls)):
            try:
                code, out, err = _loader.run_cli(module, ["parse-llms-txt.py", *argv])
            except Exception as e:  # a crash fails the test instead of erroring the run
                raise AssertionError(f"crash on {argv}: {e!r}") from None
        return code, out, err, len(calls)


class ShimRunTest(unittest.TestCase):

    def test_exec_target_is_the_generic_loader_with_this_python(self):
        for kind in SHIMS:
            with self.subTest(kind=kind):
                argv = _shim_argv(kind, ["search", "q"])
                self.assertEqual(argv[0], sys.executable)
                self.assertEqual(argv[1], sys.executable)
                self.assertEqual(argv[2], GENERIC)
                self.assertEqual(argv[3:], _shim.convert(kind, ["search", "q"]))

    def test_windows_runs_the_generic_loader_and_passes_its_exit_code_on(self):
        with mock.patch.object(sys, "argv", ["parse-ai-sdk.py", "search", "q"]), \
                mock.patch.object(_shim.os, "name", "nt"), \
                mock.patch("subprocess.call", return_value=3) as call, \
                mock.patch.object(_shim.os, "execv", side_effect=AssertionError("execv on nt")):
            with self.assertRaises(SystemExit) as cm:
                _shim.run("ai-sdk")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(call.call_args[0][0], [sys.executable, GENERIC, "search", "--source", "ai-sdk", "q"])

    def test_every_parity_case_prints_what_the_preset_prints(self):
        """Through the shim, each parity case gives the generic loader's own
        output for the preset, byte for byte (stdout, stderr, exit, fetches)."""
        for case_id, corpus, argv in parity.CASES:
            with self.subTest(case=case_id):
                staged = _Corpus(self, corpus)
                shim_args = _shim_argv(corpus, [*argv, "--cache-dir", staged.cache])[3:]
                direct = parity._generic_argv(corpus, argv, "unused")[:-2] + ["--cache-dir", staged.cache]
                self.assertEqual(staged.run(shim_args), staged.run(direct))


# A command an output recommends: a ``Next:`` line, the command in a
# truncation note, or an indented candidate command on stderr.
_NEXT_RE = re.compile(r"^\s*Next: (.*)$")
_NARROW_RE = re.compile(r"narrow with (parse-llms-txt\.py .*)\)$")
_CANDIDATE_RE = re.compile(r"^\s+(parse-llms-txt\.py .*)$")
# The placeholders the outputs ask to replace, and what replaces them here.
_PLACEHOLDERS = {"<page_ref>": "0", "<query>": "zzz", "<heading_path>": "(top)",
                 "<heading_path from above>": "(top)"}


def recommended_commands(text: str) -> list:
    out = []
    for line in text.splitlines():
        for rx in (_NEXT_RE, _NARROW_RE, _CANDIDATE_RE):
            m = rx.search(line)
            if m:
                out.append(m.group(1))
                break
    return out


class ShimNextRoundTripTest(unittest.TestCase):

    def test_every_recommended_command_names_the_generic_loader_and_runs(self):
        ran = placeholders = 0
        for case_id, corpus, argv in parity.CASES:
            staged = _Corpus(self, corpus)
            code, out, err, _ = staged.run(_shim_argv(corpus, [*argv, "--cache-dir", staged.cache])[3:])
            for line in recommended_commands(out) + recommended_commands(err):
                with self.subTest(case=case_id, line=line):
                    words = shlex.split(line, comments=True)
                    self.assertEqual(words[0], "parse-llms-txt.py")
                    self.assertNotIn("parse-claude-docs.py", line)
                    replaced = [_PLACEHOLDERS.get(w, w) for w in words[1:]]
                    placeholders += replaced != words[1:]
                    self.assertFalse([w for w in replaced if re.fullmatch(r"<[^>]+>", w)], line)
                    run = staged.run(replaced)
                    self.assertEqual(run[0], 0, run[2])
                    self.assertEqual(run[3], 0)
                    ran += 1
        self.assertGreater(ran, 40)
        self.assertGreater(placeholders, 5)

    def test_a_truncation_note_word_replaced_by_a_dash_word_runs(self):
        """The no-subsection truncation note asks to swap its word for the one
        to look for; that word goes after ``--``, so ``--resume`` runs too."""
        staged = _Corpus(self, "claude-docs")
        code, out, _err, _ = staged.run(_shim_argv("claude-docs", [
            "content", "quillon-tag", "--max-chars", "200", "--cache-dir", staged.cache])[3:])
        self.assertEqual(code, 0)
        lines = [c for c in recommended_commands(out) if " -- " in c]
        self.assertTrue(lines, out)
        for line in lines:
            words = shlex.split(line, comments=True)
            words[-1] = "--resume"
            run = staged.run(words[1:])
            self.assertEqual(run[0], 0, run[2])


PLUGIN_DIR = Path(_loader.SCRIPTS_DIR).parent
_DOC_CMD_RE = re.compile(r'^\s*python3 (?:"\$\{CLAUDE_PLUGIN_ROOT\}/scripts/|plugins/llms-docs/scripts/)'
                         r'parse-llms-txt\.py"? (.*)$')


def _doc_files() -> list:
    return [PLUGIN_DIR / "README.md", *sorted((PLUGIN_DIR / "skills").glob("*/SKILL.md")),
            *sorted((PLUGIN_DIR / "skills").glob("*/references/*.md"))]


class DocCommandLinesTest(unittest.TestCase):
    """The skills and the README name only the generic loader, and each command
    line they show parses there with a configured ``--source``."""

    def test_no_retired_script_name(self):
        for path in _doc_files():
            with self.subTest(file=str(path.relative_to(PLUGIN_DIR))):
                text = path.read_text(encoding="utf-8")
                for name in SHIMS.values():
                    self.assertNotIn(name, text)

    def test_every_command_line_parses_with_a_configured_source(self):
        g = _loader.load_script("parse-llms-txt.py")
        presets = g._read_sources_file(g.PRESETS_FILE)
        seen = 0
        for path in _doc_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _DOC_CMD_RE.match(line)
                if not m:
                    continue
                with self.subTest(file=path.name, line=line):
                    words = [re.sub(r"<[^>]*>", "1", w) for w in shlex.split(m.group(1), comments=True)]
                    ns = _parse_generic(words)
                    if words[0] != "sources" and "<name>" not in line:
                        self.assertIn(ns.source, presets)
                    seen += 1
        self.assertGreater(seen, 30)


class ShimProcessTest(unittest.TestCase):
    """The shims as processes: ``os.execv`` replaces them with the generic
    loader (``Next:`` names ``parse-llms-txt.py``) and its exit code is theirs."""

    def _env(self, home: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                            "LLMS_DOCS_SOURCES_FILE", "NO_PROXY", "no_proxy")}
        env["HOME"] = home
        # a fetch would fail fast instead of reaching the network
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = "http://127.0.0.1:9"
        return env

    def test_each_shim_runs_the_generic_loader(self):
        cases = {"claude-docs": ["search", "snapshot restore"], "ai-sdk": ["sections", "stream-shape"],
                 "firebase": ["sections", "14"]}
        for kind, argv in cases.items():
            with self.subTest(kind=kind):
                staged = _Corpus(self, kind)
                full = [*argv, "--cache-dir", staged.cache]
                proc = subprocess.run([sys.executable, os.path.join(_loader.SCRIPTS_DIR, SHIMS[kind]), *full],
                                      capture_output=True, text=True, timeout=120, env=self._env(staged.home))
                code, out, err, fetches = staged.run(_shim.convert(kind, full))
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (code, out, err))
                self.assertEqual(fetches, 0)
                nexts = [c for c in recommended_commands(proc.stdout)]
                self.assertTrue(nexts, proc.stdout)
                # the printed command, its script name replaced by the path as the
                # skills say, runs as a process too
                words = [_PLACEHOLDERS.get(w, w) for w in shlex.split(nexts[0], comments=True)]
                self.assertEqual(words[0], "parse-llms-txt.py")
                again = subprocess.run([sys.executable, GENERIC, *words[1:]], capture_output=True, text=True,
                                       timeout=120, env=self._env(staged.home))
                self.assertEqual(again.returncode, 0, again.stderr)

    def test_exit_code_is_the_generic_loaders(self):
        staged = _Corpus(self, "claude-docs")
        proc = subprocess.run([sys.executable, os.path.join(_loader.SCRIPTS_DIR, "parse-claude-docs.py"),
                               "fetch-index", "--source", "both", "--cache-dir", staged.cache],
                              capture_output=True, text=True, timeout=120, env=self._env(staged.home))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("only 'search' reads several at once", proc.stderr)


if __name__ == "__main__":
    unittest.main()
