"""``index_heading_level``: how deep the index of parse-llms-txt.py reads headings.

The index ranks pages on their title, description, tags and headings
(``search-index`` / ``search``), and ``--show-sections`` lists the same
headings. By default (3) it reads H1 to H3, so a reference page that names
its API in H3 (Vitest's ``Vi``: ``## Mock Modules`` / ``### vi.mock``) is
found by the function name. An H3 adds +1 per keyword but never completes
the all-keyword bonus, so a page titled exactly one of the words stays ahead
of pages whose H3 option groups name every word. The Claude docs and Firebase
presets set 2 (the dedicated scripts read H1 / H2 only); the AI SDK preset moved
to 3 once the real corpus showed its pages dropping out of search.

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
H2_PRESETS = ("claude-code", "claude-platform", "firebase")
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
        # the explicit id of ``vi.hoisted {#vi-hoisted}`` is not part of the text
        self.assertEqual(generic._heading_rows(self.BODY, 3),
                         [(1, "Vi"), (2, "Mock Modules"), (3, "vi.mock"), (3, "vi.hoisted")])
        self.assertEqual(generic._headings(self.BODY, 3),
                         ["Vi", "Mock Modules", "vi.mock", "vi.hoisted"])

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
                          "vi.hoisted"])

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
                         ["Migrating to Vitest 5.0", "Hoisted Mocking Calls Must Be at the Top Level"])


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
                self.assertEqual(self.pages(out), [("1", "Vi"), ("0", "Migrating to Vitest 5.0")], out)

    def _vi_file(self):
        f = Path(self.tmp, "vi.txt")
        f.write_text(VITEST_VI_API_H3, encoding="utf-8")
        return f

    def test_search_index_order_is_not_changed(self):
        code, out, err = self.run_cmd("search-index", "testTimeout")
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^\[(\d+)\] (.+?) \(score: (\d+)\)", out),
                         [("0", "testTimeout", "11"), ("1", "Command Line Interface", "1")])


def _row(idx, *, body_only=False, title="", hits=1):
    return {"doc_idx": idx, "body_only": body_only, "title": title, "body_hits": {"total_matches": hits}}


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
            # a full match with no body hits is not an anchor: H (with hits) stays above it
            "H M0: nothing moves": ([_row(1, hits=10), _row(3, hits=0)], [1, 3]),
            "H M0 M0: nothing moves": ([_row(1, hits=10), _row(3, hits=0), _row(4, hits=0)], [1, 3, 4]),
            "H M M0 -> M H M0 (below the last M with hits)": (
                [_row(1, hits=10), _row(3, hits=2), _row(4, hits=0)], [3, 1, 4]),
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

    def test_citations_on_a_full_search(self):
        """The shape that broke it: two pages whose description says the word
        and whose bodies do not, and a page that names it in an H3 and in its
        body. The sort puts the hits first; the demotion must leave that."""
        corpus = (
            "---\ntitle: MCP Elicitation\ndescription: citations in forms\nurl: https://x.example/a\n---\n\nNothing here.\n\n"
            "---\ntitle: MCP\ndescription: citations of servers\nurl: https://x.example/b\n---\n\nNothing here.\n\n"
            "---\ntitle: Generating Text\ndescription: text\nurl: https://x.example/c\n---\n\n"
            "## Sources\n\n### citations\n\nThe citations of a call. More citations here.\n"
        )
        path = Path(tempfile.mkdtemp(), "ai.txt")
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(corpus, encoding="utf-8")
        code, out, err = _loader.run_cli(generic, [SCRIPT, "search", "citations", "--source", "ai-sdk", "--file", str(path)])
        self.assertEqual(code, 0, err)
        titles = re.findall(r"(?m)^\[\d+\] (.+?) (?:\(index_score: \d+\)|\[body-only\])$", out)
        self.assertEqual(titles[0], "Generating Text", out)

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


# Cut from the AI SDK's llms-full.txt (2026-11): an MCP page whose description
# says the word, the guide with an H3 "Citations", and a provider page that
# has the word only in its body.
AI_SDK_CITATIONS = """\
---
title: MCP Elicitation
description: citations in forms
url: https://ai-sdk.dev/docs/mcp-elicitation
---

Nothing here.

---
title: Generating Text
description: Learn how to generate text with the AI SDK.
url: https://ai-sdk.dev/docs/ai-sdk-core/generating-text
---

## Sources

### Citations

OpenAI Responses and Anthropic attach an optional `citations` array to each
text part. Use citations to render inline references.

---
title: Anthropic
description: Learn how to use the Anthropic provider.
url: https://ai-sdk.dev/providers/ai-sdk-providers/anthropic
---

## Provider Tools

