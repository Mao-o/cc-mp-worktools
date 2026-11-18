"""``page_ref_forms`` and ``page_url_strip_suffix`` of parse-llms-txt.py.

A page reference (``sections`` / ``content`` / ``search-content --page-ref``)
is resolved by the forms the profile lists, in that order: ``int``, ``url``
(a full URL, compared normalised), ``slug`` (the end of the URL, optionally
with a ``/<lang>/`` exact match), ``title`` and ``url_substring``. The default
is the order before the key existed (``int``, ``title``, ``url_substring``).
``page_url_strip_suffix`` (``.md.txt``) is removed from the page URLs and from
the reference before the URL and slug forms compare them.

No network: the pages are passed with ``--file``.
"""

import contextlib
import io
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

generic = _loader.load_script("parse-llms-txt.py")

URL = "https://example.com/llms-full.txt"
H1 = {"url": URL, "split": "h1", "page_url": "line:Source: "}
SCRIPT = "parse-llms-txt.py"

# 0 hooks (en), 1 agent-sdk/hooks (en), 2 hooks (ja), 3 plugins/install, 4 the same
# URL again (the upstream files repeat a page), 5 title "Install" whose URL ends
# "/setup", 6 title "Setup" whose URL ends "/install", 7 a page behind a ``.md.txt`` URL
PAGES = [
    ("Hooks", "https://example.com/docs/en/hooks"),
    ("Agent SDK Hooks", "https://example.com/docs/en/agent-sdk/hooks"),
    ("Hooks ja", "https://example.com/docs/ja/hooks"),
    ("Plugin installation", "https://example.com/docs/en/plugins/install"),
    ("Plugin installation again", "https://example.com/docs/en/plugins/install"),
    ("Install", "https://example.com/docs/en/setup"),
    ("Setup", "https://example.com/docs/en/install"),
    ("Auth overview", "https://example.com/docs/auth/overview.md.txt"),
]


def corpus_text(pages=PAGES) -> str:
    return "".join(f"# {t}\nSource: {u}\n\nBody of {t}.\n\n## Part of {t}\n\ntext\n\n" for t, u in pages)


def docs_of(pages=PAGES) -> list:
    return [{"title": t, "url": u, "body_lines": [], "min_level": 1} for t, u in pages]


def resolve(ref, forms=("int", "url", "slug"), suffix=None, pages=PAGES):
    """``(index, stderr)`` of resolving *ref*, or ``(None, stderr)`` when it exits."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return generic._resolve_page_ref(docs_of(pages), ref, None, tuple(forms), suffix), err.getvalue()
        except SystemExit as e:
            assert e.code == 1, e.code
            return None, err.getvalue()
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            raise AssertionError(f"resolving {ref!r} crashed: {e!r}") from e


def validate(extra: dict, base: dict = H1) -> dict:
    """The validated profile; a rejection fails the test (``die`` exits)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return generic._validate_profile("sources.json", "site", {**base, **extra})
        except SystemExit:
            pass
    raise AssertionError(f"profile rejected: {err.getvalue()}")


def rejection(extra: dict, base: dict = H1) -> str:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            generic._validate_profile("sources.json", "site", {**base, **extra})
            return ""
        except SystemExit:
            pass
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            raise AssertionError(f"validating {extra!r} crashed: {e!r}") from e
    return err.getvalue()


class ProfileKeysTest(unittest.TestCase):
    def test_default_is_the_order_before_the_key_existed(self):
        p = validate({})
        self.assertEqual(p["page_ref_forms"], ("int", "title", "url_substring"))
        self.assertIsNone(p["page_url_strip_suffix"])

    def test_forms_and_suffix_are_read(self):
        p = validate({"page_ref_forms": ["int", "url", "slug", "title"], "page_url_strip_suffix": ".md.txt"})
        self.assertEqual(p["page_ref_forms"], ("int", "url", "slug", "title"))
        self.assertEqual(p["page_url_strip_suffix"], ".md.txt")

    def test_bad_forms_are_rejected(self):
        for label, value in (("not a list", "int"), ("empty", []), ("unknown form", ["int", "path"]),
                             ("not a string", ["int", 3]), ("duplicate", ["int", "slug", "slug"]),
                             ("object", {"int": True}), ("case", ["int", "URL"])):
            with self.subTest(label):
                self.assertIn("page_ref_forms", rejection({"page_ref_forms": value}))

    def test_int_must_come_first(self):
        # the listings and the retry lines print indexes as page refs
        self.assertIn('must start with "int"', rejection({"page_ref_forms": ["title", "int"]}))
        self.assertIn('must start with "int"', rejection({"page_ref_forms": ["slug"]}))
        self.assertEqual(rejection({"page_ref_forms": ["int"]}), "")

    def test_url_and_slug_need_page_urls(self):
        no_urls = {"url": URL, "split": "h1"}
        for form in ("url", "slug"):
            with self.subTest(form):
                self.assertIn("needs page_url or index_url",
                              rejection({"page_ref_forms": ["int", form]}, base=no_urls))
        # an index_url gives the pages URLs by title; title / url_substring never need them
        self.assertEqual(rejection({"page_ref_forms": ["int", "slug"], "index_url": "https://example.com/llms.txt"},
                                   base=no_urls), "")
        self.assertEqual(rejection({"page_ref_forms": ["int", "title", "url_substring"]}, base=no_urls), "")

    def test_bad_suffix_is_rejected(self):
        for label, value in (("number", 3), ("empty", ""), ("space", ".md txt"), ("slash", "/x.md"),
                             ("query", "?x"), ("fragment", "#x"), ("list", [".md.txt"])):
            with self.subTest(label):
                self.assertIn("page_url_strip_suffix",
                              rejection({"page_ref_forms": ["int", "url"], "page_url_strip_suffix": value}))

    def test_suffix_without_url_or_slug_form_is_rejected(self):
        self.assertIn("only used by", rejection({"page_url_strip_suffix": ".md.txt"}))
        self.assertIn("only used by", rejection({"page_ref_forms": ["int", "title"],
                                                  "page_url_strip_suffix": ".md.txt"}))
        self.assertEqual(rejection({"page_ref_forms": ["int", "slug"], "page_url_strip_suffix": ".md.txt"}), "")

    def test_keys_are_shared_by_the_candidates_of_split_auto(self):
        auto = {"url": URL, "split": "auto", "page_ref_forms": ["int", "url", "slug"],
                "page_url_strip_suffix": ".md.txt",
                "split_candidates": [{"split": "h1", "page_url": "line:Source: "},
                                     {"split": "frontmatter", "page_url": "frontmatter:url"}]}
        p = validate(auto, base={})
        self.assertEqual(p["page_ref_forms"], ("int", "url", "slug"))
        for cand in p["split_candidates"]:
            self.assertEqual(cand["page_ref_forms"], ("int", "url", "slug"))
            self.assertEqual(cand["page_url_strip_suffix"], ".md.txt")
        moved = {**auto, "split_candidates": [{**auto["split_candidates"][0], "page_ref_forms": ["int"]},
                                              auto["split_candidates"][1]]}
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            generic._validate_profile("sources.json", "site", moved)
        self.assertIn("page_ref_forms", err.getvalue())


