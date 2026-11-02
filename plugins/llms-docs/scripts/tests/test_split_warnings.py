"""``_warn_if_split_suspect``: a page split that looks wrong is said on stderr.

The split rules stay as they are (a boundary inside a code block is not a
boundary, so a frontmatter example in a code sample is not a page). What this
adds is the warning that a file came out wrong, instead of a silent single page.

The motivating file: a Next.js ``llms-full.txt`` fetched on 2026-10-15 whose
closing fences ran into the next line (``` ```## Heading ```), which split into
9 pages of 474 with no warning. The next day's copy was fine, so the file is
rebuilt here from the pages' real frontmatter and the same joined fences.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
FULL_URL = "https://example.com/llms-full.txt"
INDEX_URL = "https://example.com/llms.txt"


def page(k: int, *body) -> str:
    # the frontmatter of nextjs.org/docs/llms-full.txt (2026-10-28)
    return "\n".join(["---", f"title: Page {k}", f"description: Welcome to page {k}.",
                      f'url: "https://example.com/docs/p{k}"', "docs_index: /docs/llms.txt",
                      "related:", "  - /docs/app/getting-started", "---", "",
                      "> For an index of all documentation, see [/docs/llms.txt](/docs/llms.txt).",
                      "", f"## Heading {k}", "", f"zzq body {k}.", "", *body, ""])


def joined_fences_file(total: int = 474, whole: int = 9) -> str:
    """*whole* pages split as they should; the last of them holds a code block
    whose closer ran into the next line, as does every later page's."""
    good = "".join(page(k) for k in range(whole - 1))
    broken = "".join(page(k, "```ts", "const a = 1", "```## Next heading", "", "More text.")
                     for k in range(whole - 1, total))
    return good + broken


def index_for(total: int) -> str:
    return "".join(f"- [Page {k}](https://example.com/docs/p{k}): page {k}\n" for k in range(total))


FM = {"url": FULL_URL, "split": "frontmatter", "page_url": "frontmatter:url"}
SOURCES = {
    "no-index": FM,
    "title-join": {**FM, "index_url": INDEX_URL},
    "url-join": {**FM, "index_url": INDEX_URL, "rank_from": "index", "index_join": "url",
                 "page_ref_forms": ["int", "url", "slug"]},
    "auto": {"url": FULL_URL, "split": "auto", "split_candidates": [
        {"split": "frontmatter", "page_url": "frontmatter:url"},
        {"split": "h1", "page_url": "line:Source: "}]},
    "h1": {"url": FULL_URL, "split": "h1", "page_url": "line:Source: ", "index_url": INDEX_URL},
}


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llms split ")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = str(Path(self.tmp, "sources.json"))
        Path(self.sources).write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")

    def profile(self, name: str) -> dict:
        return generic._validate_profile(self.sources, name, SOURCES[name])

    def stage(self, name: str, full: str, index: str | None = None) -> None:
        profile = self.profile(name)
        Path(generic._cache_path(self.tmp, profile)).write_text(full, encoding="utf-8")
        if index is not None:
            Path(generic._index_cache_path(self.tmp, profile)).write_text(index, encoding="utf-8")

    def files(self, full: str, index: str | None = None) -> tuple:
        path = Path(self.tmp, "full.txt")
        path.write_text(full, encoding="utf-8")
        opts = ("--file", str(path))
        if index is not None:
            path = Path(self.tmp, "index.txt")
            path.write_text(index, encoding="utf-8")
            opts += ("--index-file", str(path))
        return opts

    def run_g(self, source: str, *argv, corpus: tuple | None = None):
        opts = corpus if corpus is not None else ("--cache-dir", self.tmp)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled")):
            try:
                return _loader.run_cli(generic, [SCRIPT, *argv, "--source", source,
                                                 "--sources-file", self.sources, *opts])
            except Exception as e:  # a crash fails the assertions, not the run
                self.fail(f"{argv} crashed: {e!r}")

    def warnings(self, err: str, what: str) -> list:
        return [ln for ln in err.splitlines() if ln.startswith("WARNING:") and what in ln]

    def warning(self, err: str, what: str) -> str:
        found = self.warnings(err, what)
        self.assertEqual(len(found), 1, err)
        return found[0]


