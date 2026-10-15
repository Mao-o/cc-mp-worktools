"""``rank_from: "index"`` (the index-first engine) of parse-llms-txt.py.

With ``rank_from: "index"`` the ``llms.txt`` entries are ranked and listed
(their titles, descriptions and URLs are shown) and the page bodies are taken
from ``llms-full.txt``, as ``parse-claude-docs.py`` does:

* ``index_join`` says how an entry finds its page: ``"url"`` (the normalised
  URL) or ``"title"`` (a title unique on both sides, the default)
* ``fetch-index`` / ``search-index`` never download ``llms-full.txt``: they
  join only against ``--file`` or a cached copy that ``fetch_url`` would serve
  as is (younger than ``--max-age``, fetched from the profile's URL); else
  each entry is listed by a URL slug unique across the index
* ``index_group_variants`` collapses ``X (Python)`` / ``X (Go)`` runs into one
  line with a ``Variants:`` list
* ``search`` skips an entry with no page (on stderr) and, with ``index_join:
  "url"``, checks the join rate (exit 2 below 50%)

``FetchIndexDocIdxJoinTest`` / ``SearchIndexDocIdxJoinTest`` /
``JoinRateTest`` are ported from ``test_parse_claude_docs.py``. No network:
``urllib.request.urlopen`` is patched to fail and every call is recorded.
"""

import json
import os
import re
import shlex
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")
import _common  # noqa: E402  (scripts/ is on sys.path via _loader)

SCRIPT = "parse-llms-txt.py"
FULL_URL = "https://example.com/llms-full.txt"
INDEX_URL = "https://example.com/llms.txt"
CODE = {"url": FULL_URL, "split": "h1", "page_url": "line:Source: ", "index_url": INDEX_URL,
        "page_ref_forms": ["int", "url", "slug"], "rank_from": "index", "index_join": "url"}
SOURCES = {
    "code": CODE,
    "grouped": {**CODE, "index_group_variants": True},
    # title join: for a profile whose pages carry no URL of their own
    "by-title": {**CODE, "index_join": "title", "page_url": "none"},
    "label": {**CODE, "label": "Example Docs", "anchor_style": "github"},
    "packed": {**CODE, "compact_pack": True},
    "pages": {k: v for k, v in CODE.items() if k not in ("rank_from", "index_join")},
}

SKILLS_HOOKS_INDEX = ("- [Skills](https://example.com/en/skills): Package reusable capabilities\n"
                      "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n")
# llms-full.txt order differs from llms.txt order: Skills is entry 0 but page 1
HOOKS_SKILLS_FULL = ("# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
                     "# Skills\nSource: https://example.com/en/skills\n\nbody\n")


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        Path(self.sources).write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
        self.calls: list = []

    def profile(self, name: str = "code") -> dict:
        return self.validate(SOURCES[name], name)

    def validate(self, raw: dict, name: str = "x") -> dict:
        try:
            return generic._validate_profile(self.sources, name, raw)
        except SystemExit:  # a refused profile is a failure, not an error of the run
            self.fail(f"profile {name} refused")

    def full_cache(self, name: str = "code") -> Path:
        return Path(generic._cache_path(self.tmp, self.profile(name)))

    def index_cache(self, name: str = "code") -> Path:
        return Path(generic._index_cache_path(self.tmp, self.profile(name)))

    def write(self, index: str | None, full: str | None = None, name: str = "code") -> None:
        if index is not None:
            self.index_cache(name).write_text(index, encoding="utf-8")
        if full is not None:
            self.full_cache(name).write_text(full, encoding="utf-8")

    def age(self, path: Path, seconds: int) -> None:
        then = time.time() - seconds
        os.utime(path, (then, then))

    def run_g(self, *argv, source: str = "code", corpus: tuple | None = None):
        """Run the generic loader on the temp cache (or *corpus* options)."""
        opts = corpus if corpus is not None else ("--cache-dir", self.tmp)
        full = [SCRIPT, *argv, "--source", source, "--sources-file", self.sources, *opts]
        return self.run_argv(full)

    def run_argv(self, argv: list):
        def fake_urlopen(req, *a, **kw):
            self.calls.append(getattr(req, "full_url", req))
            raise OSError("test: network disabled")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            try:
                return _loader.run_cli(generic, list(argv))
            except Exception as e:  # a crash is a failure, not an error of the run
                self.fail(f"{argv[1:3]} crashed: {e!r}")

    def full_fetches(self) -> int:
        return sum(1 for u in self.calls if u == FULL_URL)


class ProfileValidationTest(_Case):
    """Each key is checked, and the combinations that cannot work are refused."""

    def _err(self, raw: dict) -> str:
        code, _out, err = _loader.run_cli(generic, [SCRIPT, "sources", "--sources-file", self._file(raw)])
        self.assertEqual(code, 1)
        return err

    def _file(self, raw: dict) -> str:
        path = os.path.join(self.tmp, "bad.json")
        Path(path).write_text(json.dumps({"sources": {"x": raw}}), encoding="utf-8")
        return path

    def test_defaults_keep_the_page_ranking(self):
        p = generic._validate_profile("s.json", "x", {"url": FULL_URL, "split": "h1"})
        self.assertEqual((p["rank_from"], p["index_join"], p["index_group_variants"]),
                         ("pages", "title", False))

    def test_bad_values_and_combinations_are_refused(self):
        cases = {
            "rank_from must be one of": {**CODE, "rank_from": "entries"},
            "index_join must be one of": {**CODE, "index_join": "slug"},
            "index_group_variants must be true or false": {**CODE, "index_group_variants": "yes"},
            'rank_from "index" needs index_url': {k: v for k, v in CODE.items() if k != "index_url"},
            'rank_from "index" needs "slug" and "url" in page_ref_forms':
                {**CODE, "page_ref_forms": ["int", "url"]},
            'needs "slug" and "url" in page_ref_forms (entries not joined to a page are listed by '
            'URL slug; a URL listed twice is shown by its full URL)': {**CODE, "page_ref_forms": ["int", "slug"]},
            'rank_from "index" with page_url needs index_join "url" (entries listed by URL slug '
            "must match the pages' own URLs)": {**CODE, "index_join": "title"},
            'rank_from "index" with page_url needs index_join "url"': {k: v for k, v in CODE.items()
                                                                       if k != "index_join"},
            'index_join is only valid with rank_from "index"': SOURCES["pages"] | {"index_join": "title"},
            'index_group_variants is only valid with rank_from "index"':
                SOURCES["pages"] | {"index_group_variants": True},
            'index_join "url" needs page_url': {**CODE, "page_url": "none"},
            "join_rate_guard is redundant": {**CODE, "join_rate_guard": True},
        }
        for needle, raw in cases.items():
            with self.subTest(needle):
                self.assertIn(needle, self._err(raw))

    def test_title_join_and_group_variants_are_accepted(self):
        for name in ("by-title", "grouped"):
            with self.subTest(name):
                p = self.profile(name)
                self.assertEqual(p["rank_from"], "index")

    def test_title_join_without_page_url_is_accepted(self):
        """The refusal of title join + page_url keys on page_url only."""
        p = self.validate({**CODE, "index_join": "title", "page_url": "none"})
        self.assertEqual((p["index_join"], p["page_url"]), ("title", None))

    def test_split_auto_shares_the_index_keys(self):
        raw = {"url": FULL_URL, "split": "auto", "index_url": INDEX_URL,
               "page_ref_forms": ["int", "url", "slug"], "rank_from": "index", "index_join": "url",
               "split_candidates": [{"split": "h1", "page_url": "line:Source: "},
                                    {"split": "frontmatter", "page_url": "frontmatter:url"}]}
        p = generic._validate_profile("s.json", "x", raw)
        self.assertEqual([c["index_join"] for c in p["split_candidates"]], ["url", "url"])


