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
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

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
            for ref in (str(idx), url):
                with self.subTest(ref=ref):
                    code, out, err = self.run_cmd("suffix", "sections", ref)
                    self.assertEqual(code, 0 if ref == str(idx) or url == PAGES[idx][1] else 1, err)
                    if ref == str(idx) or PAGES[idx][1] != PAGES[idx - 1][1]:
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


if __name__ == "__main__":
    unittest.main()