class DefaultFormsTest(unittest.TestCase):
    """Without the key, a reference resolves as before it existed."""

    def test_int_title_url_substring(self):
        d = ("int", "title", "url_substring")
        self.assertEqual(resolve("3", d)[0], 3)
        self.assertEqual(resolve("plugin installation again", d)[0], 4)
        self.assertEqual(resolve("PLUGIN INSTALLATION AGAIN", d)[0], 4)
        self.assertEqual(resolve("AUTH/Overview", d)[0], 7)
        self.assertEqual(resolve("auth/overview", d)[0], 7)
        # a URL is a URL substring; a slug is not a form (title then URL substring)
        self.assertEqual(resolve("https://example.com/docs/en/agent-sdk/hooks", d)[0], 1)
        self.assertEqual(resolve("agent-sdk/hooks", d)[0], 1)
        # without the url form, a URL that is only the start of a page's URL
        # still reaches that page by URL substring (as before the key existed)
        self.assertEqual(resolve("https://example.com/docs/en/agent", d)[0], 1)
        self.assertEqual(resolve("https://example.com/docs/en/agent", ("int", "slug", "title", "url_substring"))[0], 1)

    def test_title_substring_wins_over_url_substring_and_lists_ambiguity(self):
        d = ("int", "title", "url_substring")
        idx, err = resolve("hooks", d)
        self.assertIsNone(idx)
        self.assertIn("Ambiguous title substring 'hooks'", err)
        idx, err = resolve("docs/en/plugins", d)  # no title has it: the URL substring takes over
        self.assertIsNone(idx)
        self.assertIn("Ambiguous url substring", err)

    def test_failure_message_is_the_old_one(self):
        d = ("int", "title", "url_substring")
        self.assertEqual(resolve("nothing like it", d), (None, "Error: No document found for: nothing like it\n"))
        self.assertEqual(resolve("https://example.com/none", d),
                         (None, "Error: No document found for: https://example.com/none\n"))

    def test_out_of_range_and_empty(self):
        self.assertIn("out of range (0-7)", resolve("8", ("int", "title"))[1])
        self.assertIn("out of range", resolve("-1", ("int", "title"))[1])
        self.assertIn("page_ref required", resolve("", ("int", "title"))[1])

    def test_empty_ref_names_the_forms_of_the_profile(self):
        # the default reads as the message before page_ref_forms existed
        self.assertEqual(resolve("", ("int", "title", "url_substring")),
                         (None, "Error: page_ref required: integer index, title substring or URL substring\n"))
        self.assertEqual(resolve("", ("int", "url", "slug")),
                         (None, "Error: page_ref required: integer index, full URL or URL slug\n"))
        self.assertEqual(resolve("", ("int",)), (None, "Error: page_ref required: integer index\n"))

    def test_int_is_not_tried_when_the_form_is_not_listed(self):
        # such a profile cannot be written (int must come first), but the
        # resolver itself goes by the list it is given
        self.assertEqual(resolve("3", ("title",), pages=[("Chapter 3", "https://x.example/a")] * 1)[0], 0)


class UrlFormTest(unittest.TestCase):
    F = ("int", "url")

    def test_exact_url(self):
        self.assertEqual(resolve("https://example.com/docs/en/hooks", self.F)[0], 0)
        self.assertEqual(resolve("https://example.com/docs/ja/hooks", self.F)[0], 2)

    def test_both_sides_are_normalised(self):
        for ref in ("https://example.com/docs/en/hooks/", "https://example.com/docs/en/hooks#events",
                    "https://example.com/docs/en/hooks?x=1", "https://example.com/docs/en/hooks.md",
                    "https://example.com/docs/en/hooks/#a", "https://example.com/docs/en/hooks.md#a"):
            with self.subTest(ref=ref):
                self.assertEqual(resolve(ref, self.F)[0], 0)
        # the page side too: a URL listed with .md / a trailing slash
        pages = [("A", "https://example.com/a.md"), ("B", "https://example.com/b/")]
        self.assertEqual(resolve("https://example.com/a", self.F, pages=pages)[0], 0)
        self.assertEqual(resolve("https://example.com/b", self.F, pages=pages)[0], 1)

    def test_a_different_url_is_not_a_prefix_match(self):
        idx, err = resolve("https://example.com/docs/en/hook", self.F)
        self.assertIsNone(idx)
        self.assertEqual(err, "Error: No page found for URL: https://example.com/docs/en/hook\n")
        self.assertIsNone(resolve("https://example.com/docs/en/hooks/extra", self.F)[0])

    def test_the_first_page_of_a_repeated_url_wins(self):
        self.assertEqual(resolve("https://example.com/docs/en/plugins/install", self.F)[0], 3)

    def test_no_match_ends_the_search(self):
        # a URL names one page: one that matches no page is not handed to
        # url_substring / title, which would take it for a longer page
        for forms in (("int", "url", "url_substring"), ("int", "url", "slug", "title", "url_substring"),
                      ("int", "url", "title")):
            with self.subTest(forms=forms):
                self.assertEqual(resolve("https://example.com/docs/en/agent", forms),
                                 (None, "Error: No page found for URL: https://example.com/docs/en/agent\n"))

    def test_a_missing_full_url_does_not_reach_a_longer_page(self):
        idx, err = resolve("https://example.com/docs/en/agent", ("int", "url", "slug", "title", "url_substring"))
        self.assertIsNone(idx)
        # "…/en/hook" is the start of "…/en/hooks" (and of its title "Hooks")
        pages = [("Hooks guide", "https://example.com/docs/en/hooks-guide"), ("Other", "https://example.com/o")]
        idx, err = resolve("https://example.com/docs/en/hooks-guid", ("int", "url", "slug", "title", "url_substring"),
                           pages=pages)
        self.assertEqual((idx, err), (None, "Error: No page found for URL: https://example.com/docs/en/hooks-guid\n"))

    def test_a_query_picks_the_page_with_that_query(self):
        pages = [("CLI", "https://example.com/docs/x?surface=cli"), ("API", "https://example.com/docs/x?surface=api"),
                 ("Other", "https://example.com/docs/y")]
        for ref, want in (("https://example.com/docs/x?surface=api", 1), ("https://example.com/docs/x?surface=cli", 0),
                          ("https://example.com/docs/x/?surface=api", 1),
                          ("https://example.com/docs/x?surface=api#part", 1),
                          ("https://example.com/docs/x.md?surface=api", 1),
                          # a reference with no query: the first page with the URL, as before
                          ("https://example.com/docs/x", 0)):
            with self.subTest(ref=ref):
                self.assertEqual(resolve(ref, self.F, pages=pages)[0], want)

    def test_a_query_no_page_has_names_no_page(self):
        """``?surface=web`` with cli and api pages (or with one cli page)
        opens neither: it is another page that is not there."""
        two = [("CLI", "https://example.com/docs/x?surface=cli"), ("API", "https://example.com/docs/x?surface=api")]
        for pages in (two, two[:1]):
            for ref in ("https://example.com/docs/x?surface=web", "https://example.com/docs/x.md?surface=web#a"):
                with self.subTest(pages=len(pages), ref=ref):
                    self.assertEqual(resolve(ref, self.F, pages=pages),
                                     (None, f"Error: No page found for URL: {ref}\n"))

    def test_a_query_falls_back_to_the_page_with_no_query(self):
        """``api?id=strings`` is a place in the one ``api`` page: the first
        page of the URL with no query, ahead of pages with another query."""
        pages = [("CLI", "https://example.com/api?surface=cli"), ("API", "https://example.com/api"),
                 ("API again", "https://example.com/api/"), ("Other", "https://example.com/y")]
        for ref in ("https://example.com/api?id=strings", "https://example.com/api.md?id=strings#x"):
            with self.subTest(ref=ref):
                self.assertEqual(resolve(ref, self.F, pages=pages)[0], 1)
        self.assertEqual(resolve("https://example.com/api?surface=cli", self.F, pages=pages)[0], 0)
        self.assertEqual(resolve("https://example.com/api", self.F, pages=pages)[0], 1)
        self.assertEqual(resolve("https://example.com/api?id=a", self.F, pages=pages[1:2])[0], 0)

    def test_a_query_is_compared_with_the_suffix_off_both_sides(self):
        pages = [("CLI", "https://example.com/docs/x.md.txt?surface=cli"),
                 ("API", "https://example.com/docs/x.md.txt?surface=api")]
        for ref in ("https://example.com/docs/x?surface=api", "https://example.com/docs/x.md.txt?surface=api"):
            with self.subTest(ref=ref):
                self.assertEqual(resolve(ref, self.F, ".md.txt", pages=pages)[0], 1)

    def test_the_form_is_for_http_urls_only(self):
        # a profile without url_base lists relative URLs; the url form does not read a path as one…
        pages = [("A", "/docs/a"), ("B", "/docs/b")]
        self.assertIsNone(resolve("/docs/a", self.F, pages=pages)[0])
        # …the slug form reads a pasted path
        self.assertEqual(resolve("/docs/b", ("int", "slug"), pages=pages)[0], 1)

    def test_a_reference_that_is_not_a_url_skips_the_form(self):
        self.assertEqual(resolve("hooks", ("int", "url", "title"))[0], None)  # ambiguous title
        self.assertEqual(resolve("auth overview", ("int", "url", "title"))[0], 7)

    def test_pages_without_urls_never_match(self):
        pages = [("A", ""), ("B", "https://example.com/b")]
        self.assertEqual(resolve("https://example.com/b", self.F, pages=pages)[0], 1)
        self.assertIsNone(resolve("https://", self.F, pages=pages)[0])