class FetchIndexDocIdxJoinTest(_Case):
    """Ported from ``test_parse_claude_docs.py``: fetch-index shows the
    llms-full.txt page number of each entry when it is already cached (and
    fresh), else a URL slug; it never fetches llms-full.txt for this."""

    def test_shows_slug_and_does_not_fetch_full_text_when_uncached(self):
        self.write("- [Hooks](https://example.com/en/hooks): Configure hook matchers\n")
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertNotIn("[0]", out)
        self.assertFalse(self.full_cache().exists())
        self.assertEqual(self.calls, [])

    def test_shows_joined_full_text_doc_idx_when_full_text_already_cached(self):
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Skills", out)
        self.assertIn("[0] Hooks", out)
        self.assertIn("Next: parse-llms-txt.py sections <page_ref>", out)

    def test_grouped_variants_show_per_variant_ref_not_misleading_range(self):
        self.write("- [Batches (Python)](https://example.com/en/batches-py): Python batches\n"
                   "- [Batches (Go)](https://example.com/en/batches-go): Go batches\n", name="grouped")
        code, out, err = self.run_g("fetch-index", source="grouped")
        self.assertEqual(code, 0, err)
        self.assertIn("Batches\n    Python batches\n    Variants: Python [batches-py], Go [batches-go]\n", out)
        self.assertNotIn("[0-1]", out)
        self.assertIn("(2 documents total, 1 entries shown — 2 documents grouped)", out)

    def test_falls_back_to_slug_when_full_text_cache_is_stale(self):
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        self.age(self.full_cache(), 1000)
        code, out, err = self.run_g("fetch-index", "--max-age", "100")
        self.assertEqual(code, 0, err)
        self.assertIn("[skills] Skills", out)
        self.assertIn("[hooks] Hooks", out)
        self.assertNotIn("[0]", out)
        self.assertNotIn("[1]", out)

    def test_fresh_full_text_cache_within_max_age_still_joins(self):
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        self.age(self.full_cache(), 500)
        code, out, err = self.run_g("fetch-index", "--max-age", "1000")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Skills", out)
        self.assertIn("[0] Hooks", out)

    def test_colliding_last_segment_slugs_are_disambiguated(self):
        self.write("- [Hooks](https://example.com/en/hooks): zzcommon hook matchers\n"
                   "- [Agent SDK Hooks](https://example.com/en/agent-sdk/hooks): Agent SDK hook matchers\n")
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("[en/hooks] Hooks", out)
        self.assertIn("[agent-sdk/hooks] Agent SDK Hooks", out)
        self.assertNotIn("[hooks]", out)


