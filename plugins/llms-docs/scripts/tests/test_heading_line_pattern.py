"""``heading_line_pattern``: body lines read as H2 headings.

The Claude Code changelog page heads each release with a bold line
(``**2.1.310**``), not a Markdown heading, so every hit on that page used to
belong to one section ``(top)`` and nothing said which release a line came
from. The ``claude-code`` preset reads those lines as ``## 2.1.310``: a hit
names its release, ``sections`` lists the releases, and the ``Next:`` line,
run as printed, reads that release. The rendered page gives each release
line an id (``#2-1-310``), so the ``Section:`` line of a made heading carries
its anchor like any other.
"""

import json
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
URL = "https://code.claude.com/docs/en/changelog"
PRESET_PATTERN = r"^\*\*(\d+\.\d+\.\d+)\*\*\s*$"

# Cut from the real Claude Code changelog page of llms-full.txt (the shape of
# its release lines, the date line and the ``**Added**`` group labels kept;
# entries shortened), plus a release line inside a code block and a real
# heading after the releases.
CHANGELOG = f"""\
# Claude Code changelog
Source: {URL}

Release notes for Claude Code, including new features, improvements, and bug fixes by version.

Run `claude --version` to check your installed version.

**2.1.310**

October 23, 2026

**Added**

* Added the Monitor tool and plugin monitors on Bedrock, Vertex, and Foundry

**14 bug fixes**

* Fixed file read permission prompts not showing where a symbolic link leads

**2.1.154**

* Fixed `worktree.baseRef: "head"` resolving to the main checkout's HEAD instead of the current worktree's HEAD

**2.1.133**

May 7, 2026

**Added**

* Added `worktree.baseRef` setting (`fresh` | `head`) to choose whether `--worktree` branches from `origin/<default>`

```text
**9.9.9**
```

## Older releases

See GitHub for releases before 2.1.0.
"""
INDEX = f"- [Claude Code changelog]({URL}): Release notes\n"


def _profile(raw: dict) -> dict:
    return generic._validate_profile("sources.json", "site", raw)


def _split(text: str, pattern, **extra) -> list:
    raw = {"url": "https://example.com/llms-full.txt", "split": "h1", "page_url": "line:Source: ", **extra}
    if pattern is not None:
        raw["heading_line_pattern"] = pattern
    return generic.split_documents(text.splitlines(keepends=True), _profile(raw))


def _h2(doc: dict) -> list:
    """The ``## <title>`` lines of a page's body, in order (the fixtures of
    ``SplitTest`` have no real H2 besides the made ones unless named)."""
    return [ln for ln in doc["body_lines"] if ln.startswith("## ")]


