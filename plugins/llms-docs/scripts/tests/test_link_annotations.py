"""In-body link annotations ``→ [doc_idx N]`` printed by ``content``.

``parse-claude-docs.py content`` appends `` → [doc_idx N]`` after a Markdown
link that points at another page of the corpus, so the reader can open it
with ``content N``. Links inside code fences and on table rows, links to the
page itself and links to URLs that are no page are left as they are;
``/docs/en/…`` and ``/en/…`` name the same page; ``--no-link-annotations``
turns the annotations off.

The cases below pin that behaviour on the old script first. They run on
both routes the page bodies come from (``--file`` and the cache), with the
network patched to fail.
"""

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _loader

CD_SCRIPT = "parse-claude-docs.py"

HOST = "https://code.claude.com"

# page 0 is the page under test; every line of its body that carries a link
# starts with a label naming the case.
PAGES = [
    ("Alpha", f"{HOST}/docs/en/alpha", [
        "Path [b](/docs/en/beta) end.",
        f"Abs [b]({HOST}/docs/en/beta) end.",
        f"Md [b]({HOST}/docs/en/beta.md) end.",
        "PathMd [b](/docs/en/beta.md) end.",
        "Frag [b](/docs/en/beta#part-two) end.",
        "Slash [b](/docs/en/beta/) end.",
        "Query [b](/docs/en/beta?tab=one) end.",
        "Alias [b](/en/beta) end.",
        "AliasRev [g](/docs/en/gamma) end.",
        "AliasPage [g](/en/gamma) end.",
        "Two [b](/docs/en/beta) and [d](/docs/en/delta) end.",
        "Unknown [x](/docs/en/nope) end.",
        "External [x](https://example.invalid/docs/en/beta) end.",
        "Relative [b](beta) end.",
        "Self [a](/docs/en/alpha) end.",
        f"SelfAbs [a]({HOST}/docs/en/alpha#top) end.",
        "",
        "```",
        "Fence [b](/docs/en/beta) end.",
        "```",
        "",
        "~~~md",
        "Tilde [b](/docs/en/beta) end.",
        "~~~",
        "",
        "| Table | [b](/docs/en/beta) |",
        "| Open [b](/docs/en/beta) end.",
        "|---|---|",
        "",
        "## Second part",
        "",
        "Section [d](/docs/en/delta) end.",
    ]),
    ("Beta", f"{HOST}/docs/en/beta", ["Beta body. Back to [a](/docs/en/alpha)."]),
    # a page published under the short ``/en/`` form
    ("Gamma", f"{HOST}/en/gamma", ["Gamma body."]),
    ("Delta", f"{HOST}/docs/en/delta", ["Delta body."]),
]

# label -> the line as ``content`` prints it (annotations on)
EXPECTED = {
    "Path": "Path [b](/docs/en/beta) → [doc_idx 1] end.",
    "Abs": f"Abs [b]({HOST}/docs/en/beta) → [doc_idx 1] end.",
    "Md": f"Md [b]({HOST}/docs/en/beta.md) → [doc_idx 1] end.",
    "PathMd": "PathMd [b](/docs/en/beta.md) → [doc_idx 1] end.",
    "Frag": "Frag [b](/docs/en/beta#part-two) → [doc_idx 1] end.",
    "Slash": "Slash [b](/docs/en/beta/) → [doc_idx 1] end.",
    "Query": "Query [b](/docs/en/beta?tab=one) → [doc_idx 1] end.",
    "Alias": "Alias [b](/en/beta) → [doc_idx 1] end.",
    "AliasRev": "AliasRev [g](/docs/en/gamma) → [doc_idx 2] end.",
    "AliasPage": "AliasPage [g](/en/gamma) → [doc_idx 2] end.",
    "Two": "Two [b](/docs/en/beta) → [doc_idx 1] and [d](/docs/en/delta) → [doc_idx 3] end.",
    "Unknown": "Unknown [x](/docs/en/nope) end.",
    "External": "External [x](https://example.invalid/docs/en/beta) end.",
    "Relative": "Relative [b](beta) end.",
    "Self": "Self [a](/docs/en/alpha) end.",
    "SelfAbs": f"SelfAbs [a]({HOST}/docs/en/alpha#top) end.",
    "Fence": "Fence [b](/docs/en/beta) end.",
    "Tilde": "Tilde [b](/docs/en/beta) end.",
    "| Table": "| Table | [b](/docs/en/beta) |",
    # a line that only starts with ``|`` is no table row
    "| Open": "| Open [b](/docs/en/beta) → [doc_idx 1] end.",
    "Section": "Section [d](/docs/en/delta) → [doc_idx 3] end.",
}