class JoinedFencesFileTest(_Case):
    """The Next.js file: no index in its preset, so the open fence catches it."""

    FULL = joined_fences_file()

    def test_the_rule_stands_and_the_file_is_caught_by_every_command(self):
        self.assertEqual(len(generic.split_documents(self.FULL.splitlines(keepends=True), self.profile("no-index"))), 9)
        self.stage("no-index", self.FULL)
        for argv in (["sections", "0"], ["content", "0"], ["search", "zzq"], ["search-content", "zzq"],
                     ["search-index", "zzq"], ["fetch-index"]):
            with self.subTest(argv=argv):
                code, _out, err = self.run_g("no-index", *argv)
                self.assertEqual(code, 0, err)
                found = self.warnings(err, "never closes")
                self.assertEqual(len(found), 1, err)
                self.assertIn(generic._cache_path(self.tmp, self.profile("no-index")), found[0])

    def test_the_warning_names_where_the_block_opened(self):
        full = self.FULL
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        # the block that never closes is the first one (the first "```ts")
        opened = full.splitlines().index("```ts") + 1
        self.assertIn(f"opened near line {opened} of", self.warning(err, "never closes"))

    def test_with_an_index_the_page_count_is_said_as_well(self):
        code, _out, err = self.run_g("title-join", "sections", "0",
                                     corpus=self.files(self.FULL, index_for(474)))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)
        self.assertIn("split into 9 pages, but its llms.txt lists 474", self.warning(err, "split into"))

    def test_the_same_pages_with_their_fences_whole_say_nothing(self):
        full = self.FULL.replace("```## Next heading", "```\n\n## Next heading")
        self.assertEqual(len(generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))), 474)
        code, _out, err = self.run_g("title-join", "sections", "0", corpus=self.files(full, index_for(474)))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")


class UnclosedFenceTest(_Case):
    def test_a_closed_block_says_nothing(self):
        full = page(0, "```ts", "x", "```") + page(1)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_frontmatter_example_in_a_closed_block_is_still_no_page_and_says_nothing(self):
        full = page(0, "```yaml", "---", "url: https://example.com/not-a-page", "---", "```") + page(1)
        docs = generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))
        self.assertEqual([d["title"] for d in docs], ["Page 0", "Page 1"])
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_left_open_on_the_last_page_is_said(self):
        full = page(0) + page(1, "```ts", "x")
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)

    def test_it_survives_dropping_empty_pages(self):
        """``skip_empty`` removes pages from the list; what the split found
        about the file is not a property of one of them."""
        full = page(0) + "---\ntitle: Empty\nurl: https://example.com/e\n---\n\n" + page(1, "```ts", "x")
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)

    def test_split_auto_reports_only_the_candidate_that_won(self):
        # frontmatter wins (it finds the URLs), and it left a block open
        lost_h1 = page(0) + page(1, "```ts", "x")
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(lost_h1))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "never closes")), 1, err)
        # h1 wins (Source: lines); the frontmatter candidate, which never
        # finds a closing block here, has an open fence that is not the file's
        won_h1 = ("# One\nSource: https://example.com/a\n\nbody\n"
                  "# Two\nSource: https://example.com/b\n\n```ts\nx\n"
                  "---\nurl: https://example.com/c\n---\n")
        code, _out, err = self.run_g("auto", "sections", "0", corpus=self.files(won_h1))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "never closes"), [], err)


class FewPagesThanTheIndexTest(_Case):
    def test_the_ratio_boundary(self):
        """Half the index is not yet "far fewer"; below half is."""
        full = "".join(page(k) for k in range(5))
        for listed, warned in ((10, False), (11, True), (5, False)):
            with self.subTest(listed=listed):
                code, _out, err = self.run_g("title-join", "sections", "0",
                                             corpus=self.files(full, index_for(listed)))
                self.assertEqual(code, 0, err)
                self.assertEqual(bool(self.warnings(err, "split into")), warned, err)

    def test_it_counts_pages_not_entries_that_link_a_place_in_one(self):
        """Zod's index lists one page per anchor: 326 entries, 17 pages."""
        full = page(0)
        index = "".join(f"- [Place {k}](https://example.com/docs/p0?id=place{k}): a place\n" for k in range(50))
        index += "".join(f"- [Other {k}](https://example.com/docs/llms-{k}.txt): another index\n" for k in range(50))
        code, _out, err = self.run_g("title-join", "sections", "0", corpus=self.files(full, index))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "split into"), [], err)

    def test_an_h1_split_is_compared_too(self):
        full = "# One\nSource: https://example.com/a\n\nbody\n"
        code, _out, err = self.run_g("h1", "sections", "0", corpus=self.files(full, index_for(4)))
        self.assertEqual(code, 0, err)
        self.assertIn("split into 1 pages, but its llms.txt lists 4", self.warning(err, "split into"))

    def test_every_way_the_index_is_at_hand(self):
        full = "".join(page(k) for k in range(2))
        index = index_for(10)
        ways = {
            "title join, cached": ("title-join", None, ["sections", "0"]),
            "title join, files": ("title-join", self.files(full, index), ["sections", "0"]),
            "url join, cached": ("url-join", None, ["sections", "0"]),
            "url join, files": ("url-join", self.files(full, index), ["sections", "0"]),
            "listing, cached": ("url-join", None, ["fetch-index"]),
            "listing, files": ("url-join", self.files(full, index), ["fetch-index"]),
            "search index, cached": ("url-join", None, ["search-index", "page"]),
        }
        self.stage("title-join", full, index)
        self.stage("url-join", full, index)
        for what, (source, corpus, argv) in ways.items():
            with self.subTest(what):
                _code, _out, err = self.run_g(source, *argv, corpus=corpus)
                self.assertEqual(len(self.warnings(err, "split into 2 pages")), 1, err)

    def test_a_command_that_needs_no_index_does_not_download_one_to_compare(self):
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full)  # no index in the cache
        calls = []

        def fake(req, *a, **kw):
            calls.append(getattr(req, "full_url", req))
            raise OSError("test: network disabled")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            code, _out, err = _loader.run_cli(generic, [SCRIPT, "sections", "0", "--source", "url-join",
                                                        "--sources-file", self.sources, "--cache-dir", self.tmp])
        self.assertEqual((code, err, calls), (0, "", []))

    def test_a_stale_cached_index_is_not_compared(self):
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full, index_for(10))
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=("--cache-dir", self.tmp, "--max-age", "999999999"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err, "split into")), 1, err)
        # an index cached for another URL is another site's
        meta = Path(generic._index_cache_path(self.tmp, self.profile("url-join")) + ".meta.json")
        meta.write_text(json.dumps({"url": "https://other.example/llms.txt"}), encoding="utf-8")
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=("--cache-dir", self.tmp, "--max-age", "999999999"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.warnings(err, "split into"), [], err)

    def test_file_without_index_file_does_not_compare_with_the_cache(self):
        """``--file`` is another corpus than the cache: its index is not this file's."""
        full = "".join(page(k) for k in range(2))
        self.stage("url-join", full, index_for(10))
        code, _out, err = self.run_g("url-join", "sections", "0",
                                     corpus=self.files(full) + ("--cache-dir", self.tmp))
        self.assertEqual((code, err), (0, ""))

    def test_a_missing_index_file_is_left_to_the_command_that_needs_it(self):
        full = "".join(page(k) for k in range(2))
        corpus = self.files(full) + ("--index-file", str(Path(self.tmp, "gone.txt")))
        code, _out, err = self.run_g("url-join", "sections", "0", corpus=corpus)
        self.assertEqual((code, err), (0, ""))
        code, _out, err = self.run_g("url-join", "search", "page", corpus=corpus)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


