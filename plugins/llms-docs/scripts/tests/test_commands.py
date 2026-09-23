"""Tests for scripts/_commands.py (the shared command/render layer).

The three ``parse-*.py`` scripts print through these renderers since
2wd.25 Phase 0. Output parity with the pre-refactor per-script copies was
verified with an offline snapshot of every subcommand; these tests pin the
template details a future edit is most likely to drift on.
"""

import contextlib
import io
import types
import unittest

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

import _commands
from _commands import (
    PageView,
    print_entry,
    print_page_hits,
    print_search_result,
    render_content,
    render_sections,
)

BODY = [
    "# Top\n",
    "intro\n",
    "## Alpha\n",
    "alpha body\n",
    "### Alpha child\n",
    "```\ncode\n```\n",
    "## Beta\n",
    "beta body\n",
]


def _capture(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


def _hits(results, total=None, partial=False, overflow=()):
    return {
        "total_matches": total if total is not None else sum(r["hit_count"] for r in results),
        "results": results,
        "match_mode": "partial" if partial else "all",
        "overflow_sections": list(overflow),
    }


def _hit(title, path, n=1, snippet="line one\nline two", kws=()):
    return {"title": title, "heading_path": path, "hit_count": n,
            "snippet": snippet, "matched_keywords": list(kws)}


class RenderSectionsTest(unittest.TestCase):
    def test_default_floor_indents_from_h2(self):
        page = PageView(idx=3, title="T", body_lines=BODY, header_lines=["  URL: u"])
        out = _capture(render_sections, page, hint_args=("--cache-dir", "/c"))
        self.assertTrue(out.startswith('Sections in [3] "T"\n  URL: u\n' + "=" * 60 + "\n"))
        self.assertIn("\n[L2] Alpha\n", out)
        self.assertIn("\n  [L3] Alpha/Alpha child [code]\n", out)
        self.assertNotIn("[L1]", out)
        self.assertIn("--cache-dir /c", out.splitlines()[-1])

    def test_min_level_one_includes_h1_and_shifts_indent(self):
        page = PageView(idx=0, title="T", body_lines=BODY, min_level=1)
        out = _capture(render_sections, page, hint_args=())
        self.assertIn("\n[L1] Top\n", out)
        self.assertIn("\n  [L2] Top/Alpha\n", out)


class RenderContentTest(unittest.TestCase):
    def _args(self, **kw):
        base = {"heading_path": None, "max_chars": 0, "no_subsection_hints": True}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_transform_runs_before_truncation(self):
        page = PageView(idx=1, title="T", body_lines=BODY, meta={"source": "https://x"})
        seen = []

        def transform(content):
            seen.append(content)
            return content.replace("alpha body", "ALPHA")

        out = _capture(render_content, page, self._args(heading_path="Alpha"),
                       script="parse-x.py", hint_args=(), transform=transform)
        self.assertEqual(len(seen), 1)
        self.assertIn("ALPHA", out)
        self.assertIn("https://x", out)

    def test_truncation_hint_names_the_script_and_hint_args(self):
        page = PageView(idx=7, title="T", body_lines=BODY * 50)
        out = _capture(render_content, page, self._args(max_chars=40),
                       script="parse-x.py", hint_args=("--max-age", "5"))
        self.assertIn('parse-x.py content 7 "<heading_path>" --max-age 5', out)


class RowRenderersTest(unittest.TestCase):
    def test_print_entry_cuts_description_at_120(self):
        out = _capture(print_entry, "[1] T", description="d" * 121, extra_lines=["    URL: u"])
        self.assertEqual(out, "[1] T\n    " + "d" * 117 + "...\n    URL: u\n\n")
        out = _capture(print_entry, "[1] T", description="d" * 120)
        self.assertIn("d" * 120 + "\n", out)

    def test_print_page_hits_noun_anchor_and_overflow(self):
        hits = _hits([_hit("Alpha", "Top/Alpha", 2)], total=5,
                     overflow=[{"heading_path": "Top/Beta", "hit_count": 3}])
        out = _capture(print_page_hits, "[2] T", hits, noun="document",
                       anchor_for=lambda title: f" [#{title}]", show_overflow=True)
        self.assertIn("    (5 hits in this document, showing 1)\n", out)
        self.assertIn("    Section: Top/Alpha  (x2) [#Alpha]\n", out)
        self.assertIn("      line one\n      line two\n", out)
        self.assertIn("    Other sections with hits (not shown):\n      - Top/Beta  (x3)\n", out)
        out = _capture(print_page_hits, "[2] T", hits)
        self.assertNotIn("Other sections", out)

    def test_partial_match_marks_the_block_and_lists_keywords(self):
        hits = _hits([_hit("A", "A", kws=("foo", "bar"))], partial=True)
        out = _capture(print_page_hits, "[0] T", hits)
        self.assertIn("showing 1) [partial match]\n", out)
        self.assertIn("(x1)  keywords: foo, bar\n", out)

    def test_print_search_result_keeps_index_only_rows(self):
        out = _capture(print_search_result, "[4] T (index_score: 9)", _hits([], total=0),
                       extra_lines=["    tags: a"])
        self.assertEqual(out, "[4] T (index_score: 9)\n    tags: a\n    (no body hits — index match only)\n\n")


class HintArgsAreNotBuiltHereTest(unittest.TestCase):
    def test_module_does_not_import_corpus_hint_args(self):
        """The renderers must forward the caller's hint_args, never build them."""
        self.assertFalse(hasattr(_commands, "corpus_hint_args"))


if __name__ == "__main__":
    unittest.main()
