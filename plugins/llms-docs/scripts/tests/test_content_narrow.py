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
        # 注記は、差し替える仮置きの語 (節の見出し) を名指しする
        self.assertIn("this section has no subsections to narrow to", out)
        self.assertIn("run the command below with 'variables' (the section heading, "
                      "a stand-in) replaced by the term you are looking for:", out)
        nexts = self.next_lines(out)
        self.assertEqual(len(nexts), 1, out)
        # 出力された行を加工せずに実行する
        argv = shlex.split(nexts[0], comments=True)
        self.assertEqual(argv[:2], [self.script, "search-content"], nexts[0])
        # the stand-in keyword is always last, after "--"
        self.assertEqual(argv[-2:], ["--", "variables"], nexts[0])
        self.assertEqual(argv[argv.index("--page-ref") + 1], "0")
        self.assertIn("--context", argv)
        self.assertEqual(argv[argv.index("--context") + 1], "0")
        code, got, err = _loader.run_cli(self.module, argv)
        self.assertEqual(code, 0, err)
        self.assertIn(f"[0] {self.page_title}", got)
        self.assertIn("→ ", got)
        # 利用者が keyword を差し替えると、長い表の行の後ろの一致行に届く
        swapped = argv[:-1] + ["NEEDLE_VARIABLE"]
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



# AI SDK の実 corpus から切り出した 1 ページ (タイトルは frontmatter にあり、本文に H1 が無い)
AI_SDK_PAGE_WITHOUT_H1 = """---
title: Azure OpenAI Slow to Stream
description: Learn to troubleshoot Azure OpenAI slow to stream issues.
url: "https://ai-sdk.dev/docs/troubleshooting/azure-stream-slow"
docs_index: /llms.txt
---

> For an index of all documentation, see [/llms.txt](/llms.txt).

## Issue

When using OpenAI hosted on Azure, streaming is slow and in big chunks.

## Cause

This is a Microsoft Azure issue. Some users have reported the following solutions:

- **Update Content Filtering Settings**:
  Inside [Azure AI Studio](https://ai.azure.com/), within "Shared resources" > "Content filters", create a new
  content filter and set the "Streaming mode (Preview)" under "Output filter" from "Default"
  to "Asynchronous Filter".

## Solution

You can use the [`smoothStream` transformation](/docs/ai-sdk-core/generating-text#smoothing-streams) to stream each word individually.

```tsx {6}
import { smoothStream, streamText } from 'ai';

const result = streamText({
  model,
  prompt,
  experimental_transform: smoothStream(),
});
```

---

For a semantic overview of all documentation, see [/sitemap.md](/sitemap.md)

For an index of all available documentation, see [/llms.txt](/llms.txt)

For agent-facing discovery, including API and MCP surfaces, see [/agents.md](/agents.md)

"""

AI_SDK_PAGE_WITHOUT_HEADINGS = (
    "---\ntitle: Getting Started\ndescription: no headings\n---\n\n"
    + "".join(f"Paragraph {i} about getting started with the SDK.\n" for i in range(40))
)


