"""Fixture-based tests for scripts/_common.py shared helpers.

Covers the fixtures identified during the 2026-08 plugin audit: index
bullet-marker variants, heading-level skips, code-fence / table protection
in content extraction, keyword scoring, AND-to-partial search fallback,
overflow reporting, and the format-change detection helpers
(assert_parsed / check_join_rate / full_corpus_body_search).
"""

import re
import types
import unittest
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

import _common


class ParseLlmsIndexTest(unittest.TestCase):
    def test_colon_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks): Configure hook events\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries, [{
            "title": "Hooks",
            "url": "https://example.com/hooks",
            "description": "Configure hook events",
        }])

    def test_dash_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks) - Configure hook events\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries[0]["description"], "Configure hook events")

    def test_no_description_form(self):
        lines = ["- [Hooks](https://example.com/hooks)\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries, [{
            "title": "Hooks", "url": "https://example.com/hooks", "description": "",
        }])

    def test_asterisk_bullet_accepted(self):
        lines = ["* [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Hooks")

    def test_plus_bullet_accepted(self):
        lines = ["+ [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)

    def test_indented_bullet_accepted(self):
        lines = ["  - [Hooks](https://example.com/hooks): desc\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(len(entries), 1)

    def test_non_ascii_title_and_description(self):
        lines = ["- [フック](https://example.com/hooks): フックイベントの設定\n"]
        entries = _common.parse_llms_index(lines)
        self.assertEqual(entries[0]["title"], "フック")
        self.assertEqual(entries[0]["description"], "フックイベントの設定")

    def test_asterisk_prose_line_without_link_produces_no_entry(self):
        # A plain '* some text' bullet (no markdown link) must not become a
        # spurious entry now that '*' is accepted as a bullet marker.
        lines = ["* just some text, no link here\n"]
        self.assertEqual(_common.parse_llms_index(lines), [])

    def test_ignores_non_bullet_lines(self):
        lines = ["# Heading\n", "\n", "Some prose.\n"]
        self.assertEqual(_common.parse_llms_index(lines), [])


class ExtractSectionsTest(unittest.TestCase):
    def test_level_skip_h2_to_h4(self):
        body = [
            "## Top\n",
            "text\n",
            "#### Deep\n",
            "deep text\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["heading_path"] for s in sections], ["Top", "Top/Deep"])
        self.assertEqual(sections[1]["level"], 4)

    def test_tilde_fence_hides_heading_shaped_lines(self):
        """CommonMark tilde fences (``~~~``) are code blocks too; a heading-
        shaped line inside one is not a section (internal backlog).
        Zero ``~~~`` lines exist in the live claude-docs / ai-sdk corpora,
        so the real-corpus before/after sections dump is identical."""
        body = [
            "## Real\n",
            "~~~\n",
            "## Not a heading\n",
            "~~~\n",
            "## Also Real\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Real", "Also Real"])

    def test_fence_closes_only_with_same_char_and_length(self):
        body = [
            "~~~\n",
            "```\n",            # backticks inside a tilde fence: content
            "## Still fenced\n",
            "~~\n",             # too short to close
            "## Still fenced 2\n",
            "~~~~\n",           # longer run of the opener char closes
            "## Real\n",
            "````\n",
            "~~~\n",            # tildes inside a backtick fence: content
            "## Fenced again\n",
            "```\n",            # shorter than the 4-backtick opener
            "## Fenced again 2\n",
            "````\n",
            "## Real 2\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Real", "Real 2"])


class FenceTrackerCloserTest(unittest.TestCase):
    """A closing fence carries no info string (CommonMark), plus the two
    departures measured on MDX corpora: any indentation, and ``*/}`` ending
    a code block written inside an MDX comment."""

    @staticmethod
    def _states(lines):
        tracker = _common.FenceTracker()
        return [tracker.update(line) for line in lines]

    def test_stray_indented_closer_after_indented_code_opens_nothing(self):
        # Firebase: indented code in a list item, then a lone indented ```
        lines = ["1. Step\n", "\n", "       code();\n", "       ```\n", "\n",
                 "### Java\n", "text\n"]
        self.assertEqual(self._states(lines), [False] * 7)
        # and a heading after it is extracted
        paths = [s["heading_path"] for s in _common.extract_sections(lines)]
        self.assertIn("Java", paths)

    def test_indented_bare_opener_with_code_at_its_depth_still_opens(self):
        lines = ["1. Run:\n", "\n", "    ```\n", "    # not a heading\n", "    ```\n", "## After\n"]
        # the opener is reported outside until the next line confirms it
        self.assertEqual(self._states(lines), [False, False, False, True, False, False])
        self.assertEqual([s["title"] for s in _common.extract_sections(lines)], ["After"])

    def test_tentative_rule_is_only_for_bare_runs_indented_four_or_more(self):
        # an info string, or an indent under 4, opens as before even when
        # the next line is shallower
        self.assertEqual(self._states(["    ```ts\n", "code\n"]), [True, True])
        self.assertEqual(self._states(["   ```\n", "code\n"]), [True, True])
        # an empty indented block (opener then closer at the same depth) closes
        self.assertEqual(self._states(["    ```\n", "    ```\n", "text\n"]), [False, False, False])

    def test_info_string_line_inside_an_open_block_is_content(self):
        self.assertEqual(
            self._states(["```\n", "```ts\n", "code\n", "```\n", "text\n"]),
            [True, True, True, False, False],
        )

    def test_whitespace_after_the_run_is_not_an_info_string(self):
        for closer in ("```\n", "```   \n", "```\t\n", "  ```\n", "````\n", "```"):
            with self.subTest(closer=closer):
                self.assertEqual(self._states(["```\n", closer]), [True, False])

    def test_unclosed_block_recovers_at_the_next_bare_closer(self):
        """A stray fence line, then the next block's ```` ```ts ```` opener:
        taking the opener for a closer flipped the state for the rest of
        the page, hiding real headings and exposing code comments."""
        body = [
            "## Cache\n",
            "```\n",                       # stray: nothing closes it on its own
            "- Using cache in transactions\n",
            "```ts\n",                     # the next block's opener: content here
            "await db.transaction();\n",
            "```\n",                       # bare closer ends the stray block
            "\n",
            "## Limitations\n",
            "```ts\n",
            "## not a heading\n",
            "```\n",
            "## After\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Cache", "Limitations", "After"])

    def test_mdx_comment_end_closes_an_open_block(self):
        body = [
            "## Unions\n",
            "{/* For convenience:\n",
            "\n",
            "  ```ts\n",
            "  const either = z.string().or(z.number());\n",
            "  ``` */}\n",
            "\n",
            "## Optional\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Unions", "Optional"])

    def test_mdx_comment_end_never_opens_a_block(self):
        # the comment's first line (``{/* ```sql``) is not a fence line
        body = [
            "## Queries\n",
            "{/* ```sql\n",
            "SELECT * FROM users ...\n",
            "``` */}\n",
            "</Section>\n",
            "\n",
            "## Advanced\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Queries", "Advanced"])

    def test_mdx_comment_end_still_needs_the_opener_run(self):
        # inside a comment, a shorter run or the other fence character is
        # content even when followed by */}
        for opener in ("````ts\n", "~~~ts\n"):
            with self.subTest(opener=opener):
                self.assertEqual(
                    self._states(["{/* note\n", opener, "``` */}\n", "text\n"]),
                    [False, True, True, True],
                )

    def test_mdx_comment_example_inside_a_block_is_content(self):
        """A page showing how to write an MDX comment, in a same-length
        fence: the ``{/*`` is inside the block, so ```` ``` */} ```` is
        content and the bare ```` ``` ```` closes the block (CommonMark)."""
        body = [
            "## Comments\n",
            "```mdx\n",
            "{/*\n",
            "```js\n",
            "console.log(1)\n",
            "``` */}\n",
            "```\n",
            "\n",
            "## Next\n",
        ]
        sections = _common.extract_sections(body, min_level=2)
        self.assertEqual([s["title"] for s in sections], ["Comments", "Next"])

    def test_mdx_comment_state_ends_with_the_comment(self):
        """Once a real comment ends (on a plain line, at the fence that closes
        a block inside it, or on a ``` */} line outside a block), a later
        example's ``` */} inside a same-length block is content again."""
        example = ["```mdx\n", "{/*\n", "```js\n", "x\n", "``` */}\n", "```\n", "\n", "## Next\n"]
        endings = {
            "plain line": ["{/* note\n", "*/}\n"],
            "closing fence": ["{/* note\n", "```ts\n", "x\n", "``` */}\n"],
            "comment end outside a block": ["{/* ```sql\n", "SELECT 1\n", "``` */}\n"],
        }
        for name, comment in endings.items():
            with self.subTest(ending=name):
                body = ["## Top\n"] + comment + ["\n"] + example
                sections = _common.extract_sections(body, min_level=2)
                self.assertEqual([s["title"] for s in sections], ["Top", "Next"])

    def test_fence_indented_in_jsx_is_still_a_block(self):
        """Deliberately looser than CommonMark's 0-3 spaces: MDX code blocks
        nested in JSX are indented 4+ spaces and render as code. With the
        CommonMark limit, truncation could cut inside one."""
        content = (
            "<Tab>\n"
            "    ```bash\n"
            "    export TOKEN=your-token\n"
            "    echo done\n"
            "    ```\n"
            "</Tab>\n"
        )
        out = _common.truncate_content(content, content.index("    echo done"), narrow_hint="X")
        self.assertTrue(out.startswith("<Tab>\n\n... ("), out)


