"""``index_heading_level``: how deep the index of parse-llms-txt.py reads headings.

The index ranks pages on their title, description, tags and headings
(``search-index`` / ``search``), and ``--show-sections`` lists the same
headings. By default (3) it reads H1 to H3, so a reference page that names
its API in H3 (Vitest's ``Vi``: ``## Mock Modules`` / ``### vi.mock``) is
found by the function name. An H3 adds +1 per keyword but never completes
the all-keyword bonus, so a page titled exactly one of the words stays ahead
of pages whose H3 option groups name every word. The Claude docs / AI SDK /
Firebase presets set 2 (the dedicated scripts read H1 / H2 only).

No network: the CLI tests read ``--file`` or a fresh cache, with
``urlopen`` patched to fail.
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
import _common  # noqa: E402

generic = _loader.load_script("parse-llms-txt.py")

SCRIPT = "parse-llms-txt.py"
URL = "https://example.com/llms-full.txt"
# the presets that keep the dedicated scripts' H1 / H2 index (``claude`` is an
# alias of the two Claude sources and reads no file of its own)
H2_PRESETS = ("claude-code", "claude-platform", "ai-sdk", "firebase")
H2_ALIASES = ("claude",)

# Vitest llms-full.txt (2026-10-15), abridged: the migration guide names
# `vi.mock` / `vi.hoisted` only in its body and "Hoisted" in an H2; the API
# page ``Vi`` lists each function as an H3 under an H2 group.
VITEST_VI_API_H3 = """\
---
url: /guide/migration.md
---

# Migrating to Vitest 5.0 {#vitest-5}

## Hoisted Mocking Calls Must Be at the Top Level

