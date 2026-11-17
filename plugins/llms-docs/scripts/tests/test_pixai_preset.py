"""The ``pixai`` preset: the PixAI API docs (``platform.pixai.art``).

One ``llms-full.txt`` holds the same 39 pages in English, Japanese and Korean,
one language after another. It is split on H1; the file has no page URL, so
the English pages get theirs from ``llms.txt`` by title. A prose page writes
its headings as a line ``Title [#id]`` with no ``#`` (indented inside a Steps
block), which ``heading_line_pattern`` reads as ``## Title``; an API reference
page uses real ``##`` / ``###`` headings.

The fixtures are cut from the real files (2026-11-17): English and Japanese
pages, H1-shaped comments inside a code block, the ``[#id]`` lines, and
``Webhook``, a page with the same title in every language. Every printed
``Next:`` line is run as the shell splits it, on a fetch through a mocked
network, on the cache, and on ``--file`` (with and without ``--index-file``).
"""

import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader
from test_page_fetch import _Net

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"
FULL_URL = "https://platform.pixai.art/llms-full.txt"
INDEX_URL = "https://platform.pixai.art/llms.txt"
PATTERN = r"^\s*(.+?) \[#[^\]\s]+\]\s*$"
SKILL = Path(_loader.SCRIPTS_DIR).parent / "skills" / "researching-library-docs" / "SKILL.md"


def preset() -> dict:
    # read when a test asks: a preset refused by validation fails the tests
    # that use it, not the import of this module (a missing one fails them too)
    presets = g._read_sources_file(g.PRESETS_FILE)
    if "pixai" not in presets:
        raise AssertionError("presets.json has no pixai")
    return presets["pixai"]


FULL = """\
# Docs MCP server

Give Claude Code, Cursor, VS Code or any MCP client search and read access to this documentation through the docs MCP server at https://platform.pixai.art/docs-mcp. It serves the docs only; it does not generate images.



* **URL:** `https://platform.pixai.art/docs-mcp`
* **Transport:** Streamable HTTP

Connect your editor or agent [#connect-your-editor-or-agent]

Claude Code [#claude-code]

```bash
claude mcp add --transport http pixai-docs https://platform.pixai.art/docs-mcp
```

Cursor [#cursor]

Add the server to `~/.cursor/mcp.json`, or to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "pixai-docs": {
      "url": "https://platform.pixai.art/docs-mcp"
    }
  }
}
```


# Your First API Call

Submit a task, poll its status and download the image, step by step, to make your first PixAI API request.

Steps [#steps]

<div className="fd-steps">
  <div className="fd-step">
    Submit a Task [#submit-a-task-step]

    Send a `POST /image/create`. The required fields are `modelVersionId` and `prompt`.

    ```bash
    curl -X POST https://api.pixai.art/v2/image/create -H "Authorization: Bearer $PIXAI_API_KEY" -d '{"modelVersionId": "2024383379556065549"}'
    ```
  </div>

  <div className="fd-step">
    Poll for Status [#poll-for-status-step]

    Repeat until `status` reaches a terminal state. Right after the task is created, this can answer `404` for a moment; keep polling.
  </div>
</div>

All steps in one script [#all-steps-in-one-script]

Create, poll and save in Python, with `requests` installed and your key in `PIXAI_API_KEY`:

```python
API = "https://api.pixai.art/v2"
```


# Webhook

Get a POST request when a PixAI API task starts, completes, is cancelled or fails, through webhooks on your profile or a per-request callbackUrl.



If you are using `callbackUrl` in the `REST` API, the body format follows this format:

```json
{
    "action": "task_running"
}
```


# Create a generation task

Submit an asynchronous image generation task with the v1 parameters. The response returns the task ID with a waiting status; fetch the task to read its outputs.



## POST https://api.pixai.art/v1/task

### Request body (`application/json`)

- `parameters` (object, required)
  The schema fully defines the parameters allowed for the image generation tasks we currently support.


# Get an upload URL for an image

Step 1 of uploading an image you want to use as an input, for example a `contextImages` entry, a video frame or an inpaint base image.



## POST https://api.pixai.art/v2/media/image/upload-url

### Example (cURL)

```bash
# 1. Get an upload URL
UPLOAD=$(curl -s -X POST https://api.pixai.art/v2/media/image/upload-url -H "Content-Type: application/json" -d '{}')

# 2. Upload the bytes
curl -X PUT "$URL" -H "Content-Type: image/png" --data-binary @input.png

# 3. Register it and read the media ID
curl -s -X POST https://api.pixai.art/v2/media/image/register -H "Content-Type: application/json"
```

### Responses

- `200`
  The upload target.


# ドキュメント MCP サーバー

Claude Code、Cursor、VS Code などの MCP クライアントをドキュメント MCP サーバー (https://platform.pixai.art/docs-mcp) に接続すると、AI エージェントがこのドキュメントを検索・参照できます。画像の生成は行いません。

エディターやエージェントに接続する [#エディターやエージェントに接続する]

Claude Code [#claude-code]

```bash
claude mcp add --transport http pixai-docs https://platform.pixai.art/docs-mcp
```


# 最初のAPIリクエスト

タスクの送信、ステータスの確認、画像のダウンロードまで、PixAI API への最初のリクエストをステップごとに説明します。

手順 [#手順]

<div className="fd-steps">
  <div className="fd-step">
    タスクを送信する [#タスクを送信する-step]

    `POST /image/create` を送信します。必須フィールドは `modelVersionId` と `prompt` です。
  </div>
</div>


# Webhook

PixAI API のタスクが開始・完了・キャンセル・失敗したときに POST リクエストで通知を受け取れます。プロフィールの Webhook またはリクエストごとの callbackUrl で設定します。


# 画像のアップロード URL の取得

入力として使う画像（`contextImages` の要素、動画のフレーム、インペイントのベース画像など）をアップロードする手順のステップ 1 です。



## POST https://api.pixai.art/v2/media/image/upload-url

### Example (cURL)

```bash
# 1. アップロード URL を取得
UPLOAD=$(curl -s -X POST https://api.pixai.art/v2/media/image/upload-url -H "Content-Type: application/json" -d '{}')
```
"""