class FrontmatterCutOffTest(_Case):
    L = generic._FM_LOOKAHEAD

    def full(self, list_lines: int, *, closed: bool = True, after: int = 20) -> str:
        """A page, then a block with a list of *list_lines* items."""
        block = ["---", "title: Long", 'url: "https://example.com/docs/long"', "supportedModels:",
                 *[f"  - model-{n}" for n in range(list_lines)]]
        tail = (["---"] if closed else []) + ["", "body of the long page", *["more text"] * after, ""]
        return page(0) + "\n".join(block + tail)

    def test_the_boundary_of_the_bound(self):
        # the closing line is the block's line 3 + list_lines + 1
        fits = self.L - 5
        for list_lines, warned, pages in ((fits, False, 2), (fits + 1, True, 1), (fits + 200, True, 1)):
            with self.subTest(list_lines=list_lines):
                full = self.full(list_lines)
                docs = generic.split_documents(full.splitlines(keepends=True), self.profile("no-index"))
                self.assertEqual(len(docs), pages)
                code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                found = self.warnings(err, "did not close within")
                self.assertEqual(bool(found), warned, err)
                if warned:
                    opened = full.splitlines(keepends=True).index("title: Long\n")
                    self.assertIn("1 frontmatter block(s) of", found[0])
                    self.assertIn(f"{self.L} lines (first near line {opened}) ", found[0])

    def test_a_block_cut_by_the_end_of_the_file_is_not_cut_by_the_bound(self):
        """It ran out of file, not of lookahead: raising the bound changes
        nothing, and a file ending in a rule and a list is common."""
        for list_lines in (self.L - 6, self.L - 5, self.L - 4):
            with self.subTest(list_lines=list_lines):
                full = page(0) + "\n".join(["---", "title: Long", 'url: "https://example.com/l"', "items:",
                                            *[f"  - m{n}" for n in range(list_lines)]]) + "\n"
                total = len(full.splitlines()) - len(page(0).splitlines())
                code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
                self.assertEqual(code, 0, err)
                self.assertEqual(bool(self.warnings(err, "did not close within")), total > self.L, (total, err))

    def test_a_rule_followed_by_a_long_list_is_no_cut_block(self):
        """A horizontal rule and a list with no required key is plain text."""
        rule = "\n".join(["---", "", *[f"- item {n}" for n in range(self.L + 50)], "", "text", ""])
        full = page(0) + rule
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_that_stops_being_yaml_is_no_cut_block(self):
        rule = "\n".join(["---", "title: A note", "", "# Not yaml", *["more text"] * (self.L + 50), ""])
        full = page(0) + rule
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))

    def test_a_block_in_a_code_fence_is_not_looked_at(self):
        block = ["---", "title: Long", 'url: "https://example.com/l"', "items:",
                 *[f"  - m{n}" for n in range(self.L + 20)]]
        full = page(0, "```yaml", *block, "```") + page(1)
        code, _out, err = self.run_g("no-index", "sections", "0", corpus=self.files(full))
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