_ANNOTATION_RE = re.compile(r"\(([^\s)]+)\) → \[doc_idx (\d+)\]")


def full_text(pages=PAGES) -> str:
    out = []
    for title, url, body in pages:
        out += [f"# {title}", f"Source: {url}", "", *body, "", ""]
    return "\n".join(out)


def _no_network(*_a, **_kw):
    raise OSError("test: network disabled")


def labelled_lines(stdout: str) -> dict:
    """label -> the printed line, for the lines of page 0's body."""
    out = {}
    for line in stdout.split("\n"):
        for label in EXPECTED:
            if line.startswith(label + " "):
                out[label] = line
    return out


class _LinkAnnotationCases:
    """The behaviour pinned on every route. Subclasses say how to run
    ``content`` (``invoke(argv)`` -> (exit, stdout, stderr)) and with which
    route (``route``: ``"file"`` or ``"cache"``)."""

    route = "file"

    def invoke(self, argv):
        raise NotImplementedError

    def content(self, *argv):
        code, out, err = self.invoke(["content", *argv])
        self.assertEqual(code, 0, err)
        return out

    def test_each_link_shape_is_annotated_as_pinned(self):
        lines = labelled_lines(self.content("0"))
        for label, expected in EXPECTED.items():
            with self.subTest(label=label):
                self.assertEqual(lines.get(label), expected)

    def test_flag_turns_every_annotation_off(self):
        out = self.content("0", "--no-link-annotations")
        self.assertNotIn("doc_idx", out)
        self.assertIn("Path [b](/docs/en/beta) end.", out)
        self.assertIn("Two [b](/docs/en/beta) and [d](/docs/en/delta) end.", out)

    def test_a_narrowed_section_is_annotated_too(self):
        out = self.content("0", "Second part")
        self.assertIn(EXPECTED["Section"], out)
        self.assertNotIn("Path [b]", out)

    def test_annotation_round_trips_to_the_linked_page(self):
        """Every ``[doc_idx N]`` printed opens, as ``content N``, the page the
        link names (its ``# source:`` is the link's URL, ``/en/`` and
        ``/docs/en/`` taken as one)."""
        out = self.content("0")
        found = _ANNOTATION_RE.findall(out)
        self.assertGreaterEqual(len(found), 12)
        for link, n in found:
            with self.subTest(link=link, n=n):
                target = self.content(n)
                source = re.search(r"^# source: (\S+)$", target, re.M).group(1)
                self.assertEqual(_page_key(source), _page_key(link))
                self.assertNotEqual(n, "0")


def _page_key(url: str) -> str:
    """The path of a page URL or link with the forms that name one page
    folded together (host, ``#…``, ``?…``, a trailing ``/``, ``.md``,
    ``/docs`` before ``/en/``)."""
    u = re.sub(r"^https?://[^/]+", "", url).split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if u.endswith(".md"):
        u = u[:-3]
    if u.startswith("/docs/en/"):
        u = u[len("/docs"):]
    return u