INDEX = """\
# PixAI Platform

> Developer documentation for the PixAI API, a REST API that generates anime-style AI images and videos with the same models that power pixai.art.

Coding agents can also search and read these docs through a docs-only MCP server at `https://platform.pixai.art/docs-mcp` (Streamable HTTP, no authentication); setup: [Docs MCP server](https://platform.pixai.art/en/docs/docs-mcp).

The sections below list the English pages. The docs are also available in other languages; their indexes are listed under Optional.

## Documentation

- [Docs MCP server](https://platform.pixai.art/en/docs/docs-mcp): Give Claude Code, Cursor, VS Code or any MCP client search and read access to this documentation through the docs MCP server at https://platform.pixai.art/docs-mcp. It serves the docs only; it does not generate images.

## Quick Start

- [Your First API Call](https://platform.pixai.art/en/docs/quick-start/first-api-call): Submit a task, poll its status and download the image, step by step, to make your first PixAI API request.

### Media

- [Get an upload URL for an image](https://platform.pixai.art/en/docs/api-v2/media/getImageUploadUrl): Step 1 of uploading an image you want to use as an input, for example a contextImages entry, a video frame or an inpaint base image.

## Resources

- [Webhook](https://platform.pixai.art/en/docs/references/webhook): Get a POST request when a PixAI API task starts, completes, is cancelled or fails, through webhooks on your profile or a per-request callbackUrl.

## OpenAPI specs

- [API v2 spec](https://platform.pixai.art/openapi-spec.v2.yaml): image and video generation, inpainting, uploads, tasks and model lookups, with every request field and error code. The English spec is also served as [/openapi.json](https://platform.pixai.art/openapi.json).
- [API v1 spec](https://platform.pixai.art/openapi-spec.yaml): task status, media lookup and media upload.

## Optional

- [Japanese index](https://platform.pixai.art/ja/llms.txt): This index for the Japanese pages.
- [Korean index](https://platform.pixai.art/ko/llms.txt): This index for the Korean pages.
- [Full documentation text](https://platform.pixai.art/llms-full.txt): Every docs page, in every language, as Markdown in one file.
"""