class SlugFormTest(unittest.TestCase):
    F = ("int", "slug")

    def test_last_segment_and_longer_tails(self):
        self.assertEqual(resolve("overview", self.F, ".md.txt")[0], 7)
        self.assertEqual(resolve("auth/overview", self.F, ".md.txt")[0], 7)
        self.assertEqual(resolve("agent-sdk/hooks", self.F)[0], 1)
        idx, err = resolve("plugins/install", self.F)  # the repeated URL (see below)
        self.assertIsNone(idx)
        self.assertIn("Ambiguous slug", err)

    def test_the_slug_ends_at_a_segment_boundary(self):
        # "ooks" ends "hooks" but is not a segment
        idx, err = resolve("ooks", self.F)
        self.assertIsNone(idx)
        self.assertEqual(err, "Error: No page found for slug: ooks\n")
        self.assertIsNone(resolve("docs/en/hook", self.F)[0])

    def test_slashes_around_the_slug_are_ignored(self):
        self.assertEqual(resolve("/auth/overview/", self.F, ".md.txt")[0], 7)
        self.assertEqual(resolve("auth/overview/", self.F, ".md.txt")[0], 7)
        for ref in ("/", "//"):
            with self.subTest(ref):
                self.assertEqual(resolve(ref, self.F), (None, "Error: No page found for slug: " + ref + "\n"))

    def test_the_exact_lang_page_wins_and_the_others_are_named(self):
        pages = PAGES[:2]
        idx, err = resolve("hooks", self.F, pages=pages)
        self.assertEqual(idx, 0)
        self.assertIn("Note: 'hooks' also matches [1] https://example.com/docs/en/agent-sdk/hooks; "
                      "resolved to [0] (exact <lang>/hooks).", err)

    def test_two_languages_stay_ambiguous_and_are_listed(self):
        idx, err = resolve("hooks", self.F, pages=[PAGES[0], PAGES[2]])
        self.assertIsNone(idx)
        self.assertIn("Ambiguous slug 'hooks'", err)
        self.assertIn("[0] https://example.com/docs/en/hooks", err)
        self.assertIn("[1] https://example.com/docs/ja/hooks", err)

    def test_the_repeated_page_is_ambiguous_by_slug(self):
        # same as the dedicated loader: only the full-URL form picks the first
        idx, err = resolve("install", self.F, pages=PAGES[3:5])
        self.assertIsNone(idx)
        self.assertIn("Ambiguous slug 'install'", err)

    def test_regex_characters_are_literal(self):
        pages = [("A", "https://example.com/docs/a.b"), ("B", "https://example.com/docs/axb")]
        self.assertEqual(resolve("a.b", self.F, pages=pages)[0], 0)
        self.assertEqual(resolve("a.*", self.F, pages=pages), (None, "Error: No page found for slug: a.*\n"))
        for hostile in ("(", "[", "\\", "\\_", "-x", "---", "'", "-", "the"):
            with self.subTest(hostile):
                idx, err = resolve(hostile, self.F, pages=pages)
                self.assertIsNone(idx)
                self.assertIn("No page found for slug", err)

    def test_a_url_is_not_read_as_a_slug(self):
        idx, err = resolve("https://example.com/docs/en/hooks/extra", self.F)
        self.assertIsNone(idx)
        self.assertEqual(err, "Error: No document found for: https://example.com/docs/en/hooks/extra\n")


