"""子見出しの無い大きな節を ``content`` で切り詰めたときの案内。

見出しでは絞れない節 (子見出しが無い) に ``narrow with content N
"<heading_path>"`` と案内しても行き止まりになる。代わりに、本文内を検索する
``Next:`` 行を出す。その行は、出力されたまま (加工せず) shell の規則で分割して
実行でき、名前どおりのページに届く。オプションの軸 (``--cache-dir`` /
``--file``) は両方回す。
"""

import json
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

claude = _loader.load_script("parse-claude-docs.py")
ai_sdk = _loader.load_script("parse-ai-sdk.py")
firebase = _loader.load_script("parse-firebase.py")
generic = _loader.load_script("parse-llms-txt.py")

LONG_ROW = "| `SOME_VARIABLE_{i}` | " + ("filler text " * 30) + "|\n"
NEEDLE_ROW = "| `NEEDLE_VARIABLE` | the row that the reader wants |\n"


def _big_section() -> str:
    return "".join(LONG_ROW.format(i=i) for i in range(40)) + NEEDLE_ROW


class _NarrowBase:
    script: str
    module = None
    heading: str          # heading_path of the childless section
    # corpus args to try, as (label, builder(tmp) -> list)
    page_title = "Env vars"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.write_corpus()

    def run_cli(self, *args):
        return _loader.run_cli(self.module, [self.script, *args])

    def next_lines(self, out):
        return [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]

    def check(self, corpus_args):
        code, out, err = self.run_cli("content", "0", self.heading, *corpus_args,
                                      "--max-chars", "3000")
        self.assertEqual(code, 0, err)
        self.assertIn("chars truncated", out)
        self.assertNotIn("narrow with", out)
        nexts = self.next_lines(out)
        self.assertEqual(len(nexts), 1, out)
        # 出力された行を加工せずに実行する
        argv = shlex.split(nexts[0], comments=True)
        self.assertEqual(argv[:2], [self.script, "search-content"], nexts[0])
        self.assertEqual(argv[argv.index("--page-ref") + 1], "0")
        self.assertIn("--context", argv)
        self.assertEqual(argv[argv.index("--context") + 1], "0")
        code, got, err = _loader.run_cli(self.module, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(f"[0] {self.page_title}", got)
        self.assertIn("→ ", got)
        # 利用者が keyword を差し替えると、長い表の行の後ろの一致行に届く
        swapped = [argv[0], argv[1], "NEEDLE_VARIABLE", *argv[3:]]
        code, got, err = _loader.run_cli(self.module, swapped)
        self.assertEqual(code, 0, err)
        self.assertIn("→ | `NEEDLE_VARIABLE` | the row that the reader wants |", got)


class ClaudeDocsNarrowTest(_NarrowBase, unittest.TestCase):
    script = "parse-claude-docs.py"
    module = claude
    heading = "Variables"

    def write_corpus(self):
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Env vars](https://example.com/docs/en/env): about env\n", encoding="utf-8")
        self.full = Path(self.tmp, "claude-code-llms-full.txt")
        self.full.write_text(
            "# Env vars\nSource: https://example.com/docs/en/env\n\n"
            "## Variables\n" + _big_section(), encoding="utf-8")

    def test_cache_dir(self):
        self.check(["--cache-dir", self.tmp])

    def test_file(self):
        self.check(["--file", str(self.full), "--cache-dir", self.tmp])


class AiSdkNarrowTest(_NarrowBase, unittest.TestCase):
    script = "parse-ai-sdk.py"
    module = ai_sdk
    heading = "Env vars/Variables"

    def write_corpus(self):
        self.full = Path(self.tmp, "ai-sdk-llms-full.txt")
        self.full.write_text(
            "---\ntitle: Env vars\ndescription: about env\n---\n\n"
            "# Env vars\n\n## Variables\n" + _big_section(), encoding="utf-8")

    def test_cache_dir(self):
        self.check(["--cache-dir", self.tmp])

    def test_file(self):
        self.check(["--file", str(self.full), "--cache-dir", self.tmp])


class FirebaseNarrowTest(_NarrowBase, unittest.TestCase):
    script = "parse-firebase.py"
    module = firebase
    heading = "Variables"

    def write_corpus(self):
        url = "https://firebase.google.com/docs/env.md.txt"
        Path(self.tmp, "firebase-llms.txt").write_text(
            f"- [Env vars]({url}): about env\n", encoding="utf-8")
        pages = Path(self.tmp, "firebase-docs")
        pages.mkdir()
        (pages / firebase._url_to_cache_filename(url)).write_text(
            "# Env vars\n\n## Variables\n" + _big_section(), encoding="utf-8")

    def test_cache_dir(self):
        self.check(["--cache-dir", self.tmp])


class GenericLlmsTxtNarrowTest(_NarrowBase, unittest.TestCase):
    script = "parse-llms-txt.py"
    module = generic
    heading = "Variables"

    def write_corpus(self):
        self.full = Path(self.tmp, "plain.txt")
        self.full.write_text(
            "# Env vars\n\n## Variables\n" + _big_section(), encoding="utf-8")
        self.sources = Path(self.tmp, "sources.json")
        self.sources.write_text(json.dumps({"sources": {"plain": {
            "url": "https://example.com/llms-full.txt", "split": "h1"}}}), encoding="utf-8")

    def corpus(self, *extra):
        return ["--source", "plain", "--sources-file", str(self.sources), *extra]

    def test_file(self):
        self.check(self.corpus("--file", str(self.full)))


class SectionWithSubsectionsKeepsHeadingHintTest(unittest.TestCase):
    """子見出しのある節は、従来どおり子見出しへ絞る案内のまま。"""

    def test_parent_section_still_points_at_content(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Path(tmp, "ai-sdk-llms-full.txt").write_text(
            "---\ntitle: Env vars\n---\n\n# Env vars\n\n## Parent\n"
            + _big_section() + "### Child\ntext\n", encoding="utf-8")
        code, out, err = _loader.run_cli(ai_sdk, [
            "parse-ai-sdk.py", "content", "0", "Env vars/Parent",
            "--cache-dir", tmp, "--max-chars", "3000"])
        self.assertEqual(code, 0, err)
        self.assertIn('narrow with parse-ai-sdk.py content 0 "<heading_path>"', out)
        self.assertNotIn("search-content", out)


if __name__ == "__main__":
    unittest.main()
