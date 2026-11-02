"""The ``ai-sdk`` preset on cases the retired ``parse-ai-sdk.py`` tested and
no other suite covers on the generic loader (moved here when its tests were
removed in 0.48.0): which ``---`` lines start a page, how frontmatter values
are read, a page reference holding a quote, and two messages."""

import contextlib
import io
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"


class _AiSdk(unittest.TestCase):
    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "ai-sdk-llms-full.txt")

    def write(self, text):
        self.full.write_text(text, encoding="utf-8")

    def run_cmd(self, *args):
        return _loader.run_cli(generic, [SCRIPT, *args, "--source", "ai-sdk",
                                         "--sources-file", self.sources, "--cache-dir", self.tmp])

    def docs(self):
        p = generic._read_sources_file(generic.PRESETS_FILE)["ai-sdk"]
        return generic.split_documents(self.full.read_text(encoding="utf-8").splitlines(keepends=True), p)


class PageBoundaryTest(_AiSdk):
    """A ``---`` pair is a page only when the lines between are frontmatter
    holding a ``title`` (every line YAML-shaped is not enough)."""

    def test_hr_followed_by_kv_like_prose_is_not_a_boundary(self):
        self.write("---\ntitle: streamText\n---\n\n## Options\n\n---\n"
                   "Note: this is prose, not frontmatter\n---\n\n## Options continued\nmore\n")
        docs = self.docs()
        self.assertEqual([d["title"] for d in docs], ["streamText"])
        self.assertIn("## Options continued\n", docs[0]["body_lines"])

    def test_hr_around_yaml_shaped_lines_without_title_is_not_a_boundary(self):
        self.write("---\ntitle: A\n---\n\nbody\n\n---\nkey: value\nother: thing\n---\n\nmore\n")
        docs = self.docs()
        self.assertEqual([d["title"] for d in docs], ["A"])
        self.assertIn("more\n", docs[0]["body_lines"])


class FrontmatterValueTest(_AiSdk):
    """``tags_key`` + ``frontmatter_yaml_values`` as the preset sets them."""

    def test_block_tags_followed_by_another_key(self):
        self.write("---\ntitle: T\ntags:\n  - 'one'\n  - \"two words\"\ndescription: after\n---\n\nbody\n")
        [d] = self.docs()
        self.assertEqual(d["tags"], ["one", "two words"])
        self.assertEqual(d["description"], "after")

    def test_tags_after_a_nested_mapping_are_read(self):
        self.write("---\ntitle: T\nsidebar:\n  label: Side\n  order: 2\ntags: [api]\n---\n\nbody\n")
        [d] = self.docs()
        self.assertEqual(d["tags"], ["api"])

    def test_plain_titles(self):
        for raw, want in (('"Quoted: yes"', "Quoted: yes"), ("Don't stop", "Don't stop"),
                          ("streamText", "streamText")):
            with self.subTest(raw=raw):
                self.write(f"---\ntitle: {raw}\n---\n\nbody\n")
                [d] = self.docs()
                self.assertEqual(d["title"], want)


class EscapedQuoteTitleTest(_AiSdk):
    def test_sections_finds_the_page_by_its_real_title(self):
        self.write('---\ntitle: "React error \\"Maximum update depth exceeded\\""\n---\n\n'
                   "## Cause\ntext\n")
        code, out, err = self.run_cmd("sections", 'React error "Maximum update depth exceeded"')
        self.assertEqual(code, 0, err)
        self.assertIn('"React error "Maximum update depth exceeded""', out.splitlines()[0])