class SuffixTest(unittest.TestCase):
    PAGES = [("Overview", "https://example.com/docs/auth/overview.md.txt"),
             ("Limits", "https://example.com/docs/firestore/limits.md.txt"),
             ("Plain", "https://example.com/docs/plain")]

    def r(self, ref, forms=("int", "url", "slug"), suffix=".md.txt"):
        return resolve(ref, forms, suffix, pages=self.PAGES)

    def test_both_the_listed_and_the_human_url_reach_the_page(self):
        for ref in ("https://example.com/docs/auth/overview.md.txt", "https://example.com/docs/auth/overview",
                    "https://example.com/docs/auth/overview/", "https://example.com/docs/auth/overview.md.txt#x",
                    "https://example.com/docs/auth/overview.md.txt/", "https://example.com/docs/auth/overview?a=1"):
            with self.subTest(ref=ref):
                self.assertEqual(self.r(ref)[0], 0)

    def test_slugs_ignore_the_suffix(self):
        for ref in ("overview", "auth/overview", "limits"):
            with self.subTest(ref=ref):
                self.assertEqual(self.r(ref)[0], {"overview": 0, "auth/overview": 0, "limits": 1}[ref])
        self.assertEqual(self.r("plain")[0], 2)  # a page without the suffix is unaffected

    def test_a_slug_with_the_suffix_reaches_its_page(self):
        # the suffix comes off the reference too (a slug copied from a listed URL)
        for ref, want in (("overview.md.txt", 0), ("auth/overview.md.txt", 0), ("/auth/overview.md.txt/", 0),
                          ("firestore/limits.md.txt", 1)):
            with self.subTest(ref=ref):
                self.assertEqual(self.r(ref, ("int", "slug"))[0], want)
        # only at the end: nothing is cut out of the middle
        self.assertIsNone(self.r("overview.md.txt.x", ("int", "slug"))[0])

    def test_lang_pages_with_the_suffix_stay_ambiguous(self):
        # prefer_lang_exact reads the raw URL, suffix included, so neither page
        # is "exactly /en/hooks" (kept so: see the slug form in the resolver)
        pages = [("Hooks", "https://example.com/docs/en/hooks.md.txt"),
                 ("Agent SDK Hooks", "https://example.com/docs/en/agent-sdk/hooks.md.txt")]
        idx, err = resolve("hooks", ("int", "slug"), ".md.txt", pages=pages)
        self.assertIsNone(idx)
        self.assertIn("Ambiguous slug 'hooks'", err)
        self.assertNotIn("Note:", err)

    def test_without_the_key_the_suffix_is_part_of_the_url(self):
        self.assertIsNone(self.r("https://example.com/docs/auth/overview", suffix=None)[0])
        self.assertEqual(self.r("https://example.com/docs/auth/overview.md.txt", suffix=None)[0], 0)
        self.assertIsNone(self.r("overview", suffix=None)[0])
        self.assertEqual(self.r("overview.md.txt", suffix=None)[0], 0)

    def test_the_suffix_is_removed_only_at_the_end(self):
        pages = [("A", "https://example.com/docs/a.md.txt.html"), ("B", "https://example.com/docs/b")]
        self.assertIsNone(resolve("https://example.com/docs/a.html", ("int", "url"), ".md.txt", pages=pages)[0])
        self.assertEqual(resolve("https://example.com/docs/a.md.txt.html", ("int", "url"), ".md.txt", pages=pages)[0], 0)
        # nothing is cut out of the middle: the page URL is not "…/a.md.t"
        self.assertIsNone(resolve("https://example.com/docs/a.md.t", ("int", "url"), ".md.txt", pages=pages)[0])

    def test_url_substring_still_sees_the_raw_url(self):
        self.assertEqual(self.r("overview.md.txt", ("int", "url_substring"))[0], 0)
        # …and nothing is stripped from it: a substring that is only the human URL has no match
        self.assertIsNone(self.r("auth/overview.md.txt/", ("int", "url_substring"))[0])


class FormOrderTest(unittest.TestCase):
    """The title of one page is the slug of another: the listed order decides."""

    def test_order_decides(self):
        # "install": the title of [5] ("Install", URL …/setup), the slug of [6] ("Setup", URL …/install)
        pages = PAGES[5:7]
        self.assertEqual(resolve("install", ("int", "slug", "title"), pages=pages)[0], 1)
        self.assertEqual(resolve("install", ("int", "title", "slug"), pages=pages)[0], 0)
        self.assertEqual(resolve("setup", ("int", "slug", "title"), pages=pages)[0], 0)
        self.assertEqual(resolve("setup", ("int", "title", "slug"), pages=pages)[0], 1)

    def test_an_ambiguous_earlier_form_stops_the_search(self):
        # slug "hooks" is ambiguous (en / ja): it is listed, not skipped for the title form
        idx, err = resolve("hooks", ("int", "slug", "title"), pages=[PAGES[0], PAGES[2]])
        self.assertIsNone(idx)
        self.assertIn("Ambiguous slug", err)

    def test_a_form_without_a_match_passes_on(self):
        self.assertEqual(resolve("auth overview", ("int", "slug", "title"))[0], 7)
        self.assertEqual(resolve("https://example.com/docs/auth/overview", ("int", "url", "slug", "title"),
                                 ".md.txt")[0], 7)

    def test_url_failure_message_wins_for_a_url_when_url_is_a_form(self):
        self.assertEqual(resolve("https://example.com/none", ("int", "url", "slug", "title")),
                         (None, "Error: No page found for URL: https://example.com/none\n"))

    def test_slug_failure_message_when_slug_is_a_form(self):
        self.assertEqual(resolve("nothing", ("int", "url", "slug")),
                         (None, "Error: No page found for slug: nothing\n"))
        # with a title (or URL substring) form too, the ref may have been meant
        # as either: the message names neither (ai-sdk: "int", "title", "url", "slug")
        for forms in (("int", "slug", "title"), ("int", "title", "slug"),
                      ("int", "title", "url", "slug"), ("int", "slug", "url_substring")):
            with self.subTest(forms=forms):
                self.assertEqual(resolve("nothing", forms),
                                 (None, "Error: No document found for: nothing\n"))


def candidates(err: str) -> list:
    """The ``[N]`` of the candidates an ambiguity error lists, in order."""
    return [int(n) for n in re.findall(r"(?m)^  \[(\d+)\] ", err)]


def count_lines(err: str) -> list:
    return re.findall(r"(?m)^  \(showing .*\)$", err)


# 22 pages whose title holds "tool", and last the page titled exactly that
TOOL_PAGES = ([(f"Tool kit {i}", f"https://example.com/docs/kit-{i}") for i in range(22)]
              + [("tool", "https://example.com/docs/reference/tool")])

# 22 pages under https://example.com/docs/kit, and last the page at that URL:
# its full URL is part of all 23 URLs
KIT_URL = "https://example.com/docs/kit"
KIT_PAGES = [(f"Kit part {i}", f"{KIT_URL}/part-{i}") for i in range(22)] + [("Kit", KIT_URL)]
WHOLE_URL_LINE = "  (showing 20 of 23 matches; other URLs contain this whole URL, so pass a page number)"