TITLES = ["Docs MCP server", "Your First API Call", "Webhook", "Create a generation task",
          "Get an upload URL for an image", "ドキュメント MCP サーバー", "最初のAPIリクエスト", "Webhook",
          "画像のアップロード URL の取得"]
URLS = ["https://platform.pixai.art/en/docs/docs-mcp", "https://platform.pixai.art/en/docs/quick-start/first-api-call",
        "", "", "https://platform.pixai.art/en/docs/api-v2/media/getImageUploadUrl", "", "", "", ""]
# page -> its ``## `` lines: the [#id] lines read as H2, and the real ones
H2 = {
    0: ["Connect your editor or agent", "Claude Code", "Cursor"],
    1: ["Steps", "Submit a Task", "Poll for Status", "All steps in one script"],
    2: [],
    3: ["POST https://api.pixai.art/v1/task"],
    4: ["POST https://api.pixai.art/v2/media/image/upload-url"],
    5: ["エディターやエージェントに接続する", "Claude Code"],
    6: ["手順", "タスクを送信する"],
    7: [],
    8: ["POST https://api.pixai.art/v2/media/image/upload-url"],
}
# the ``sections`` rows of each page
SECTIONS = {
    0: ["[L2] Connect your editor or agent", "[L2] Claude Code [code]", "[L2] Cursor [code]"],
    1: ["[L2] Steps", "[L2] Submit a Task [code]", "[L2] Poll for Status", "[L2] All steps in one script [code]"],
    4: ["[L2] POST https://api.pixai.art/v2/media/image/upload-url",
        "  [L3] POST https://api.pixai.art/v2/media/image/upload-url/Example (cURL) [code]",
        "  [L3] POST https://api.pixai.art/v2/media/image/upload-url/Responses"],
    5: ["[L2] エディターやエージェントに接続する", "[L2] Claude Code [code]"],
    6: ["[L2] 手順", "[L2] タスクを送信する"],
}


def _lines(text: str) -> list:
    return text.splitlines(keepends=True)


def _split(text: str = FULL, **change) -> list:
    profile = {**preset(), **change}
    return g.split_documents(_lines(text), profile)


def _h2(doc: dict) -> list:
    return [ln[3:].rstrip("\n") for ln in doc["body_lines"] if ln.startswith("## ")]