class QueryVariantJoinTest(_Case):
    """index_join "url": pages whose URLs differ only by the query
    (``?surface=cli`` / ``?surface=api``) are different pages, each entry
    joined to the page with its own query (as a URL page_ref chooses)."""

    INDEX = ("- [Overview CLI](https://example.com/docs/x?surface=cli): cli zzq\n"
             "- [Overview API](https://example.com/docs/x?surface=api): api zzq\n")
    FULL = ("# Overview CLI\nSource: https://example.com/docs/x?surface=cli\n\n## C\ncli zzq body\n"
            "# Overview API\nSource: https://example.com/docs/x?surface=api\n\n## A\napi zzq body\n")

    def _corpora(self):
        self.write(self.INDEX, self.FULL)
        full, index = os.path.join(self.tmp, "f.txt"), os.path.join(self.tmp, "i.txt")
        Path(full).write_text(self.FULL, encoding="utf-8")
        Path(index).write_text(self.INDEX, encoding="utf-8")
        return (("--cache-dir", self.tmp), ("--file", full, "--index-file", index))

    def test_each_entry_lists_and_searches_its_own_page(self):
        for corpus in self._corpora():
            with self.subTest(corpus=corpus[0]):
                code, out, err = self.run_g("fetch-index", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertIn("[0] Overview CLI\n", out)
                self.assertIn("[1] Overview API\n", out)
                code, out, err = self.run_g("search", "zzq", corpus=corpus)
                self.assertEqual(code, 0, err)
                blocks = {b.split(" (index_score")[0]: b for b in re.split(r"\n(?=\[\d+\] )", out)}
                self.assertIn("URL: https://example.com/docs/x?surface=cli", blocks["[0] Overview CLI"])
                self.assertIn("cli zzq body", blocks["[0] Overview CLI"])
                self.assertNotIn("api zzq body", blocks["[0] Overview CLI"])
                self.assertIn("api zzq body", blocks["[1] Overview API"])
        self.assertEqual(self.calls, [])

    def test_which_page_of_a_shared_url_an_entry_joins(self):
        """The last page with the entry's query; else the last page with no
        query (``api?id=…`` anchors of one ``api`` page); else, for an entry
        with no query, the first page (the one its ref opens before
        llms-full.txt is cached: a URL page_ref takes the first); else none
        (``?surface=web`` is not the cli or api page). Two pages with the
        very same URL: the last (as the dedicated parser)."""
        profile = self.profile()
        docs = [{"title": t, "url": u} for t, u in (
            ("A", "https://example.com/docs/x?surface=cli"), ("B", "https://example.com/docs/x?surface=api"),
            ("C", "https://example.com/docs/y"), ("D", "https://example.com/docs/y.md"), ("E", None),
            ("F", "https://example.com/docs/w?v=2"), ("G", "https://example.com/docs/w"),
            ("H", "https://example.com/docs/w?v=3"))]
        entries = [{"url": u} for u in ("https://example.com/docs/x?surface=web",
                                        "https://example.com/docs/x.md?surface=cli#top",
                                        "https://example.com/docs/y/", "https://example.com/docs/z", "",
                                        "https://example.com/docs/w?id=a", "https://example.com/docs/x")]
        try:
            got = generic._entry_docs(entries, docs, profile)
        except Exception as e:  # a crash is a failure, not an error of the run
            self.fail(f"_entry_docs crashed: {e!r}")
        # a page with no URL is joined to no entry, not even one whose URL is empty
        self.assertEqual(got, [None, 0, 3, None, None, 6, 0])

    def test_entry_whose_query_no_page_has_is_skipped(self):
        """``?surface=web`` with pages for cli and api only: search skips the
        entry (stderr) instead of showing the api body under its title."""
        index = self.INDEX + "- [Overview Web](https://example.com/docs/x?surface=web): web zzq\n"
        self.write(index, self.FULL)
        full, idx = os.path.join(self.tmp, "f.txt"), os.path.join(self.tmp, "i.txt")
        Path(full).write_text(self.FULL, encoding="utf-8")
        Path(idx).write_text(index, encoding="utf-8")
        for corpus in (("--cache-dir", self.tmp), ("--file", full, "--index-file", idx)):
            with self.subTest(corpus=corpus[0]):
                code, out, err = self.run_g("search", "zzq", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertIn("(skip: no full-text for https://example.com/docs/x?surface=web)", err)
                self.assertNotIn("Overview Web", out)
                self.assertIn("(2 documents from", out)
                code, out, err = self.run_g("fetch-index", corpus=corpus)
                self.assertEqual(code, 0, err)
                self.assertIn("[0] Overview CLI\n", out)
                self.assertIn("[1] Overview API\n", out)
                self.assertNotRegex(out, r"\[\d+\] Overview Web")
                # its listed ref, put in the Next: line, opens no page (not the cli one)
                refs, next_line = self._listed(["fetch-index"], corpus)
                self.assertEqual(shlex.split(refs["Overview Web"]), ["https://example.com/docs/x?surface=web"])
                argv = shlex.split(next_line.replace("<page_ref>", refs["Overview Web"]), comments=True)
                code, out, err = self.run_argv(argv)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("No page found for URL: https://example.com/docs/x?surface=web", err)

    # pages for all three queries, CLI first: a ref that loses the query
    # opens the CLI page for every entry
    INDEX3 = ("- [Overview CLI](https://example.com/docs/x?surface=cli): cli zzq\n"
              "- [Overview API](https://example.com/docs/x?surface=api): api zzq\n"
              "- [Overview Web](https://example.com/docs/x?surface=web): web zzq\n")
    FULL3 = ("# Overview CLI\nSource: https://example.com/docs/x?surface=cli\n\n## C\ncli zzq body\n"
             "# Overview API\nSource: https://example.com/docs/x?surface=api\n\n## A\napi zzq body\n"
             "# Overview Web\nSource: https://example.com/docs/x?surface=web\n\n## W\nweb zzq body\n")

    def _listed(self, command: list, corpus) -> tuple[dict, str]:
        """{title: ref} and the ``Next:`` line of a fetch-index / search-index run."""
        code, out, err = self.run_g(*command, corpus=corpus)
        self.assertEqual(code, 0, err)
        refs = {m.group(2): m.group(1) for m in re.finditer(r"^\[(.+?)\] (Overview \w+)(?: \(score: \d+\))?$",
                                                            out, re.M)}
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertEqual(len(nexts), 1, out)
        return refs, nexts[0]

    def _open(self, command: list, next_line: str, ref: str, extra: tuple = ()) -> str:
        """Put *ref* in place of the placeholder of *next_line* as it reads,
        split it by the shell rules, run it, return the output."""
        placeholder = "<page_ref>" if "<page_ref>" in next_line else "<slug>"
        if command[0] == "search-index":  # it points at search; open the ref with sections
            next_line = f"{SCRIPT} sections {placeholder} " + next_line.split(" ", 3)[3]
        try:
            argv = shlex.split(next_line.replace(placeholder, ref), comments=True)
        except ValueError as e:
            self.fail(f"not a shell command: {e}")
        code, out, err = self.run_argv(argv + list(extra))
        self.assertEqual(code, 0, err)
        return out

    def test_uncached_query_variant_refs_open_their_own_page(self):
        """No llms-full.txt cached: each ref (the full URL, query included,
        shell-quoted) opens the page with the entry's query once the bodies
        are there, cached or as ``--file``."""
        full = os.path.join(self.tmp, "full3.txt")
        Path(full).write_text(self.FULL3, encoding="utf-8")
        want = {"Overview CLI": 0, "Overview API": 1, "Overview Web": 2}
        for command in (["fetch-index"], ["search-index", "zzq"]):
            for via in ("cache", "file"):
                with self.subTest(command=command[0], via=via):
                    if self.full_cache().exists():
                        self.full_cache().unlink()
                    self.write(self.INDEX3)
                    refs, next_line = self._listed(command, ("--cache-dir", self.tmp))
                    self.assertEqual(sorted(refs), sorted(want))
                    for title, ref in refs.items():
                        query = title.split()[1].lower()
                        self.assertEqual(shlex.split(ref), [f"https://example.com/docs/x?surface={query}"])
                    if via == "cache":
                        self.write(None, self.FULL3)
                    for title, ref in refs.items():
                        out = self._open(command, next_line, ref, () if via == "cache" else ("--file", full))
                        self.assertRegex(out, rf'^Sections in \[{want[title]}\] "{title}"')
        self.assertEqual(self.full_fetches(), 0)

    def test_joined_query_variant_refs_open_their_own_page(self):
        """Cached and ``--file``: each page-number ref opens its entry's page."""
        full, index = os.path.join(self.tmp, "f3.txt"), os.path.join(self.tmp, "i3.txt")
        Path(full).write_text(self.FULL3, encoding="utf-8")
        Path(index).write_text(self.INDEX3, encoding="utf-8")
        self.write(self.INDEX3, self.FULL3)
        want = {"Overview CLI": "0", "Overview API": "1", "Overview Web": "2"}
        for corpus in (("--cache-dir", self.tmp), ("--file", full, "--index-file", index)):
            for command in (["fetch-index"], ["search-index", "zzq"]):
                with self.subTest(command=command[0], corpus=corpus[0]):
                    refs, next_line = self._listed(command, corpus)
                    self.assertEqual(refs, want)
                    for title, ref in refs.items():
                        self.assertRegex(self._open(command, next_line, ref),
                                         rf'^Sections in \[{ref}\] "{title}"')
        self.assertEqual(self.calls, [])

    def test_anchor_queries_of_one_page_open_that_page(self):
        """``api?id=strings`` / ``api?id=numbers`` (anchors in the one ``api``
        page): uncached, the full-URL refs fall back to that page; cached,
        both entries join it."""
        index = ("- [Strings](https://example.com/api?id=strings): zzq strings\n"
                 "- [Numbers](https://example.com/api?id=numbers): zzq numbers\n"
                 "- [Intro](https://example.com/intro): zzq intro\n"
                 "- [Guide](https://example.com/guide?tab=a): zzq guide\n")
        full_text = ("# Intro\nSource: https://example.com/intro\n\n## I\nzzq intro body\n"
                     "# API\nSource: https://example.com/api\n\n## Strings\nzzq strings body\n")
        self.write(index)
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("['https://example.com/api?id=strings'] Strings\n", out)
        self.assertIn("['https://example.com/api?id=numbers'] Numbers\n", out)
        # an entry with a query is listed by its full URL even when its URL
        # is listed once: a slug drops the query and could open another
        # query's page; one with no query keeps its slug
        self.assertIn("['https://example.com/guide?tab=a'] Guide\n", out)
        self.assertIn("[intro] Intro\n", out)
        next_line = next(ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: "))
        self.write(None, full_text)
        for ref in ("'https://example.com/api?id=strings'", "'https://example.com/api?id=numbers'"):
            with self.subTest(ref=ref):
                self.assertRegex(self._open(["fetch-index"], next_line, ref), r'^Sections in \[1\] "API"')
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Strings\n", out)
        self.assertIn("[1] Numbers\n", out)
        code, out, err = self.run_g("search", "zzq strings")
        self.assertEqual(code, 0, err)
        self.assertIn("zzq strings body", out)
        self.assertNotIn("skip: no full-text for https://example.com/api", err)

    # lines copied verbatim from a real llms.txt (zod.dev): anchors of the
    # api and basics pages, and the basics page itself
    ZOD_INDEX = ("- [Primitives](https://zod.dev/api?id=primitives)\n"
                 "- [Strings](https://zod.dev/api?id=strings)\n"
                 "- [Basic usage](https://zod.dev/basics): Basic usage guide covering schema definition, "
                 "parsing data, error handling, and type inference\n"
                 "- [Defining a schema](https://zod.dev/basics?id=defining-a-schema)\n"
                 "- [Parsing data](https://zod.dev/basics?id=parsing-data)\n")
    ZOD_FULL = ("# Basic usage\nSource: https://zod.dev/basics\n\n## Defining a schema\nx\n"
                "# Defining schemas\nSource: https://zod.dev/api\n\n## Primitives\nx\n")

    def test_real_anchor_index_lines_round_trip(self):
        """Uncached, cached and ``--file``: every ref opens the page of the
        entry's URL (the query only picks a place in it)."""
        page = {"https://zod.dev/api": "Defining schemas", "https://zod.dev/basics": "Basic usage"}
        full, index = os.path.join(self.tmp, "zf.txt"), os.path.join(self.tmp, "zi.txt")
        Path(full).write_text(self.ZOD_FULL, encoding="utf-8")
        Path(index).write_text(self.ZOD_INDEX, encoding="utf-8")
        entries = _common.parse_llms_index(self.ZOD_INDEX.splitlines())
        for mode in ("uncached", "cached", "file"):
            with self.subTest(mode=mode):
                if self.full_cache().exists():
                    self.full_cache().unlink()
                self.write(self.ZOD_INDEX, self.ZOD_FULL if mode == "cached" else None)
                corpus = ("--file", full, "--index-file", index) if mode == "file" else ("--cache-dir", self.tmp)
                code, out, err = self.run_g("fetch-index", corpus=corpus)
                self.assertEqual(code, 0, err)
                refs = re.findall(r"^\[(.+?)\] ", out, re.M)
                self.assertEqual(len(refs), len(entries), out)
                if mode == "uncached":
                    self.assertIn("[https://zod.dev/basics] Basic usage\n", out)
                    self.assertIn("['https://zod.dev/basics?id=parsing-data'] Parsing data\n", out)
                    self.write(None, self.ZOD_FULL)
                else:
                    self.assertTrue(all(r.isdigit() for r in refs), refs)
                next_line = next(ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: "))
                for ref, e in zip(refs, entries):
                    title = page[_common.normalize_doc_url(e["url"])]
                    self.assertRegex(self._open(["fetch-index"], next_line, ref), rf'^Sections in \[\d+\] "{title}"')
        self.assertEqual(self.calls, [])


class FileHintTest(_Case):
    """With ``--file`` nothing is fetched: fetch-index / search-index say
    neither that llms-full.txt will be fetched nor that it is not cached."""

    def _corpus(self, full_text: str) -> tuple:
        full, index = os.path.join(self.tmp, "full.txt"), os.path.join(self.tmp, "index.txt")
        Path(full).write_text(full_text, encoding="utf-8")
        Path(index).write_text(SKILLS_HOOKS_INDEX, encoding="utf-8")
        return ("--file", full, "--index-file", index)

    def test_joined_file_does_not_promise_a_fetch(self):
        code, out, err = self.run_g("fetch-index", corpus=self._corpus(HOOKS_SKILLS_FULL))
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Hooks", out)
        self.assertNotIn("fetched automatically", out)
        # the cache keeps it: the next full-text command does fetch it
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("  (llms-full.txt will be fetched automatically on first use)\n", out)

    def test_unjoined_file_says_no_entry_matched(self):
        other = "# Other\nSource: https://example.com/en/other\n\nbody\n"
        corpus = self._corpus(other)
        code, out, err = self.run_g("fetch-index", corpus=corpus)
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertIn("  (no entry matched a page of --file, so [ref] is a URL slug)\n", out)
        self.assertNotIn("not cached", out)
        code, out, err = self.run_g("search-index", "hooks", corpus=corpus)
        self.assertEqual(code, 0, err)
        self.assertIn("Note: no entry matched a page of --file, so [ref] is a URL slug.\n", out)
        self.assertNotIn("not cached", out)
        self.assertEqual(self.calls, [])
        # the cache keeps the fetch advice
        self.write(SKILLS_HOOKS_INDEX)
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("  (llms-full.txt not cached yet, so the bracketed ref above is a\n", out)
        code, out, err = self.run_g("search-index", "hooks")
        self.assertEqual(code, 0, err)
        self.assertIn(generic._SLUG_REF_NOTE + "\n", out)


class SearchIndexDocIdxJoinTest(_Case):
    """Ported from ``test_parse_claude_docs.py``: search-index joins when
    llms-full.txt is cached and fresh (no note), else shows slugs and a note."""

    def test_uses_slug_and_shows_note_when_full_text_uncached(self):
        self.write("- [Hooks](https://example.com/en/hooks): Configure hook matchers\n")
        code, out, err = self.run_g("search-index", "hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertIn("Note:", out)

    def test_uses_joined_doc_idx_and_omits_note_when_full_text_cached(self):
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        code, out, err = self.run_g("search-index", "hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Hooks", out)
        self.assertNotIn("Note:", out)

    def test_falls_back_to_slug_and_shows_note_when_full_text_cache_is_stale(self):
        self.write(SKILLS_HOOKS_INDEX, HOOKS_SKILLS_FULL)
        self.age(self.full_cache(), 1000)
        code, out, err = self.run_g("search-index", "hooks", "--max-age", "100")
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)
        self.assertIn("Note:", out)


class IndexCommandsNeverFetchFullTextTest(_Case):
    """Cost: fetch-index / search-index fetch llms-full.txt 0 times whether
    its cache is fresh, stale, fetched from another URL or absent. The
    index itself is cached and fresh, so no request at all is made."""

    STATES = ("fresh", "stale", "other-url", "absent")

    def _stage(self, state: str) -> None:
        self.write(SKILLS_HOOKS_INDEX, None if state == "absent" else HOOKS_SKILLS_FULL)
        if state == "stale":
            self.age(self.full_cache(), 1000)
        if state == "other-url":
            Path(str(self.full_cache()) + ".meta.json").write_text(
                json.dumps({"url": "https://other.example.com/llms-full.txt"}), encoding="utf-8")

    def test_no_request_in_any_cache_state(self):
        for command in (["fetch-index"], ["fetch-index", "--compact"], ["search-index", "hooks"],
                        ["search-index", "hooks", "--show-sections"]):
            for state in self.STATES:
                with self.subTest(command=command, state=state):
                    shutil.rmtree(self.tmp)
                    os.makedirs(self.tmp)
                    Path(self.sources).write_text(json.dumps({"sources": SOURCES}), encoding="utf-8")
                    self.calls = []
                    self._stage(state)
                    code, out, err = self.run_g(*command, "--max-age", "100")
                    self.assertEqual(code, 0, err)
                    self.assertEqual(self.calls, [])
                    joined = re.search(r"^\[0\] Hooks", out, re.M) is not None
                    self.assertEqual(joined, state == "fresh", out)

    def test_the_mock_does_see_a_full_text_fetch(self):
        """Positive control: ``search`` needs the bodies and does fetch
        llms-full.txt when it is not cached (the mocked fetch fails)."""
        self.write(SKILLS_HOOKS_INDEX)
        code, _out, _err = self.run_g("search", "hooks")
        self.assertEqual(code, 1)
        self.assertEqual(self.full_fetches(), 1)

    def test_cached_if_fresh_follows_fetch_url(self):
        path = self.full_cache()
        self.assertIsNone(_common.cached_if_fresh(str(path), 100, FULL_URL))
        path.write_text("x", encoding="utf-8")
        self.assertEqual(_common.cached_if_fresh(str(path), 100, FULL_URL), str(path))
        self.assertEqual(_common.cached_if_fresh(str(path), None, FULL_URL), str(path))
        self.age(path, 1000)
        self.assertIsNone(_common.cached_if_fresh(str(path), 100, FULL_URL))
        self.assertEqual(_common.cached_if_fresh(str(path), None, FULL_URL), str(path))
        self.age(path, 0)
        meta = Path(str(path) + ".meta.json")
        meta.write_text(json.dumps({"url": "https://other.example.com/x"}), encoding="utf-8")
        self.assertIsNone(_common.cached_if_fresh(str(path), 100, FULL_URL))
        self.assertEqual(_common.cached_if_fresh(str(path), 100), str(path))
        meta.write_text(json.dumps({"url": FULL_URL}), encoding="utf-8")
        self.assertEqual(_common.cached_if_fresh(str(path), 100, FULL_URL), str(path))


class JoinRateTest(_Case):
    """Ported from ``test_parse_claude_docs.py``: with index_join "url",
    search exits 2 when fewer than half of the entries join a page."""

    ONE_PAGE = "# A\nSource: https://example.com/a\n\nbody a desc\n"

    def test_low_join_rate_exits_2(self):
        self.write("- [A](https://example.com/a): desc a\n"
                   "- [B](https://example.com/b): desc b\n"
                   "- [C](https://example.com/c): desc c\n"
                   "- [D](https://example.com/d): desc d\n", self.ONE_PAGE)
        code, _out, err = self.run_g("search", "desc")
        self.assertEqual(code, 2)
        self.assertIn("join rate", err)

    def test_high_join_rate_does_not_fail(self):
        self.write("- [A](https://example.com/a): desc a\n", self.ONE_PAGE)
        code, _out, err = self.run_g("search", "desc")
        self.assertEqual(code, 0, err)
        self.assertNotIn("join rate", err)

    def test_title_join_is_not_rate_checked(self):
        self.write("- [A](https://example.com/a): desc a\n"
                   "- [B](https://example.com/b): desc b\n"
                   "- [C](https://example.com/c): desc c\n", self.ONE_PAGE, name="by-title")
        code, _out, err = self.run_g("search", "desc", source="by-title")
        self.assertEqual(code, 0, err)
        self.assertNotIn("join rate", err)


class SearchFromIndexTest(_Case):
    """search ranks the entries, drills into the joined pages and shows the
    entry's title and URL."""

    def test_entry_title_and_url_are_shown_and_unjoined_entries_skipped(self):
        self.write("- [Hook guide](https://example.com/en/hooks.md): Configure hook matchers\n"
                   "- [Hook recipes](https://example.com/en/recipes.md): More hook matchers\n"
                   "- [Skills](https://example.com/en/skills.md): Package\n",
                   "# Hooks\nSource: https://example.com/en/hooks\n\n## Matchers\nhook matchers here\n"
                   "# Skills\nSource: https://example.com/en/skills\n\nbody\n", name="label")
        code, out, err = self.run_g("search", "hook matchers", source="label")
        self.assertEqual(code, 0, err)
        self.assertIn(f"\n  (index: {self.index_cache('label')})\n===", out)
        self.assertIn("[0] Hook guide (index_score: ", out)
        self.assertIn("    URL: https://example.com/en/hooks.md\n", out)
        self.assertIn("[https://example.com/en/hooks#matchers]", out)
        self.assertNotIn("Hook recipes", out)
        self.assertIn("  (skip: no full-text for https://example.com/en/recipes.md)", err)
        self.assertIn("(1 documents from Example Docs)", out)
        self.assertIn("Next: parse-llms-txt.py content 0 Matchers", out)

    def test_title_join_reads_the_page_of_the_same_title(self):
        self.write("- [Hooks](https://example.com/docs/hooks): zzcommon hook matchers\n"
                   "- [Skills](https://example.com/docs/skills): Package\n"
                   "- [Skills](https://example.com/docs/skills-2): Package again\n",
                   "# Skills\nSource: https://example.com/en/skills\n\nbody\n"
                   "# Hooks\nSource: https://example.com/en/hooks\n\nhook matchers here\n",
                   name="by-title")
        code, out, err = self.run_g("search", "hooks", source="by-title")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Hooks (index_score: ", out)
        code, out, err = self.run_g("fetch-index", source="by-title")
        self.assertEqual(code, 0, err)
        # "Skills" names two entries: neither is joined (slugs)
        self.assertIn("[1] Hooks", out)
        self.assertIn("[skills] Skills", out)
        self.assertIn("[skills-2] Skills", out)

    def test_title_shared_by_two_pages_is_not_joined(self):
        self.write("- [Hooks](https://example.com/docs/hooks): Configure hook matchers\n",
                   "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
                   "# Hooks\nSource: https://example.com/en/agent/hooks\n\nbody\n", name="by-title")
        code, out, err = self.run_g("fetch-index", source="by-title")
        self.assertEqual(code, 0, err)
        self.assertIn("[hooks] Hooks", out)

    def test_entries_linking_another_llms_txt_are_not_pages(self):
        self.write("- [Hooks](https://example.com/en/hooks): Configure hook matchers\n"
                   "- [Full text](https://example.com/llms-full.txt): Everything in one file\n"
                   "- [SDK docs](https://example.com/sdk/llms.txt): Another index\n")
        for command in (["fetch-index"], ["search-index", "file index hooks"]):
            with self.subTest(command=command):
                code, out, err = self.run_g(*command)
                self.assertEqual(code, 0, err)
                self.assertIn("[hooks] Hooks", out)
                self.assertNotIn("Full text", out)
                self.assertNotIn("SDK docs", out)
        code, out, err = self.run_g("fetch-index")
        self.assertIn("(1 documents total,", out)

    def test_body_only_fallback_still_runs(self):
        self.write("- [Hooks](https://example.com/en/hooks): Configure\n",
                   "# Hooks\nSource: https://example.com/en/hooks\n\nbody\n"
                   "# Notes\nSource: https://example.com/en/notes\n\nzzqword here\n")
        code, out, err = self.run_g("search", "zzqword")
        self.assertEqual(code, 0, err)
        self.assertIn("[1] Notes [body-only]", out)
        self.assertIn("    URL: https://example.com/en/notes\n", out)

    def test_file_with_no_pages_exits_2(self):
        """``--file`` that parses to 0 pages is a format failure (exit 2), as
        for the other commands, not "llms-full.txt not cached yet"."""
        full, index = os.path.join(self.tmp, "empty.txt"), os.path.join(self.tmp, "index.txt")
        Path(full).write_text("no heading here\n", encoding="utf-8")
        Path(index).write_text(SKILLS_HOOKS_INDEX, encoding="utf-8")
        for command in (["fetch-index"], ["search-index", "hooks"]):
            with self.subTest(command=command):
                code, out, err = self.run_g(*command, corpus=("--file", full, "--index-file", index))
                self.assertEqual(code, 2, out)
                self.assertIn(f"0 entries parsed from {full}", err)
                self.assertNotIn("not cached yet", out)

    def test_file_needs_index_file(self):
        full = os.path.join(self.tmp, "full.txt")
        Path(full).write_text(HOOKS_SKILLS_FULL, encoding="utf-8")
        for command in (["fetch-index"], ["search-index", "hooks"], ["search", "hooks"]):
            with self.subTest(command=command):
                code, _out, err = self.run_g(*command, corpus=("--file", full))
                self.assertEqual(code, 1)
                self.assertIn("also pass --index-file", err)
        missing = os.path.join(self.tmp, "no-such-index.txt")
        code, _out, err = self.run_g("fetch-index", corpus=("--file", full, "--index-file", missing))
        self.assertEqual(code, 1)
        self.assertIn(f"--index-file '{missing}' does not exist", err)
        self.assertEqual(self.calls, [])

    def test_sections_and_content_do_not_read_the_index(self):
        """With index_join "url" the pages carry their URLs: the full-text
        commands do not fetch llms.txt (no index cache here)."""
        self.write(None, HOOKS_SKILLS_FULL)
        for command in (["sections", "hooks"], ["content", "skills"], ["search-content", "body"]):
            with self.subTest(command=command):
                code, _out, err = self.run_g(*command)
                self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [])

    def test_compact_lists_one_entry_per_line_or_packed(self):
        index = ("- [Batches (Python)](https://example.com/en/batches-py): Python batches\n"
                 "- [Batches (Go)](https://example.com/en/batches-go): Go batches\n"
                 "- [Hooks](https://example.com/en/hooks): Configure hook matchers\n")
        self.write(index, name="grouped")
        code, out, err = self.run_g("fetch-index", "--compact", source="grouped")
        self.assertEqual(code, 0, err)
        self.assertIn("\nBatches — Variants: Python [batches-py], Go [batches-go]\n[hooks] Hooks\n", out)
        self.assertNotIn("Configure hook matchers", out)
        self.write(index, name="packed")
        code, out, err = self.run_g("fetch-index", "--compact", source="packed")
        self.assertEqual(code, 0, err)
        self.assertIn("\n[batches-py] Batches (Python)  [batches-go] Batches (Go)  [hooks] Hooks\n", out)

    def test_show_sections_lists_the_joined_pages_headings(self):
        self.write(SKILLS_HOOKS_INDEX,
                   "# Hooks\nSource: https://example.com/en/hooks\n\n## Matchers\nx\n"
                   "# Skills\nSource: https://example.com/en/skills\n\nbody\n")
        code, out, err = self.run_g("search-index", "hooks", "--show-sections")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Hooks (score: ", out)
        self.assertIn("      - Matchers\n", out)


class RefRoundTripTest(_Case):
    """Every ``[ref]`` that fetch-index / search-index print, put in place of
    the ``<slug>`` / ``<page_ref>`` of their ``Next:`` line exactly as the line
    reads, is a command that runs (shell rules) and opens that entry's page.
    Both corpus option sets: the cache dir, and ``--file`` + ``--index-file``.
    The URLs include the awkward shapes: a last segment starting with ``-``,
    one with a quote, one with ``&``, and a ``.md`` suffix."""

    INDEX = ("- [Dash](https://example.com/en/-dash.md): zzcommon dash\n"
             "- [Quote](https://example.com/en/it's): zzcommon quote\n"
             "- [Amp](https://example.com/en/a&b): zzcommon amp\n"
             "- [Hooks](https://example.com/en/hooks): zzcommon hook matchers\n"
             "- [Agent hooks](https://example.com/en/agent-sdk/hooks): zzcommon agent hooks\n")
    FULL = ("# Agent hooks\nSource: https://example.com/en/agent-sdk/hooks\n\n## A\nx\n"
            "# Hooks\nSource: https://example.com/en/hooks\n\n## H\nx\n"
            "# Amp\nSource: https://example.com/en/a&b\n\n## B\nx\n"
            "# Quote\nSource: https://example.com/en/it's\n\n## Q\nx\n"
            "# Dash\nSource: https://example.com/en/-dash\n\n## D\nx\n")
    TITLES = ("Dash", "Quote", "Amp", "Hooks", "Agent hooks")

    def _refs_and_next(self, out: str) -> tuple[list, str]:
        refs = [(m.group(2), m.group(1)) for m in re.finditer(r"^\[(.+?)\] (\w[\w ]*?)(?: \(score: \d+\))?$",
                                                              out, re.M)]
        nexts = [line[len("Next: "):] for line in out.splitlines() if line.startswith("Next: ")]
        self.assertEqual(len(nexts), 1, out)
        return refs, nexts[0]

    def _follow(self, command: list, corpus, *, cache_full_after: bool) -> set:
        code, out, err = self.run_g(*command, corpus=corpus)
        self.assertEqual(code, 0, err)
        refs, next_line = self._refs_and_next(out)
        self.assertEqual(sorted(t for t, _r in refs), sorted(self.TITLES))
        if cache_full_after:
            # the bodies get cached (by a search) before the ref is used
            self.full_cache().write_text(self.FULL, encoding="utf-8")
        forms = set()
        for title, ref in refs:
            placeholder = "<page_ref>" if "<page_ref>" in next_line else "<slug>"
            if command[0] == "search-index":
                # search-index points at search; open the ref with sections
                line = f"{SCRIPT} sections {placeholder} " + next_line.split(" ", 3)[3]
            else:
                line = next_line
            with self.subTest(command=command, title=title, ref=ref):
                try:
                    argv = shlex.split(line.replace(placeholder, ref), comments=True)
                except ValueError as e:
                    self.fail(f"not a shell command: {e}")
                code, out, err = self.run_argv(argv)
                self.assertEqual(code, 0, err)
                self.assertRegex(out, rf'^Sections in \[\d+\] "{re.escape(title)}"', out)
            forms.add("int" if ref.isdigit() else "slug")
        return forms

    def test_slug_refs_reach_their_pages(self):
        self.write(self.INDEX)
        forms = self._follow(["fetch-index"], ("--cache-dir", self.tmp), cache_full_after=True)
        self.assertEqual(forms, {"slug"})
        self.full_cache().unlink()
        forms = self._follow(["search-index", "zzcommon"],
                             ("--cache-dir", self.tmp), cache_full_after=True)
        self.assertEqual(forms, {"slug"})

    def test_page_number_refs_reach_their_pages(self):
        self.write(self.INDEX, self.FULL)
        self.assertEqual(self._follow(["fetch-index"], ("--cache-dir", self.tmp), cache_full_after=False),
                         {"int"})
        full = os.path.join(self.tmp, "local full.txt")
        index = os.path.join(self.tmp, "local -index.txt")
        Path(full).write_text(self.FULL, encoding="utf-8")
        Path(index).write_text(self.INDEX, encoding="utf-8")
        corpus = ("--file", full, "--index-file", index)
        self.assertEqual(self._follow(["fetch-index"], corpus, cache_full_after=False), {"int"})
        self.assertEqual(self._follow(["search-index", "zzcommon"], corpus,
                                      cache_full_after=False), {"int"})
        self.assertEqual(self.calls, [])

    def test_slug_never_starts_with_a_dash(self):
        urls = ["https://example.com/en/-dash", "https://example.com/-x", "https://example.com/en/ok"]
        self.assertEqual(_common.build_unique_slugs(urls, avoid_dash=True),
                         {"https://example.com/en/-dash": "en/-dash",
                          "https://example.com/-x": "example.com/-x",
                          "https://example.com/en/ok": "ok"})
        # off by default: the dedicated parser's listing is unchanged
        self.assertEqual(_common.build_unique_slugs(urls),
                         {"https://example.com/en/-dash": "-dash", "https://example.com/-x": "-x",
                          "https://example.com/en/ok": "ok"})

    def test_url_listed_twice_is_shown_and_opened_by_its_full_url(self):
        """A URL the index lists twice has no unique slug: it is listed by
        its full URL, which the url page_ref form opens."""
        self.write("- [Hooks](https://example.com/en/hooks): zzcommon hooks\n"
                   "- [Hooks again](https://example.com/en/hooks): zzcommon hooks again\n"
                   "- [Skills](https://example.com/en/skills): zzcommon skills\n")
        code, out, err = self.run_g("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("[https://example.com/en/hooks] Hooks\n", out)
        next_line = next(ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: "))
        self.full_cache().write_text(HOOKS_SKILLS_FULL, encoding="utf-8")
        argv = shlex.split(next_line.replace("<slug>", "https://example.com/en/hooks"), comments=True)
        code, out, err = self.run_argv(argv)
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r'^Sections in \[0\] "Hooks"')


class QueryDecisionTableTest(_Case):
    """index_join "url": every combination of the queries of the entries
    sharing one URL in ``llms.txt``, of the pages sharing it in
    ``llms-full.txt``, and of the way the refs are listed (``llms-full.txt``
    not cached yet / cached / ``--file`` + ``--index-file``). Each ref a
    listing prints, put in its ``Next:`` line as the line reads and split by
    the shell rules, is run with ``sections``; and ``search`` is run. The
    invariants:

    * I1: an entry with a query opens its query's page, or a page with no
      query (``api?id=strings``, a place in the one ``api`` page), never
      another query's page. An entry with no query opens the page with no
      query when there is one, else the first page of that URL (as a URL
      page_ref resolves it)
    * I2: an entry with a query that no page has, and no page without a
      query, opens nothing (exit 1); an entry with a page opens it (exit 0)
    * I3: the three ways open the same page (or all fail) for each entry
    * I4: each ``search`` result shows the body of the page its entry opens

    Repeated pages (the same URL and query twice) are copies that read the
    same, as upstream repeats a page: which copy opens is not asserted.

    One split of I3 is left as it is (``SLUG_SPLIT``): an entry with no
    query, listed once, whose URL heads several pages, is listed by its
    slug before ``llms-full.txt`` is cached, and a slug that several pages
    end is ambiguous (exit 1, as with the dedicated parser: only the full
    URL form picks one of them); the page number listed once the bodies
    are cached opens the page. Pinned, so that a change to either side
    shows here."""

    URL = "https://example.com/docs/x"
    QUERIES = {None: "", "cli": "?surface=cli", "api": "?surface=api"}
    ENTRY_SHAPES = {"none": (None,), "cli": ("cli",), "none+cli": (None, "cli"),
                    "cli+api": ("cli", "api"), "none*2": (None, None), "cli*2": ("cli", "cli")}
    PAGE_SHAPES = {"no page": (), "none": (None,), "cli": ("cli",), "api": ("api",),
                   "none+cli": (None, "cli"), "cli+api": ("cli", "api"), "none*2": (None, None),
                   "cli*2": ("cli", "cli")}
    SLUG_SPLIT = {("none", "none+cli"), ("none", "cli+api"), ("none", "none*2"), ("none", "cli*2")}
    # pages that always join keep the join rate of search above its guard
    FILLERS = ("alpha", "beta")
    REF_RE = re.compile(r"^\[(.+?)\] (Entry \d|Filler \w+)(?: \(score: \d+\))?$", re.M)
    OPENED_RE = re.compile(r'^Sections in \[\d+\] "Page (\w+)"')

    @classmethod
    def _corpus(cls, entry_qs: tuple, page_qs: tuple) -> tuple[str, str]:
        index = "".join(f"- [Entry {k}]({cls.URL}{cls.QUERIES[q]}): zzq entry {k}\n"
                        for k, q in enumerate(entry_qs))
        index += "".join(f"- [Filler {f}](https://example.com/docs/{f}): zzq filler\n" for f in cls.FILLERS)
        full = "".join(f"# Page {q or 'none'}\nSource: {cls.URL}{cls.QUERIES[q]}\n\n## Body\n"
                       f"zzq marker-{q or 'none'}\n" for q in page_qs)
        full += "".join(f"# Page {f}\nSource: https://example.com/docs/{f}\n\n## Body\nzzq marker-{f}\n"
                        for f in cls.FILLERS)
        return index, full

    @staticmethod
    def _expected(q, page_qs: tuple):
        """The page (its query name) entry query *q* opens, or ``None``."""
        if q is not None and q in page_qs:
            return q
        if None in page_qs:
            return "none"
        if q is None and page_qs:
            return page_qs[0]
        return None

    def _listed(self, command: list, corpus: tuple, n_entries: int) -> tuple[dict, str]:
        code, out, err = self.run_g(*command, corpus=corpus)
        self.assertEqual(code, 0, err)
        refs = {t: r for r, t in self.REF_RE.findall(out) if t.startswith("Entry ")}
        self.assertEqual(sorted(refs), [f"Entry {k}" for k in range(n_entries)], out)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertEqual(len(nexts), 1, out)
        next_line = nexts[0]
        if command[0] == "search-index":  # it points at search; open the ref with sections
            next_line = f"{SCRIPT} sections <page_ref> " + next_line.split(" ", 3)[3]
        return refs, next_line

    def _open(self, next_line: str, ref: str):
        """The page (its query name) the ref opens, or ``None`` on exit 1."""
        placeholder = "<page_ref>" if "<page_ref>" in next_line else "<slug>"
        try:
            argv = shlex.split(next_line.replace(placeholder, ref), comments=True)
        except ValueError as e:
            self.fail(f"{ref}: not a shell command: {e}")
        code, out, err = self.run_argv(argv)
        if code == 1:
            self.assertEqual(out, "", err)
            return None
        self.assertEqual(code, 0, err)
        m = self.OPENED_RE.match(out)
        self.assertIsNotNone(m, out)
        return m.group(1)

    def _opened_by_way(self, index: str, full: str, n_entries: int) -> dict:
        """{way: {entry title: opened page or None}}, over fetch-index and
        search-index (which must agree)."""
        full_path, index_path = os.path.join(self.tmp, "f.txt"), os.path.join(self.tmp, "i.txt")
        Path(full_path).write_text(full, encoding="utf-8")
        Path(index_path).write_text(index, encoding="utf-8")
        got = {}
        for way in ("uncached", "cached", "file"):
            corpus = ("--file", full_path, "--index-file", index_path) if way == "file" \
                else ("--cache-dir", self.tmp)
            per_command = []
            for command in (["fetch-index"], ["search-index", "zzq"]):
                if self.full_cache().exists():
                    self.full_cache().unlink()
                self.write(index, full if way == "cached" else None)
                refs, next_line = self._listed(command, corpus, n_entries)
                if way == "uncached":
                    self.assertFalse(any(r.isdigit() for r in refs.values()), refs)
                    self.write(None, full)  # the bodies get cached before the ref is used
                per_command.append({t: self._open(next_line, r) for t, r in refs.items()})
            self.assertEqual(per_command[0], per_command[1], f"{way}: fetch-index vs search-index")
            got[way] = per_command[0]
        return got

    def _search_bodies(self, index: str, full: str) -> dict:
        """{entry title: the marker of the body search shows under it}."""
        self.write(index, full)
        code, out, err = self.run_g("search", "zzq", "--top-n", "10")
        self.assertEqual(code, 0, err)
        shown = {}
        for block in re.split(r"\n(?=\[\d+\] )", out):
            m = re.match(r"\[\d+\] (Entry \d) \(index_score", block)
            if m:
                markers = set(re.findall(r"marker-(\w+)", block))
                self.assertEqual(len(markers), 1, block)
                shown[m.group(1)] = markers.pop()
        return shown

    def test_every_cell_keeps_the_invariants(self):
        for e_name, entry_qs in self.ENTRY_SHAPES.items():
            for p_name, page_qs in self.PAGE_SHAPES.items():
                with self.subTest(entries=e_name, pages=p_name):
                    index, full = self._corpus(entry_qs, page_qs)
                    got = self._opened_by_way(index, full, len(entry_qs))
                    for k, q in enumerate(entry_qs):
                        title = f"Entry {k}"
                        want = self._expected(q, page_qs)
                        opened = {way: got[way][title] for way in got}
                        if (e_name, p_name) in self.SLUG_SPLIT:
                            self.assertIsNotNone(want)
                            self.assertEqual(opened, {"uncached": None, "cached": want, "file": want})
                            continue
                        # I3: the three ways agree
                        self.assertEqual(len(set(opened.values())), 1,
                                         f"{title} ({q or 'no query'}) opens {opened}")
                        # I1 / I2: the expected page, or nothing
                        self.assertEqual(opened["cached"], want, f"{title} ({q or 'no query'})")
                    # I4: search shows each entry with the body of its page
                    shown = self._search_bodies(index, full)
                    want_shown = {f"Entry {k}": self._expected(q, page_qs) for k, q in enumerate(entry_qs)}
                    self.assertEqual(shown, {t: p for t, p in want_shown.items() if p is not None})
        self.assertEqual(self.full_fetches(), 0)


class DefaultProfilesUnchangedTest(_Case):
    """Without rank_from, the full-text ranking and listing are as before
    (the pages, not the index entries)."""

    def test_pages_profile_lists_pages(self):
        self.write("- [Only in index](https://example.com/en/x): x\n", HOOKS_SKILLS_FULL, name="pages")
        code, out, err = self.run_g("fetch-index", source="pages")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Hooks", out)
        self.assertNotIn("Only in index", out)
        self.assertIn("(2 documents total)", out)


@unittest.skipUnless(os.environ.get("LLMS_DOCS_PARITY_CORPUS") == "1",
                     "local-only full-corpus gate (LLMS_DOCS_PARITY_CORPUS=1)")
class CorpusRefsResolveTest(unittest.TestCase):
    """On the full local Claude Code / Platform caches: every ref fetch-index
    prints (page numbers when llms-full.txt is cached, slugs when not) opens
    the page with the entry's URL, or, for an entry with no page, fails."""

    def test_every_index_ref_opens_its_page(self):
        from test_migration_parity import GENERIC_PROFILES
        cache = Path(os.environ.get("LLMS_DOCS_CACHE_DIR")
                     or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                                     "llms-docs"))
        checked = 0
        for name in ("claude-code", "claude-platform"):
            full, index = cache / f"{name}-llms-full.txt", cache / f"{name}-llms.txt"
            if not (full.exists() and index.exists()):
                continue
            profile = generic._validate_profile("sources.json", name, GENERIC_PROFILES[name])
            docs = generic.resolve_split(_common.load_lines(str(full)), profile)[2]
            entries = [e for e in _common.parse_llms_index(_common.load_lines(str(index)))
                       if not generic._is_index_file_url(e["url"])]
            joined = generic._entry_docs(entries, docs, profile)
            for doc_of in (joined, [None] * len(entries)):
                for e, ref, idx in zip(entries, generic._entry_refs(entries, doc_of), joined):
                    (arg,) = shlex.split(ref)
                    with self.subTest(name=name, ref=ref):
                        try:
                            with mock.patch("sys.stderr"):
                                got = generic._resolve_page_ref(docs, arg, None, profile["page_ref_forms"],
                                                                profile["page_url_strip_suffix"])
                        except SystemExit:
                            got = None
                        if idx is None:
                            self.assertIsNone(got, f"{e['url']} has no page but {ref} opened one")
                        else:
                            self.assertIsNotNone(got, ref)
                            self.assertEqual(_common.normalize_doc_url(docs[got]["url"]),
                                             _common.normalize_doc_url(e["url"]))
                    checked += 1
        if not checked:
            self.skipTest(f"no Claude Code / Platform caches in {cache}")
        print(f"\n  checked {checked} refs")


if __name__ == "__main__":
    unittest.main()
