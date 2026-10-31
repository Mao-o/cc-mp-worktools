"""The ``ai-sdk`` preset on cases the retired ``parse-ai-sdk.py`` tested and
no other suite covers on the generic loader (moved here when its tests were
removed in 0.48.0): which ``---`` lines start a page, how frontmatter values
are read, a page reference holding a quote, and two messages."""

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