class SplitTest(unittest.TestCase):
    def test_release_lines_become_h2_and_the_rest_is_unchanged(self):
        [old] = _split(CHANGELOG, None)
        [new] = _split(CHANGELOG, PRESET_PATTERN)
        self.assertEqual(len(old["body_lines"]), len(new["body_lines"]))
        changed = [(a, b) for a, b in zip(old["body_lines"], new["body_lines"]) if a != b]
        self.assertEqual(changed, [("**2.1.310**\n", "## 2.1.310\n"), ("**2.1.154**\n", "## 2.1.154\n"),
                                   ("**2.1.133**\n", "## 2.1.133\n")])

    def test_a_line_inside_a_code_block_stays(self):
        [new] = _split(CHANGELOG, PRESET_PATTERN)
        self.assertIn("**9.9.9**\n", new["body_lines"])
        self.assertNotIn("## 9.9.9\n", new["body_lines"])

    def test_only_a_whole_line_match_counts(self):
        text = (f"# P\nSource: {URL}\n\nsee **2.1.1** for more\n**2.1.2** and text\n"
                " **2.1.3**\n**2.1.4**  \n**2.1.5**\r\n")
        [new] = _split(text, PRESET_PATTERN)
        self.assertEqual(_h2(new), ["## 2.1.4\n", "## 2.1.5\n"])
        self.assertIn("see **2.1.1** for more\n", new["body_lines"])
        self.assertIn("**2.1.2** and text\n", new["body_lines"])
        self.assertIn(" **2.1.3**\n", new["body_lines"])
        # without ^ / $ in the pattern, a line holding more than the match stays
        [new] = _split(f"# P\nSource: {URL}\n\nsee **7** here\n**8**\n", r"\*\*(\d+)\*\*")
        self.assertEqual(_h2(new), ["## 8\n"])
        self.assertIn("see **7** here\n", new["body_lines"])

    def test_the_line_break_is_not_part_of_the_match(self):
        # a pattern ending in ``$`` without ``\s*`` still matches a line
        # that ends in a line break (``\n`` or ``\r\n``)
        [new] = _split(f"# P\nSource: {URL}\n\n**7**\n**8**\r\n**9**", r"^\*\*(\d+)\*\*$")
        self.assertEqual(_h2(new), ["## 7\n", "## 8\n", "## 9\n"])

    def test_an_empty_group_keeps_the_line(self):
        [new] = _split(f"# P\nSource: {URL}\n\n**x**\n", r"^\*\*(\s*)x\*\*$")
        self.assertEqual(_h2(new), [])
        self.assertIn("**x**\n", new["body_lines"])

    def test_promoted_after_the_trailing_headings_are_stripped(self):
        # README: ``heading_line_pattern`` applies after
        # ``strip_trailing_headings``, so a release line ending the page is
        # not stripped as a dangling heading
        [new] = _split(f"# P\nSource: {URL}\n\nsome text\n\n**1.2.3**\n", r"^\*\*(\d+\.\d+\.\d+)\*\*$",
                       strip_trailing_headings=True)
        self.assertEqual(_h2(new), ["## 1.2.3\n"])

    def test_off_by_default_and_in_every_other_preset(self):
        [new] = _split(CHANGELOG, None)
        self.assertEqual(_h2(new), ["## Older releases\n"])
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        with_key = sorted(n for n, p in presets.items() if p.get("heading_line_pattern") is not None)
        # amp writes its H2 as HTML lines (``<h2 id="x">X</h2>``), pixai as
        # ``Title [#id]`` lines (test_pixai_preset)
        self.assertEqual(with_key, ["amp", "claude-code", "pixai"])
        self.assertEqual(presets["claude-code"]["heading_line_pattern"].pattern, PRESET_PATTERN)


class ValidationTest(unittest.TestCase):
    def test_rejected_values(self):
        base = {"url": "https://example.com/llms-full.txt", "split": "h1"}
        for value, msg in (("", "must be a regular expression"), (3, "must be a regular expression"),
                           (["x"], "must be a regular expression"), ("(", "is not a valid regular expression"),
                           ("x", "must have exactly one group"), ("(a)(b)", "must have exactly one group")):
            with self.subTest(value=value):
                try:
                    with self.assertRaises(SystemExit), _loader_stderr() as err:
                        _profile({**base, "heading_line_pattern": value})
                except Exception as e:  # a crash is a failure of the check, not an error of the run
                    self.fail(f"{value!r}: {e!r}")
                self.assertIn(f"heading_line_pattern {msg}", err.getvalue())

    def test_body_fetch_takes_it(self):
        p = _profile({"body": "fetch", "rank_from": "index", "index_url": "https://example.com/llms.txt",
                      "page_ref_forms": ["int", "url", "slug"], "heading_line_pattern": PRESET_PATTERN})
        self.assertEqual(p["heading_line_pattern"].pattern, PRESET_PATTERN)


def _loader_stderr():
    import contextlib
    import io

    class _Ctx:
        def __enter__(self):
            self.buf = io.StringIO()
            self.cm = contextlib.redirect_stderr(self.buf)
            self.cm.__enter__()
            return self.buf

        def __exit__(self, *exc):
            return self.cm.__exit__(*exc)
    return _Ctx()