class _LegacyRunner:
    """``parse-claude-docs.py`` on a temp copy of the fixture."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="llms-link-annot-")
        cls.module = _loader.load_script(CD_SCRIPT)
        cls.file = os.path.join(cls.tmp, "fixture-llms-full.txt")
        with open(cls.file, "w", encoding="utf-8") as f:
            f.write(full_text())
        cls.cache_dir = os.path.join(cls.tmp, "cache")
        os.makedirs(cls.cache_dir)
        shutil.copyfile(cls.file, os.path.join(cls.cache_dir, "claude-code-llms-full.txt"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def invoke(self, argv):
        cached = os.path.join(self.cache_dir, "claude-code-llms-full.txt")
        now = time.time()
        os.utime(cached, (now, now))
        where = (["--file", self.file] if self.route == "file" else [])
        with mock.patch("urllib.request.urlopen", side_effect=_no_network):
            return _loader.run_cli(self.module, [CD_SCRIPT, *argv, *where,
                                                 "--cache-dir", self.cache_dir])


class LegacyFileTest(_LegacyRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "file"


class LegacyCacheTest(_LegacyRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "cache"


# ---------------------------------------------------------------------------
# parse-llms-txt.py with ``link_annotations`` (the same cases, then its own)
# ---------------------------------------------------------------------------

G_SCRIPT = "parse-llms-txt.py"
generic = _loader.load_script(G_SCRIPT)

CODE_PROFILE = {
    "url": f"{HOST}/docs/llms-full.txt", "split": "h1", "page_url": "line:Source: ",
    "link_annotations": True, "link_path_aliases": [["/docs/en/", "/en/"]],
}


class _GenericRunner:
    """``parse-llms-txt.py`` on a temp copy of a fixture, with the profiles
    in ``SOURCES`` (``source`` picks one)."""

    source = "code"
    SOURCES = {"code": CODE_PROFILE, "off": {k: v for k, v in CODE_PROFILE.items()
                                             if not k.startswith("link_")}}
    TEXT = staticmethod(full_text)
    INDEX = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="llms-link-annot-")
        cls.file = os.path.join(cls.tmp, "fixture-llms-full.txt")
        with open(cls.file, "w", encoding="utf-8") as f:
            f.write(cls.TEXT())
        cls.cache_dir = os.path.join(cls.tmp, "cache")
        os.makedirs(cls.cache_dir)
        cls.sources_file = os.path.join(cls.tmp, "sources.json")
        with open(cls.sources_file, "w", encoding="utf-8") as f:
            json.dump({"sources": cls.SOURCES}, f)
        cls.cached = []
        cls.index_file = os.path.join(cls.tmp, "fixture-llms.txt")
        if cls.INDEX is not None:
            with open(cls.index_file, "w", encoding="utf-8") as f:
                f.write(cls.INDEX)
        cls.refused = None
        for name, raw in cls.SOURCES.items():
            try:
                profile = generic._validate_profile(cls.sources_file, name, raw)
            except SystemExit:  # reported by each test (a failure, not an error of the class)
                cls.refused = name
                continue
            target = generic._cache_path(cls.cache_dir, profile)
            shutil.copyfile(cls.file, target)
            cls.cached.append(target)
            if cls.INDEX is not None and profile["index_url"]:
                target = generic._index_cache_path(cls.cache_dir, profile)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(cls.INDEX)
                cls.cached.append(target)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def invoke(self, argv, source=None):
        if self.refused:
            self.fail(f"profile {self.refused} refused")
        now = time.time()
        for path in self.cached:
            os.utime(path, (now, now))
        where = (["--file", self.file] if self.route == "file" else [])
        argv = [G_SCRIPT, *argv, "--source", source or self.source,
                "--sources-file", self.sources_file, *where, "--cache-dir", self.cache_dir]
        with mock.patch("urllib.request.urlopen", side_effect=_no_network):
            try:
                return _loader.run_cli(generic, argv)
            except Exception as e:  # a crash is a failure, not an error of the run
                self.fail(f"{argv[1:3]} crashed: {e!r}")


class GenericFileTest(_GenericRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "file"

    def test_off_by_default_and_the_flag_is_accepted(self):
        for extra in ((), ("--no-link-annotations",)):
            with self.subTest(extra=extra):
                code, out, err = self.invoke(["content", "0", *extra], source="off")
                self.assertEqual(code, 0, err)
                self.assertNotIn("doc_idx", out)
                self.assertIn("Path [b](/docs/en/beta) end.", out)


class GenericCacheTest(_GenericRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "cache"


# The matrix: every link target (pages that differ only by their query, one
# URL heading two pages, a page under the short ``/en/`` form, queries and
# URLs no page has) x every way of writing it x every place it can sit, on
# both routes and with both rankings (pages / llms.txt entries).
M_HOST = "https://docs.example.test"
M_PAGES = [
    # (title, URL); page 0 carries the links
    ("Home", f"{M_HOST}/docs/en/home"),
    ("Beta", f"{M_HOST}/docs/en/beta"),
    ("Tool for the terminal", f"{M_HOST}/docs/en/tool?surface=cli"),
    ("Tool over the API", f"{M_HOST}/docs/en/tool?surface=api"),
    ("Twin", f"{M_HOST}/docs/en/twin"),
    ("Twin", f"{M_HOST}/docs/en/twin"),
    ("Short", f"{M_HOST}/en/short"),
    ("Mix", f"{M_HOST}/docs/en/mix"),
    ("Mix two", f"{M_HOST}/docs/en/mix?v=2"),
    ("Dash", f"{M_HOST}/docs/en/-flag"),
    # a page under each form of one path: a link names its own form first
    ("Both long", f"{M_HOST}/docs/en/both"),
    ("Both short", f"{M_HOST}/en/both"),
]
# target path (+ query) -> the page it names, written out by hand (None: no
# annotation). The page an llms.txt entry with that URL is joined to.
M_TARGETS = {
    "/docs/en/home": None,                 # the page itself
    "/docs/en/beta": 1,
    "/en/beta": 1,                         # alias
    "/docs/en/tool?surface=cli": 2,
    "/docs/en/tool?surface=api": 3,
    "/docs/en/tool?surface=web": None,     # a query no page has
    "/docs/en/tool": 2,                    # no query: the first page
    "/docs/en/twin": 5,                    # one URL, two pages: the last (as joined in listings)
    "/docs/en/twin?tab=x": 5,              # a query neither has: the last of them
    "/en/short": 6,
    "/docs/en/short": 6,                   # alias, the other way
    "/docs/en/mix": 7,
    "/docs/en/mix?v=2": 8,
    "/docs/en/mix?v=9": 7,                 # a query no page has, a page with none
    "/docs/en/-flag": 9,
    "/docs/en/both": 10,
    "/en/both": 11,
    "/docs/en/nope": None,
}
# Targets that name a page only through ``link_path_aliases``: an alias
# applies to root-relative links, as in ``parse-claude-docs.py``; an
# absolute URL names a page by itself.
M_ALIAS_ONLY = {"/en/beta", "/docs/en/short"}


def _forms(target: str) -> dict:
    """form name -> the link as written."""
    path, sep, query = target.partition("?")
    q = sep + query
    return {
        "path": target,
        "abs": M_HOST + target,
        "abs-md": f"{M_HOST}{path}.md{q}",
        "path-frag": f"{target}#part-two",
        "slash": f"{path}/{q}",
    }


_PLACES = ("body", "inline", "fence", "table")


def _matrix():
    """(label, place, link, expected) for every cell, and page 0's body."""
    cells, body, fenced, table = [], [], [], []
    k = 0
    for target, expected in M_TARGETS.items():
        for form, link in _forms(target).items():
            for place in _PLACES:
                label = f"L{k}"
                k += 1
                want = None if form.startswith("abs") and target in M_ALIAS_ONLY else expected
                cells.append((label, place, link, want, f"{target} {form}"))
                text = f"{label} [x]({link}) end."
                if place == "body":
                    body.append(text)
                elif place == "inline":
                    body.append(f"{label} `[x]({link})` end.")
                elif place == "fence":
                    fenced.append(text)
                else:
                    table.append(f"| {label} | [x]({link}) |")
    lines = [*body, "", "```", *fenced, "```", "", "| Case | Link |", "|---|---|", *table]
    return cells, lines