class ExtractContentTest(unittest.TestCase):
    def test_extends_to_close_unclosed_code_fence(self):
        body = [
            "## Section A\n",
            "```python\n",
            "x = 1\n",
            "## Section B\n",  # inside the fence: not a real heading
            "```\n",
            "## Section C\n",
        ]
        content, resolved = _common.extract_content(body, "Section A")
        self.assertIn("x = 1", content)
        self.assertIn("## Section B", content)  # still inside the fenced block
        self.assertNotIn("Section C", content)
        self.assertEqual(resolved, "Section A")

    def test_extends_to_include_straddling_table(self):
        body = [
            "## Section A\n",
            "text\n",
            "## Section B\n",
            "| a | b |\n",
            "|---|---|\n",
            "| 1 | 2 |\n",
            "## Section C\n",
            "more\n",
        ]
        content, resolved = _common.extract_content(body, "Section B")
        self.assertIn("| 1 | 2 |", content)
        self.assertNotIn("Section C", content)
        self.assertEqual(resolved, "Section B")

    def test_full_document_when_heading_path_none(self):
        body = ["## A\n", "x\n"]
        content, resolved = _common.extract_content(body, None)
        self.assertEqual(content, "".join(body))
        self.assertIsNone(resolved)

    def test_partial_match_resolves_to_canonical_heading_path(self):
        # The header/hint block must show the section that was ACTUALLY
        # picked, not echo back the caller's raw (possibly partial) input
        # — "config" here only matches the nested H3.
        body = [
            "## Frontmatter reference\n",
            "text\n",
            "### Advanced Configuration\n",
            "deep\n",
        ]
        content, resolved = _common.extract_content(body, "config")
        self.assertEqual(resolved, "Frontmatter reference/Advanced Configuration")
        self.assertIn("deep", content)

    def test_ambiguous_partial_match_dies_instead_of_silently_picking_first(self):
        # Two sibling "... Configuration" sections both match the case-
        # insensitive substring "config" — silently taking the first (the
        # old behavior) risks the caller reading/citing the wrong section
        # with no indication another candidate existed.
        body = [
            "## Client Configuration\n",
            "a\n",
            "## Server Configuration\n",
            "b\n",
        ]
        with self.assertRaises(SystemExit) as cm:
            _common.extract_content(body, "config")
        self.assertEqual(cm.exception.code, 1)

    def test_exact_heading_path_copied_from_output_never_hits_ambiguity_check(self):
        # Interaction between the two fixes above: a heading_path copied
        # verbatim from a previous sections/content/search call must
        # always resolve via the exact-match branch first, even when it
        # would ALSO satisfy
        # >= 2 other sections' partial-match pattern — the ambiguity check
        # must never run once an exact match exists. Otherwise the tool's
        # own documented copy-paste round trip (see SKILL.md Quick Start)
        # would newly break for any heading_path that happens to be a
        # substring of a sibling section's path too.
        body = [
            "## Client Configuration\n",
            "a\n",
            "## Server Configuration\n",
            "b\n",
        ]
        content, resolved = _common.extract_content(body, "Client Configuration")
        self.assertEqual(resolved, "Client Configuration")
        self.assertIn("a", content)
        self.assertNotIn("b", content)

    def test_case_insensitive_exact_match_beats_descendant_partial_match(self):
        # Heading matching is documented as case-insensitive. A query that
        # differs from a section's title only by case must resolve via the
        # (new) case-folded exact-match tier and win outright — not fall
        # through to the substring tier, where it would also match its own
        # nested child ("Configuration/Options" contains "configuration")
        # and die as ambiguous even though only one *heading* actually
        # equals the query.
        body = [
            "## Configuration\n",
            "top-level config text\n",
            "### Options\n",
            "nested options text\n",
        ]
        content, resolved = _common.extract_content(body, "configuration")
        self.assertEqual(resolved, "Configuration")
        self.assertIn("top-level config text", content)

    def test_case_insensitive_exact_match_on_heading_path_form(self):
        # Same tier, exercised against a multi-segment heading_path (not
        # just a bare title) copied back with different casing.
        body = [
            "## Guide\n",
            "intro\n",
            "### Setup\n",
            "setup text\n",
        ]
        content, resolved = _common.extract_content(body, "guide/setup")
        self.assertEqual(resolved, "Guide/Setup")
        self.assertIn("setup text", content)

    def test_top_heading_path_returns_preamble_before_first_heading(self):
        # search can report a body hit above the first heading as
        # Section: (top), and its Next hint tells the caller to copy that
        # value straight into content's heading_path argument — this must
        # resolve, not fall through to "heading not found".
        body = [
            "preamble line\n",
            "## First\n",
            "first body\n",
        ]
        content, resolved = _common.extract_content(body, "(top)")
        self.assertEqual(resolved, "(top)")
        self.assertIn("preamble line", content)
        self.assertNotIn("first body", content)

    def test_top_heading_path_on_document_with_no_headings_returns_everything(self):
        body = ["only preamble\n", "more preamble\n"]
        content, resolved = _common.extract_content(body, "(top)")
        self.assertEqual(resolved, "(top)")
        self.assertEqual(content, "".join(body))