class PresetRoundTripTest(unittest.TestCase):
    """The bundled ``claude-code`` preset, on the cache and on ``--file`` +
    ``--index-file``: the hit names its release, and every printed line runs
    as printed and reads that release."""

    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "claude-code-llms-full.txt")
        self.full.write_text(CHANGELOG, encoding="utf-8")
        self.index = Path(self.tmp, "claude-code-llms.txt")
        self.index.write_text(INDEX, encoding="utf-8")

    def corpora(self):
        preset = ["--source", "claude-code", "--sources-file", self.sources]
        return {"cache": [*preset, "--cache-dir", self.tmp],
                "file": [*preset, "--index-file", str(self.index), "--file", str(self.full),
                         "--cache-dir", self.tmp]}

    def run_line(self, line):
        return _loader.run_cli(generic, shlex.split(line, comments=True))

    def test_search_content_names_the_release_and_runs_as_printed(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = _loader.run_cli(generic, [SCRIPT, "search-content", *corpus,
                                                           "--", "worktree.baseRef"])
                self.assertEqual(code, 0, err)
                self.assertIn(f"    Section: 2.1.154  (x1)  [{URL}#2-1-154]\n", out)
                self.assertIn(f"    Section: 2.1.133  (x1)  [{URL}#2-1-133]\n", out)
                self.assertNotIn("(top)", out)
                nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
                self.assertEqual(len(nexts), 2, out)
                for line, release, text in zip(nexts, ("2.1.154", "2.1.133"),
                                               ("resolving to the main checkout", "May 7, 2026")):
                    argv = shlex.split(line, comments=True)
                    self.assertEqual(argv[1:4], ["content", "0", release], line)
                    code, got, err = self.run_line(line)
                    self.assertEqual(code, 0, err)
                    self.assertIn(f"# heading_path: {release}\n", got)
                    self.assertIn(text, got)
                    self.assertNotIn("2.1.310", got)

    def test_a_made_heading_has_the_anchor_of_the_page_and_a_real_one_keeps_it(self):
        # the rendered changelog page has an id per release (``id="2-1-310"``)
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = _loader.run_cli(generic, [SCRIPT, "search-content", *corpus,
                                                           "--", "Monitor"])
                self.assertEqual(code, 0, err)
                self.assertIn(f"    Section: 2.1.310  (x1)  [{URL}#2-1-310]\n", out)
                code, out, err = _loader.run_cli(generic, [SCRIPT, "search-content", *corpus,
                                                           "--", "GitHub"])
                self.assertEqual(code, 0, err)
                self.assertIn(f"    Section: Older releases  (x1)  [{URL}#older-releases]\n", out)

    def test_search_and_search_content_give_a_release_the_same_anchor(self):
        # ``search`` ranks on the index row (its URL), ``search-content`` on
        # the page: the same release line carries the same anchor on both
        for label, corpus in self.corpora().items():
            for command in ("search", "search-content"):
                with self.subTest(corpus=label, command=command):
                    code, out, err = _loader.run_cli(generic, [SCRIPT, command, *corpus,
                                                               "--", "changelog Monitor"])
                    self.assertEqual(code, 0, err)
                    if command == "search":
                        self.assertIn("(index_score: ", out)  # ranked on the index row
                    self.assertIn(f"    Section: 2.1.310  (x1)  keywords: monitor  [{URL}#2-1-310]\n", out)

    def test_sections_lists_the_releases_and_each_reads_its_own(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = _loader.run_cli(generic, [SCRIPT, "sections", *corpus, "--", "0"])
                self.assertEqual(code, 0, err)
                rows = [ln for ln in out.splitlines() if ln.startswith("[L")]
                self.assertEqual(rows, ["[L2] 2.1.310", "[L2] 2.1.154", "[L2] 2.1.133 [code]",
                                        "[L2] Older releases"])
                for release, text in (("2.1.310", "Monitor tool"), ("2.1.154", "main checkout"),
                                      ("2.1.133", "May 7, 2026")):
                    code, got, err = _loader.run_cli(generic, [SCRIPT, "content", *corpus,
                                                               "--", "0", release])
                    self.assertEqual(code, 0, err)
                    self.assertIn(text, got)

    def test_search_on_the_release_number_reaches_it(self):
        code, out, err = _loader.run_cli(generic, [SCRIPT, "search", *self.corpora()["cache"],
                                                   "--", "2.1.133"])
        self.assertEqual(code, 0, err)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        self.assertEqual(shlex.split(nexts[0])[1:4], ["content", "0", "2.1.133"], out)


if __name__ == "__main__":
    unittest.main()
