"""The ``claude-code`` / ``claude-platform`` presets on cases the retired
``parse-claude-docs.py`` tested and no other suite covers on the generic
loader (moved here when its tests were removed in 0.48.0): page boundaries,
messages and exit codes, and two options."""

import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
HOST = "https://example.com/docs/en"


def _preset(name):
    return generic._read_sources_file(generic.PRESETS_FILE)[name]


def _split(text, name):
    return generic.split_documents(text.splitlines(keepends=True), _preset(name))


class PageBoundaryTest(unittest.TestCase):
    def test_h1_inside_fence_is_not_a_boundary(self):
        docs = _split(f"# Hooks\nSource: {HOST}/hooks\n\n```bash\n# not a real heading\necho hi\n```\n\n"
                      f"# Skills\nSource: {HOST}/skills\n\nbody\n", "claude-code")
        self.assertEqual([d["title"] for d in docs], ["Hooks", "Skills"])
        self.assertIn("# not a real heading\n", docs[0]["body_lines"])

    def test_unclosed_fence_recovers_before_the_next_page(self):
        docs = _split(f"# Hooks\nSource: {HOST}/hooks\n\n```\n```ts\nconst x = 1;\n```\n\n"
                      f"# Skills\nSource: {HOST}/skills\n\nbody\n", "claude-code")
        self.assertEqual([d["title"] for d in docs], ["Hooks", "Skills"])

    def test_h1_corpus_with_frontmatter_example_stays_in_h1_mode(self):
        docs = _split(f"# Skills\nSource: https://example.com/skills\n\nintro\n\n---\ntitle: Example\n"
                      f"url: https://example.com/not-a-page\n---\n\n# Hooks\nSource: https://example.com/hooks\n\n"
                      "body\n", "claude-code")
        self.assertEqual([d["title"] for d in docs], ["Skills", "Hooks"])
        self.assertEqual(docs[0]["url"], "https://example.com/skills")

    def test_hr_and_unfenced_h1_inside_a_frontmatter_body_are_not_boundaries(self):
        docs = _split("---\ntitle: Documentation\nurl: https://platform.example.com/docs/en/home\n---\n\nhome\n\n"
                      "---\ntitle: \"Tutorial: Build an agent\"\nurl: https://platform.example.com/docs/en/tut\n---\n\n"
                      "Text with a horizontal rule below.\n\n---\n\n# not a page boundary\nmore\n",
                      "claude-platform")
        self.assertEqual([d["title"] for d in docs], ["Documentation", "Tutorial: Build an agent"])
        body = "".join(docs[1]["body_lines"])
        self.assertIn("horizontal rule below.\n\n---\n", body)
        self.assertIn("# not a page boundary\n", body)

    def test_frontmatter_without_url_is_not_a_platform_boundary(self):
        # a YAML sample outside a code fence has the frontmatter shape and a
        # ``title:``, but no ``url:``: every claude-platform page carries one
        sample = "---\ntitle: just a yaml sample\nname: foo\n---\n"
        docs = _split("---\ntitle: Documentation\nurl: https://example.com/a\n---\nbody\n"
                      f"{sample}more\n"
                      "---\ntitle: Next page\nurl: https://example.com/b\n---\nnext\n",
                      "claude-platform")
        self.assertEqual([(d["title"], d["url"]) for d in docs],
                         [("Documentation", "https://example.com/a"), ("Next page", "https://example.com/b")])
        self.assertEqual("".join(docs[0]["body_lines"]), f"body\n{sample}more\n")