class AmbiguousListTest(unittest.TestCase):
    """The ambiguity error of a title / URL substring lists first the pages
    the reference most likely names (a title that is it, then a URL that ends
    in it), then the rest in page order, and cuts the list at 20 with a line
    that says so: the AI SDK's ``tool`` is part of 42 titles, and the page
    titled ``tool`` was the 42nd, cut off without a word. The URL error lists
    the pages that share it the same way (page order)."""

    D = ("int", "title", "url_substring")

    def test_the_title_that_is_the_reference_comes_first_and_the_cut_is_said(self):
        idx, err = resolve("tool", self.D, pages=TOOL_PAGES)
        self.assertIsNone(idx)
        self.assertTrue(err.startswith("Error: Ambiguous title substring 'tool'. Matches:\n  [22] tool "
                                       "(https://example.com/docs/reference/tool)\n"), err)
        self.assertEqual(candidates(err), [22] + list(range(19)))
        self.assertEqual(count_lines(err), ["  (showing 20 of 23 matches; pass more of the title, "
                                            "a longer URL tail, or the full URL)"])
        self.assertTrue(err.rstrip("\n").endswith(count_lines(err)[0]), err)

    def test_twenty_or_fewer_are_all_listed_with_no_count_line(self):
        for n in (2, 19, 20):
            with self.subTest(n=n):
                idx, err = resolve("tool", self.D, pages=TOOL_PAGES[:n - 1] + TOOL_PAGES[-1:])
                self.assertIsNone(idx)
                self.assertEqual(candidates(err), [n - 1] + list(range(n - 1)))
                self.assertEqual(count_lines(err), [])
        idx, err = resolve("tool", self.D, pages=TOOL_PAGES[:21])
        self.assertEqual((len(candidates(err)), len(count_lines(err))), (20, 1))
        self.assertIn("(showing 20 of 21 matches;", err)

    def test_then_a_url_that_ends_in_it_then_page_order(self):
        pages = [("Tool kit", "https://example.com/a"), ("Tooling", "https://example.com/docs/tool"),
                 ("TOOL", "https://example.com/b"), ("Big tool", "https://example.com/c"),
                 ("Tool box", "https://example.com/docs/tool/")]
        for forms in (self.D, ("int", "title"), ("int", "title", "slug")):
            with self.subTest(forms=forms):
                # with "slug" after "title", two URLs end in "tool": the titles are listed
                self.assertEqual(candidates(resolve("tool", forms, pages=pages)[1]), [2, 1, 4, 0, 3])
        # the URL's end as the slug form reads it (page_url_strip_suffix off)
        pages = [("Guide two", "https://example.com/two"), ("Guide one", "https://example.com/docs/guide.md.txt")]
        self.assertEqual(candidates(resolve("guide", self.D, ".md.txt", pages=pages)[1]), [1, 0])
        self.assertEqual(candidates(resolve("guide", self.D, None, pages=pages)[1]), [0, 1])
        # a URL substring that no title holds: the URL that ends in it first
        urls = [("Page A", "https://example.com/docs/tool-a"), ("Page B", "https://example.com/docs/tool"),
                ("Page C", "https://example.com/x/docs/tool-b")]
        idx, err = resolve("docs/tool", self.D, pages=urls)
        self.assertIsNone(idx)
        self.assertTrue(err.startswith("Error: Ambiguous url substring 'docs/tool'. Matches:\n"), err)
        self.assertEqual(candidates(err), [1, 0, 2])
        # a title as written (with its heading id) is the reference too: the
        # page titled "Setup {#start}" goes ahead of the one before it whose
        # title as written only holds it
        docs = docs_of([("Setup guide", "https://example.com/a"), ("Setup", "https://example.com/b")])
        docs[1]["raw_title"] = "Setup {#start}"
        listed = [docs[0], {**docs[1], "raw_title": "Setup {#start} again"}, docs[1]]
        for ref, want in (("setup {#start}", [2, 1]), ("SETUP", [1, 2, 0])):
            err = io.StringIO()
            with self.subTest(ref=ref), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    generic._resolve_page_ref(listed, ref, None, self.D)
                self.assertEqual(candidates(err.getvalue()), want, err.getvalue())

    def test_the_count_line_names_the_forms_the_profile_has(self):
        for forms, ways in ((self.D, "more of the title, a longer URL tail, or the full URL"),
                            (("int", "title"), "more of the title"),
                            (("int", "title", "url"), "more of the title, or the full URL"),
                            (("int", "title", "slug"), "more of the title, or a longer URL tail")):
            with self.subTest(forms=forms):
                pages = [(f"Tool kit {i}", f"https://example.com/k{i}") for i in range(21)]
                self.assertEqual(count_lines(resolve("tool", forms, pages=pages)[1]),
                                 [f"  (showing 20 of 21 matches; pass {ways})"])
        # pages with no URL: only the title
        no_urls = [(f"Tool kit {i}", "") for i in range(21)]
        self.assertEqual(count_lines(resolve("tool", self.D, pages=no_urls)[1]),
                         ["  (showing 20 of 21 matches; pass more of the title)"])
        # a URL substring (no title has it)
        urls = [(f"Page {i}", f"https://example.com/docs/x{i}") for i in range(21)]
        idx, err = resolve("docs/x", self.D, pages=urls)
        self.assertTrue(err.startswith("Error: Ambiguous url substring 'docs/x'."), err)
        self.assertEqual(count_lines(err), ["  (showing 20 of 21 matches; pass more of the URL, or the full URL)"])

    def test_a_full_url_that_other_urls_hold_lists_its_page_first(self):
        """A full URL is part of the longer URLs under it, so the URL
        substring form matches them all (nextjs's
        ``https://nextjs.org/docs/app/guides`` matched 81 pages, and the page
        at that URL was the 23rd, cut off). The page whose URL is the
        reference (compared as the ``url`` form compares URLs, and
        case-insensitively) comes first, and the count line asks for a page
        number: the reference is that page's full URL already."""
        # the reference in another case; the page's URL with what the url form
        # ignores (a trailing "/", ".md", a query, a fragment)
        cases = [(ref, KIT_URL) for ref in (KIT_URL, "https://example.com/DOCS/Kit")]
        cases += [(KIT_URL, KIT_URL + tail) for tail in ("/", ".md", "?from=list", "#top")]
        for ref, page_url in cases:
            pages = KIT_PAGES[:22] + [("Kit", page_url)]
            with self.subTest(ref=ref, page_url=page_url):
                idx, err = resolve(ref, self.D, pages=pages)
                self.assertIsNone(idx)
                self.assertTrue(err.startswith(f"Error: Ambiguous url substring '{ref}'. Matches:\n"
                                               f"  [22] Kit ({page_url})\n"), err)
                self.assertEqual(candidates(err), [22] + list(range(19)))
                self.assertEqual(count_lines(err), [WHOLE_URL_LINE])
        # page_url_strip_suffix comes off the page's URL first, as in the url
        # form; without the key, ".md.txt" is part of the URL
        listed = [(t, u + ".md.txt") for t, u in KIT_PAGES]
        for suffix, first, line in ((".md.txt", 22, WHOLE_URL_LINE),
                                    (None, 0, "  (showing 20 of 23 matches; pass more of the URL, or the full URL)")):
            with self.subTest(suffix=suffix):
                idx, err = resolve(KIT_URL, self.D, suffix, pages=listed)
                self.assertIsNone(idx)
                self.assertEqual(len(candidates(err)), 20, err)
                self.assertEqual(candidates(err)[0], first)
                self.assertEqual(count_lines(err), [line])
        # a URL that is no candidate's: cut short (every URL holds it), or with
        # a "/" that the page's URL does not hold (that page is no candidate);
        # more of the URL, or the full URL, names one: page order, as before
        for ref, total in ((KIT_URL[:-1], 23), (KIT_URL + "/", 22)):
            with self.subTest(ref=ref):
                idx, err = resolve(ref, self.D, pages=KIT_PAGES)
                self.assertIsNone(idx)
                self.assertEqual(candidates(err), list(range(20)))
                self.assertEqual(count_lines(err),
                                 [f"  (showing 20 of {total} matches; pass more of the URL, or the full URL)"])
        # at the cut: 20 matches are all listed (no count line); of 21, the
        # page at the URL is the 21st in page order and is listed first
        for subpages, lines in ((19, []), (20, ["  (showing 20 of 21 matches; other URLs contain this whole URL, "
                                                "so pass a page number)"])):
            with self.subTest(matches=subpages + 1):
                idx, err = resolve(KIT_URL, self.D, pages=KIT_PAGES[:subpages] + KIT_PAGES[-1:])
                self.assertIsNone(idx)
                self.assertEqual(candidates(err), [subpages] + list(range(19)))
                self.assertEqual(count_lines(err), lines)
        # URL-shaped references that are no page's URL fail as any URL substring
        for ref in ("https://", "https://example.com/", "https://example.com/docs/kit/-x", "https://example.com/\\_"):
            with self.subTest(ref=ref):
                idx, err = resolve(ref, self.D, pages=KIT_PAGES)
                self.assertIsNone(idx)
                self.assertNotIn("whole URL", err)
        # a full URL that is part of titles, on pages with no URL: the titles are listed
        idx, err = resolve(KIT_URL, self.D, pages=[(f"See {KIT_URL}", None), (f"Also {KIT_URL}", None)])
        self.assertIsNone(idx)
        self.assertTrue(err.startswith(f"Error: Ambiguous title substring '{KIT_URL}'."), err)
        self.assertEqual(candidates(err), [0, 1])
        # a reference with no http(s):// is no full URL (the url form does not
        # take it either): its count line is any URL substring's
        rel = [(t, u[len("https://example.com"):]) for t, u in KIT_PAGES]
        idx, err = resolve("/docs/kit", self.D, pages=rel)
        self.assertIsNone(idx)
        self.assertEqual(count_lines(err), ["  (showing 20 of 23 matches; pass more of the URL, or the full URL)"])

    def test_the_pages_of_one_url(self):
        pages = [(f"Page {i}", "https://example.com/docs/same") for i in range(21)]
        docs = docs_of(pages)
        for i, d in enumerate(docs):
            d["body_lines"] = [f"body {i}\n"]  # different pages, not copies
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            generic._resolve_page_ref(docs, "https://example.com/docs/same", None, ("int", "url"))
        self.assertTrue(err.getvalue().startswith("Error: Ambiguous URL 'https://example.com/docs/same'."), err)
        self.assertEqual(candidates(err.getvalue()), list(range(20)))
        self.assertEqual(count_lines(err.getvalue()),
                         ["  (showing 20 of 21 matches; the pages share this URL, so pass a page number)"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            generic._resolve_page_ref(docs[:20], "https://example.com/docs/same", None, ("int", "url"))
        self.assertEqual((len(candidates(err.getvalue())), count_lines(err.getvalue())), (20, []))

    def test_the_candidates_run_as_printed_and_the_count_line_is_no_command(self):
        """Through the commands, each listed candidate's command, split by the
        shell rules and run as printed, opens the page it names; the count
        line is not taken for a command (it starts with no script name)."""
        with tempfile.TemporaryDirectory() as tmp:
            full = Path(tmp, "full.txt")
            full.write_text(corpus_text(TOOL_PAGES), encoding="utf-8")
            sources = Path(tmp, "sources.json")
            sources.write_text(json.dumps({"sources": {"plain": H1}}), encoding="utf-8")
            env = {"LLMS_DOCS_CACHE_DIR": str(Path(tmp, "default-cache"))}
            with mock.patch.dict(os.environ, env), \
                    mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled")):
                for cmd, tail, reached in (("sections", ("tool",), "Sections in [{n}] "),
                                           ("content", ("tool", "Part of x"), "# doc_title: "),
                                           ("search-content", ("Body",), "Body of ")):
                    args = (cmd, "--page-ref", "tool") if cmd == "search-content" else (cmd,)
                    with self.subTest(cmd=cmd):
                        code, out, err = _loader.run_cli(generic, [
                            SCRIPT, *args, "--source", "plain", "--sources-file", str(sources),
                            "--file", str(full), "--", *tail])
                        self.assertEqual(code, 1, err)
                        self.assertEqual(len(count_lines(err)), 1, err)
                        lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith(SCRIPT + " ")]
                        self.assertEqual(len(lines), 20, err)
                        for n, line in zip(candidates(err), lines):
                            argv = shlex.split(line, comments=True)
                            self.assertEqual(argv[argv.index("--source"):argv.index("--source") + 6],
                                             ["--source", "plain", "--sources-file", str(sources),
                                              "--file", str(full)], line)
                            code, opened, err2 = _loader.run_cli(generic, argv)
                            title = TOOL_PAGES[n][0]
                            if cmd == "content":
                                # the page has no "Part of x": the heading error names the page
                                self.assertEqual(code, 1, line)
                                self.assertIn(f"Part of {title}", err2)
                                continue
                            self.assertEqual(code, 0, (line, err2))
                            self.assertIn(reached.format(n=n) + (f'"{title}"' if cmd == "sections" else title),
                                          opened)


# The pages whose URL holds https://vercel.com/docs/connect in Vercel's
# llms-full.txt (2026-10), in page order: the page at that URL is the 23rd.
VERCEL_CONNECT_URL = "https://vercel.com/docs/connect"
VERCEL_CONNECT = [
    ("Authentication in Vercel Connect", "https://vercel.com/docs/connect/concepts/authentication"),
    ("Connectors in Vercel Connect", "https://vercel.com/docs/connect/concepts/connectors"),
    ("Connector installations in Vercel Connect", "https://vercel.com/docs/connect/concepts/installations"),
    ("Vercel Connect Concepts", "https://vercel.com/docs/connect/concepts"),
    ("Project links in Vercel Connect", "https://vercel.com/docs/connect/concepts/project-links"),
    ("Provider tokens in Vercel Connect", "https://vercel.com/docs/connect/concepts/tokens"),
    ("Webhook triggers for Vercel Connect", "https://vercel.com/docs/connect/concepts/triggers"),
    ("Enterprise-Managed Authorization for Linear with Okta", "https://vercel.com/docs/connect/ema/linear"),
    ("Enterprise-Managed Authorization (EMA)", "https://vercel.com/docs/connect/ema"),
    ("Enterprise-Managed Authorization for Slack with Okta", "https://vercel.com/docs/connect/ema/slack"),
    ("AI SDK and MCP with Vercel Connect", "https://vercel.com/docs/connect/frameworks/ai-sdk-and-mcp"),
    ("Auth.js with Vercel Connect", "https://vercel.com/docs/connect/frameworks/authjs"),
    ("Better Auth with Vercel Connect", "https://vercel.com/docs/connect/frameworks/better-auth"),
    ("Chat SDK with Vercel Connect", "https://vercel.com/docs/connect/frameworks/chat-sdk"),
    ("eve with Vercel Connect", "https://vercel.com/docs/connect/frameworks/eve"),
    ("Frameworks and Adapters for Vercel Connect", "https://vercel.com/docs/connect/frameworks"),
    ("TanStack AI with Vercel Connect", "https://vercel.com/docs/connect/frameworks/tanstack-ai"),
    ("Vercel Connect product terms", "https://vercel.com/docs/connect/legal"),
    ("Vercel Connect Limits", "https://vercel.com/docs/connect/limits"),
    ("MCP Events", "https://vercel.com/docs/connect/mcp-events"),
    ("Observability", "https://vercel.com/docs/connect/observability"),
    ("Optimizing Vercel Connect Usage", "https://vercel.com/docs/connect/optimizing-usage"),
    ("Vercel Connect", "https://vercel.com/docs/connect"),
    ("Vercel Connect Pricing", "https://vercel.com/docs/connect/pricing"),
    ("For Service Providers", "https://vercel.com/docs/connect/providers"),
    ("Python SDK Reference", "https://vercel.com/docs/connect/python-sdk-reference"),
    ("Quickstart", "https://vercel.com/docs/connect/quickstart"),
    ("TypeScript SDK Reference", "https://vercel.com/docs/connect/ts-sdk-reference"),
]


class FullUrlRealPagesTest(unittest.TestCase):
    """The bundled ``vercel`` preset (int / title / url_substring) on those
    pages, in the frontmatter of its llms-full.txt, through ``--file`` and a
    cache dir: the full URL of "Vercel Connect" is part of all 28 URLs, and
    the page was the 23rd candidate, not among the 20 listed."""

    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        rule = "-" * 80
        text = "# Vercel documentation\n\n" + "".join(
            f'{rule}\ntitle: "{t}"\nsource: "{u}"\n{rule}\n\n# {t}\n\nzzq {t}\n\n' for t, u in VERCEL_CONNECT)
        self.full = Path(self.tmp, "vercel-llms-full.txt")
        self.full.write_text(text, encoding="utf-8")
        cache = Path(self.tmp, "cache")
        cache.mkdir()
        preset = generic._read_sources_file(generic.PRESETS_FILE)["vercel"]
        Path(generic._cache_path(str(cache), preset)).write_text(text, encoding="utf-8")
        self.corpora = {"file": ["--file", str(self.full)],
                        "cache-dir": ["--cache-dir", str(cache), "--max-age", "999999999"]}
        # a printed command that dropped the corpus options would read an
        # empty default cache and fail to fetch
        for patcher in (mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": str(Path(self.tmp, "default-cache"))}),
                        mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, argv: list) -> tuple:
        try:
            return _loader.run_cli(generic, argv)
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            self.fail(f"{argv} crashed: {e!r}")

    def opened(self, argv: list) -> str:
        """The first line ``sections`` prints for *argv* (it must open a page)."""
        code, out, err = self.cli(argv)
        self.assertEqual(code, 0, (argv, err))
        return out.splitlines()[0]

    def test_the_page_at_the_url_comes_first_and_the_commands_run_as_printed(self):
        for label, corpus in self.corpora.items():
            with self.subTest(corpus=label):
                argv = [SCRIPT, "sections", "--source", "vercel", *corpus, "--", VERCEL_CONNECT_URL]
                code, out, err = self.cli(argv)
                self.assertEqual((code, out), (1, ""), err)
                self.assertTrue(err.startswith(f"Error: Ambiguous url substring '{VERCEL_CONNECT_URL}'. Matches:\n"
                                               f"  [22] Vercel Connect ({VERCEL_CONNECT_URL})\n"), err)
                self.assertEqual(candidates(err), [22] + list(range(19)))
                self.assertEqual(count_lines(err), ["  (showing 20 of 28 matches; other URLs contain this whole "
                                                    "URL, so pass a page number)"])
                # each candidate's command, split by the shell rules and run as
                # printed, opens the page it names
                lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith(SCRIPT + " ")]
                self.assertEqual(len(lines), 20, err)
                for n, line in zip(candidates(err), lines):
                    self.assertEqual(self.opened(shlex.split(line, comments=True)),
                                     f'Sections in [{n}] "{VERCEL_CONNECT[n][0]}"', line)
                # the page number the count line asks for, in place of the URL,
                # opens that page, listed or not
                for n in (22, 27):
                    self.assertEqual(self.opened(argv[:-1] + [str(n)]), f'Sections in [{n}] "{VERCEL_CONNECT[n][0]}"')