M_CELLS, M_BODY = _matrix()


def matrix_text() -> str:
    out = []
    for i, (title, url) in enumerate(M_PAGES):
        out += [f"# {title}", f"Source: {url}", "", *(M_BODY if i == 0 else [f"{title} body."]), "", ""]
    return "\n".join(out)


def matrix_index() -> str:
    return "# Docs\n\n" + "".join(f"- [{t}]({u}): about {t}\n" for t, u in M_PAGES)


M_PROFILE = {"url": f"{M_HOST}/llms-full.txt", "split": "h1", "page_url": "line:Source: ",
             "link_annotations": True, "link_path_aliases": [["/docs/en/", "/en/"]]}
M_INDEX_FIRST = {**M_PROFILE, "index_url": f"{M_HOST}/llms.txt", "rank_from": "index",
                 "index_join": "url", "page_ref_forms": ["int", "url", "slug"]}


class _MatrixCases:
    SOURCES = {"pages": M_PROFILE, "index-first": M_INDEX_FIRST,
               "url-refs": {**M_PROFILE, "page_ref_forms": ["int", "url", "slug"]}}
    TEXT = staticmethod(matrix_text)
    INDEX = matrix_index()

    def test_every_cell(self):
        for source in ("pages", "index-first"):
            code, out, err = self.invoke(["content", "0", "--max-chars", "0"], source=source)
            self.assertEqual(code, 0, err)
            printed = {}
            for line in out.split("\n"):
                m = re.match(r"^(?:\| )?(L\d+) ", line)
                if m:
                    printed[m.group(1)] = line
            self.assertEqual(len(printed), len(M_CELLS))
            for label, place, link, expected, what in M_CELLS:
                with self.subTest(source=source, cell=what, place=place):
                    line = printed[label]
                    found = re.findall(r"\(([^\s)]+)\) → \[doc_idx (\d+)\]", line)
                    if place in ("fence", "table") or expected is None:
                        self.assertEqual(found, [], line)
                    else:
                        self.assertEqual(found, [(link, str(expected))], line)

    def test_each_number_opens_the_page_the_url_names(self):
        """``content N`` and ``content <URL>`` (the link as a full URL, the
        short form written out) print the same page, for every N printed (the
        page number in hints aside: of two copies under one URL, the URL
        opens the first)."""
        code, out, err = self.invoke(["content", "0", "--max-chars", "0"], source="url-refs")
        self.assertEqual(code, 0, err)
        pairs = set(re.findall(r"\(([^\s)]+)\) → \[doc_idx (\d+)\]", out))
        self.assertGreaterEqual(len({n for _l, n in pairs}), 7)
        for link, n in sorted(pairs):
            with self.subTest(link=link, n=n):
                url = link if link.startswith("http") else M_HOST + link
                if generic._ref_url_key(url, None) not in {generic._ref_url_key(u, None) for _t, u in M_PAGES}:
                    # named through the alias: written out as the page's own form
                    url = (url.replace("/docs/en/", "/en/", 1) if "/docs/en/" in url
                           else url.replace("/en/", "/docs/en/", 1))
                by_n = self.invoke(["content", n], source="url-refs")
                by_url = self.invoke(["content", url], source="url-refs")
                self.assertEqual(by_n[0], 0, by_n[2])
                self.assertIn(f"# source: {M_PAGES[int(n)][1]}\n", by_n[1])
                self.assertEqual(by_n[0], by_url[0])
                self.assertEqual(re.sub(r"content \d+ ", "content <n> ", by_n[1]),
                                 re.sub(r"content \d+ ", "content <n> ", by_url[1]))

    def test_numbers_match_the_index_listing(self):
        """With ``rank_from: "index"`` the annotation is the page number the
        llms.txt listing prints for the same page."""
        extra = ["--index-file", self.index_file] if self.route == "file" else []
        code, out, err = self.invoke(["fetch-index", *extra], source="index-first")
        self.assertEqual(code, 0, err)
        listed = dict(re.findall(r"^\s*\[(\d+)\] (.+?)\s*$", out, re.M))
        by_title = {}
        for n, title in listed.items():
            by_title.setdefault(title, n)
        self.assertIn("[5] Twin", out)  # two entries, one URL: both joined to the last copy
        code, out, err = self.invoke(["content", "0", "--max-chars", "0"], source="index-first")
        self.assertEqual(code, 0, err)
        checked = 0
        for _link, n in set(re.findall(r"\(([^\s)]+)\) → \[doc_idx (\d+)\]", out)):
            with self.subTest(n=n):
                self.assertEqual(by_title.get(M_PAGES[int(n)][0]), n)
                checked += 1
        self.assertGreaterEqual(checked, 6)