class PresetTest(unittest.TestCase):
    def test_the_profile(self):
        p = preset()
        self.assertEqual((p["url"], p["split"], p["index_url"]), (FULL_URL, "h1", INDEX_URL))
        self.assertIsNotNone(p["heading_line_pattern"])
        self.assertEqual(p["heading_line_pattern"].pattern, PATTERN)
        # no URL line in the file, no H1 inside a page body, no page without a body
        self.assertIsNone(p["page_url"])
        self.assertFalse(p["h1_needs_url"])
        self.assertFalse(p["skip_empty"])

    def test_left_out_on_purpose(self):
        # ``rank_from: index`` ranks the llms.txt entries (the English pages, by
        # their descriptions), and the Webhook entry joins no page (three pages
        # share its title). ``anchor_style`` would make anchors from the titles,
        # and 76 of the 198 ``[#id]`` lines of the real file carry another id
        # (``#submit-a-task-step``).
        p = preset()
        self.assertEqual(p["rank_from"], "pages")
        self.assertIsNone(p["anchor_style"])


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.docs = _split()

    def page(self, n: int) -> dict:
        self.assertLess(n, len(self.docs), [d["title"] for d in self.docs])
        return self.docs[n]

    def test_pages_and_titles(self):
        self.assertEqual([d["title"] for d in self.docs], TITLES)

    def test_h1_comments_in_a_code_block_stay_in_their_page(self):
        for page, comments in ((4, ["# 1. Get an upload URL\n", "# 2. Upload the bytes\n",
                                    "# 3. Register it and read the media ID\n"]),
                               (8, ["# 1. アップロード URL を取得\n"])):
            with self.subTest(page=page):
                body = self.page(page)["body_lines"]
                for comment in comments:
                    self.assertIn(comment, body)
                self.assertIn("```bash\n", body)
                self.assertLess(body.index("```bash\n"), body.index(comments[0]))

    def test_anchor_lines_are_h2_and_real_headings_stay(self):
        for page, want in H2.items():
            with self.subTest(page=page):
                self.assertEqual(_h2(self.page(page)), want)
        self.assertIn("### Example (cURL)\n", self.page(4)["body_lines"])

    def test_only_the_anchor_lines_change(self):
        plain = _split(heading_line_pattern=None)
        self.assertEqual(len(plain), len(self.docs))
        changed = []
        for old, new in zip(plain, self.docs, strict=True):
            self.assertEqual(len(old["body_lines"]), len(new["body_lines"]))
            changed += [(a, b) for a, b in zip(old["body_lines"], new["body_lines"]) if a != b]
        self.assertEqual(changed, [
            ("Connect your editor or agent [#connect-your-editor-or-agent]\n", "## Connect your editor or agent\n"),
            ("Claude Code [#claude-code]\n", "## Claude Code\n"),
            ("Cursor [#cursor]\n", "## Cursor\n"),
            ("Steps [#steps]\n", "## Steps\n"),
            ("    Submit a Task [#submit-a-task-step]\n", "## Submit a Task\n"),
            ("    Poll for Status [#poll-for-status-step]\n", "## Poll for Status\n"),
            ("All steps in one script [#all-steps-in-one-script]\n", "## All steps in one script\n"),
            ("エディターやエージェントに接続する [#エディターやエージェントに接続する]\n", "## エディターやエージェントに接続する\n"),
            ("Claude Code [#claude-code]\n", "## Claude Code\n"),
            ("手順 [#手順]\n", "## 手順\n"),
            ("    タスクを送信する [#タスクを送信する-step]\n", "## タスクを送信する\n"),
        ])
        self.assertFalse([ln for d in self.docs for ln in d["body_lines"] if "[#" in ln])

    def test_a_line_of_that_shape_in_a_code_block_is_text(self):
        docs = _split("# P\n\nbody\n\n```text\nTitle [#title]\n    Step [#step-step]\n```\n\nTitle [#title]\n")
        self.assertEqual([d["title"] for d in docs], ["P"])
        body = docs[0]["body_lines"]
        self.assertIn("Title [#title]\n", body)
        self.assertIn("    Step [#step-step]\n", body)
        self.assertEqual(_h2(docs[0]), ["Title"])

    def test_which_lines_the_pattern_reads(self):
        rx = preset()["heading_line_pattern"]
        self.assertIsNotNone(rx)
        cases = {
            "Steps [#steps]": "Steps",
            "    Submit a Task [#submit-a-task-step]": "Submit a Task",
            "Steps [#steps]  ": "Steps",
            "Q：APIを通じて生成した画像は？ [#qapiを通じて生成した画像は]": "Q：APIを通じて生成した画像は？",
            # not a heading line: the anchor is not the end of the line, has a
            # space, has no title or no space before it
            "See [#steps] for the list": None,
            "Steps [#a b]": None,
            "[#steps]": None,
            " [#steps]": None,
            "Steps[#steps]": None,
            "Steps [#]": None,
            "Steps [steps]": None,
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                m = rx.fullmatch(line)
                self.assertEqual(m.group(1).strip() if m else None, want)


class IndexUrlTest(unittest.TestCase):
    def test_the_english_pages_get_their_url_by_title(self):
        docs = _split()
        self.assertEqual(g.join_index_urls(docs, _lines(INDEX)), 3)
        self.assertEqual([d["url"] for d in docs], URLS)

    def test_webhook_has_the_same_title_in_both_languages_so_neither_gets_one(self):
        docs = _split()
        self.assertEqual([d["title"] for d in docs], TITLES)
        # the English pages alone: Webhook is unique and gets its URL
        english = docs[:5]
        g.join_index_urls(english, _lines(INDEX))
        self.assertEqual(english[2]["url"], "https://platform.pixai.art/en/docs/references/webhook")
        # with the Japanese page of the same title, neither page gets it
        docs = _split()
        g.join_index_urls(docs, _lines(INDEX))
        self.assertEqual((docs[2]["url"], docs[7]["url"]), ("", ""))

    def test_the_index_files_and_the_specs_name_no_page(self):
        docs = _split()
        g.join_index_urls(docs, _lines(INDEX))
        self.assertFalse([d["url"] for d in docs if d["url"].endswith((".txt", ".yaml", ".json"))])


class RoundTripTest(unittest.TestCase):
    """The CLI on four corpora. Every ``Next:`` line runs as the shell splits
    it and opens the page and heading it names; a ``<heading_path>`` it says
    to fill in is filled with each row ``sections`` printed."""

    def setUp(self):
        # the cache file names below are made from both URLs
        self.assertTrue(preset()["url"] and preset()["index_url"], "the pixai preset lost a URL")
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.full = Path(self.tmp, "local-llms-full.txt")
        self.full.write_text(FULL, encoding="utf-8")
        self.index = Path(self.tmp, "local-llms.txt")
        self.index.write_text(INDEX, encoding="utf-8")

    def corpora(self):
        """label -> (arguments, network, whether the pages carry their URL).
        Each corpus has its own cache dir."""
        out = {}
        for label in ("fetch", "cache", "file", "file without index"):
            cache = tempfile.mkdtemp(dir=self.tmp)
            args = ["--source", "pixai", "--sources-file", self.sources, "--cache-dir", cache]
            net = _Net({FULL_URL: FULL, INDEX_URL: INDEX} if label == "fetch" else {})
            if label == "cache":
                Path(g._cache_path(cache, preset())).write_text(FULL, encoding="utf-8")
                Path(g._index_cache_path(cache, preset())).write_text(INDEX, encoding="utf-8")
            elif label == "file":
                args += ["--file", str(self.full), "--index-file", str(self.index)]
            elif label == "file without index":
                args += ["--file", str(self.full)]
            out[label] = (args, net, label != "file without index")
        return out

    def run_cli(self, net, argv):
        with mock.patch("urllib.request.urlopen", side_effect=net):
            return _loader.run_cli(g, argv)

    def run_next_lines(self, net, out, *, fill=()):
        """Run every ``Next:`` line of *out*; return how many ran."""
        ran = 0
        for line in (ln for ln in out.splitlines() if ln.startswith("Next:")):
            words = shlex.split(line, comments=True)
            self.assertEqual(words[:2], ["Next:", SCRIPT], line)
            holes = [w for w in words if w == "<heading_path>"]
            for value in (fill if holes else [None]):
                argv = [value if w == "<heading_path>" else w for w in words[1:]]
                code, got, err = self.run_cli(net, argv)
                self.assertEqual(code, 0, f"{shlex.join(argv)}\n{err}")
                if argv[1:2] == ["content"]:
                    page, heading = self.named(argv)
                    self.assertLess(page, len(TITLES), line)
                    self.assertIn(f"# doc_title: {TITLES[page]}\n", got, line)
                    if heading is not None:
                        self.assertIn(f"# heading_path: {heading}\n", got, line)
                ran += 1
        return ran

    def named(self, argv):
        """The page number and heading a ``content`` command names."""
        flags = {"--no-subsection-hints", "--no-link-annotations"}
        words, positional, k = argv[2:], [], 0
        while k < len(words):
            if words[k] == "--":
                positional += words[k + 1:]
                break
            if words[k].startswith("--"):
                k += 1 if words[k] in flags else 2
                continue
            positional.append(words[k])
            k += 1
        self.assertTrue(positional and positional[0].isdigit(), argv)
        return int(positional[0]), (positional[1] if len(positional) > 1 else None)

    def test_only_the_two_files_are_fetched(self):
        args, net, _url = self.corpora()["fetch"]
        for command in (["fetch-index"], ["search", "--", "Cursor"], ["sections", "--", "0"]):
            with self.subTest(command=command[0]):
                code, _out, err = self.run_cli(net, [SCRIPT, command[0], *args, *command[1:]])
                self.assertEqual(code, 0, err)
        self.assertEqual(set(net.calls), {FULL_URL, INDEX_URL})
        self.assertEqual(len(net.calls), 2, "each file once; then the cache")

    def test_search_names_the_heading_and_every_next_line_runs(self):
        cases = (("Cursor", 0, "Cursor"), ("Submit a Task", 1, "Submit a Task"),
                 ("エディター", 5, "エディターやエージェントに接続する"),
                 ("タスクを送信する", 6, "タスクを送信する"))
        for label, (args, net, _url) in self.corpora().items():
            for query, page, heading in cases:
                with self.subTest(corpus=label, query=query):
                    code, out, err = self.run_cli(net, [SCRIPT, "search", *args, "--", query])
                    self.assertEqual(code, 0, err)
                    first = re.search(r"^Next: .*$", out, re.M)
                    self.assertIsNotNone(first, out)
                    self.assertEqual(self.named(shlex.split(first.group(0), comments=True)[1:]), (page, heading), out)
                    self.assertGreater(self.run_next_lines(net, out), 0)
            with self.subTest(corpus=label):
                self.assertEqual(net.calls, [FULL_URL, INDEX_URL] if label == "fetch" else [])

    def test_search_content_and_odd_queries_run(self):
        # a query that starts with "-", an escape, only function words, only symbols
        for label, (args, net, _url) in self.corpora().items():
            for command, query in (("search-content", "callbackUrl"), ("search-content", "Poll for Status"),
                                   ("search", "-step"), ("search-content", "-step"), ("search", "\\_"),
                                   ("search", "the of"), ("search", "[#]"), ("search", "zzznotthere")):
                with self.subTest(corpus=label, command=command, query=query):
                    code, out, err = self.run_cli(net, [SCRIPT, command, *args, "--", query])
                    self.assertEqual(code, 0, err)
                    self.run_next_lines(net, out)

    def test_sections_lists_the_made_headings_and_each_reads_its_own(self):
        for label, (args, net, _url) in self.corpora().items():
            for page, rows in SECTIONS.items():
                with self.subTest(corpus=label, page=page):
                    code, out, err = self.run_cli(net, [SCRIPT, "sections", *args, "--", str(page)])
                    self.assertEqual(code, 0, err)
                    got = [ln for ln in out.splitlines() if re.match(r"^\s*\[L\d\] ", ln)]
                    self.assertEqual(got, rows)
                    paths = [re.sub(r"^\s*\[L\d\] | \[code\]$", "", row) for row in rows]
                    self.assertEqual(self.run_next_lines(net, out, fill=paths), len(paths))

    def test_an_english_page_prints_its_url_and_a_japanese_one_does_not(self):
        for label, (args, net, with_url) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "content", *args, "--", "0", "Cursor"])
                self.assertEqual(code, 0, err)
                self.assertEqual("# source: https://platform.pixai.art/en/docs/docs-mcp\n" in out, with_url)
                self.assertIn("## Cursor\n", out)
                self.assertNotIn("[#cursor]", out)
                code, out, err = self.run_cli(net, [SCRIPT, "content", *args, "--", "5", "Claude Code"])
                self.assertEqual(code, 0, err)
                self.assertNotIn("# source:", out)
                self.assertIn("claude mcp add", out)

    def test_the_english_copy_comes_first(self):
        # the same page in two languages: the English one ranks first
        for label, (args, net, with_url) in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_cli(net, [SCRIPT, "search", *args, "--", "webhook"])
                self.assertEqual(code, 0, err)
                self.assertEqual(re.findall(r"^\[(\d+)\] ", out, re.M)[:2], ["2", "7"], out)
                self.assertNotIn("URL: https://platform.pixai.art/en/docs/references/webhook", out)
                code, out, err = self.run_cli(net, [SCRIPT, "search", *args, "--", "contextImages"])
                self.assertEqual(code, 0, err)
                self.assertEqual(re.findall(r"^\[(\d+)\] ", out, re.M), ["4", "8"], out)
                self.assertEqual(
                    "URL: https://platform.pixai.art/en/docs/api-v2/media/getImageUploadUrl" in out, with_url)


class SkillNoteTest(unittest.TestCase):
    def test_the_library_skill_says_how_the_three_languages_read(self):
        text = SKILL.read_text(encoding="utf-8")
        heading = "### PixAI (`--source pixai`)"
        self.assertTrue(heading in text, f"the skill has no {heading}")
        body = text.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
        for needle in ("英・日・韓", "`URL:` 行", "`--top-n`", "日本語", "`Webhook`", "H2"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


if __name__ == "__main__":
    unittest.main()