class ScoreEntryTest(unittest.TestCase):
    def test_title_exact_match_scores_highest(self):
        self.assertEqual(_common.score_entry("Hooks", "", ["hooks"]), 10)

    def test_plural_query_now_matches_multiword_title(self):
        # Previously a characterization test pinning score 0 (no
        # stemming). _norm() now folds "hooks" -> "hook", which is a
        # substring of normalized "hook events" -> scores as a partial
        # match (5), not an exact one (10), since the full normalized
        # strings still differ.
        self.assertEqual(_common.score_entry("Hook events", "", ["hooks"]), 5)

    def test_title_substring_match(self):
        self.assertGreater(_common.score_entry("Hooks reference", "", ["hooks"]), 0)

    def test_closed_compound_matches_open_spelling(self):
        # "statusline" finds "status line" as whole consecutive words
        self.assertEqual(_common.score_entry("Customize your status line", "", ["statusline"]), 5)
        self.assertEqual(_common.score_entry("Status line", "", ["statusline"]), 10)
        self.assertEqual(_common.score_entry("Durable Objects", "", ["durableobjects"]), 10)
        self.assertEqual(_common.score_entry("x", "Use the Bash tool", ["bashtool"]), 2)
        self.assertEqual(_common.score_entry("x", "", ["subagent"], headings=["Sub agent setup"]), 1)

    def test_compound_needs_word_boundaries_on_both_ends(self):
        # "handler" is inside "handle redirects" only by straddling a boundary
        self.assertEqual(_common.score_entry("Handle redirects", "", ["handler"]), 0)
        # and "atusli" is inside "status line" only mid-word
        self.assertEqual(_common.score_entry("Status line", "", ["atusline"]), 0)

    def test_compound_is_a_fallback_only(self):
        # a keyword that already matches as written scores the same as before
        self.assertEqual(_common.score_entry("Status line", "", ["status"]), 5)
        self.assertTrue(_common._joins_words("abc", ["a", "b", "c"]))
        self.assertFalse(_common._joins_words("abcd", ["a", "b", "c", "d"]))  # > 3 words
        self.assertFalse(_common._joins_words("ab", ["ab"]))  # a single word is not a compound

    def test_all_keyword_bonus(self):
        multi = _common.score_entry("Hook events", "matcher config", ["hook", "matcher"])
        single = _common.score_entry("Hook events", "matcher config", ["hook"])
        self.assertGreater(multi, single)


class NormalizationStemmingTest(unittest.TestCase):
    """A light plural-to-singular + separator-stripping fold so
    plural/singular and hyphen/camelCase spelling variants score as
    equivalent, fixing the previously-zero cases fixed above and pinning
    two more representative pairs: skills/Skill, hooks/Hook events,
    stream-text/streamText."""

    def test_plural_query_matches_singular_title_exactly(self):
        self.assertEqual(_common.score_entry("Skill", "", ["skills"]), 10)

    def test_normalized_exact_match_outranks_mere_substring(self):
        exact = _common.score_entry("Skill", "", ["skills"])
        substring = _common.score_entry("Skill reference", "", ["skills"])
        self.assertGreater(exact, substring)

    def test_hyphenated_query_matches_camelcase_title_exactly(self):
        self.assertEqual(_common.score_entry("streamText", "", ["stream-text"]), 10)

    def test_singular_query_matches_plural_title_as_exact(self):
        # The already-working reverse direction (score_entry('Skills', '',
        # ['skill']) == 5, substring only) should be UPGRADED to an exact
        # match now that both sides are normalized before comparing.
        self.assertEqual(_common.score_entry("Hooks", "", ["hook"]), 10)

    def test_unrelated_keyword_still_scores_zero(self):
        # Normalization must not turn matching lenient enough to create
        # false positives between unrelated words.
        self.assertEqual(_common.score_entry("Skill", "", ["firestore"]), 0)

    def test_mixed_case_acronym_is_not_treated_as_plural(self):
        # "iOS" is not the plural of "iO" — stripping its trailing "s" the
        # same way "Hooks" -> "Hook" is stripped turns it into "io", which
        # then substring-matches any title/description merely containing
        # that pair of letters (e.g. "Configuration", "Migrations").
        self.assertEqual(_common.score_entry("Configuration", "", ["iOS"]), 0)
        self.assertEqual(_common.score_entry("Migrations", "", ["iOS"]), 0)

    def test_mixed_case_acronym_still_matches_itself_exactly(self):
        self.assertEqual(_common.score_entry("iOS", "", ["iOS"]), 10)
        # Substring match still works when the title spells out the same
        # acronym with its real capitalization (as any actual "iOS ..."
        # doc title would) — both sides normalize to the same "ios" token.
        self.assertEqual(_common.score_entry("iOS App Development", "", ["iOS"]), 5)

    def test_single_capital_word_is_still_stemmed_normally(self):
        # The guard is specifically for mixed-case acronyms; an ordinary
        # Title-cased single word (exactly one capital, at position 0)
        # must keep stemming as before.
        self.assertEqual(_common.score_entry("Skill", "", ["Skills"]), 10)

    def test_all_caps_ordinary_plural_is_still_stemmed_and_matches(self):
        # An ALL-CAPS query ("HOOKS") is a user typing an ordinary plural
        # in shouty case, not an acronym — unlike "iOS" it has no
        # lowercase letter anywhere. It must still stem to "hook" (the
        # same result "Hooks" stems to) rather than staying "hooks" and
        # silently missing an otherwise-exact match solely because of
        # capitalization. This is the regression case for the mixed-case
        # (not just "2+ capitals") guard above.
        self.assertEqual(_common.score_entry("Hooks", "", ["HOOKS"]), 10)
        self.assertEqual(_common.score_entry("Skill", "", ["SKILLS"]), 10)

    def test_silent_e_plurals_fold_to_their_singular(self):
        """``-ses`` / ``-zes`` plurals are read as silent-e root + ``s``
        (internal backlog): "Responses" must match "response"."""
        for plural, singular in [
            ("Responses", "response"), ("Releases", "release"),
            ("Databases", "database"), ("Cases", "case"), ("Sizes", "size"),
            ("Caches", "cache"), ("Uses", "use"),
        ]:
            with self.subTest(plural=plural):
                self.assertEqual(_common.score_entry(plural, "", [singular]), 10)
                self.assertEqual(_common.score_entry(singular, "", [plural]), 10)

    def test_hard_consonant_plurals_still_fold(self):
        """The hard-consonant + ``es`` readings that the old rule handled
        keep working, including the listed ``-ses`` exceptions."""
        for plural, singular in [
            ("Matches", "match"), ("Batches", "batch"), ("Searches", "search"),
            ("Boxes", "box"), ("Indexes", "index"), ("Prefixes", "prefix"),
            ("Hashes", "hash"), ("Classes", "class"), ("Processes", "process"),
            ("Addresses", "address"),
            ("Aliases", "alias"), ("Statuses", "status"),
        ]:
            with self.subTest(plural=plural):
                self.assertEqual(_common.score_entry(plural, "", [singular]), 10)
                self.assertEqual(_common.score_entry(singular, "", [plural]), 10)

    def test_norm_examples(self):
        for tok, expected in [
            ("responses", "response"), ("matches", "match"), ("caches", "cache"),
            ("classes", "class"), ("aliases", "alias"), ("sizes", "size"),
            ("hashes", "hash"), ("uses", "use"), ("status", "status"),
            ("process", "process"), ("iOS", "ios"),
        ]:
            with self.subTest(tok=tok):
                self.assertEqual(_common._norm(tok), expected)