class MatrixFileTest(_MatrixCases, _GenericRunner, unittest.TestCase):
    route = "file"


class MatrixCacheTest(_MatrixCases, _GenericRunner, unittest.TestCase):
    route = "cache"


class SelfLinkByUrlTest(_GenericRunner, unittest.TestCase):
    """A link to the URL of the page itself is not annotated, also on the
    second of two pages one URL heads (it would name the first copy), and a
    page with a query is not the page without one."""

    route = "file"
    SOURCES = {"m": M_PROFILE}
    INDEX = None

    @staticmethod
    def TEXT():
        pages = [("Twin", f"{M_HOST}/docs/en/twin", ["A [x](/docs/en/twin) end."]),
                 ("Twin", f"{M_HOST}/docs/en/twin", ["B [x](/docs/en/twin) end."]),
                 ("Tool", f"{M_HOST}/docs/en/tool?surface=cli", ["C [x](/docs/en/tool?surface=api) end."]),
                 ("Tool", f"{M_HOST}/docs/en/tool?surface=api", ["D [x](/docs/en/tool) end.",
                                                                  "E [x](/docs/en/tool?surface=api) end."])]
        return full_text(pages)

    def test_self_links(self):
        expect = {"0": ["A [x](/docs/en/twin) end."],
                  "1": ["B [x](/docs/en/twin) end."],
                  "2": ["C [x](/docs/en/tool?surface=api) → [doc_idx 3] end."],
                  "3": ["D [x](/docs/en/tool) → [doc_idx 2] end.",
                        "E [x](/docs/en/tool?surface=api) end."]}
        for n, lines in expect.items():
            with self.subTest(page=n):
                code, out, err = self.invoke(["content", n], source="m")
                self.assertEqual(code, 0, err)
                for line in lines:
                    self.assertIn(line, out.split("\n"))