class _Cli(unittest.TestCase):
    PAGES = [("Hooks", "hooks", "## Matchers\nmatcher text\n"),
             ("Widget One", "w1", "## A\nwidget one\n"),
             ("Widget Two", "w2", "## A\nwidget two\n"),
             ("Widget Three", "w3", "## A\nwidget three\n")]
    PREFIX = "claude-code"

    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.write_index("".join(f"- [{t}]({HOST}/{s}): about {t}\n" for t, s, _b in self.PAGES))
        Path(self.tmp, f"{self.PREFIX}-llms-full.txt").write_text(
            "".join(f"# {t}\nSource: {HOST}/{s}\n\n{b}\n" for t, s, b in self.PAGES), encoding="utf-8")

    def write_index(self, text):
        Path(self.tmp, f"{self.PREFIX}-llms.txt").write_text(text, encoding="utf-8")

    def run_cmd(self, *args, source="claude-code"):
        return _loader.run_cli(generic, [SCRIPT, *args, "--source", source,
                                         "--sources-file", self.sources, "--cache-dir", self.tmp])


class CliTest(_Cli):
    def test_default_top_n_keeps_every_candidate_of_three(self):
        code, out, err = self.run_cmd("search", "widget")
        self.assertEqual(code, 0, err)
        for title in ("Widget One", "Widget Two", "Widget Three"):
            self.assertIn(f"] {title}", out)

    def test_malformed_index_exits_2(self):
        self.write_index("not a valid llms.txt line at all\n")
        code, out, err = self.run_cmd("fetch-index")
        self.assertEqual(code, 2, err)
        self.assertIn("format may have changed", err)

    def test_search_index_zero_result_tip_mentions_search_content(self):
        code, out, err = self.run_cmd("search-index", "totallyabsentkeyword")
        self.assertEqual(code, 0, err)
        tips = [ln for ln in out.splitlines() if ln.startswith("Tip:")]
        self.assertTrue(any("search-content" in t for t in tips), out)

    def test_missing_file_dies_with_clear_message(self):
        missing = str(Path(self.tmp, "does-not-exist.txt"))
        code, out, err = _loader.run_cli(generic, [SCRIPT, "sections", "0", "--source", "claude-code",
                                                   "--sources-file", self.sources, "--file", missing])
        self.assertEqual(code, 1, err)
        self.assertIn(f"--file '{missing}' does not exist", err)

    def test_argparse_errors_exit_2(self):
        for argv in (["search-index", "--source", "claude-code"], ["not-a-real-subcommand"]):
            with self.subTest(argv=argv):
                code, _out, _err = _loader.run_cli(generic, [SCRIPT, *argv])
                self.assertEqual(code, 2)

    def test_max_chars_zero_disables_truncation(self):
        line = "0123456789 " * 5
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            f"# Hooks\nSource: {HOST}/hooks\n\n{line}\n", encoding="utf-8")
        code, out, err = self.run_cmd("content", "0", "--max-chars", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("chars truncated", out)
        self.assertIn(line, out)
        code, out, err = self.run_cmd("content", "0", "--max-chars", "20")
        self.assertIn("chars truncated", out)


class PlatformHintTest(_Cli):
    PREFIX = "claude-platform"

    def setUp(self):
        super().setUp()
        Path(self.tmp, "claude-platform-llms-full.txt").write_text(
            "".join(f"---\ntitle: {t}\nurl: {HOST}/{s}\n---\n\n{b}\n" for t, s, b in self.PAGES),
            encoding="utf-8")

    def test_search_index_hint_keeps_source_cache_dir_and_max_age(self):
        code, out, err = _loader.run_cli(generic, [
            SCRIPT, "search-index", "widget", "--source", "claude-platform",
            "--sources-file", self.sources, "--cache-dir", self.tmp, "--max-age", "100000"])
        self.assertEqual(code, 0, err)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertTrue(nexts, out)
        for line in nexts:
            argv = shlex.split(line, comments=True)
            with self.subTest(line=line):
                self.assertEqual(argv[argv.index("--source") + 1], "claude-platform")
                self.assertEqual(argv[argv.index("--cache-dir") + 1], self.tmp)
                self.assertEqual(argv[argv.index("--max-age") + 1], "100000")


if __name__ == "__main__":
    unittest.main()