class SlugShadowedByTitleTest(_AiSdk):
    """``acp`` is a substring of the title "ACP (Agent Client Protocol)", and
    the preset tries ``title`` before ``slug``, so the page whose URL ends in
    ``/acp`` is never opened. The result stays; stderr names that page."""

    def docs_of(self, text):
        self.write(text)
        return self.docs()

    CORPUS = (
        '---\ntitle: ACP (Agent Client Protocol)\nurl: "https://ai-sdk.dev/docs/protocols/agent-client"\n---\n\n'
        "## Setup\n\nzzq overview body\n\n"
        '---\ntitle: Agent Client Protocol\nurl: "https://ai-sdk.dev/providers/ai-sdk-harnesses/acp"\n---\n\n'
        "## Setup\n\nzzq harness body\n\n"
        '---\ntitle: Streaming\nurl: "https://ai-sdk.dev/docs/streaming"\n---\n\n'
        "## Setup\n\nzzq streaming body\n"
    )

    def test_the_page_that_ends_in_the_slug_is_named_and_the_result_stays(self):
        self.write(self.CORPUS)
        code, out, err = self.run_cmd("sections", "acp")
        self.assertEqual(code, 0, err)
        self.assertIn("docs/protocols/agent-client", out)  # still the title match
        notes = [ln for ln in err.splitlines() if ln.startswith("Note: 'acp'")]
        self.assertEqual(len(notes), 1, err)
        self.assertIn("[1] https://ai-sdk.dev/providers/ai-sdk-harnesses/acp", notes[0])
        self.assertIn("opened [0]", notes[0])
        self.assertTrue(notes[0].startswith("Note: 'acp' matched the title of [0]"), notes[0])

    def test_the_printed_command_reaches_the_named_page_for_each_subcommand(self):
        self.write(self.CORPUS)
        for cmd in (("sections", "acp"), ("content", "acp", "Setup"),
                    ("search-content", "zzq", "--page-ref", "acp")):
            with self.subTest(cmd=cmd[0]):
                code, out, err = self.run_cmd(*cmd)
                notes = [ln for ln in err.splitlines() if ln.startswith("Note: 'acp'")]
                self.assertEqual(len(notes), 1, err)
                self.assertIn("For [1]: ", notes[0])
                printed = notes[0].split("For [1]: ", 1)[1]
                code, out, err = _loader.run_cli(generic, shlex.split(printed, comments=True))
                self.assertEqual(code, 0, (printed, err))
                self.assertIn("zzq harness body" if cmd[0] != "sections" else "ai-sdk-harnesses/acp", out)

    def test_no_note_when_the_title_match_is_the_slug_page_or_nothing_else_ends_in_it(self):
        self.write(self.CORPUS)
        for ref in ("streaming", "Agent Client Protocol"):
            with self.subTest(ref):
                code, out, err = self.run_cmd("sections", ref)
                self.assertNotIn("Note:", err)
        self.write(self.CORPUS.replace("ai-sdk-harnesses/acp", "ai-sdk-harnesses/other"))
        code, out, err = self.run_cmd("sections", "acp")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Note:", err)

    def test_a_title_page_whose_own_url_ends_in_the_slug_is_noted_too(self):
        # the shape of the real corpus: both pages end in /acp, the title picks one
        self.write(self.CORPUS.replace("docs/protocols/agent-client", "providers/community-providers/acp"))
        code, out, err = self.run_cmd("sections", "acp")
        self.assertEqual(code, 0, err)
        self.assertIn("community-providers/acp", out)
        notes = [ln for ln in err.splitlines() if ln.startswith("Note: 'acp'")]
        self.assertEqual(len(notes), 1, err)
        self.assertIn("[1] https://ai-sdk.dev/providers/ai-sdk-harnesses/acp", notes[0])

    def test_slashes_and_regex_characters_in_the_reference(self):
        # a title can hold "/acp/" or "a.p"; neither is a regex or a path here
        extra = ('---\ntitle: Using /acp/ in prose\nurl: "https://ai-sdk.dev/docs/prose"\n---\n\n## Setup\n\nzzq prose\n\n'
                 '---\ntitle: Using a.p syntax\nurl: "https://ai-sdk.dev/docs/dotted"\n---\n\n## Setup\n\nzzq dotted\n\n')
        self.write(extra + self.CORPUS)
        code, out, err = self.run_cmd("sections", "/acp/")
        self.assertEqual(code, 0, err)
        self.assertIn("docs/prose", out)
        self.assertIn("also ends in it", err)  # the slashes come off the reference
        code, out, err = self.run_cmd("sections", "a.p")
        self.assertEqual(code, 0, err)
        self.assertIn("docs/dotted", out)
        self.assertNotIn("Note:", err)  # "." is literal: no URL ends in "/a.p"

    def test_no_note_when_the_profile_has_no_slug_form(self):
        docs = self.docs_of(self.CORPUS)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            idx = generic._resolve_page_ref(docs, "acp", None, ("int", "title", "url_substring"))
        self.assertEqual(idx, 0)
        self.assertEqual(err.getvalue(), "")

    def test_slug_forms_that_match_no_title_open_the_slug_page_without_a_note(self):
        self.write(self.CORPUS)
        for ref in ("ai-sdk-harnesses/acp", "acp/", "/acp"):
            with self.subTest(ref):
                code, out, err = self.run_cmd("sections", ref)
                # "acp/" and "/acp" are no title substring; the slug form takes them
                self.assertEqual(code, 0, err)
                self.assertIn("ai-sdk-harnesses/acp", out)
                self.assertNotIn("Note:", err)


class MessagesTest(_AiSdk):
    def test_empty_title_values_count_toward_the_untitled_warning(self):
        # 1 page of 5 with an empty ``title:`` is above the preset's 0.1
        pages = ["---\ntitle:\n---\n\n# Fallback\n\nbody\n"]
        pages += [f"---\ntitle: Page {i}\n---\n\nbody\n" for i in range(4)]
        self.write("".join(pages))
        code, out, err = self.run_cmd("fetch-index")
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING: 1/5 documents (20%) have no title", err)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        self.write("---\ntitle: Doc\n---\n\n# Doc\n\nbody\n")
        code, out, err = self.run_cmd("search-index", "totallyabsentkeyword")
        self.assertEqual(code, 0, err)
        self.assertIn("No matching documents found.", out)
        tips = [ln for ln in out.splitlines() if ln.startswith("Tip:")]
        self.assertTrue(any("search-content" in t for t in tips), out)

    def test_missing_file_dies_with_clear_message(self):
        missing = str(Path(self.tmp, "does-not-exist.txt"))
        code, out, err = _loader.run_cli(generic, [SCRIPT, "sections", "0", "--source", "ai-sdk",
                                                   "--sources-file", self.sources, "--file", missing])
        self.assertEqual(code, 1, err)
        self.assertIn(f"--file '{missing}' does not exist", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
