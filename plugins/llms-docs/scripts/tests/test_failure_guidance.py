"""What the four ``parse-*.py`` scripts print after an error or an empty result.

An ambiguous page reference, a heading path that does not resolve, a search
whose ``Next:`` line was a placeholder, and a search with no hits all used to
leave the caller to rebuild the next command by hand (a copied heading with a
typo, a reworded query that cannot help). Each script is checked for the same
four behaviours against a small local corpus (no network): the shared tests
live in ``_GuidanceTests`` and every script only supplies its fixture.

Plus the rule that is specific to URL slugs (``hooks`` -> ``/en/hooks`` rather
than ``/en/agent-sdk/hooks``), which only claude-docs has.
"""

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)
import _common
import _commands

claude = _loader.load_script("parse-claude-docs.py")
ai_sdk = _loader.load_script("parse-ai-sdk.py")
firebase = _loader.load_script("parse-firebase.py")
generic = _loader.load_script("parse-llms-txt.py")

# Three terms that live in three different sections of the first page, so a
# query naming all of them has every term in the corpus and no section with
# two of them (nothing matches).
HOOKS_BODY = (
    "## Hook events\n"
    "### PreToolUse\n"
    "Runs before a tool call. AlphaTerm\n"
    "## Configuration\n"
    "betaterm\n"
    "## Output\n"
    "gammaterm\n"
)
OTHER_BODY = "## Overview\nNothing relevant here.\n"