class PageWithoutUrlTest(_GenericRunner, unittest.TestCase):
    """A page with no URL of its own reads root-relative links on the host of
    the profile's ``url``; absolute links resolve as anywhere."""

    route = "file"
    SOURCES = {"m": {**M_PROFILE, "page_url": "link:Source"}}
    INDEX = None

    @staticmethod
    def TEXT():
        return ("# Loose\n\nA [x](/docs/en/beta) and [y](https://other.example.test/docs/en/beta) end.\n\n"
                f"# Beta\n\n[Source]({M_HOST}/docs/en/beta)\n\nBeta body.\n")

    def test_relative_link_uses_the_profile_host(self):
        code, out, err = self.invoke(["content", "0"], source="m")
        self.assertEqual(code, 0, err)
        self.assertIn("A [x](/docs/en/beta) → [doc_idx 1] and "
                      "[y](https://other.example.test/docs/en/beta) end.", out)


class StripSuffixTest(_GenericRunner, unittest.TestCase):
    """Pages fetched as ``<page>.md.txt`` (``page_url_strip_suffix``): a link
    to the page, with or without the suffix, names it."""

    route = "file"
    SOURCES = {"m": {**M_PROFILE, "page_url_strip_suffix": ".md.txt",
                     "page_ref_forms": ["int", "url", "slug"]}}
    INDEX = None

    @staticmethod
    def TEXT():
        return full_text([
            ("Home", f"{M_HOST}/docs/home.md.txt",
             ["A [x](/docs/beta) and [y](/docs/beta.md.txt) and [z](/docs/home) end.",
              "B [x](/docs/tool?s=api) and [y](/docs/tool?s=web) end."]),
            ("Beta", f"{M_HOST}/docs/beta.md.txt", ["Beta body."]),
            ("Tool cli", f"{M_HOST}/docs/tool.md.txt?s=cli", ["Cli body."]),
            ("Tool api", f"{M_HOST}/docs/tool.md.txt?s=api", ["Api body."]),
        ])

    def test_suffix_is_ignored_on_both_sides(self):
        code, out, err = self.invoke(["content", "0"], source="m")
        self.assertEqual(code, 0, err)
        self.assertIn("A [x](/docs/beta) → [doc_idx 1] and [y](/docs/beta.md.txt) → [doc_idx 1] "
                      "and [z](/docs/home) end.", out)
        self.assertIn("B [x](/docs/tool?s=api) → [doc_idx 3] and [y](/docs/tool?s=web) end.", out)