[`vi.mock`](/api/vi#vi-mock), [`vi.unmock`](/api/vi#vi-unmock), and [`vi.hoisted`](/api/vi#vi-hoisted) are hoisted to the top of the file and run before any surrounding code. Calling them inside a function, block, or `describe`/`test` callback previously only logged a warning. Vitest 5.0 now throws, because the call does not execute where it is written:

```ts
describe('calculator', () => {
  vi.mock('./calculator') // [!code --]
})
```

---

---
url: /api/vi.md
---

# Vi

Vitest provides utility functions to help you out through its `vi` helper.

## Mock Modules

This section describes the API that you can use when [mocking a module](/guide/mocking/modules).

### vi.mock

Substitutes all imported modules from provided `path` with another module. The call to `vi.mock` is hoisted, so it doesn't matter where you call it. It will always be executed before all imports.

`vi.mock` is hoisted (in other words, *moved*) to **top of the file**. It means that whenever you write it (be it inside `beforeEach` or `test`), it will actually be called before that.

You can also reference variables defined by `vi.hoisted` method if it was declared before `vi.mock`:

#### Options

The factory may return a promise.

### vi.doMock

If you need to use variables inside the factory, try `vi.doMock`. It works the same way but isn't hoisted.

```md
### vi.fakeHeading
```

## Miscellaneous

### vi.hoisted {#vi-hoisted}

All static `import` statements in ES modules are hoisted to the top of the file.
"""

# Bun llms-full.txt (2026-10-15), abridged: the CLI pages repeat the same H3
# option groups ("Project Files & Lockfiles", "Installation Control"), and
# Catalogs has "## Lockfile Integration" / "### 3. Run Bun Install". The
# canonical page for "lockfile" is the one titled exactly "Lockfile".
BUN_LOCKFILE_CLI_H3 = """\
# bun install
Source: https://bun.com/docs/pm/cli/install

Install packages with Bun's fast package manager

## Lockfile

### Lockfile Control

# Catalogs
Source: https://bun.com/docs/pm/catalogs

Share common dependency versions across multiple packages in a monorepo

### 3. Run Bun Install

## Lockfile Integration

# bun add
Source: https://bun.com/docs/pm/cli/add

Add packages to your project with Bun's fast package manager

### Project Files & Lockfiles

### Installation Control

# bun link
Source: https://bun.com/docs/pm/cli/link

Link local packages for development

# CLI Usage

### Project Files & Lockfiles

### Installation Control

# bun patch
Source: https://bun.com/docs/pm/cli/patch

Persistently patch node_modules packages in a git-friendly way

# CLI Usage

### Project Files & Lockfiles

### Installation Control

# Lockfile
Source: https://bun.com/docs/pm/lockfile

Bun's lockfile format and configuration

`bun install` creates a lockfile called `bun.lock`.
"""


def _preset(name: str) -> dict:
    return generic._read_sources_file(generic.PRESETS_FILE)[name]


class _Cli(unittest.TestCase):
    """Runs a preset on *corpus* through ``--file`` or a fresh cache dir (the
    two corpus option sets), with no user ``sources.json`` and no network."""

    SOURCE = ""
    CORPUS = ""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.file = Path(self.tmp, "llms-full.txt")
        self.file.write_text(self.CORPUS, encoding="utf-8")
        self.cache = Path(self.tmp, "cache")
        self.cache.mkdir()
        Path(generic._cache_path(str(self.cache), _preset(self.SOURCE))).write_text(self.CORPUS, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != generic.SOURCES_ENV}
        env["XDG_CONFIG_HOME"] = str(Path(self.tmp, "config"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def corpora(self):
        return {"file": ("--file", str(self.file)), "cache-dir": ("--cache-dir", str(self.cache))}

    def run_argv(self, argv: list):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled")):
            try:
                return _loader.run_cli(generic, list(argv))
            except Exception as e:  # a crash is a failure, not an error of the run
                self.fail(f"{argv[1:3]} crashed: {e!r}")

    def run_cmd(self, *argv, corpus=None):
        corpus = corpus if corpus is not None else ("--file", str(self.file))
        return self.run_argv([SCRIPT, *argv, "--source", self.SOURCE, *corpus])


class ValidationTest(unittest.TestCase):
    def load(self, raw: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "sources.json")
            f.write_text(json.dumps({"sources": {"x": raw}}), encoding="utf-8")
            code, _out, err = _loader.run_cli(generic, [SCRIPT, "sources", "--sources-file", str(f)])
            return code, err

    FULL = {"url": URL, "split": "frontmatter"}
    FETCH = {"body": "fetch", "rank_from": "index", "index_url": "https://example.com/llms.txt",
             "page_ref_forms": ["int", "url", "slug"]}

    def validate(self, raw: dict) -> dict:
        try:
            return generic._validate_profile("sources.json", "x", dict(raw))
        except SystemExit:  # a refused profile is a failure, not an error of the run
            self.fail(f"profile refused: {raw}")

    def test_default_is_3_and_2_or_3_are_accepted(self):
        for kind, raw in (("full", self.FULL), ("fetch", self.FETCH)):
            with self.subTest(kind):
                self.assertEqual(self.validate(raw)["index_heading_level"], 3)
                for value in (2, 3):
                    p = self.validate({**raw, "index_heading_level": value})
                    self.assertEqual(p["index_heading_level"], value)

    def test_split_auto_candidates_carry_the_level(self):
        raw = {"url": URL, "split": "auto", "index_heading_level": 2, "split_candidates": [
            {"split": "h1", "page_url": "line:Source: "},
            {"split": "frontmatter", "page_url": "frontmatter:url"}]}
        p = self.validate(raw)
        self.assertEqual([c["index_heading_level"] for c in p["split_candidates"]], [2, 2])

    def test_rejected_values(self):
        for kind, base in (("full", self.FULL), ("fetch", self.FETCH)):
            for label, value in (("1", 1), ("4", 4), ("0", 0), ("true", True), ("false", False),
                                 ("string", "3"), ("float", 3.0), ("null", None), ("list", [3])):
                with self.subTest(kind=kind, value=label):
                    code, err = self.load({**base, "index_heading_level": value})
                    self.assertEqual(code, 1, err)
                    self.assertIn("sources.x.index_heading_level must be 2 or 3", err)
                    self.assertIn("README", err)

    def test_an_alias_takes_no_level(self):
        code, err = self.load({"members": ["a", "b"], "index_heading_level": 2})
        self.assertEqual(code, 1, err)
        self.assertIn("index_heading_level cannot go with it", err)


class HeadingRowsTest(unittest.TestCase):
    BODY = ["# Vi\n", "## Mock Modules\n", "### vi.mock\n", "#### Options\n", "```md\n",
            "### not a heading\n", "```\n", "### vi.hoisted {#vi-hoisted}\n", "####### seven\n"]

    def test_depth_3_reads_h1_to_h3_outside_code_blocks(self):
        self.assertEqual(generic._heading_rows(self.BODY, 3),
                         [(1, "Vi"), (2, "Mock Modules"), (3, "vi.mock"), (3, "vi.hoisted {#vi-hoisted}")])
        self.assertEqual(generic._headings(self.BODY, 3),
                         ["Vi", "Mock Modules", "vi.mock", "vi.hoisted {#vi-hoisted}"])

    def test_depth_2_reads_h1_and_h2_only(self):
        self.assertEqual(generic._headings(self.BODY, 2), ["Vi", "Mock Modules"])


class ScoreEntryHeadingsExtraTest(unittest.TestCase):
    """``headings_extra`` (H3) adds +1 per keyword and never completes the
    all-keyword bonus; ``headings`` (H1 / H2) keeps doing both."""

    def score(self, keywords, **kw):
        return _common.score_entry("", "", keywords, **kw)

    def test_one_keyword_in_extra_scores_1(self):
        self.assertEqual(self.score(["vi.mock"], headings_extra=["vi.mock"]), 1)
        # closed compound ("statusline" ~ "status line"), as for headings
        self.assertEqual(self.score(["statusline"], headings_extra=["Status line"]), 1)
        self.assertEqual(self.score(["statusline"], headings=["Status line"]), 1)

    def test_extra_does_not_complete_the_bonus(self):
        # both words only in H3: +1 each, no bonus
        self.assertEqual(self.score(["lockfile", "install"],
                                    headings_extra=["Project Files & Lockfiles", "Installation Control"]), 2)
        # the same words in H2 complete it
        self.assertEqual(self.score(["lockfile", "install"],
                                    headings=["Project Files & Lockfiles", "Installation Control"]), 12)
        # a compound matched only in H3 does not count as matched either
        self.assertEqual(self.score(["statusline", "hooks"], headings=["Hooks"],
                                    headings_extra=["Status line"]), 2)

    def test_extra_adds_to_a_keyword_matched_elsewhere(self):
        # title exact (+10) and H3 (+1)
        self.assertEqual(_common.score_entry("Lockfile", "", ["lockfile"], headings_extra=["Lockfile Control"]), 11)
        # an H2 match already gave the heading +1: H3 adds nothing more
        self.assertEqual(self.score(["lockfile"], headings=["Lockfile"], headings_extra=["Lockfile Control"]), 1)

    def test_h3_substring_keeps_the_closed_compound_match(self):
        """An H3 that contains the keyword as written must not switch off
        the closed-compound match on the title / description / H1-H2."""
        # title "status line" (+5), "customize" (+5), H3 (+1), bonus (+10)
        self.assertEqual(_common.score_entry("Customize your status line", "", ["customize", "statusline"],
                                             headings_extra=["statusline settings"]), 21)
        # title exact (+10), H2 "Code Mode" (+1, the H3 adds nothing more), bonus (+10)
        self.assertEqual(_common.score_entry("Intro", "", ["intro", "codemode"], headings=["Code Mode"],
                                             headings_extra=["codemode options"]), 21)
        self.assertEqual(_common.score_entry("Intro", "", ["codemode"], headings=["Code Mode"],
                                             headings_extra=["codemode options"]), 1)
        # the title compound still counts: 10 without the H3, 11 with it
        self.assertEqual(_common.score_entry("Chat agent", "", ["ChatAgent"]), 10)
        self.assertEqual(_common.score_entry("Chat agent", "", ["ChatAgent"], headings_extra=["ChatAgent class"]), 11)
        # an H2 compound completes the bonus despite the H3
        self.assertEqual(self.score(["statusline", "hooks"], headings=["Status line", "Hooks"],
                                    headings_extra=["statusline command"]), 12)
        # title compound (+10), description (+2), H3 (+1), bonus (+10)
        self.assertEqual(_common.score_entry("Audit Logs", "Export audit data", ["auditLog", "export"],
                                             headings_extra=["auditLog API"]), 23)

    def test_search_index_entries_passes_headings_extra(self):
        entries = [{"title": "Vi", "description": ""}, {"title": "Other", "description": ""}]
        scored = _common.search_index_entries(
            entries, "vi.spyOn",
            get_extras=lambda _e, idx: {"headings_extra": ["vi.spyOn"] if idx == 0 else []})
        self.assertEqual([(s, i) for s, i, _e in scored], [(1, 0)])


class SearchFindsApiReferenceH3Test(_Cli):
    """The index reads H3: an API page that names its functions in H3
    (Vitest ``Vi``) is found by the function name and ranks first, and the
    ``Next:`` line it prints opens that function's section."""

    SOURCE = "vitest"
    CORPUS = VITEST_VI_API_H3

    def test_search_index_matches_h3_api_name(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cmd("search-index", "vi.mock", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertIn("[1] Vi (score: 1)", out)
                self.assertNotIn("[0] Migrating", out)

    def test_search_ranks_vi_first_and_next_reaches_vi_mock(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cmd("search", "vi.mock", corpus=corpus)
                self.assertEqual(code, 0, err)
                pages = re.findall(r"(?m)^\[(\d+)\] (.+?) \(", out)
                self.assertEqual(pages[:1], [("1", "Vi")], out)
                # the first Next: line, run exactly as printed, opens the vi.mock section
                nxt = next((ln for ln in out.splitlines() if ln.startswith("Next: ")), "")
                self.assertTrue(nxt, out)
                argv = shlex.split(nxt[len("Next: "):], comments=True)
                self.assertEqual(argv[:4], [SCRIPT, "content", "1", "Vi/Mock Modules/vi.mock"], nxt)
                code, opened, err = self.run_argv(argv)
                self.assertEqual(code, 0, err)
                self.assertIn("The call to `vi.mock` is hoisted", opened)
                self.assertNotIn("isn't hoisted", opened)

    def test_show_sections_lists_h3_but_not_h4_or_fenced(self):
        code, out, err = self.run_cmd("search-index", "vi.mock", "--show-sections")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Vi (score: 1)", out)
        block = out[out.index("[1] Vi (score: 1)"):]
        self.assertEqual(re.findall(r"(?m)^      - (.+)$", block),
                         ["Vi", "Mock Modules", "vi.mock", "vi.doMock", "Miscellaneous",
                          "vi.hoisted {#vi-hoisted}"])

    def test_level_2_keeps_the_h1_h2_index(self):
        """The same page, with ``index_heading_level: 2`` in a user profile:
        no H3 in the ranking or in ``--show-sections``."""
        sources = Path(self.tmp, "sources.json")
        raw = json.loads(Path(generic.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]["vitest"]
        self.assertNotIn("index_heading_level", raw)
        sources.write_text(json.dumps({"sources": {"vt2": {**raw, "index_heading_level": 2}}}),
                           encoding="utf-8")
        base = [SCRIPT, "search-index", "--source", "vt2", "--sources-file", str(sources), "--file", str(self.file)]
        code, out, err = self.run_argv([*base[:2], "vi.mock", *base[2:]])
        self.assertEqual(code, 0, err)
        self.assertIn("No matching documents found.", out)
        code, out, err = self.run_argv([*base[:2], "hoisted", "--show-sections", *base[2:]])
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^      - (.+)$", out),
                         ["Migrating to Vitest 5.0 {#vitest-5}", "Hoisted Mocking Calls Must Be at the Top Level"])


class H3DoesNotCompleteAllKeywordBonusTest(_Cli):
    """H3 adds +1 per keyword but never the all-keyword bonus: a page titled
    exactly one of the words stays in the top-n against reference pages
    whose H3 option groups name every word."""

    SOURCE = "bun"
    CORPUS = BUN_LOCKFILE_CLI_H3

    def test_search_index_keeps_exact_title_in_top_n(self):
        code, out, err = self.run_cmd("search-index", "--limit", "5", "lockfile install")
        self.assertEqual(code, 0, err)
        ranked = re.findall(r"(?m)^\[(\d+)\] (.+?) \(score: (\d+)\)", out)
        self.assertEqual(ranked[0][1], "bun install", out)
        self.assertEqual(ranked[1], ("5", "Lockfile", "10"), out)
        # H3 still adds its +1 per keyword: "bun add" matches both words, only in H3
        self.assertIn(("2", "bun add", "2"), ranked, out)

    def test_search_drills_into_exact_title_page(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cmd("search", "lockfile install", corpus=corpus)
                self.assertEqual(code, 0, err)
                pages = re.findall(r"(?m)^\[(\d+)\] (.+?) \(index_score", out)
                self.assertIn(("5", "Lockfile"), pages[:2], out)


# Vitest llms-full.txt (2026-10-15), abridged: the config option page is
# titled by the option; the CLI page lists every option as an H3 and names
# it more often than the option's own page does.
VITEST_CONFIG_AND_CLI = """\
---
url: /config/testtimeout.md
---

# testTimeout

* **Type:** `number`
* **CLI:** `--testTimeout=5000`

Default timeout of a test in milliseconds. Use `0` to disable timeout completely.

---

---
url: /guide/cli.md
---

# Command Line Interface

## Options

### testTimeout

* **CLI:** `--testTimeout <timeout>`
* **Config:** [testTimeout](/config/testtimeout)

Default testTimeout of a test in milliseconds (default: `5000`). The testTimeout applies per test.

### hookTimeout

* **CLI:** `--hookTimeout <timeout>`
"""


class H3OnlyCandidateStaysBelowTest(_Cli):
    """``search``: a page that only an H3 made a candidate never ranks above
    a page whose title / description / H1 / H2 matched every keyword, even
    with more body hits. Above a page that matched only some of the
    keywords there, it keeps the search_rank_key order."""

    SOURCE = "vitest"
    CORPUS = VITEST_CONFIG_AND_CLI

    def pages(self, out):
        return re.findall(r"(?m)^\[(\d+)\] (.+?) (?:\(index_score: \d+\)|\[body-only\])$", out)

    def test_config_page_stays_above_the_cli_page(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cmd("search", "testTimeout", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.pages(out), [("0", "testTimeout"), ("1", "Command Line Interface")], out)
                # the Next: line follows the first page
                nxt = next((ln for ln in out.splitlines() if ln.startswith("Next: ")), "")
                self.assertTrue(nxt, out)
                argv = shlex.split(nxt[len("Next: "):], comments=True)
                self.assertEqual(argv[:3], [SCRIPT, "content", "0"], nxt)
                code, opened, err = self.run_argv(argv)
                self.assertEqual(code, 0, err)
                self.assertIn("Default timeout of a test in milliseconds", opened)

    def test_the_cli_page_would_lead_on_body_hits_alone(self):
        """Positive control: without the demotion, search_rank_key puts the
        CLI page (more hits, a section named by the query) first."""
        with mock.patch.object(generic, "_demote_h3_only", lambda results, _h3, _full, **_kw: results):
            code, out, err = self.run_cmd("search", "testTimeout")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pages(out)[:1], [("1", "Command Line Interface")], out)

    def test_h3_only_vi_stays_above_a_one_word_h2_match(self):
        """The migration guide's H2 names only "hoisted" (not "vi.mock"):
        not a match of every keyword, so Vi (H3-only, the better body fit)
        keeps its place above it."""
        for argv in (["search", "vi.mock hoisted"], ["search", "hoisted vi.mock"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_argv([SCRIPT, *argv, "--source", "vitest",
                                                "--file", str(self._vi_file())])
                self.assertEqual(code, 0, err)
                self.assertEqual(self.pages(out), [("1", "Vi"), ("0", "Migrating to Vitest 5.0 {#vitest-5}")], out)

    def _vi_file(self):
        f = Path(self.tmp, "vi.txt")
        f.write_text(VITEST_VI_API_H3, encoding="utf-8")
        return f

    def test_search_index_order_is_not_changed(self):
        code, out, err = self.run_cmd("search-index", "testTimeout")
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^\[(\d+)\] (.+?) \(score: (\d+)\)", out),
                         [("0", "testTimeout", "11"), ("1", "Command Line Interface", "1")])


def _row(idx, *, body_only=False, title=""):
    return {"doc_idx": idx, "body_only": body_only, "title": title}


class DemoteH3OnlyTest(unittest.TestCase):
    """The decision table of ``_demote_h3_only`` on rows already in
    search_rank_key order. H = H3-only (idx 1, 2), M = an index match of
    every keyword (idx 3, 4), P = an index match of some keywords (idx 5, 6),
    B = body-only (idx 9)."""

    H3, FULL = {1, 2}, {3, 4}

    def order(self, rows):
        return [r["doc_idx"] for r in generic._demote_h3_only(rows, set(self.H3), set(self.FULL))]

    def test_table(self):
        cases = {
            # H above M moves to just below the last M, in its own order
            "H M -> M H": ([_row(1), _row(3)], [3, 1]),
            "H H M B -> M H H B": ([_row(1), _row(2), _row(3), _row(9, body_only=True)], [3, 1, 2, 9]),
            "H B M -> B M H (body-only rows do not move)": (
                [_row(1), _row(9, body_only=True), _row(3)], [9, 3, 1]),
            "M H B: already below": ([_row(3), _row(1), _row(9, body_only=True)], [3, 1, 9]),
            "H B: no index match, nothing moves": ([_row(1), _row(9, body_only=True)], [1, 9]),
            "M B: no H3-only row": ([_row(3), _row(9, body_only=True)], [3, 9]),
            "H M1 H M2 -> M1 M2 H H": ([_row(1), _row(3), _row(2), _row(4)], [3, 4, 1, 2]),
            # a body-only row of a page that is H3-only elsewhere is still body-only
            "B(idx in set) M": ([_row(1, body_only=True), _row(3)], [1, 3]),
            # a partial match is not an anchor: H stays above it
            "H P: nothing moves": ([_row(1), _row(5)], [1, 5]),
            "H P M -> P M H": ([_row(1), _row(5), _row(3)], [5, 3, 1]),
            "H M P -> M H P (just below the last M, above P)": ([_row(1), _row(3), _row(5)], [3, 1, 5]),
            "P H P2: nothing moves": ([_row(5), _row(1), _row(6)], [5, 1, 6]),
        }
        for label, (rows, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.order(rows), want)

    def test_a_changelog_page_is_not_the_anchor(self):
        """search_rank_key puts a Changelog / Release notes page last; a full
        match there must not pull an ordinary H3-only page below it. With
        --include-changelog-priority it is an ordinary page again."""
        for title in ("Conformance changelog", "Release Notes"):
            rows = [_row(1, title="CLI Overview"), _row(3, title=title)]
            with self.subTest(title=title):
                self.assertEqual([r["doc_idx"] for r in generic._demote_h3_only(rows, {1}, {3})], [1, 3])
                out = generic._demote_h3_only(rows, {1}, {3}, include_changelog_priority=True)
                self.assertEqual([r["doc_idx"] for r in out], [3, 1])
        # an ordinary full match after the changelog one is still the anchor
        rows = [_row(1, title="CLI"), _row(4, title="Upgrade"), _row(3, title="Changelog")]
        self.assertEqual([r["doc_idx"] for r in generic._demote_h3_only(rows, {1}, {3, 4})], [4, 1, 3])

    def test_ranked_marks_h3_only_and_full_match_pages(self):
        p = generic._validate_profile("sources.json", "x", {"url": URL, "split": "h1"})
        docs = [
            {"title": "Alpha", "description": "", "body_lines": ["## Setup\n", "### lockfile install\n"]},
            {"title": "Lockfile", "description": "", "body_lines": ["### install\n"]},
            {"title": "Beta", "description": "Run the install", "body_lines": ["## lockfile notes\n"]},
            {"title": "Gamma", "description": "about the lockfile", "body_lines": []},
            # only an H2 (besides the H3) names the words: an index match, not H3-only
            {"title": "Delta", "description": "", "body_lines": ["## Lockfile and install\n", "### install\n"]},
        ]
        args = mock.Mock(query="lockfile install")
        scored, _h, h3_only, full = generic._ranked(args, p, docs, 5)
        self.assertEqual({i for _s, i, _e in scored}, {0, 1, 2, 3, 4})
        self.assertEqual(h3_only, {0})
        # title + H3 is partial; description + H2 and an H2 alone cover both; one description word is partial
        self.assertEqual(full, {2, 4})
        # one word: the title matches it fully
        _s, _h, _h3, full = generic._ranked(mock.Mock(query="lockfile"), p, docs, 5)
        self.assertEqual(full, {1, 2, 3, 4})
        # a keyword made only of "_" is skipped, as score_entry skips it
        _s, _h, _h3, full = generic._ranked(mock.Mock(query="lockfile _"), p, docs, 5)
        self.assertEqual(full, {1, 2, 3, 4})
        _s, _h, h3_only, _full = generic._ranked(args, {**p, "index_heading_level": 2}, docs, 5)
        self.assertEqual(h3_only, set())


VITEST_CHANGELOG_AND_CLI = """\
---
url: /guide/changelog.md
---

# testTimeout changelog

The default changed.

---

---
url: /guide/cli.md
---

# Command Line Interface

## Options

### testTimeout

Default testTimeout of a test in milliseconds (default: `5000`). The testTimeout applies per test.
"""


class ChangelogStaysLastTest(_Cli):
    """``search``: a Changelog page whose title names the query stays last;
    the H3-only CLI page is not moved below it. With
    ``--include-changelog-priority`` the changelog is ranked as any page,
    so the H3-only page goes below it."""

    SOURCE = "vitest"
    CORPUS = VITEST_CHANGELOG_AND_CLI

    def pages(self, out):
        return re.findall(r"(?m)^\[(\d+)\] (.+?) (?:\(index_score: \d+\)|\[body-only\])$", out)

    def test_order(self):
        for label, corpus in self.corpora().items():
            for extra, want in (((), [("1", "Command Line Interface"), ("0", "testTimeout changelog")]),
                                (("--include-changelog-priority",),
                                 [("0", "testTimeout changelog"), ("1", "Command Line Interface")])):
                with self.subTest(corpus=label, extra=extra):
                    code, out, err = self.run_cmd("search", "testTimeout", *extra, corpus=corpus)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(self.pages(out), want, out)
                    nxt = next((ln for ln in out.splitlines() if ln.startswith("Next: ")), "")
                    self.assertTrue(nxt, out)
                    argv = shlex.split(nxt[len("Next: "):], comments=True)
                    self.assertEqual(argv[:3], [SCRIPT, "content", want[0][0]], nxt)
                    code, _opened, err = self.run_argv(argv)
                    self.assertEqual(code, 0, err)


class PresetLevelsTest(unittest.TestCase):
    """The Claude docs / AI SDK / Firebase presets keep the dedicated scripts'
    H1 / H2 index; every other bundled preset reads H3 (the default)."""

    def test_levels(self):
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        for name, p in presets.items():
            with self.subTest(preset=name):
                if name in H2_ALIASES:
                    self.assertEqual({presets[m]["index_heading_level"] for m in p["members"]}, {2})
                else:
                    self.assertEqual(p["index_heading_level"], 2 if name in H2_PRESETS else 3)
        self.assertEqual(set(H2_PRESETS) | set(H2_ALIASES) <= set(presets), True)


# One AI SDK page (frontmatter as ai-sdk.dev writes it) with a function named
# only in an H3.
AI_SDK_H3 = """\
---
title: generateText
description: API Reference for generateText.
url: https://ai-sdk.dev/docs/reference/ai-sdk-core/generate-text
---

# `generateText()`

## Parameters

### experimental_repairToolCall

Repairs a tool call that failed to parse.
"""


class AiSdkPresetReadsNoH3Test(_Cli):
    SOURCE = "ai-sdk"
    CORPUS = AI_SDK_H3

    def test_h3_name_is_not_in_the_index(self):
        code, out, err = self.run_cmd("search-index", "experimental_repairToolCall")
        self.assertEqual(code, 0, err)
        self.assertIn("No matching documents found.", out)
        code, out, err = self.run_cmd("search-index", "generateText", "--show-sections")
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^      - (.+)$", out), ["`generateText()`", "Parameters"])


class ClaudePresetsShowNoH3Test(unittest.TestCase):
    """``rank_from: index`` (the Claude docs): the ranking reads only the
    llms.txt entries; ``--show-sections`` lists the joined page's H1 / H2."""

    CASES = {
        "claude-code": ("- [Hooks](https://code.claude.com/docs/en/hooks): Configure hooks\n",
                        "# Hooks\nSource: https://code.claude.com/docs/en/hooks\n\n## Matchers\n\n"
                        "### PreToolUse\n\nx\n"),
        "claude-platform": ("- [Hooks](https://platform.claude.com/docs/en/hooks): Configure hooks\n",
                            "---\ntitle: Hooks\nurl: https://platform.claude.com/docs/en/hooks\n---\n\n"
                            "# Hooks\n\n## Matchers\n\n### PreToolUse\n\nx\n"),
    }

    def test_show_sections_lists_no_h3(self):
        for name, (index, full) in self.CASES.items():
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "llms.txt").write_text(index, encoding="utf-8")
                Path(tmp, "llms-full.txt").write_text(full, encoding="utf-8")
                env = {k: v for k, v in os.environ.items() if k != generic.SOURCES_ENV}
                env["XDG_CONFIG_HOME"] = str(Path(tmp, "config"))
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch("urllib.request.urlopen", side_effect=OSError("network disabled")):
                    code, out, err = _loader.run_cli(generic, [
                        SCRIPT, "search-index", "hooks", "--show-sections", "--source", name,
                        "--file", str(Path(tmp, "llms-full.txt")), "--index-file", str(Path(tmp, "llms.txt"))])
                self.assertEqual(code, 0, err)
                listed = re.findall(r"(?m)^      - (.+)$", out)
                self.assertIn("Matchers", listed, out)
                self.assertNotIn("PreToolUse", listed, out)


if __name__ == "__main__":
    unittest.main()