class _GuidanceTests:
    """Shared tests; a subclass sets ``module`` / ``script`` and ``write_corpus``.

    ``write_corpus`` leaves two pages: page 0 holds ``HOOKS_BODY`` and a bare
    ``hooks`` reference is ambiguous between pages 0 and 1.
    ``corpus_args`` are the options every command needs; ``tail`` is how the
    scripts echo them in a ``Next:`` line.
    """

    module = None
    script = None
    corpus_args: list
    tail: str

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.write_corpus()

    def run_cmd(self, *args):
        return _loader.run_cli(self.module, [self.script, *args, *self.corpus_args])

    def next_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("Next:")]

    # 1. ambiguous page reference

    def test_ambiguous_reference_prints_a_runnable_command_per_candidate(self):
        code, out, err = self.run_cmd("content", "hooks", "Hook events")
        self.assertEqual(code, 1)
        self.assertIn("Ambiguous", err)
        for idx in (0, 1):
            self.assertRegex(
                err,
                rf"{re.escape(self.script)} content {idx} 'Hook events' {re.escape(self.tail)}",
            )

    def test_ambiguous_reference_keeps_the_query_of_search_content(self):
        code, out, err = self.run_cmd("search-content", "alphaterm beta", "--page-ref", "hooks")
        self.assertEqual(code, 1)
        for idx in (0, 1):
            self.assertIn(
                f"{self.script} search-content 'alphaterm beta' --page-ref {idx} {self.tail}", err)

    def test_ambiguous_reference_in_sections_has_no_heading(self):
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.script} sections 0 {self.tail}", err)

    # 2. heading not found

    def test_missing_heading_lists_close_sections_as_commands_first(self):
        code, out, err = self.run_cmd("content", "0", "Hook events/Pre Tool Use")
        self.assertEqual(code, 1)
        self.assertIn("Closest sections:", err)
        self.assertIn("Available sections:", err)
        self.assertLess(err.index("Closest sections:"), err.index("Available sections:"))
        closest = err[err.index("Closest sections:"):err.index("Available sections:")]
        self.assertRegex(
            closest,
            rf"{re.escape(self.script)} content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}",
        )

    # 3. Next: filled in from the search results

    def test_search_next_line_names_the_top_hit_and_its_heading(self):
        code, out, err = self.run_cmd("search", "pretooluse")
        self.assertEqual(code, 0, err)
        lines = self.next_lines(out)
        self.assertTrue(lines, out)
        self.assertRegex(
            lines[0],
            rf"^Next: {re.escape(self.script)} content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}$",
        )
        self.assertFalse([ln for ln in lines if "<heading_path>" in ln or "<page_ref>" in ln], lines)

    def test_search_content_next_line_is_filled_in_too(self):
        code, out, err = self.run_cmd("search-content", "pretooluse")
        self.assertEqual(code, 0, err)
        lines = self.next_lines(out)
        self.assertRegex(lines[0], rf"content 0 '[^'\n]*PreToolUse' {re.escape(self.tail)}$")
        self.assertLessEqual(len(lines), 3)

    # 4. no hits

    def test_zero_hits_term_not_in_corpus(self):
        code, out, err = self.run_cmd("search-content", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn("not in this corpus", out)
        self.assertIn("rewording", out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)

    def test_zero_hits_some_terms_present_drops_the_absent_ones(self):
        code, out, err = self.run_cmd("search-content", "alphaterm zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 1 of', out)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn(f"Next: {self.script} search-content alphaterm {self.tail}", out)

    def test_zero_hits_terms_never_together_say_so(self):
        code, out, err = self.run_cmd("search-content", "alphaterm betaterm gammaterm")
        self.assertEqual(code, 0, err)
        self.assertIn("No matching content found", out)
        self.assertIn("not together in one section", out)
        self.assertNotIn("not in this corpus", out)
        self.assertIn(f"Next: {self.script} search-content alphaterm {self.tail}", out)

    def test_zero_hits_with_page_ref_says_the_page_cut_it_out(self):
        code, out, err = self.run_cmd("search-content", "alphaterm", "--page-ref", "1")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 1 of', out)
        self.assertIn("limited the search to one page", out)

    def test_smart_search_with_no_result_gets_the_same_diagnosis(self):
        code, out, err = self.run_cmd("search", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn('"zzzmissing": 0 of', out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)


class ClaudeDocsGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = claude
    script = "parse-claude-docs.py"

    def write_corpus(self):
        pages = [
            ("Hooks", "https://example.com/en/cli/hooks", HOOKS_BODY),
            ("Agent SDK Hooks", "https://example.com/en/agent-sdk/hooks", OTHER_BODY),
        ]
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in pages), encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"


class AiSdkGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = ai_sdk
    script = "parse-ai-sdk.py"

    def write_corpus(self):
        pages = [("Hooks", HOOKS_BODY), ("Agent Hooks", OTHER_BODY)]
        Path(self.tmp, "ai-sdk-llms-full.txt").write_text(
            "".join(f"---\ntitle: {t}\ndescription: about {t}\n---\n\n# {t}\n\n{b}\n" for t, b in pages),
            encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"


class FirebaseGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = firebase
    script = "parse-firebase.py"

    def write_corpus(self):
        pages = [
            ("Hooks", "https://firebase.google.com/docs/functions/hooks.md.txt", HOOKS_BODY),
            ("Auth Hooks", "https://firebase.google.com/docs/auth/hooks.md.txt", OTHER_BODY),
        ]
        # the index (all Firebase's smart search ranks on) must name the term
        Path(self.tmp, "firebase-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}, pretooluse\n" for t, u, _b in pages), encoding="utf-8")
        pages_dir = Path(self.tmp, "firebase-docs")
        pages_dir.mkdir()
        for t, u, b in pages:
            (pages_dir / firebase._url_to_cache_filename(u)).write_text(f"# {t}\n\n{b}\n", encoding="utf-8")
        self.corpus_args = ["--cache-dir", self.tmp]
        self.tail = f"--cache-dir {self.tmp}"

    def test_zero_hits_with_page_ref_says_the_page_cut_it_out(self):
        # Pages are fetched lazily: only the --page-ref page is counted.
        code, out, err = self.run_cmd("search-content", "alphaterm", "--page-ref", "1")
        self.assertEqual(code, 0, err)
        self.assertIn('"alphaterm": 0 of 1', out)
        self.assertIn("counts cover only the --page-ref 1 page", out)
        self.assertNotIn("not anywhere", out)

    def test_smart_search_with_no_result_gets_the_same_diagnosis(self):
        # Firebase's smart search ranks on the index alone, so the counts
        # cover title/description, and the page bodies are not fetched.
        code, out, err = self.run_cmd("search", "zzzmissing qqqmissing")
        self.assertEqual(code, 0, err)
        self.assertIn("index entries (title/description)", out)
        self.assertIn('"zzzmissing": 0 of 2', out)
        self.assertIn(f"Next: {self.script} search-index 'zzzmissing qqqmissing' {self.tail}", out)


class GenericGuidanceTest(_GuidanceTests, unittest.TestCase):
    module = generic
    script = "parse-llms-txt.py"

    def write_corpus(self):
        sources = Path(self.tmp, "sources.json")
        sources.write_text(json.dumps({"sources": {"site": {
            "url": "https://example.com/llms-full.txt", "split": "h1"}}}), encoding="utf-8")
        corpus = Path(self.tmp, "full.txt")
        corpus.write_text(f"# Hooks\n\n{HOOKS_BODY}\n# Agent Hooks\n\n{OTHER_BODY}", encoding="utf-8")
        self.corpus_args = ["--source", "site", "--sources-file", str(sources), "--file", str(corpus)]
        self.tail = f"--source site --sources-file {sources} --file {corpus}"


class ClaudeDocsSlugTest(unittest.TestCase):
    """``hooks`` is ``/en/hooks``, not ``/en/agent-sdk/hooks``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        pages = [
            ("Agent SDK Hooks", "https://example.com/docs/en/agent-sdk/hooks", OTHER_BODY),
            ("Hooks", "https://example.com/docs/en/hooks", HOOKS_BODY),
        ]
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "".join(f"- [{t}]({u}): about {t}\n" for t, u, _b in pages), encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "".join(f"# {t}\nSource: {u}\n\n{b}\n" for t, u, b in pages), encoding="utf-8")

    def run_cmd(self, *args):
        return _loader.run_cli(claude, ["parse-claude-docs.py", *args, "--cache-dir", self.tmp])

    def test_bare_slug_resolves_to_the_exact_lang_page_and_names_the_others(self):
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 0, err)
        self.assertIn('Sections in [1] "Hooks"', out)
        self.assertIn("[0] https://example.com/docs/en/agent-sdk/hooks", err)

    def test_longer_slug_still_reaches_the_other_page(self):
        code, out, err = self.run_cmd("sections", "agent-sdk/hooks")
        self.assertEqual(code, 0, err)
        self.assertIn('Sections in [0] "Agent SDK Hooks"', out)

    def test_two_languages_of_the_same_slug_stay_ambiguous(self):
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "# Hooks\nSource: https://example.com/docs/en/hooks\n\nx\n"
            "# Hooks ja\nSource: https://example.com/docs/ja/hooks\n\nx\n", encoding="utf-8")
        code, out, err = self.run_cmd("sections", "hooks")
        self.assertEqual(code, 1)
        self.assertIn("Ambiguous slug 'hooks'", err)
        self.assertIn("parse-claude-docs.py sections 1 --cache-dir", err)


SKILLS_DIR = Path(_loader.SCRIPTS_DIR).parent / "skills"
SKILL_NAMES = ["researching-claude-docs", "researching-ai-sdk", "researching-firebase",
               "researching-library-docs"]


class SkillGuidanceTest(unittest.TestCase):
    """The four SKILL.md say the same thing about how many calls to spend and
    how to read the new error output; only the per-source details differ."""

    def text(self, name):
        return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    def test_every_skill_carries_the_same_rules(self):
        shared = [
            "## 調査の進め方 (手数の目安)",
            "**`search` 1 回 → `content` 1〜2 回**",
            "同じ論点で `search` を **3 回外したら**",
            "**`--max-chars 0`** は、出力に `... (N chars truncated; narrow with ...)` が出て",
            "`| head` / `| grep` で出力を切らず",
            "`already executing in this forked",
            "## Skill を呼べない文脈 (subagent など)",
            "WebFetch する前に、",
            "`${CLAUDE_PLUGIN_ROOT}/scripts/",
        ]
        for name in SKILL_NAMES:
            text = self.text(name)
            for needle in shared:
                with self.subTest(skill=name, needle=needle):
                    self.assertIn(needle, text)

    def test_failure_table_names_what_the_scripts_print(self):
        # The strings the table quotes must be strings the scripts still print.
        printed = "".join(
            (Path(_loader.SCRIPTS_DIR) / f).read_text(encoding="utf-8")
            for f in ("_common.py", "_commands.py", "parse-claude-docs.py", "parse-ai-sdk.py",
                      "parse-firebase.py", "parse-llms-txt.py"))
        for quoted in ("Why nothing matched", "Closest sections:", "Available sections:",
                       "Ambiguous {kind}", "Note: '{page_ref}' also matches"):
            self.assertIn(quoted, printed)
        for name in SKILL_NAMES:
            text = self.text(name)
            for needle in ("Why nothing matched:", "Closest sections:", "Available sections:",
                           "Ambiguous"):
                with self.subTest(skill=name, needle=needle):
                    self.assertIn(needle, text)


class NearHeadingsTest(unittest.TestCase):
    SECTIONS = [
        {"title": t, "heading_path": p, "level": 2}
        for t, p in [
            ("Hook events", "Hook events"),
            ("PreToolUse", "Hook events/PreToolUse"),
            ("PostToolUse", "Hook events/PostToolUse"),
            ("Configuration", "Configuration"),
        ]
    ]

    def paths(self, query):
        return [s["heading_path"] for s in _common.near_headings(query, self.SECTIONS)]

    def test_last_element_is_matched_ignoring_case_and_spacing(self):
        self.assertEqual(self.paths("Events/pre tool use")[0], "Hook events/PreToolUse")
        self.assertEqual(self.paths("CONFIGURATION")[0], "Configuration")

    def test_title_containing_the_guess_ranks_before_a_path_match(self):
        self.assertEqual(self.paths("tooluse")[:2],
                         ["Hook events/PreToolUse", "Hook events/PostToolUse"])

    def test_a_longer_guess_still_finds_the_shorter_title(self):
        self.assertEqual(self.paths("Hook events/PreToolUse hook handler configuration")[:1],
                         ["Hook events/PreToolUse"])

    def test_typo_falls_back_to_closeness(self):
        self.assertEqual(self.paths("Configuraton"), ["Configuration"])

    def test_nothing_close_gives_nothing(self):
        self.assertEqual(self.paths("zzzzzz"), [])

    def test_at_most_five(self):
        many = [{"title": f"Tool {i}", "heading_path": f"Tool {i}", "level": 2} for i in range(9)]
        self.assertEqual(len(_common.near_headings("tool", many)), 5)


class PreferLangExactTest(unittest.TestCase):
    def test_only_a_lang_segment_counts_as_exact(self):
        cands = [(0, "https://x.example/docs/en/agent-sdk/hooks"), (1, "https://x.example/docs/en/hooks")]
        self.assertEqual(_common.prefer_lang_exact(cands, "hooks"), [cands[1]])
        # no language segment: a longer path is not "more exact"
        plain = [(0, "https://x.example/a/hooks"), (1, "https://x.example/b/c/hooks")]
        self.assertEqual(_common.prefer_lang_exact(plain, "hooks"), plain)
        # a directory that merely looks like a two-letter code is not one
        # when it is not followed by exactly the slug
        deeper = [(0, "https://x.example/ai/tools/hooks"), (1, "https://x.example/ai/hooks")]
        self.assertEqual(_common.prefer_lang_exact(deeper, "hooks"), [deeper[1]])


class HitCandidatesTest(unittest.TestCase):
    @staticmethod
    def hits(*paths):
        return {"results": [{"heading_path": p} for p in paths]}

    def test_best_section_of_each_top_page_first(self):
        ranked = [(3, self.hits("A", "A2"), ()), (7, self.hits("B"), ()), (9, self.hits("C"), ())]
        self.assertEqual([(r, h) for r, h, _e in _commands.hit_candidates(ranked)],
                         [(3, "A"), (7, "B"), (9, "C")])

    def test_single_page_fills_with_its_next_sections(self):
        ranked = [(3, self.hits("A", "A2", "A3", "A4"), ())]
        self.assertEqual([h for _r, h, _e in _commands.hit_candidates(ranked)], ["A", "A2", "A3"])

    def test_index_only_page_has_no_heading(self):
        self.assertEqual(_commands.hit_candidates([(4, {"results": []}, ())]), [(4, None, ())])

    def test_index_only_pages_are_dropped_when_a_page_has_body_hits(self):
        ranked = [(4, {"results": []}, ()), (5, self.hits("A"), ())]
        self.assertEqual(_commands.hit_candidates(ranked), [(5, "A", ())])

    def test_per_page_source_is_kept(self):
        ranked = [(1, self.hits("A"), ("--source", "platform"))]
        self.assertEqual(_commands.hit_candidates(ranked), [(1, "A", ("--source", "platform"))])


if __name__ == "__main__":
    unittest.main()