class ScoreEntryEmptyNormalizedKeywordTest(unittest.TestCase):
    """A keyword consisting only of characters _norm() strips (separators
    like '-'/'_') normalizes to "". Left unguarded, "" is a substring of
    every string, so every 'kw_norm in <field>' check in score_entry would
    trivially succeed — turning a degenerate keyword into a match against
    the entire corpus instead of a no-op."""

    def test_separator_only_keyword_does_not_match_everything(self):
        self.assertEqual(_common.score_entry("Hooks", "Configure hook matchers", ["_"]), 0)
        self.assertEqual(_common.score_entry("Skills", "Reusable capabilities", ["--"]), 0)
        self.assertEqual(_common.score_entry("Anything", "Any description at all", ["_-"]), 0)

    def test_separator_only_keyword_does_not_inflate_score_when_mixed_with_real_keyword(self):
        # The garbage keyword must contribute nothing — no extra points,
        # and it must not count toward (or against) the all-keywords-
        # matched AND bonus threshold.
        only_real = _common.score_entry("Hooks", "", ["hooks"])
        real_plus_garbage = _common.score_entry("Hooks", "", ["hooks", "_"])
        self.assertEqual(only_real, real_plus_garbage)

    def test_two_real_keywords_still_get_and_bonus_alongside_garbage_keyword(self):
        two_real = _common.score_entry("Hook events", "matcher config", ["hook", "matcher"])
        two_real_plus_garbage = _common.score_entry(
            "Hook events", "matcher config", ["hook", "matcher", "--"]
        )
        self.assertEqual(two_real, two_real_plus_garbage)


class ScoreEntryRegressionFloorTest(unittest.TestCase):
    """Regression floor, per this repo's discipline of diffing a
    changed analysis/ranking function against its pre-change output over a
    representative corpus (not just mutation-testing the new code): these
    exact (title, description, keywords) -> minimum-score pairs were
    verified via an old-vs-new differential script against the
    pre-stemming score_entry (merged main, commit 054815f) to have zero
    unexplained losses across a ~2800-combination matrix of doc-corpus-like
    titles/descriptions/keywords.

    They were chosen to pin the specific bug caught in review: _norm()'s
    length gates (``len(t) > 4`` / ``len(t) > 2``) are sized for a single
    word. An early version of this change applied _norm() directly to
    whole multi-word title/description strings, so the gate was measured
    against the combined length of every word instead of the last word
    alone — "Common uses" (len 11) took the multi-char sibilant branch
    (stripping "es") while the bare keyword "uses" (len 4) fell through to
    the single-"s" branch instead, producing "common us" vs "use" — two
    strings that no longer share the substring the *unnormalized* text did
    ("uses" in "Common uses"). ``_norm_phrase()`` (word-by-word, then
    rejoin) fixes this by keeping every word's stemming decision local to
    that word. If any case here drops below its floor, _norm_phrase
    regressed back to whole-string normalization.
    """

    def test_previously_working_matches_do_not_score_lower_than_before(self):
        cases = [
            ("Common uses", "", ["uses"], 5),
            ("Chart axes", "", ["axes"], 5),
            ("Tool use", "Common uses of this API", ["uses"], 2),
            ("Hooks", "Configure hook matchers here", ["hooks"], 10),
            ("Rate limits and quotas", "", ["limits"], 5),
            ("Cloud Functions for Firebase", "", ["functions"], 5),
        ]
        for title, desc, kws, minimum in cases:
            with self.subTest(title=title, keywords=kws):
                self.assertGreaterEqual(
                    _common.score_entry(title, desc, kws), minimum
                )

    def test_previously_exact_matches_are_still_at_least_exact(self):
        for title, kw in [("Hooks", "hooks"), ("Skill", "skill"), ("Analysis", "analysis")]:
            with self.subTest(title=title):
                self.assertGreaterEqual(_common.score_entry(title, "", [kw]), 10)


class FormatHeadingPathForDisplayTest(unittest.TestCase):
    def test_top_sentinel_gets_clarifying_suffix(self):
        rendered = _common.format_heading_path_for_display("(top)")
        self.assertTrue(rendered.startswith("(top)"))
        self.assertIn("before first heading", rendered)

    def test_regular_heading_path_is_unchanged(self):
        self.assertEqual(
            _common.format_heading_path_for_display("Hooks/Configuration"),
            "Hooks/Configuration",
        )


class HeadingAnchorSlugTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(_common.heading_anchor_slug("PreToolUse"), "pretooluse")

    def test_whitespace_becomes_hyphen(self):
        self.assertEqual(_common.heading_anchor_slug("Hook events"), "hook-events")

    def test_symbols_are_stripped(self):
        self.assertEqual(
            _common.heading_anchor_slug("Client (Advanced)!"), "client-advanced"
        )
        self.assertEqual(_common.heading_anchor_slug("What's New?"), "whats-new")

    def test_symbols_between_words_become_hyphens(self):
        """見出しの記号 (. ( / など) は削らずハイフンにする。Mintlify の id は
        `loop.md` を `loop-md` にする (ページ内リンクと突き合わせて確認した形)。"""
        cases = {
            "Customize the default prompt with loop.md":
                "customize-the-default-prompt-with-loop-md",
            "worktree.baseRef": "worktree-baseref",
            "claude.ai rejected the session token": "claude-ai-rejected-the-session-token",
            "/security-review fails without origin/HEAD":
                "security-review-fails-without-origin-head",
            "Conflict between `--bg` and `--print`": "conflict-between-bg-and-print",
            "Invalid `--agents` configuration": "invalid-agents-configuration",
            "Set up (optional) hooks": "set-up-optional-hooks",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(_common.heading_anchor_slug(title), expected)

    def test_apostrophes_are_dropped_not_hyphenated(self):
        self.assertEqual(_common.heading_anchor_slug("Can't read .mcp.json"), "cant-read-mcp-json")
        self.assertEqual(_common.heading_anchor_slug("Anthropic’s marketplaces"),
                         "anthropics-marketplaces")

    def test_backslash_escaped_underscore_is_the_underscore(self):
        """Markdown の `\\_` は `_` として表示される。バックスラッシュ を記号として
        ハイフンにしない。これは best-effort で、`\\_` を `-` にする id のページもある
        (その形には寄せていない)。"""
        self.assertEqual(_common.heading_anchor_slug(r"subagent\_type is required"),
                         "subagent_type-is-required")
        self.assertEqual(_common.heading_anchor_slug(r"REVIEW\.md"), "review-md")

    def test_slug_matches_in_page_link_for_symbol_headings(self):
        """往復: ページ内リンク (#...) に書かれた id を、見出しから作った slug で引き直せる。"""
        body = [
            "See [loop](#customize-the-default-prompt-with-loop-md) and "
            "[base](#worktree-baseref).\n",
            "\n",
            "## Customize the default prompt with loop.md\n",
            "text\n",
            "## worktree.baseRef\n",
            "text\n",
        ]
        links = set(re.findall(r"\]\(#([^)\s]+)\)", "".join(body)))
        slugs = {_common.heading_anchor_slug(s["title"]) for s in _common.extract_sections(body)}
        self.assertEqual(slugs, links)

    def test_devsite_style_keeps_stripping_symbols(self):
        """devsite の記号の扱いは実測の根拠が無いため変えない (github 系だけ直した)。"""
        self.assertEqual(_common.heading_anchor_slug("Read loop.md now", style="devsite"),
                         "read_loopmd_now")

    def test_existing_hyphens_are_preserved(self):
        self.assertEqual(
            _common.heading_anchor_slug("Multi-Word-Title"), "multi-word-title"
        )

    def test_multiple_spaces_collapse_to_one_hyphen(self):
        self.assertEqual(_common.heading_anchor_slug("Foo   Bar"), "foo-bar")

    def test_leading_trailing_whitespace_and_symbols_are_trimmed(self):
        self.assertEqual(_common.heading_anchor_slug("  Step 1: Setup  "), "step-1-setup")

    def test_all_symbol_heading_normalizes_to_empty(self):
        self.assertEqual(_common.heading_anchor_slug("---"), "")
        self.assertEqual(_common.heading_anchor_slug("???"), "")


class HeadingAnchorSlugMarkupTest(unittest.TestCase):
    """見出しに inline Markdown が含まれるとき、レンダリング後のテキストから slug を作る
    (マージ前レビューの指摘: link 先 URL が slug に混入していた)。"""

    def test_link_destination_is_dropped(self):
        self.assertEqual(_common.heading_anchor_slug("[Hooks](https://example.com/hooks)"), "hooks")
        self.assertEqual(
            _common.heading_anchor_slug("See [the guide](https://x.y/z) now", style="devsite"),
            "see_the_guide_now",
        )

    def test_image_alt_is_kept(self):
        self.assertEqual(_common.heading_anchor_slug("![Logo](img/logo.png) Setup"), "logo-setup")

    def test_html_tags_and_entities(self):
        self.assertEqual(_common.heading_anchor_slug("Tom &amp; Jerry <sup>beta</sup>"), "tom-jerry-beta")

    def test_emphasis_and_code_markers_are_removed_content_kept(self):
        self.assertEqual(_common.heading_anchor_slug("Using `query()` and **ClaudeSDKClient**"),
                         "using-query-and-claudesdkclient")
        self.assertEqual(_common.heading_anchor_slug("snake_case_name stays"), "snake_case_name-stays")

    def test_code_span_contents_keep_underscores_and_asterisks(self):
        """コードスパンの中身は逐語: `__init__` の `_` や `*args` の `*` を強調記号として
        剥がさない (マージ前レビューの指摘)。"""
        self.assertEqual(_common.heading_anchor_slug("The `__init__` method"), "the-__init__-method")
        self.assertEqual(
            _common.heading_anchor_slug("Using `*args` in `snake_case_fn`", style="devsite"),
            "using_args_in_snake_case_fn",
        )

    def test_reference_style_links_and_footnotes(self):
        """参照形式リンク `[text][ref]` / `[text][]` は text に、脚注 `[^1]` は除く
        (マージ前レビューの指摘)。"""
        self.assertEqual(_common.heading_anchor_slug("[Hooks][hooks-docs]"), "hooks")
        self.assertEqual(_common.heading_anchor_slug("See [Hooks][] now[^1]"), "see-hooks-now")
        self.assertEqual(
            _common.heading_anchor_slug("[Set a document][ref]", style="devsite"), "set_a_document"
        )

    def test_angle_brackets_inside_code_span_are_kept_as_text(self):
        """コードスパン内の `<name>` は逐語表示されるので HTML タグとして削らない
        (マージ前レビューの指摘)。スパン外の実タグは従来どおり削る。"""
        self.assertEqual(_common.heading_anchor_slug("Set `<name>` value"), "set-name-value")
        self.assertEqual(
            _common.heading_anchor_slug("Run `firebase use <alias>` <sup>beta</sup>", style="devsite"),
            "run_firebase_use_alias_beta",
        )

    def test_underscore_emphasis_delimiters_are_removed(self):
        """`_word_` の下線は強調区切りなので slug から除く。識別子内の `_` は残す
        (マージ前レビューの指摘)。"""
        self.assertEqual(_common.heading_anchor_slug("_Config_ options"), "config-options")
        self.assertEqual(_common.heading_anchor_slug("Use __strong__ words"), "use-strong-words")
        self.assertEqual(
            _common.heading_anchor_slug("_Config_ for snake_case_name", style="devsite"),
            "config_for_snake_case_name",
        )


class HeadingAnchorSlugDevsiteStyleTest(unittest.TestCase):
    """firebase.google.com is Google DevSite, not GitHub/Mintlify — DevSite
    joins heading-id words with "_", not "-". Live-verified on
    https://firebase.google.com/docs/firestore/manage-data/add-data: the
    "Set a document" heading renders as id="set_a_document" (not the
    default style's "set-a-document", which does not resolve on that page)
    (マージ前レビューの指摘)."""

    def test_whitespace_becomes_underscore(self):
        self.assertEqual(
            _common.heading_anchor_slug("Set a document", style="devsite"),
            "set_a_document",
        )

    def test_words_with_no_separator_are_unaffected(self):
        # devsite: an internal "/" with no spaces around it is stripped,
        # leaving one run with nothing to join (the default style turns the
        # "/" into a hyphen: "ci-cd").
        self.assertEqual(_common.heading_anchor_slug("CI/CD", style="devsite"), "cicd")

    def test_slash_surrounded_by_spaces_becomes_single_underscore(self):
        self.assertEqual(
            _common.heading_anchor_slug("Read / write data", style="devsite"),
            "read_write_data",
        )

    def test_default_style_is_unchanged_by_devsite_addition(self):
        # Default (github) behavior must stay byte-identical after adding
        # the style parameter — no default-arg regression.
        self.assertEqual(_common.heading_anchor_slug("Set a document"), "set-a-document")


class SectionUrlAnchorTest(unittest.TestCase):
    def test_appends_url_and_slug_from_leaf_heading(self):
        # section_url_anchor takes the section's own leaf title directly
        # (not heading_path, the ancestor breadcrumb) — the caller is
        # responsible for resolving which heading to anchor to.
        self.assertEqual(
            _common.section_url_anchor(
                "https://code.claude.com/docs/hooks", "PreToolUse"
            ),
            "  [https://code.claude.com/docs/hooks#pretooluse]",
        )

    def test_title_containing_slash_is_not_split_as_a_breadcrumb(self):
        # Regression (merge-review finding): a leaf heading whose own title
        # legitimately contains "/" (e.g. "## CI/CD") must not be treated
        # as a multi-segment heading_path and truncated to the text after
        # the last "/". Passing the title directly — instead of deriving it
        # from heading_path via rsplit("/", 1) — sidesteps that ambiguity.
        self.assertEqual(
            _common.section_url_anchor("https://example.com/p", "CI/CD"),
            "  [https://example.com/p#ci-cd]",
        )
        self.assertEqual(
            _common.section_url_anchor("https://example.com/p", "Read / write data"),
            "  [https://example.com/p#read-write-data]",
        )

    def test_no_url_means_no_suffix(self):
        self.assertEqual(_common.section_url_anchor("", "Configuration"), "")
        self.assertEqual(_common.section_url_anchor(None, "Configuration"), "")

    def test_top_sentinel_has_no_anchor(self):
        self.assertEqual(_common.section_url_anchor("https://example.com/p", "(top)"), "")

    def test_all_symbol_leaf_heading_produces_no_suffix(self):
        self.assertEqual(_common.section_url_anchor("https://example.com/p", "???"), "")

    def test_style_is_forwarded_to_heading_anchor_slug(self):
        # Firebase callers pass style="devsite" — confirm section_url_anchor
        # threads it through rather than always using the github default.
        self.assertEqual(
            _common.section_url_anchor(
                "https://firebase.google.com/docs/firestore/manage-data/add-data",
                "Set a document",
                style="devsite",
            ),
            "  [https://firebase.google.com/docs/firestore/manage-data/add-data#set_a_document]",
        )


ADD_DIR_ROW = (
    "| `/add-dir <path>` | Add a working directory for file access during the current session. "
    "Type a partial path to see matching directory suggestions; press `Tab` to accept one. "
    "Most `.claude/` configuration [isn't discovered](/docs/en/permissions#additional-directories-"
    "grant-file-access-not-configuration) from the added directory. You can't add most "
    "[network paths](/docs/en/errors#working-directory-is-a-network-path), such as "
    "`\\\\server\\share`. After a successful add, your [`DirectoryAdded` hooks]"
    "(/docs/en/hooks#directoryadded) run. When you run it while Claude is responding, Claude Code "
    "asks you to confirm the directory right away, and once you confirm, Claude's next tool call "
    "in the same turn can access it. Before v2.1.234, Claude Code queued the command until the "
    "turn finished |")
BATCH_ROW = (
    "| `/batch <instruction>` | **[Skill](/docs/en/skills#bundled-skills).** Orchestrate "
    "large-scale changes across a codebase in parallel. Researches the codebase, decomposes the "
    "work into 5 to 30 independent units, and presents a plan. Once approved, spawns one "
    "[background subagent](/docs/en/sub-agents#run-subagents-in-foreground-or-background) per "
    "unit in an isolated [worktree](/docs/en/worktrees). Each subagent implements its unit, runs "
    "tests, and publishes its change. Requires a git repository or a [`WorktreeCreate` hook]"
    "(/docs/en/worktrees#non-git-version-control) that creates the worktrees. Outside a git "
    "repository, `/batch` requires Claude Code v2.1.281 or later. Example: `/batch migrate src/ "
    "from JavaScript to TypeScript` |")
HOOKS_ROW = "| `/hooks` | View [hook](/docs/en/hooks#the-%2Fhooks-menu) configurations |"
UPDATE_CONFIG_ROW = (
    "| `/update-config [request]` | **[Skill](/docs/en/skills#bundled-skills).** Describe a "
    "settings change, such as allowing a command, setting an environment variable, or adding a "
    "[hook](/docs/en/hooks), and Claude edits the matching [`settings.json`](/docs/en/settings) "
    "file. For options such as theme and model, use `/config` instead |")


class SnippetBudgetTest(unittest.TestCase):
    """snippet の文字数の予算を、一致行の前の長い表の行が使い切って一致行が
    見えなくなっていた。一致行を優先して残し、文脈の行から削る。"""

    LONG_ROW = "| `SOME_VARIABLE` | " + ("long description " * 40) + "|"

    def _snippet(self, body, query, **kw):
        result = _common.search_content_in_body(body, query, max_snippet_chars=500, **kw)
        return result["results"][0]["snippet"]

    def test_a_word_at_the_end_of_a_long_line_closes_the_window(self):
        text = "→ " + "a" * 300 + " keyword"
        span = _common._first_keyword_span(text[2:], ["keyword"])
        cut = _common._cut_hit_line(text, 82, span)
        self.assertTrue(cut.endswith("keyword"), cut)
        self.assertEqual(len(cut), 82 + 1)  # the window is used up, plus the leading …

    def test_a_word_across_the_cut_is_kept_whole(self):
        # the word starts inside the first 80 characters and ends past them
        text = "→ " + "x" * 76 + " keyword " + "z" * 200
        span = _common._first_keyword_span(text[2:], ["keyword"])
        self.assertIn("keyword", _common._cut_hit_line(text, 82, span))

    def test_a_lowercase_of_another_length_gives_no_span(self):
        self.assertIsNone(_common._first_keyword_span("\u0130" * 5 + " keyword", ["keyword"]))

    def test_match_line_after_a_long_table_row_is_visible(self):
        body = ["## Variables\n", self.LONG_ROW + "\n", "the needleword is here\n", "after\n"]
        snippet = self._snippet(body, "needleword")
        self.assertIn("→ the needleword is here", snippet)
        self.assertNotIn("long description", snippet)
        self.assertIn("  ...", snippet)
        self.assertLessEqual(len(snippet), 500 + 20)

    def test_context_that_fits_is_kept_nearest_first(self):
        body = ["## Variables\n", "far before\n", "near before\n", "needleword\n",
                "near after\n", self.LONG_ROW + "\n"]
        snippet = self._snippet(body, "needleword", context_lines=2)
        self.assertIn("  near before", snippet)
        self.assertIn("  near after", snippet)
        self.assertNotIn("long description", snippet)

    def test_context_is_dropped_farthest_first_when_only_some_fits(self):
        body = ["## S\n", "x" * 200 + "\n", "y" * 200 + "\n", "needleword\n"]
        result = _common.search_content_in_body(body, "needleword", max_snippet_chars=300,
                                                context_lines=3)
        snippet = result["results"][0]["snippet"]
        self.assertIn("y" * 200, snippet)       # 一致行の隣は残る
        self.assertNotIn("x" * 200, snippet)    # 遠い方から落ちる
        self.assertIn("→ needleword", snippet)

    def test_a_snippet_within_the_budget_is_unchanged(self):
        body = ["## S\n", "before\n", "needleword\n", "after\n"]
        self.assertEqual(self._snippet(body, "needleword"),
                         "  ## S\n  before\n→ needleword\n  after")

    def test_hit_lines_over_the_budget_each_keep_a_share(self):
        """一致行だけで予算を超えるときは、末尾から切ると後ろの一致行が消える。
        予算を一致行の数で割り、各行をその字数 (80 字未満にはしない) で … 付きに切る。"""
        body = ["## S\n"] + [f"needleword {i} " + "x" * 600 + "\n" for i in range(3)]
        snippet = self._snippet(body, "needleword")
        hit_lines = [ln for ln in snippet.splitlines() if ln.startswith("→ ")]
        self.assertEqual([ln[:14] for ln in hit_lines],
                         ["→ needleword 0", "→ needleword 1", "→ needleword 2"])
        for ln in hit_lines:
            self.assertEqual(len(ln), 500 // 3 + 1)
            self.assertTrue(ln.endswith("x…"), ln)

    def test_hit_lines_are_not_cut_below_80_chars(self):
        body = ["## S\n"] + [f"needleword {i} " + "x" * 300 + "\n" for i in range(3)]
        result = _common.search_content_in_body(body, "needleword", max_snippet_chars=120)
        hit_lines = [ln for ln in result["results"][0]["snippet"].splitlines()
                     if ln.startswith("→ ")]
        self.assertEqual(len(hit_lines), 3)
        self.assertEqual({len(ln) for ln in hit_lines}, {81})

    def test_a_hit_line_cut_to_its_share_shows_the_matched_word(self):
        """一致した語が切る幅より後ろにあるときは、行の先頭からではなく語の前後を
        切り出す (前後に …)。→ の印は窓の外に残す。"""
        body = ["## S", "| " + "a" * 300 + " keyword " + "b" * 140 + " |",
                "keyword short line"]
        result = _common.search_content_in_body(body, "keyword", max_snippet_chars=300)
        hit_lines = [ln for ln in result["results"][0]["snippet"].splitlines()
                     if ln.startswith("→ ")]
        self.assertEqual(len(hit_lines), 2)
        for ln in hit_lines:
            self.assertIn("keyword", ln)
        self.assertTrue(hit_lines[0].startswith("→ …a"), hit_lines[0])
        self.assertTrue(hit_lines[0].endswith("b…"), hit_lines[0])
        self.assertEqual(hit_lines[1], "→ keyword short line")

    def test_rows_of_a_real_command_table_show_the_matched_word(self):
        # Rows of the "All commands" table of the Claude Code docs: "hook"
        # sits far into the first two, near the start of the third.
        body = ["## All commands", "| Command | Purpose |", "| :-- | :-- |",
                ADD_DIR_ROW, BATCH_ROW, HOOKS_ROW, UPDATE_CONFIG_ROW]
        result = _common.search_content_in_body(body, "hook", page_title="Commands",
                                                max_snippet_chars=500)
        snippet = result["results"][0]["snippet"]
        hit_lines = [ln for ln in snippet.splitlines() if ln.startswith("→ ")]
        self.assertEqual(len(hit_lines), 3)
        for ln in hit_lines:
            self.assertIn("hook", ln.lower())
        self.assertTrue(hit_lines[2].startswith("→ | `/hooks` |"), hit_lines[2])
        self.assertIn("(1 more hits in this section)", snippet)

    def test_more_hits_note_is_kept(self):
        body = ["## S\n"] + [f"needleword {i}\n" for i in range(5)]
        snippet = self._snippet(body, "needleword")
        self.assertIn("(2 more hits in this section)", snippet)


class SearchContentInBodyTest(unittest.TestCase):
    def _body(self):
        return [
            "## Section A\n",
            "alpha bravo\n",
            "## Section B\n",
            "alpha only\n",
        ]

    def test_and_match_mode_when_all_keywords_in_one_section(self):
        result = _common.search_content_in_body(self._body(), "alpha bravo")
        self.assertEqual(result["match_mode"], "and")
        self.assertEqual(result["results"][0]["heading_path"], "Section A")

    def test_falls_back_to_partial_when_no_section_has_all_keywords(self):
        result = _common.search_content_in_body(self._body(), "bravo charlie")
        # "charlie" appears nowhere, so strict AND finds 0 sections; partial
        # (>= ceil(2/2)=1 of 2 keywords) should surface "Section A" (has
        # "bravo").
        self.assertEqual(result["match_mode"], "partial")
        self.assertTrue(any(r["heading_path"] == "Section A" for r in result["results"]))

    def test_overflow_sections_reported_when_over_limit(self):
        body = []
        for i in range(5):
            body.append(f"## Section {i}\n")
            body.append("keyword hit\n")
        result = _common.search_content_in_body(body, "keyword", max_matches_per_doc=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(result["overflow_sections"]), 3)

    def test_result_carries_title_separate_from_heading_path(self):
        # "title" is the leaf heading text a caller should pass to
        # section_url_anchor — kept as its own field so callers never need
        # to derive it from heading_path by splitting on "/" (which breaks
        # when the heading's own title contains one, e.g. "## CI/CD").
        body = ["## CI/CD\n", "pipeline keyword\n"]
        result = _common.search_content_in_body(body, "pipeline")
        self.assertEqual(result["results"][0]["heading_path"], "CI/CD")
        self.assertEqual(result["results"][0]["title"], "CI/CD")

    def test_top_sentinel_result_has_matching_title(self):
        body = ["preamble keyword text\n", "## Section A\n", "alpha\n"]
        result = _common.search_content_in_body(body, "preamble")
        self.assertEqual(result["results"][0]["heading_path"], "(top)")
        self.assertEqual(result["results"][0]["title"], "(top)")


class AssertParsedTest(unittest.TestCase):
    def test_zero_count_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            _common.assert_parsed("test-source", 0, "/tmp/fixture.txt")
        self.assertEqual(cm.exception.code, 2)

    def test_nonzero_count_does_not_exit(self):
        _common.assert_parsed("test-source", 5, "/tmp/fixture.txt")  # must not raise


class CheckJoinRateTest(unittest.TestCase):
    def test_below_fail_threshold_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            _common.check_join_rate("test", 1, 10)  # 10%
        self.assertEqual(cm.exception.code, 2)

    def test_between_warn_and_fail_thresholds_warns_but_continues(self):
        # 70%: below the 80% warn threshold, above the 50% fail threshold.
        _common.check_join_rate("test", 7, 10)  # must not raise

    def test_above_warn_threshold_is_silent(self):
        _common.check_join_rate("test", 9, 10)  # must not raise

    def test_zero_total_is_noop(self):
        _common.check_join_rate("test", 0, 0)  # must not raise


class FullCorpusBodySearchTest(unittest.TestCase):
    def test_ranks_by_total_matches_and_skips_docs_with_no_hits(self):
        docs_body_lines = [
            ["## A\n", "nothing here\n"],
            ["## B\n", "target target target\n"],
            ["## C\n", "target once\n"],
        ]
        results = _common.full_corpus_body_search(docs_body_lines, "target", limit=5)
        self.assertEqual([idx for idx, _ in results], [1, 2])

    def test_limit_truncates_results(self):
        docs_body_lines = [["## D%d\n" % i, "target\n"] for i in range(5)]
        results = _common.full_corpus_body_search(docs_body_lines, "target", limit=2)
        self.assertEqual(len(results), 2)

    def test_strict_and_docs_outrank_partial_docs_before_the_limit(self):
        # doc 0 repeats one keyword (partial match, many hits); doc 1 has both
        # keywords in one section (strict AND, one hit). The AND doc must
        # survive the limit even though it has fewer hits.
        docs_body_lines = [
            ["## Frontmatter\n"] + ["frontmatter field\n"] * 9,
            ["## Reference\n", "argument-hint is a frontmatter field\n"],
        ]
        results = _common.full_corpus_body_search(docs_body_lines, "argument-hint frontmatter", limit=1)
        self.assertEqual([idx for idx, _ in results], [1])
        self.assertEqual(results[0][1]["match_mode"], "and")


class TruncateContentPreservesMarkdownBoundariesTest(unittest.TestCase):
    """A raw content[:max_chars] slice could land inside a fenced code
    block or Markdown table — extract_content's own fence/table
    protection only guards heading-section cuts, not this later
    character-budget cut — leaving the truncated construct and the
    appended notice both malformed."""

    def test_cut_inside_fence_backs_up_to_before_the_fence(self):
        content = (
            "intro text here\n"
            "```python\n"
            "x = 1\n"
            "y = 2\n"
            "z = 3\n"
            "```\n"
            "trailing text\n"
        )
        # max_chars deliberately lands partway through the fence body.
        max_chars = content.index("y = 2")
        result = _common.truncate_content(
            content, max_chars, narrow_hint='content 0 "<heading_path>"'
        )
        visible_body = result.split("\n... (")[0]
        self.assertIn("intro text here", visible_body)
        self.assertNotIn("```", visible_body)  # backed up before the fence entirely
        self.assertIn("chars truncated", result)

    def test_cut_after_an_unconfirmed_indented_opener_backs_up_before_it(self):
        # A bare run indented 4+ spaces opens a block only once the next
        # non-blank line confirms it; a cut in between would end the output
        # on a lone fence line.
        content = "intro line\n\n        ```\n\n        code line one\n        code line two\n        ```\n\nafter\n"
        for max_chars in range(content.index("```") + 4, content.rindex("```") + 3):
            result = _common.truncate_content(content, max_chars, narrow_hint="hint")
            self.assertEqual(result.split("\n... (")[0], "intro line\n\n", max_chars)

    def test_cut_inside_table_backs_up_to_before_the_table(self):
        content = (
            "intro\n"
            "| a | b |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "| 3 | 4 |\n"
            "after table\n"
        )
        # max_chars deliberately lands partway through the table.
        max_chars = content.index("| 1 | 2 |")
        result = _common.truncate_content(
            content, max_chars, narrow_hint='content 0 "<heading_path>"'
        )
        visible_body = result.split("\n... (")[0]
        self.assertIn("intro", visible_body)
        self.assertNotIn("|", visible_body)  # backed up before the table entirely

    def test_cut_mid_plain_line_backs_up_to_the_previous_line_boundary(self):
        # No fence or table involved at all — this pins the more general
        # "never split a single line in half" guarantee the line-boundary
        # walk gives for free, not just the fence/table cases above.
        content = "line one\nline two\nline three\nline four\n"
        max_chars = len("line one\nline two\n") + 3  # partway through "line three"
        result = _common.truncate_content(content, max_chars, narrow_hint="hint")
        visible_body = result.split("\n... (")[0]
        self.assertEqual(visible_body, "line one\nline two\n")

    def test_short_enough_content_is_returned_unchanged(self):
        content = "short\n"
        self.assertEqual(
            _common.truncate_content(content, 1000, narrow_hint="hint"), content
        )

    def test_no_safe_boundary_emits_empty_body_not_a_raw_cut(self):
        # Degenerate case: even the very first line alone exceeds
        # max_chars. Falling back to a raw content[:max_chars] slice here
        # would cut mid-line (or mid-fence, if the long first line opened
        # one) — exactly the malformed-cut failure this function exists
        # to prevent. Emitting no body before the notice is always
        # well-formed, if less useful.
        content = "a" * 100 + "\nsecond line\n"
        result = _common.truncate_content(content, 10, narrow_hint="hint")
        visible_body = result.split("\n... (")[0]
        self.assertEqual(visible_body, "")
        self.assertIn("chars truncated", result)

    def test_no_safe_boundary_inside_an_unclosed_fence_from_the_start(self):
        content = "```python\n" + ("x = 1\n" * 20)
        result = _common.truncate_content(content, 15, narrow_hint="hint")
        visible_body = result.split("\n... (")[0]
        self.assertEqual(visible_body, "")


class CorpusHintArgsTest(unittest.TestCase):
    """A hint suggesting a follow-up command (the --max-chars truncation
    notice, print_subsection_hints' Next: line) that drops a non-default
    --file/--cache-dir selection can point the SAME numeric page index at
    an entirely different document once the reader follows it and it
    re-resolves against the default corpus instead of the snapshot/cache
    dir just displayed."""

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_default_cache_dir_and_no_file_yields_nothing(self, _mock):
        args = types.SimpleNamespace(file=None, cache_dir="/default/cache")
        self.assertEqual(_common.corpus_hint_args(args), ())

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_non_default_cache_dir_is_included(self, _mock):
        args = types.SimpleNamespace(file=None, cache_dir="/custom/cache")
        self.assertEqual(
            _common.corpus_hint_args(args), ("--cache-dir", "/custom/cache")
        )

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_file_is_included_even_with_default_cache_dir(self, _mock):
        args = types.SimpleNamespace(file="/snap.txt", cache_dir="/default/cache")
        self.assertEqual(_common.corpus_hint_args(args), ("--file", "/snap.txt"))

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_file_takes_precedence_over_cache_dir(self, _mock):
        # --file and --cache-dir are mutually exclusive in every loader
        # that supports both (once --file is given, cache_dir is never
        # even read), so echoing --cache-dir alongside it would mislead a
        # reader into thinking it still matters.
        args = types.SimpleNamespace(file="/snap.txt", cache_dir="/custom/cache")
        self.assertEqual(_common.corpus_hint_args(args), ("--file", "/snap.txt"))

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_missing_file_attribute_is_treated_as_absent(self, _mock):
        # firebase's args namespace has no --file flag at all.
        args = types.SimpleNamespace(cache_dir="/default/cache")
        self.assertEqual(_common.corpus_hint_args(args), ())

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_file_path_with_space_is_shell_quoted(self, _mock):
        # These values are spliced verbatim into a copy-pasteable shell
        # command line; an unquoted space would split into an extra
        # argument when the reader actually runs the generated command.
        args = types.SimpleNamespace(file="/my docs/snap.txt", cache_dir="/default/cache")
        self.assertEqual(
            _common.corpus_hint_args(args), ("--file", "'/my docs/snap.txt'")
        )

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_cache_dir_with_shell_metacharacter_is_shell_quoted(self, _mock):
        args = types.SimpleNamespace(file=None, cache_dir="/cache;rm -rf /")
        self.assertEqual(
            _common.corpus_hint_args(args),
            ("--cache-dir", "'/cache;rm -rf /'"),
        )

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_non_default_max_age_is_included(self, _mock):
        args = types.SimpleNamespace(
            file=None, cache_dir="/default/cache", max_age=0,
        )
        self.assertEqual(_common.corpus_hint_args(args), ("--max-age", "0"))

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_default_max_age_is_omitted(self, _mock):
        args = types.SimpleNamespace(
            file=None, cache_dir="/default/cache",
            max_age=_common.DEFAULT_MAX_AGE_SECONDS,
        )
        self.assertEqual(_common.corpus_hint_args(args), ())

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_max_age_is_omitted_once_file_takes_precedence(self, _mock):
        # --file makes --max-age irrelevant too (read-only snapshot mode
        # never re-fetches), same reasoning as --cache-dir above.
        args = types.SimpleNamespace(file="/snap.txt", cache_dir="/default/cache", max_age=0)
        self.assertEqual(_common.corpus_hint_args(args), ("--file", "/snap.txt"))

    @mock.patch("_common.default_cache_dir", return_value="/default/cache")
    def test_missing_max_age_attribute_is_treated_as_absent(self, _mock):
        args = types.SimpleNamespace(file=None, cache_dir="/default/cache")
        self.assertEqual(_common.corpus_hint_args(args), ())


class SearchRankKeyTest(unittest.TestCase):
    """One ranking for the ``search`` subcommand of all three scripts
    (internal backlog): changelog-style pages last, then strict-AND before
    partial matches, then body hits, then index score, then doc_idx."""

    @staticmethod
    def _r(idx, title, hits, score):
        return {"doc_idx": idx, "title": title, "index_score": score,
                "body_hits": {"total_matches": hits}}

    def test_body_hits_outrank_index_score(self):
        rows = [self._r(0, "Named in title", 1, 20), self._r(1, "Covers it", 9, 2)]
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [1, 0])

    def test_index_score_then_doc_idx_break_ties(self):
        rows = [self._r(5, "b", 3, 2), self._r(2, "a", 3, 2), self._r(9, "c", 3, 7)]
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [9, 2, 5])

    def test_changelog_pages_sort_last_unless_opted_in(self):
        rows = [self._r(0, "Changelog", 50, 30), self._r(1, "Release notes", 40, 30),
                self._r(2, "Hooks", 1, 1)]
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [2, 0, 1])
        rows.sort(key=lambda r: _common.search_rank_key(r, include_changelog_priority=True))
        self.assertEqual([r["doc_idx"] for r in rows], [0, 1, 2])

    def test_strict_and_rows_outrank_partial_rows(self):
        rows = [self._r(0, "Subagents", 40, 9), self._r(1, "Skills", 3, 1)]
        rows[0]["body_hits"]["match_mode"] = "partial"
        rows[1]["body_hits"]["match_mode"] = "and"
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [1, 0])
        # an index-only row (no body hits) stays below a partial row
        rows.append(self._r(3, "Named in title", 0, 30))
        rows[2]["body_hits"]["match_mode"] = "none"
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [1, 0, 3])
        rows.pop()
        # changelog pages still sort last, even with a strict AND
        rows.append(self._r(2, "Changelog", 50, 9))
        rows[2]["body_hits"]["match_mode"] = "and"
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [1, 0, 2])

    def test_body_only_fallback_rows_have_no_index_score(self):
        rows = [self._r(0, "x", 2, None), self._r(1, "y", 2, 0)]
        rows.sort(key=_common.search_rank_key)
        self.assertEqual([r["doc_idx"] for r in rows], [0, 1])

    def test_is_low_priority_patterns(self):
        for t in ("Changelog", "Release Notes", "release-notes 2026", "Firebase release notes"):
            self.assertTrue(_common.is_low_priority(t), t)
        for t in ("Hooks", "Manage rollouts and releases", ""):
            self.assertFalse(_common.is_low_priority(t), t)


if __name__ == "__main__":
    unittest.main()