class AiSdkPageWithoutH1Test(unittest.TestCase):
    """AI SDK のページは本文に H1 が無く、見出しは H2 から始まる。ページ全体を
    ``content`` で読むとき、H2 を子として扱う (子見出しが無いと誤判定しない)。
    子見出しが無いのは、見出しが 1 つも無いページだけ。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "ai-sdk-llms-full.txt")
        self.full.write_text(AI_SDK_PAGE_WITHOUT_H1, encoding="utf-8")

    def corpora(self):
        return [["--cache-dir", self.tmp], ["--file", str(self.full), "--cache-dir", self.tmp]]

    def test_whole_page_truncation_keeps_the_heading_hint(self):
        for corpus in self.corpora():
            with self.subTest(corpus=corpus[0]):
                code, out, err = _loader.run_cli(ai_sdk, [
                    "parse-ai-sdk.py", "content", "0", *corpus, "--max-chars", "300"])
                self.assertEqual(code, 0, err)
                self.assertIn("chars truncated; narrow with parse-ai-sdk.py content 0", out)
                self.assertNotIn("search-content", out)

    def test_top_level_hints_list_the_h2s_and_each_reads_its_section(self):
        for corpus in self.corpora():
            with self.subTest(corpus=corpus[0]):
                code, out, err = _loader.run_cli(ai_sdk, [
                    "parse-ai-sdk.py", "content", "0", *corpus])
                self.assertEqual(code, 0, err)
                self.assertIn("--- Top-level sections (3) ---", out)
                listed = [ln[len("  - "):] for ln in out.splitlines() if ln.startswith("  - ")]
                self.assertEqual(listed[:3], ["Issue", "Cause", "Solution [code]"])
                # 一覧の見出しで、その節に届く
                code, got, err = _loader.run_cli(ai_sdk, [
                    "parse-ai-sdk.py", "content", "0", "Issue", *corpus])
                self.assertEqual(code, 0, err)
                self.assertIn("streaming is slow and in big chunks", got)
                self.assertNotIn("Content Filtering", got)

    def test_page_without_headings_points_at_search(self):
        Path(self.tmp, "ai-sdk-llms-full.txt").write_text(
            AI_SDK_PAGE_WITHOUT_HEADINGS, encoding="utf-8")
        code, out, err = _loader.run_cli(ai_sdk, [
            "parse-ai-sdk.py", "content", "0", "--cache-dir", self.tmp, "--max-chars", "300"])
        self.assertEqual(code, 0, err)
        self.assertIn("this page has no headings to narrow to", out)
        self.assertIn("'getting started' (the page title, a stand-in)", out)
        nexts = [ln[len("Next: "):] for ln in out.splitlines() if ln.startswith("Next: ")]
        self.assertEqual(len(nexts), 1, out)
        code, got, err = _loader.run_cli(ai_sdk, shlex.split(nexts[0], comments=True))
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Getting Started", got)
        self.assertIn("→ Paragraph 0 about getting started", got)


class SkippedLevelChildrenTest(unittest.TestCase):
    """段を飛ばした節 (H2 の下に H4 だけ) も、その H4 を子として扱う。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        Path(self.tmp, "claude-code-llms.txt").write_text(
            "- [Env vars](https://example.com/docs/en/env): about env\n", encoding="utf-8")
        Path(self.tmp, "claude-code-llms-full.txt").write_text(
            "# Env vars\nSource: https://example.com/docs/en/env\n\n"
            "## Parent\n" + _big_section() + "#### Deep\ntext under deep\n"
            "## Other\nother text\n", encoding="utf-8")

    def test_h4_under_h2_is_a_child(self):
        code, out, err = _loader.run_cli(claude, [
            "parse-claude-docs.py", "content", "0", "Parent", "--cache-dir", self.tmp,
            "--max-chars", "3000"])
        self.assertEqual(code, 0, err)
        self.assertIn("--- Subsections of 'Parent' (1) ---", out)
        self.assertIn("  - Parent/Deep", out)
        self.assertIn('narrow with parse-claude-docs.py content 0 "<heading_path>"', out)
        self.assertNotIn("search-content", out)
        code, got, err = _loader.run_cli(claude, [
            "parse-claude-docs.py", "content", "0", "Parent/Deep", "--cache-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertIn("text under deep", got)


class AdversarialHeadingRoundTripTest(unittest.TestCase):
    """案内の keyword は見出しから作る。見出しが ``-`` で始まる、``\\_`` の
    エスケープを含む、機能語だけ、記号だけのときも、出力された行を加工せずに
    実行でき、その節に届く。注記は仮置きの語を名指しする。"""

    # (見出し, 案内に入る keyword)
    CASES = [
        ("--bg", "--bg"),
        ("--help", "--help"),
        ("--persist", "--persist"),
        ("max\\_tokens", "max\\_tokens"),
        ("How to", "how to"),
        ("()", "()"),
    ]

    def write_corpus(self, tmp, heading):
        Path(tmp, "claude-code-llms.txt").write_text(
            "- [Env vars](https://example.com/docs/en/env): about env\n", encoding="utf-8")
        full = Path(tmp, "claude-code-llms-full.txt")
        full.write_text(
            "# Env vars\nSource: https://example.com/docs/en/env\n\n"
            "## Intro\nintro text\n\n"
            f"## {heading}\n" + _big_section()
            + f"The row about {heading} is here.\n", encoding="utf-8")
        return full

    def test_next_line_runs_as_printed_and_reaches_the_section(self):
        for heading, keyword in self.CASES:
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            full = self.write_corpus(tmp, heading)
            for corpus in (["--cache-dir", tmp], ["--file", str(full), "--cache-dir", tmp]):
                with self.subTest(heading=heading, corpus=corpus[0]):
                    code, out, err = _loader.run_cli(claude, [
                        "parse-claude-docs.py", "content", "0", *corpus,
                        "--max-chars", "3000", "--", heading])
                    self.assertEqual(code, 0, err)
                    self.assertIn(f"run the command below with '{keyword}' "
                                  f"(the section heading, a stand-in) replaced by", out)
                    nexts = [ln[len("Next: "):] for ln in out.splitlines()
                             if ln.startswith("Next: ")]
                    self.assertEqual(len(nexts), 1, out)
                    argv = shlex.split(nexts[0], comments=True)
                    self.assertEqual(argv[-2:], ["--", keyword], nexts[0])
                    code, got, err = _loader.run_cli(claude, argv)
                    self.assertEqual(code, 0, err)
                    self.assertIn("[0] Env vars", got)
                    self.assertIn(f"Section: {heading}  (", got)
                    self.assertIn(f"→ The row about {heading} is here.", got)
                    # replaced as the note says, with an option name
                    swapped = argv[:-1] + ["--from-env"]
                    code, got, err = _loader.run_cli(claude, swapped)
                    self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