class OtherHostTest(_GenericRunner, unittest.TestCase):
    """A root-relative link is read on the host of the page it sits on, not
    on the host the profile's file is fetched from."""

    route = "file"
    SOURCES = {"m": {**M_PROFILE, "url": "https://files.example.test/llms-full.txt"}}
    INDEX = None

    @staticmethod
    def TEXT():
        return full_text([("Home", f"{M_HOST}/docs/en/home", ["A [x](/docs/en/beta) end."]),
                          ("Beta", f"{M_HOST}/docs/en/beta", ["Beta body."]),
                          ("Beta elsewhere", "https://files.example.test/docs/en/beta", ["Other."])])

    def test_page_host(self):
        code, out, err = self.invoke(["content", "0"], source="m")
        self.assertEqual(code, 0, err)
        self.assertIn("A [x](/docs/en/beta) → [doc_idx 1] end.", out)


class ProfileKeysTest(unittest.TestCase):
    def _err(self, raw: dict) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sources": {"x": raw}}, f)
            try:
                code, _out, err = _loader.run_cli(generic, [G_SCRIPT, "sources", "--sources-file", path])
            except Exception as e:  # a crash is a failure, not an error of the run
                self.fail(f"{raw!r} crashed: {e!r}")
        self.assertEqual(code, 1, err)
        return err

    def _ok(self, raw: dict) -> dict:
        try:
            return generic._validate_profile("s.json", "x", raw)
        except SystemExit:
            self.fail(f"{raw!r} refused")

    def test_defaults(self):
        p = self._ok({"url": M_HOST + "/llms-full.txt", "split": "h1"})
        self.assertEqual((p["link_annotations"], p["link_path_aliases"]), (False, ()))
        p = self._ok(M_PROFILE)
        self.assertEqual((p["link_annotations"], p["link_path_aliases"]),
                         (True, (("/docs/en/", "/en/"),)))

    def test_bad_values_are_refused(self):
        base = {"url": M_HOST + "/llms-full.txt", "split": "h1"}
        cases = [
            ("link_annotations must be true or false", {**M_PROFILE, "link_annotations": "yes"}),
            ("link_annotations needs page_url or index_url", {**base, "link_annotations": True}),
            ("link_path_aliases is only valid with link_annotations true",
             {**base, "page_url": "line:Source: ", "link_path_aliases": [["/a/", "/b/"]]}),
        ]
        for bad in (5, True, "/a/", [["/a/"]], [["/a/", "/a/"]], [["a/", "/b/"]], [["/a", "/b/"]],
                    [["/a/", 1]], [["/a/", "/b/", "/c/"]], {"/a/": "/b/"}):
            cases.append(("link_path_aliases must be a list of pairs of path prefixes",
                          {**M_PROFILE, "link_path_aliases": bad}))
        for want, raw in cases:
            with self.subTest(raw=raw):
                self.assertIn(want, self._err(raw))

    def test_index_url_alone_is_enough(self):
        p = self._ok({"url": M_HOST + "/llms-full.txt", "split": "h1",
                      "index_url": M_HOST + "/llms.txt", "link_annotations": True})
        self.assertTrue(p["link_annotations"])


if __name__ == "__main__":
    unittest.main()