class CliTest(unittest.TestCase):
    """Through the commands: ``sections`` / ``content`` / ``search-content
    --page-ref`` honour the profile, and the retry commands they print run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": {
            "plain": H1,
            "refs": {**H1, "page_ref_forms": ["int", "url", "slug"]},
            "suffix": {**H1, "page_ref_forms": ["int", "url", "slug"], "page_url_strip_suffix": ".md.txt"},
            "all": {**H1, "page_ref_forms": ["int", "url", "slug", "title", "url_substring"]},
        }}), encoding="utf-8")
        self.full = Path(self.tmp, "full.txt")
        self.full.write_text(corpus_text(), encoding="utf-8")

    def run_cmd(self, source, *args, tail=()):
        """*tail* goes after ``--`` (a word that may look like an option)."""
        return _loader.run_cli(generic, [SCRIPT, *args, "--source", source, "--sources-file", str(self.sources),
                                         "--file", str(self.full), *(("--", *tail) if tail else ())])

    def test_the_commands_resolve_by_url_and_slug(self):
        for cmd, extra in (("sections", ()), ("content", ())):
            for ref, page in (("https://example.com/docs/en/agent-sdk/hooks", 1), ("agent-sdk/hooks", 1),
                              ("https://example.com/docs/auth/overview", 7), ("overview", 7)):
                with self.subTest(cmd=cmd, ref=ref):
                    code, out, err = self.run_cmd("suffix", cmd, ref, *extra)
                    self.assertEqual(code, 0, err)
                    self.assertIn(PAGES[page][0], out.splitlines()[0])
        code, out, err = self.run_cmd("suffix", "search-content", "Body", "--page-ref", "agent-sdk/hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("Body of Agent SDK Hooks", out)
        self.assertNotIn("Body of Hooks.", out)

    def test_the_plain_profile_does_not_get_the_new_forms(self):
        code, out, err = self.run_cmd("plain", "sections", "https://example.com/docs/en/agent-sdk/hooks")
        self.assertEqual(code, 0, err)  # a URL substring, as before
        code, out, err = self.run_cmd("plain", "sections", "overview")
        self.assertEqual(code, 0, err)  # the title substring
        code, out, err = self.run_cmd("plain", "sections", "auth/overview")
        self.assertEqual(code, 0, err)  # a URL substring, not a slug
        code, out, err = self.run_cmd("plain", "sections", "no-such")
        self.assertEqual((code, err.splitlines()[0]), (1, "Error: No document found for: no-such"))

    def test_unresolved_url_and_slug_name_what_was_tried(self):
        code, out, err = self.run_cmd("refs", "content", "https://example.com/none")
        self.assertEqual((code, err.splitlines()[0]), (1, "Error: No page found for URL: https://example.com/none"))
        code, out, err = self.run_cmd("refs", "sections", "no-such")
        self.assertEqual((code, err.splitlines()[0]), (1, "Error: No page found for slug: no-such"))

    def test_a_missing_url_stops_even_with_the_substring_forms(self):
        code, out, err = self.run_cmd("all", "sections", "https://example.com/docs/en/agent")
        self.assertEqual((code, (err.splitlines() or [""])[0]),
                         (1, "Error: No page found for URL: https://example.com/docs/en/agent"))
        self.assertEqual(out, "")
        code, out, err = self.run_cmd("all", "sections", "https://example.com/docs/en/agent-sdk/hooks")
        self.assertEqual(code, 0, err)
        self.assertIn("Agent SDK Hooks", out.splitlines()[0])

    def test_help_names_page_ref_forms(self):
        for cmd in ("sections", "content"):
            with self.subTest(cmd):
                code, out, err = _loader.run_cli(generic, [SCRIPT, cmd, "--help"])
                self.assertEqual(code, 0, err)
                self.assertIn("page_ref_forms", out)
                self.assertNotIn("Integer index, title substring", out)

    def test_the_note_for_the_other_pages_runs_as_printed(self):
        self.full.write_text(corpus_text(PAGES[:2]), encoding="utf-8")
        for cmd in (("sections", "hooks"), ("content", "hooks", "Part of Agent SDK Hooks"),
                    ("search-content", "Body", "--page-ref", "hooks")):
            with self.subTest(cmd=cmd[0]):
                code, out, err = self.run_cmd("refs", *cmd)
                self.assertEqual(code, 0 if cmd[0] != "content" else 1, err)
                notes = [ln for ln in err.splitlines() if ln.startswith("Note: 'hooks' also matches")]
                self.assertEqual(len(notes), 1, err)
                self.assertIn("For [1]: ", notes[0])
                printed = notes[0].split("For [1]: ", 1)[1]
                code, out, err = _loader.run_cli(generic, shlex.split(printed, comments=True))
                self.assertEqual(code, 0, (printed, err))
                self.assertIn("Agent SDK Hooks", out)

    def test_ambiguous_candidates_run_as_printed_and_reach_their_page(self):
        pages = [PAGES[0], PAGES[2]]
        self.full.write_text(corpus_text(pages), encoding="utf-8")
        code, out, err = self.run_cmd("refs", "sections", "hooks")
        self.assertEqual(code, 1)
        lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith(SCRIPT + " ")]
        self.assertEqual(len(lines), 2, err)
        for idx, line in enumerate(lines):
            argv = shlex.split(line, comments=True)
            code, out, err2 = _loader.run_cli(generic, argv)
            self.assertEqual(code, 0, (line, err2))
            self.assertIn(pages[idx][0], out.splitlines()[0])

    def test_listed_urls_and_indexes_resolve_back_to_their_page(self):
        """Every ref a listing prints (``[N]`` and ``URL:``) reaches its own page."""
        code, out, err = self.run_cmd("suffix", "fetch-index")
        self.assertEqual(code, 0, err)
        shown = []
        for line in out.splitlines():
            if line.startswith("["):
                shown.append([int(line[1:line.index("]")]), None])
            elif line.strip().startswith("URL: ") and shown:
                shown[-1][1] = line.strip()[len("URL: "):]
        self.assertEqual(len(shown), len(PAGES))
        for idx, url in shown:
            # pages 3 and 4 share a URL and differ in body: the URL lists both
            namesakes = [i for i, (_t, u) in enumerate(PAGES) if u == PAGES[idx][1]]
            for ref in (str(idx), url):
                with self.subTest(ref=ref):
                    code, out, err = self.run_cmd("suffix", "sections", ref)
                    if ref != str(idx) and len(namesakes) > 1:
                        self.assertEqual(code, 1, out)
                        self.assertIn(f"Ambiguous URL '{url}'", err)
                        self.assertEqual(re.findall(r"^  \[(\d+)\] ", err, re.M), [str(i) for i in namesakes])
                        continue
                    self.assertEqual(code, 0 if ref == str(idx) or url == PAGES[idx][1] else 1, err)
                    self.assertIn(f'[{idx}] "{PAGES[idx][0]}"', out.splitlines()[0])

    def test_hostile_references_fail_cleanly(self):
        for word in ("-x", "--limit", "\\_", "the", "---", "'", "-", "%", "(", "["):
            for source in ("plain", "refs", "suffix"):
                with self.subTest(word=word, source=source):
                    try:
                        code, out, err = self.run_cmd(source, "sections", tail=(word,))
                    except Exception as e:  # a crash is a failure of the test, not an error of the run
                        self.fail(f"crashed: {e!r}")
                    self.assertIn(code, (0, 1), err)
                    self.assertNotIn("Traceback", err)


class AiSdkPresetOrderTest(unittest.TestCase):
    """The bundled ``ai-sdk`` preset tries a title before a URL or a slug
    (``int``, ``title``, ``url``, ``slug``): a reference that is part of one
    page's title and the URL end of another opens the titled page, as it did
    before ``url`` and ``slug`` were added."""

    CORPUS = (
        "---\ntitle: Streaming Basics\ndescription: A\nurl: https://ai-sdk.dev/docs/intro\n---\n"
        "# Streaming Basics\n\nBody A.\n\n## Part A\n\ntext\n"
        "---\ntitle: Other Page\ndescription: B\nurl: https://ai-sdk.dev/docs/foundations/streaming\n---\n"
        "# Other Page\n\nBody B.\n\n## Part B\n\ntext\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = {k: v for k, v in os.environ.items() if k != generic.SOURCES_ENV}
        env["XDG_CONFIG_HOME"] = self._tmp.name  # no user sources.json
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.corpus = Path(self._tmp.name, "ai-sdk.txt")
        self.corpus.write_text(self.CORPUS, encoding="utf-8")

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def opened(self, ref: str) -> str:
        argv = [SCRIPT, "sections", ref, "--source", "ai-sdk", "--file", str(self.corpus)]
        try:
            code, out, err = _loader.run_cli(generic, argv)
        except Exception as e:  # a crash is a failure of the test, not an error of the run
            self.fail(f"{argv} crashed: {e!r}")
        self.assertEqual(code, 0, err)
        return out.splitlines()[0]

    def test_a_title_word_that_is_also_another_pages_slug_opens_the_titled_page(self):
        self.assertIn('[0] "Streaming Basics"', self.opened("streaming"))

    def test_the_other_page_is_still_reached_by_its_slug_and_url(self):
        for ref in ("foundations/streaming", "https://ai-sdk.dev/docs/foundations/streaming"):
            with self.subTest(ref=ref):
                self.assertIn('[1] "Other Page"', self.opened(ref))


if __name__ == "__main__":
    unittest.main()