Raw citations remain available as `text.providerMetadata.anthropic.citations`.
"""


class H3OnlyKeepsFullSearchTest(_Cli):
    """``h3_only_keeps_full_search``: a page that only an H3 made a candidate
    does not stop the full-corpus search; what it adds goes under the rows with
    body hits and above the rows with none. Measured on the real corpus, see
    docs/generic-llms-txt-source.md."""

    SOURCE = "ai-sdk"
    CORPUS = AI_SDK_CITATIONS
    WANT = [("1", "Generating Text"), ("2", "Anthropic"), ("0", "MCP Elicitation")]

    def pages(self, out):
        return re.findall(r"(?m)^\[(\d+)\] (.+?) (?:\(index_score: \d+\)|\[body-only\])$", out)

    def write(self, corpus, name="other.txt"):
        path = Path(self.tmp, name)
        path.write_text(corpus, encoding="utf-8")
        return ("--file", str(path))

    def user_source(self, **keys):
        """The ai-sdk preset's shape as a user profile, with *keys* on top
        (without the preset's two H3 keys: each test turns on the one it is about)."""
        raw = {k: v for k, v in json.loads(Path(generic.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]["ai-sdk"].items()
               if k not in ("cache_name", "h3_only_keeps_full_search", "h3_keeps_h2_results")}
        path = Path(self.tmp, "sources.json")
        path.write_text(json.dumps({"sources": {"mine": {**raw, **keys}}}), encoding="utf-8")
        return ["--source", "mine", "--sources-file", str(path), "--file", str(self.file)]

    def test_an_h3_candidate_does_not_stop_the_full_body_search(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cmd("search", "citations", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.pages(out), self.WANT, out)
                self.assertRegex(out, r"(?m)^\[2\] Anthropic \[body-only\]$")
                # the Next: line still follows the first page
                nxt = next((ln for ln in out.splitlines() if ln.startswith("Next: ")), "")
                argv = shlex.split(nxt[len("Next: "):], comments=True)
                self.assertEqual(argv[:3], [SCRIPT, "content", "1"], nxt)
                code, opened, err = self.run_argv(argv)
                self.assertEqual(code, 0, err)
                self.assertIn("Generating Text", opened)
                # the added row opens the page it names, by the number it shows
                code, opened, err = self.run_cmd("content", "2", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertIn("text.providerMetadata.anthropic.citations", opened)

    def test_off_by_default(self):
        for keys, want in (({}, [self.WANT[0], self.WANT[2]]),
                           ({"h3_only_keeps_full_search": False}, [self.WANT[0], self.WANT[2]]),
                           ({"h3_only_keeps_full_search": True}, self.WANT)):
            with self.subTest(keys=keys):
                code, out, err = self.run_argv([SCRIPT, "search", "citations", *self.user_source(**keys)])
                self.assertEqual(code, 0, err)
                self.assertEqual(self.pages(out), want, out)

    def test_only_the_ai_sdk_preset_has_it_on(self):
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        self.assertEqual(sorted(n for n, p in presets.items() if p.get("h3_only_keeps_full_search")), ["ai-sdk"])

    def test_a_candidate_outside_the_h3_still_stops_it(self):
        corpus = self.write(self.CORPUS + "\n---\ntitle: Citations\ndescription: x\nurl: https://x.example/c\n---\n\n"
                                          "## Use\n\nHow citations work.\n")
        code, out, err = self.run_cmd("search", "citations", corpus=corpus)
        self.assertEqual(code, 0, err)
        self.assertEqual([t for _i, t in self.pages(out)], ["Citations", "Generating Text", "MCP Elicitation"], out)

    def test_only_h3_candidates(self):
        no_zero_row = self.CORPUS.split("---\ntitle: Generating Text")[1]
        code, out, err = self.run_cmd("search", "citations", corpus=self.write("---\ntitle: Generating Text" + no_zero_row))
        self.assertEqual(code, 0, err)
        self.assertEqual([t for _i, t in self.pages(out)], ["Generating Text", "Anthropic"], out)

    def test_a_page_is_listed_once(self):
        # "citations array": the H3 page has both words in one section, but no
        # title or heading of it names them all, so the usual search adds the
        # page whose H4 does; the search without the H3 page finds that page again
        corpus = self.write(self.CORPUS + "\n---\ntitle: Vision\ndescription: x\nurl: https://x.example/v\n---\n\n"
                                          "#### citations array\n\nThe citations array is here.\n")
        code, out, err = self.run_cmd("search", "citations array", corpus=corpus)
        self.assertEqual(code, 0, err)
        self.assertEqual([t for _i, t in self.pages(out)],
                         ["Vision", "Generating Text", "Anthropic", "MCP Elicitation"], out)

    def test_an_added_changelog_stays_last_unless_asked_for(self):
        corpus = self.write(self.CORPUS.replace("title: Anthropic", "title: Changelog"))
        for extra, want in (((), ["Generating Text", "MCP Elicitation", "Changelog"]),
                            (("--include-changelog-priority",), ["Generating Text", "Changelog", "MCP Elicitation"])):
            with self.subTest(extra=extra):
                code, out, err = self.run_cmd("search", "citations", *extra, corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertEqual([t for _i, t in self.pages(out)], want, out)

    def test_no_second_search_without_an_h3_candidate(self):
        # "citations": the search without the H3-only page, then the H1 / H2
        # index's own (h3_keeps_h2_results: its candidates are other pages)
        real = generic.full_corpus_extra_hits
        for query, calls in (("citations", 3), ("Anthropic", 1)):
            with self.subTest(query=query), mock.patch.object(generic, "full_corpus_extra_hits", side_effect=real) as spy:
                code, _out, err = self.run_cmd("search", query)
                self.assertEqual(code, 0, err)
                self.assertEqual(spy.call_count, calls)


class PlaceBelowHitsTest(unittest.TestCase):
    """Where ``_place_below_hits`` puts the rows that the full-corpus search
    added, on rows already in display order."""

    def order(self, rows, extras, **kw):
        return [r["doc_idx"] for r in generic._place_below_hits(rows, extras, **kw)]

    def test_table(self):
        hit, zero = _row(1), _row(2, hits=0)
        added = [_row(8, body_only=True), _row(9, body_only=True)]
        log_hit = _row(3, title="Changelog")
        log_added = _row(7, body_only=True, title="Release notes")
        cases = [
            ("between", [hit, zero], added, {}, [1, 8, 9, 2]),
            ("no zero row", [hit], added, {}, [1, 8, 9]),
            ("only zero rows", [zero], added, {}, [8, 9, 2]),
            ("nothing to add", [hit, zero], [], {}, [1, 2]),
            ("a changelog row stays below", [hit, zero, log_hit], added, {}, [1, 8, 9, 2, 3]),
            ("a changelog row with hits alone", [hit, log_hit], added, {}, [1, 8, 9, 3]),
            ("an added changelog goes last", [hit, zero], [log_added, *added], {}, [1, 8, 9, 2, 7]),
            ("changelog counted as any page", [log_hit, hit, zero], [log_added, *added],
             {"include_changelog_priority": True}, [3, 1, 7, 8, 9, 2]),
        ]
        for label, rows, extras, kw, want in cases:
            with self.subTest(label):
                self.assertEqual(self.order(rows, extras, **kw), want)


class H3OnlyKeepsFullSearchValidationTest(unittest.TestCase):
    """The checks of the key; the shapes come from ``ValidationTest``."""

    KEY = "h3_only_keeps_full_search"
    FULL, FETCH = ValidationTest.FULL, ValidationTest.FETCH
    load, validate = ValidationTest.load, ValidationTest.validate

    def test_default_off_and_carried_by_the_candidates(self):
        self.assertIs(self.validate(self.FULL)[self.KEY], False)
        self.assertIs(self.validate({**self.FULL, self.KEY: True})[self.KEY], True)
        p = self.validate({"url": URL, "split": "auto", self.KEY: True, "split_candidates": [
            {"split": "h1", "page_url": "line:Source: "},
            {"split": "frontmatter", "page_url": "frontmatter:url"}]})
        self.assertEqual([c[self.KEY] for c in p["split_candidates"]], [True, True])

    def test_refused(self):
        index = {"url": URL, "split": "frontmatter", "page_url": "frontmatter:url", "rank_from": "index",
                 "index_join": "url", "index_url": "https://example.com/llms.txt",
                 "page_ref_forms": ["int", "url", "slug"]}
        for label, raw, message in (
                ("not a bool", {**self.FULL, self.KEY: 1}, "must be true or false"),
                ("level 2", {**self.FULL, self.KEY: True, "index_heading_level": 2}, "needs index_heading_level 3"),
                ("rank_from index", {**index, self.KEY: True}, 'only valid with rank_from "pages"'),
                ("body fetch", {**self.FETCH, self.KEY: True}, self.KEY),
                ("alias", {"members": ["a", "b"], self.KEY: True}, self.KEY)):
            with self.subTest(label):
                code, err = self.load(raw)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)

    def test_false_goes_with_level_2(self):
        self.assertIs(self.validate({**self.FULL, "index_heading_level": 2, self.KEY: False})[self.KEY], False)


class H3KeepsH2ResultsValidationTest(H3OnlyKeepsFullSearchValidationTest):
    """``h3_keeps_h2_results`` takes the same checks (the tests above run on
    it too); the refusals name this key."""

    KEY = "h3_keeps_h2_results"

    def test_refused(self):
        index = {"url": URL, "split": "frontmatter", "page_url": "frontmatter:url", "rank_from": "index",
                 "index_join": "url", "index_url": "https://example.com/llms.txt",
                 "page_ref_forms": ["int", "url", "slug"]}
        for label, raw, message in (
                ("not a bool", {**self.FULL, self.KEY: "yes"}, f"sources.x.{self.KEY} must be true or false"),
                ("level 2", {**self.FULL, self.KEY: True, "index_heading_level": 2},
                 f"sources.x.{self.KEY} needs index_heading_level 3"),
                ("rank_from index", {**index, self.KEY: True},
                 f'sources.x.{self.KEY} is only valid with rank_from "pages"'),
                ("body fetch", {**self.FETCH, self.KEY: True}, f"these keys do not apply: {self.KEY}"),
                ("alias", {"members": ["a", "b"], self.KEY: True}, f"{self.KEY} cannot go with it")):
            with self.subTest(label):
                code, err = self.load(raw)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)

    def test_the_other_h3_key_is_not_needed(self):
        p = self.validate({**self.FULL, self.KEY: True})
        self.assertEqual((p[self.KEY], p["h3_only_keeps_full_search"]), (True, False))

    def test_only_the_ai_sdk_preset_has_it_on(self):
        presets = generic._read_sources_file(generic.PRESETS_FILE)
        self.assertEqual(sorted(n for n, p in presets.items() if p.get(self.KEY)), ["ai-sdk"])


# The two ways a page with body hits left the list (internal backlog), on four
# synthetic pages. "alpha beta" with --top-n 2: "Alpha" has "alpha" in its
# title and "beta" only in its H3 "Beta setup" (11 points on the H1 / H2
# index, 12 with the H3). The H1 / H2 candidates are "Alpha Beta" (20) and
# "Beta" (11, ahead of "Alpha" in page order); both have only partial hits,
# so the full-corpus search adds "Alpha" and "Gamma" (every word in one
# section). With the H3, "Alpha" takes "Beta"'s slot (pushed out), and its
# "# Alpha" section has both words, which stops the full-corpus search
# ("Gamma" is not added).
ALPHA_BETA = """\
---
title: Alpha Beta
description: Overview page.
url: https://x.example/alpha-beta
---

## Overview

Alpha is described here.

## Details

Beta is described here.

---
title: Beta
description: The other page.
url: https://x.example/beta
---

# Beta

## Usage

Beta works with other things.

---
title: Alpha
description: Getting started.
url: https://x.example/alpha
---

# Alpha

See the [alpha beta guide](/alpha-beta) first.

### Beta setup

Setup steps come here.

---
title: Gamma
description: Unrelated title.
url: https://x.example/gamma
---

# Gamma

## Notes

Use alpha together with beta.
"""

# ALPHA_BETA and a fifth page whose title names both words and whose body
# names neither (a candidate with no body hits).
ALPHA_BETA_ZERO = ALPHA_BETA + """
---
title: Beta Alpha Notes
description: Nothing to see.
url: https://x.example/notes
---

# Notes

Empty.
"""

# The top index candidate is a Changelog / Release notes page (search_rank_key
# puts it last): "alpha beta" with --top-n 2. The H3 "Beta notes" lifts the
# two release pages (5 -> 6) over "Alpha guide" (5, first in page order),
# whose body has "alpha" only. Changelogs do not count for the full-corpus
# search, so it adds the two pages with every word in one section ("Gamma",
# "Delta"); "Alpha guide" (partial hits) comes back from the H1 / H2 index.
RELEASE_NOTES_FIRST = """\
---
title: Alpha guide
description: x
url: https://x.example/guide
---

## Intro

Alpha only here.

---
title: Alpha changelog
description: x
url: https://x.example/changelog
---

### Beta notes

alpha and beta here.

---
title: Alpha release notes
description: x
url: https://x.example/release-notes
---

### Beta notes

alpha and beta again.

---
title: Gamma
description: x
url: https://x.example/gamma
---

## One

alpha beta one.
alpha beta two.

---
title: Delta
description: x
url: https://x.example/delta
---

## Two

alpha beta three.
"""

_ROW_RE = re.compile(r"^(?:\[[a-z0-9-]+\] )?\[(\d+)\] (.+?) (\(index_score: \d+\)|\[body-only\])$")
_HITS_RE = re.compile(r"^    \((\d+) body hits|^    \(no body hits")


def search_rows(out: str) -> list:
    """``[(page, title, tag, body hits), ...]`` of a ``search`` output."""
    rows = []
    for line in out.splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows.append([m.group(1), m.group(2), m.group(3), None])
            continue
        m = _HITS_RE.match(line)
        if m and rows and rows[-1][3] is None:
            rows[-1][3] = int(m.group(1) or 0)
    return [tuple(r) for r in rows]


class _H3KeepsH2Cli(_Cli):
    """The ai-sdk preset and two user profiles made from it: ``off`` (no
    ``h3_keeps_h2_results``) and ``h2`` (``index_heading_level`` 2), on
    ``--file`` and on a cache dir."""

    SOURCE = "ai-sdk"
    CORPUS = ALPHA_BETA

    def setUp(self):
        super().setUp()
        raw = json.loads(Path(generic.PRESETS_FILE).read_text(encoding="utf-8"))["sources"]["ai-sdk"]
        off = {k: v for k, v in raw.items() if k not in ("cache_name", "h3_keeps_h2_results")}
        self.users = {"off": off,
                      "h2": {**{k: v for k, v in off.items() if k != "h3_only_keeps_full_search"},
                             "index_heading_level": 2}}
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": {**self.users, "mix": {"members": ["ai-sdk", "off"]}}}),
                                encoding="utf-8")
        # a printed command that dropped --cache-dir would read (and fetch
        # into) this empty dir, not the developer's cache
        patcher = mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": str(Path(self.tmp, "default-cache"))})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write(self.CORPUS)

    def write(self, corpus: str) -> None:
        """*corpus* as ``--file`` and as the cache of the preset and of each user profile."""
        self.file.write_text(corpus, encoding="utf-8")
        Path(generic._cache_path(str(self.cache), _preset(self.SOURCE))).write_text(corpus, encoding="utf-8")
        for name, raw in self.users.items():
            p = generic._validate_profile(str(self.sources), name, raw)
            Path(generic._cache_path(str(self.cache), p)).write_text(corpus, encoding="utf-8")

    def search(self, source: str, corpus, query: str, *options):
        argv = [SCRIPT, "search", "--source", source, *corpus, *options, "--", query]
        if source != self.SOURCE:
            argv[4:4] = ["--sources-file", str(self.sources)]
        code, out, err = self.run_argv(argv)
        self.assertEqual(code, 0, err)
        return out

    def assert_next_lines_open(self, out: str, titles: dict) -> list:
        """Every ``Next:`` line of *out* runs as the shell splits it and opens
        the page and section it names; returns the lines."""
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        for line in nexts:
            argv = shlex.split(line, comments=True)
            self.assertEqual(argv[:2], [SCRIPT, "content"], line)
            page, heading = argv[argv.index("--") + 1:] if "--" in argv else argv[2:4]
            code, opened, err = self.run_argv(argv)
            self.assertEqual(code, 0, (line, err))
            self.assertEqual(opened.splitlines()[0], f"# doc_title: {titles[page]}", line)
            self.assertIn(f"# heading_path: {heading}", opened, line)
        return nexts


class H3KeepsH2ResultsTest(_H3KeepsH2Cli):
    """``h3_keeps_h2_results`` (the ai-sdk preset): the rows with body hits
    that the H1 / H2 index (``index_heading_level`` 2) shows and the H3 index
    does not come back under the H3 index's rows with body hits, in the H1 /
    H2 index's order and as it shows them (its score, ``[body-only]``). The
    rows, order and first row of the H3 index stay. Measured on the real
    corpus, see docs/generic-llms-txt-source.md."""

    def test_the_rows_of_the_h1_h2_index_come_back(self):
        want = {
            "ai-sdk": [("2", "Alpha", "(index_score: 12)", 2), ("0", "Alpha Beta", "(index_score: 20)", 2),
                       ("3", "Gamma", "[body-only]", 1), ("1", "Beta", "(index_score: 11)", 2)],
            "off": [("2", "Alpha", "(index_score: 12)", 2), ("0", "Alpha Beta", "(index_score: 20)", 2)],
            "h2": [("2", "Alpha", "[body-only]", 2), ("3", "Gamma", "[body-only]", 1),
                   ("0", "Alpha Beta", "(index_score: 20)", 2), ("1", "Beta", "(index_score: 11)", 2)],
        }
        header = {"ai-sdk": "  (top-2 candidate docs drilled into bodies; 2 more from the H1/H2 index "
                            "after the rows with hits)",
                  "off": "  (top-2 candidate docs drilled into bodies)",
                  "h2": "  (top-2 candidate docs drilled into bodies)"}
        for label, corpus in self.corpora().items():
            for source, rows in want.items():
                with self.subTest(corpus=label, source=source):
                    out = self.search(source, corpus, "alpha beta", "--top-n", "2")
                    self.assertEqual(search_rows(out), rows, out)
                    self.assertEqual(out.splitlines()[1], header[source], out)
                    self.assertIn(f"({len(rows)} documents, ranked via index → body)", out)

    def test_every_h1_h2_row_with_hits_once_and_the_h3_rows_in_their_order(self):
        """On queries that do and do not change the candidates (one or two
        words, a word of the H3 only, words that start with "-", an escape,
        function words, symbols) and three --top-n: the rows are the H3
        index's rows (same order, same first row) and every row with body hits
        of the H1 / H2 index, each once; those come after the H3 index's rows
        with body hits and before its rows with none."""
        queries = ("alpha beta", "beta alpha", "alpha", "beta", "Beta setup", "alpha beta guide", "setup",
                   "-alpha", "--beta", "\\_", "the", "---", "gamma", "beta notes")
        put_back = set()
        for corpus_text in (ALPHA_BETA, ALPHA_BETA_ZERO):
            self.write(corpus_text)
            for label, corpus in self.corpora().items():
                for top_n in ("1", "2", "3", "5"):
                    for query in queries:
                        with self.subTest(pages=corpus_text.count("title:"), corpus=label, top_n=top_n, query=query):
                            got, off, h2 = (search_rows(self.search(s, corpus, query, "--top-n", top_n))
                                            for s in ("ai-sdk", "off", "h2"))
                            pages = [r[0] for r in got]
                            self.assertEqual(len(pages), len(set(pages)), got)
                            back = [r for r in got if r[0] not in {o[0] for o in off}]
                            self.assertEqual([r for r in got if r not in back], off)
                            self.assertEqual(back, [r for r in h2 if r[3] and r[0] not in {o[0] for o in off}])
                            if back:
                                put_back.add((corpus_text.count("title:"), top_n, query))
                                self.assertEqual(got[0], off[0])
                                at = next((i for i, r in enumerate(off) if not r[3]), len(off))
                                self.assertEqual(got[at:at + len(back)], back, got)
        # the cases that put rows back (so that the checks above are not
        # empty): elsewhere the H1 / H2 candidates are the same pages
        self.assertEqual(sorted(put_back), [(4, "2", "alpha beta"), (4, "2", "beta alpha"),
                                            (5, "3", "alpha beta"), (5, "3", "beta alpha")])

    def test_put_back_under_the_rows_with_hits_and_above_the_rows_without(self):
        self.write(ALPHA_BETA_ZERO)
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                out = self.search("ai-sdk", corpus, "alpha beta", "--top-n", "3")
                self.assertEqual([(r[0], r[3]) for r in search_rows(out)],
                                 [("2", 2), ("0", 2), ("3", 1), ("1", 2), ("4", 0)], out)

    def test_the_put_back_rows_open_their_page_and_the_next_lines_run_as_printed(self):
        titles = {str(i): t for i, t in enumerate(re.findall(r"(?m)^title: (.+)$", ALPHA_BETA))}
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                out = self.search("ai-sdk", corpus, "alpha beta", "--top-n", "2")
                for page, title, _tag, _hits in search_rows(out)[2:]:
                    code, opened, err = self.run_argv([SCRIPT, "content", "--source", "ai-sdk", *corpus, "--", page])
                    self.assertEqual(code, 0, err)
                    self.assertEqual(opened.splitlines()[0], f"# doc_title: {title}")
                nexts = self.assert_next_lines_open(out, titles)
                # the third is the put-back Gamma (the H3 rows have two pages with hits)
                self.assertEqual(len(nexts), 3, out)
                self.assertIn("content 3 Gamma/Notes", nexts[2])

    def test_a_put_back_changelog_stays_last_unless_asked_for(self):
        # the pushed-out H1 / H2 candidate is a changelog page, with the 11
        # points of "Beta" (title 5, tag 4, description 2)
        beta = ("---\ntitle: Beta\ndescription: The other page.\nurl: https://x.example/beta\n---\n\n"
                "# Beta\n\n## Usage\n")
        self.assertIn(beta, ALPHA_BETA)
        self.write(ALPHA_BETA.replace(beta, "---\ntitle: Beta changelog\ndescription: beta notes\ntags: [beta]\n"
                                            "url: https://x.example/beta\n---\n\n## Usage\n"))
        for label, corpus in self.corpora().items():
            # it is last in the H1 / H2 index's order either way
            for extra in ((), ("--include-changelog-priority",)):
                with self.subTest(corpus=label, extra=extra):
                    out = self.search("ai-sdk", corpus, "alpha beta", "--top-n", "2", *extra)
                    self.assertEqual([r[:3] for r in search_rows(out)],
                                     [("2", "Alpha", "(index_score: 12)"), ("0", "Alpha Beta", "(index_score: 20)"),
                                      ("3", "Gamma", "[body-only]"), ("1", "Beta changelog", "(index_score: 11)")],
                                     out)
        # "Gamma" (added by the H1 / H2 index's full-corpus search) is one
        self.write(ALPHA_BETA.replace("title: Gamma\n", "title: Gamma changelog\n"))
        for label, corpus in self.corpora().items():
            for extra, want in (((), ["2", "0", "1", "3"]), (("--include-changelog-priority",), ["2", "0", "3", "1"])):
                with self.subTest(corpus=label, extra=extra):
                    out = self.search("ai-sdk", corpus, "alpha beta", "--top-n", "2", *extra)
                    self.assertEqual([r[0] for r in search_rows(out)], want, out)

    def test_the_top_index_candidate_keeps_its_next_line(self):
        """A put-back row is not the top index candidate: the changelog that
        is (search_rank_key puts it after the pages with hits) keeps its
        ``Next:`` line, and the lines are the H3 index's."""
        self.write(RELEASE_NOTES_FIRST)
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                out = self.search("ai-sdk", corpus, "alpha beta", "--top-n", "2")
                self.assertEqual([r[0] for r in search_rows(out)], ["3", "4", "0", "1", "2"], out)
                off = self.search("off", corpus, "alpha beta", "--top-n", "2")
                self.assertEqual([r[0] for r in search_rows(off)], ["3", "4", "1", "2"], off)
                pages = [shlex.split(ln[len("Next: "):], comments=True)[2]
                         for text in (out, off) for ln in text.splitlines() if ln.startswith("Next: ")]
                self.assertEqual(pages, ["3", "4", "1"] * 2, out)

    def test_a_members_alias_puts_them_back_per_member(self):
        self.write(RELEASE_NOTES_FIRST)
        code, out, err = self.run_argv([SCRIPT, "search", "--source", "mix", "--sources-file", str(self.sources),
                                        "--cache-dir", str(self.cache), "--top-n", "2", "--", "alpha beta"])
        self.assertEqual(code, 0, err)
        groups = out.split("--- AI SDK (--source off) ---")
        self.assertEqual(len(groups), 2, out)
        self.assertEqual([r[0] for r in search_rows(groups[0])], ["3", "4", "0", "1", "2"], out)
        self.assertIn("(5 documents from AI SDK; 1 more from the H1/H2 index after the rows with hits)", groups[0])
        self.assertEqual([r[0] for r in search_rows(groups[1])], ["3", "4", "1", "2"], out)
        self.assertIn("(4 documents from AI SDK)\n", groups[1])
        pages = [shlex.split(ln[len("Next: "):], comments=True)[2:4]
                 for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertEqual([p[0] for p in pages], ["3", "4", "1"], out)

    def test_no_h1_h2_search_when_the_candidates_are_the_same(self):
        """The H1 / H2 index is ranked once to compare its candidates, on the
        headings the H3 index read (each page's once); its rows are searched
        (on that ranking) only when they differ."""
        rows, ranked, heads = generic._search_rows, generic._ranked, generic._heading_rows
        pages = len(re.findall(r"(?m)^title: ", ALPHA_BETA))
        # "beta notes" with --top-n 2: the same candidates, and the H3 index's
        # rows also have [body-only] ones (the comparison leaves them out)
        for query, top_n, calls in (("gamma", "5", 1), ("alpha", "1", 1), ("alpha beta", "5", 1),
                                    ("beta notes", "2", 1), ("alpha beta", "2", 2)):
            with self.subTest(query=query, top_n=top_n), \
                    mock.patch.object(generic, "_search_rows", side_effect=rows) as spy, \
                    mock.patch.object(generic, "_ranked", side_effect=ranked) as ranks, \
                    mock.patch.object(generic, "_heading_rows", side_effect=heads) as read:
                out = self.search("ai-sdk", ("--file", str(self.file)), query, "--top-n", top_n)
                if query == "beta notes":
                    self.assertIn("[body-only]", out)
                self.assertEqual(spy.call_count, calls)
                self.assertEqual([c.args[1]["index_heading_level"] for c in ranks.call_args_list], [3, 2])
                self.assertEqual(read.call_count, pages)
                # without the key, the ranking reads them as before
                read.reset_mock()
                self.search("off", ("--file", str(self.file)), query, "--top-n", top_n)
                self.assertEqual(read.call_count, pages)

    def test_the_h1_h2_index_ranks_on_the_h1_h2_of_those_headings(self):
        """``_ranked`` on headings read to H3 leaves the H3 out at level 2:
        the same result as reading the headings to H2."""
        p = _preset("ai-sdk")
        docs = generic.split_documents(ALPHA_BETA.splitlines(keepends=True), p)
        h3 = [generic._heading_rows(d["body_lines"], 3) for d in docs]
        self.assertIn((3, "Beta setup"), h3[2])
        for level in (2, 3):
            for query in ("alpha beta", "beta setup", "setup", "alpha"):
                with self.subTest(level=level, query=query):
                    args = mock.Mock(query=query)
                    lp = {**p, "index_heading_level": level}
                    self.assertEqual(generic._ranked(args, lp, docs, 5, heading_rows=h3),
                                     generic._ranked(args, lp, docs, 5))


# The AI SDK llms-full.txt (fetched 2026-11-03), cut: the six pages of the
# rows of "After (Anthropic)" (those of the H3 index and of the H1 / H2
# index), in their order. Each keeps its frontmatter, every heading and the
# lines that hold "after" or "anthropic" (a code block only where it holds
# one, with its fences); the lines that go change no score and no section's
# hits, so the rows are those of the real corpus: the H3 "After (Anthropic)"
# brings Reasoning into the candidates and pushes out HarnessAgent, a page
# with 9 hits that the H1 / H2 index drills into.
AI_SDK_AFTER_ANTHROPIC = """\
---
title: HarnessAgent
description: Create and run AI SDK HarnessAgent sessions.
url: "https://ai-sdk.dev/docs/ai-sdk-harnesses/harness-agent"
docs_index: /llms.txt
---
## Installation
## Create an Agent
## Run a Turn
### Image Inputs
## Lifecycle Callbacks
lifecycle after the runtime reports the result. The callbacks are still
## Runtime Context
## Generate Structured Output
## Messages and History
### Reading the Runtime's Own History
## Session Lifecycle
## Change Settings Between Turns
`prepareCall` runs for a new prompt after its custom `options` have been
## Resuming
running after `detach()` or stopped with a restorable snapshot. After
## Continue a Suspended Turn
continues the turn. After a process resume without rebinding, the continuation
## Stop After a Harness Step
Use `stopWhen` to opt into semantic step boundaries. Predicates run after real
## Prepare the Sandbox
`sandboxConfig.onBootstrap` runs during sandbox template creation, after the
`sandboxConfig.onSession` runs after each sandbox session is acquired and its
## Prepare Reusable Sandboxes
## Settings
- `stopWhen`: condition(s) for finishing a result slice after a completed
## Custom Sandbox Orchestration
### Basic Sandbox Sessions Without Network Control
## Next Steps
---
title: Reasoning
description: Learn how to control reasoning across providers with the top-level reasoning parameter.
url: "https://ai-sdk.dev/docs/ai-sdk-core/reasoning"
docs_index: /llms.txt
---
## Basic Usage
## Streaming
## Precedence Rules
## Provider Support
### How values are mapped
## Migrating from `providerOptions`
### Before (Anthropic)
### After (Anthropic)
### Before (Anthropic with older model)
### After (Anthropic with older model)
### Before (Google with `includeThoughts`)
### After (Google with `includeThoughts`)
### Before (OpenAI with `reasoningSummary`)
### After (OpenAI with `reasoningSummary`)
---
title: Chatbot Resume Streams
description: Learn how to resume chatbot streams after client disconnects.
url: "https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams"
docs_index: /llms.txt
---
`useChat` supports resuming ongoing streams after page reloads. Use this feature to build applications with long-running generations.
## How stream resumption works
## Prerequisites
## Implementation
### 1. Client-side: Enable stream resumption
The hook also reconnects when the page becomes visible again after being in the background (for example, after switching browser tabs or returning to a mobile app). This only happens when the stream was interrupted by a network disconnect. Healthy streams and other errors are left untouched.
prevents duplicate text, reasoning, and step parts after a network disconnect.
### 2. Create the POST handler
```ts title="app/api/chat/route.ts"
import { after } from 'next/server';
      const streamContext = createResumableStreamContext({ waitUntil: after });
```
### 3. Implement the GET handler
```ts title="app/api/chat/[id]/stream/route.ts"
import { after } from 'next/server';
    waitUntil: after,
```
The `after` function from Next.js allows work to continue after the response
after the initial response is returned to the client, enabling reconnection
## How it works
### Request lifecycle
3. **Client reconnection**: When the client reconnects (page reload, or returning to a backgrounded page after a network disconnect), the `resume` option triggers a GET request to `/api/chat/[id]/stream`
## Customize the resume endpoint
## Stop an Active Resumable Stream
Stream resumption lets a client reconnect to an active stream after the original connection closes. To make that possible, the server keeps the stream running even when no client is actively consuming it. If the user refreshes the page, closes the tab, loses their connection, or navigates away, the client can reconnect later with `resumeStream()`.
### Client-side: send the current assistant message
When the client knows the active stream ID, include it in the request. This lets the server ignore stale stop requests that arrive after a newer stream has already started.
### Server-side: stop the active work and clear the stream
### Keep navigation separate from stop
After a user stops a stream, avoid automatic reconnect attempts for that chat until the user sends another message or explicitly retries. Otherwise the client can reconnect before cancellation has finished.
## Important considerations
- **Stream expiration**: Streams in Redis expire after a set time (configurable in the `resumable-stream` package)
---
title: Record Final Object after Streaming Object
description: Learn how to record the final object after streaming an object using the AI SDK and Node
url: "https://ai-sdk.dev/cookbook/node/stream-object-record-final-object"
docs_index: /llms.txt
tags:
  - node
  - streaming
  - structured data
---
## `onEnd` and `output`
## `output` Promise
---
title: Record Token Usage After Streaming Object
description: Learn how to record token usage when streaming structured data using the AI SDK and Node
url: "https://ai-sdk.dev/cookbook/node/stream-object-record-token-usage"
docs_index: /llms.txt
tags:
  - node
  - streaming
  - structured data
  - observability
---
## `onEnd` Callback
## `usage` Promise
---
title: Record Token Usage after Streaming User Interfaces
description: Learn how to record token usage after streaming user interfaces using the AI SDK and React Server Components
url: "https://ai-sdk.dev/cookbook/rsc/stream-ui-record-token-usage"
docs_index: /llms.txt
tags:
  - rsc
  - usage
---
## `onFinish` Callback
## Server
"""


class H3KeepsH2ResultsRealPagesTest(_H3KeepsH2Cli):
    """``h3_keeps_h2_results`` on pages cut from the real corpus: HarnessAgent
    comes back as the third row, above the pages with no body hits, and gets
    the third ``Next:`` line."""

    CORPUS = AI_SDK_AFTER_ANTHROPIC
    QUERY = "After (Anthropic)"
    TITLES = {str(i): t for i, t in enumerate(re.findall(r"(?m)^title: (.+)$", AI_SDK_AFTER_ANTHROPIC))}

    def test_harness_agent_comes_back(self):
        reasoning, streams, harness, final, ui, usage = (
            ("1", "Reasoning", "(index_score: 2)", 1),
            ("2", "Chatbot Resume Streams", "(index_score: 2)", 14),
            ("0", "HarnessAgent", "(index_score: 1)", 9),
            ("3", "Record Final Object after Streaming Object", "(index_score: 7)", 0),
            ("5", "Record Token Usage after Streaming User Interfaces", "(index_score: 7)", 0),
            ("4", "Record Token Usage After Streaming Object", "(index_score: 5)", 0))
        want = {"ai-sdk": [reasoning, streams, harness, final, ui, usage],
                "off": [reasoning, streams, final, ui, usage],
                # the H1 / H2 index finds Reasoning only in the full-corpus search
                "h2": [(*reasoning[:2], "[body-only]", 1), streams, harness, final, ui, usage]}
        for label, corpus in self.corpora().items():
            for source, rows in want.items():
                with self.subTest(corpus=label, source=source):
                    out = self.search(source, corpus, self.QUERY)
                    self.assertEqual(search_rows(out), rows, out)
                    note = "; 1 more from the H1/H2 index after the rows with hits" if source == "ai-sdk" else ""
                    self.assertEqual(out.splitlines()[1], f"  (top-5 candidate docs drilled into bodies{note})", out)

    def test_the_next_lines_run_as_printed(self):
        for label, corpus in self.corpora().items():
            for source, pages in (("ai-sdk", ["1", "2", "0"]), ("off", ["1", "2"])):
                with self.subTest(corpus=label, source=source):
                    out = self.search(source, corpus, self.QUERY)
                    nexts = self.assert_next_lines_open(out, self.TITLES)
                    self.assertEqual([shlex.split(ln, comments=True)[2] for ln in nexts], pages, out)
                    if source == "ai-sdk":
                        self.assertIn("content 0 'Stop After a Harness Step'", nexts[2])


class PresetLevelsTest(unittest.TestCase):
    """The Claude docs / Firebase presets keep the dedicated scripts' H1 / H2
    index; every other bundled preset reads H3 (the default)."""

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


class AiSdkPresetReadsH3Test(_Cli):
    """A name the AI SDK writes only in an H3 (``### maxOutputTokens`` on its
    Settings page) finds its page; measured on the real corpus, see
    docs/generic-llms-txt-source.md."""

    SOURCE = "ai-sdk"
    CORPUS = AI_SDK_H3

    def test_h3_name_is_in_the_index(self):
        code, out, err = self.run_cmd("search-index", "experimental_repairToolCall")
        self.assertEqual(code, 0, err)
        self.assertIn("generateText", out)
        self.assertNotIn("No matching documents found.", out)
        code, out, err = self.run_cmd("search-index", "generateText", "--show-sections")
        self.assertEqual(code, 0, err)
        self.assertEqual(re.findall(r"(?m)^      - (.+)$", out),
                         ["`generateText()`", "Parameters", "experimental_repairToolCall"])


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
