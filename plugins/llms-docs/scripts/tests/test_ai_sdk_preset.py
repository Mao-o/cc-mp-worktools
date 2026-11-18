"""The ``ai-sdk`` preset on cases the retired ``parse-ai-sdk.py`` tested and
no other suite covers on the generic loader (moved here when its tests were
removed in 0.48.0): which ``---`` lines start a page, how frontmatter values
are read, a page reference holding a quote, and two messages."""

import contextlib
import io
import os
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader

generic = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"


def options_of(argv: list) -> list:
    """The source and corpus options of a command: from ``--source`` up to
    ``--`` (a ``Next:`` line may write the page and heading before them)."""
    end = argv.index("--") if "--" in argv else len(argv)
    return argv[argv.index("--source"):end]


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


def _page(title: str, url: str, body: str) -> str:
    return f'---\ntitle: {title}\nurl: "{url}"\n---\n\n## Setup\n\nzzq {body}\n\n'


class _TitleToSlug(_AiSdk):
    """The ai-sdk preset on ``CORPUS``, through a cache dir and ``--file``,
    with no network and an empty default cache."""

    CORPUS = ""

    def setUp(self):
        super().setUp()
        self.write(self.CORPUS)
        # a printed command that dropped the corpus options would read an empty
        # cache dir and fail to fetch, instead of reaching the network or the
        # developer's cache
        for patcher in (mock.patch.dict(os.environ, {"LLMS_DOCS_CACHE_DIR": str(Path(self.tmp, "default-cache"))}),
                        mock.patch("urllib.request.urlopen", side_effect=OSError("test: network disabled"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def corpora(self) -> dict:
        return {"cache-dir": ["--cache-dir", self.tmp, "--max-age", "999999999"], "file": ["--file", str(self.full)]}

    def run_on(self, corpus, *args, tail=()):
        argv = [SCRIPT, *args, "--source", "ai-sdk", "--sources-file", self.sources, *corpus, "--", *tail]
        try:
            return _loader.run_cli(generic, argv)
        except Exception as e:  # a crash is a failure, not an error of the run
            self.fail(f"{argv} crashed: {e!r}")

    def notes(self, err: str, ref: str) -> list:
        return [ln for ln in err.splitlines() if ln.startswith(f"Note: '{ref}'")]

    def note(self, err: str, ref: str) -> str:
        """The one ``Note:`` line about *ref*."""
        notes = self.notes(err, ref)
        self.assertEqual(len(notes), 1, err)
        return notes[0]

    def note_command(self, note: str, page: int) -> list:
        """The command at the end of *note* (for page *page*), split by the shell rules."""
        self.assertIn(f" For [{page}]: ", note)
        return shlex.split(note.split(f" For [{page}]: ", 1)[1], comments=True)


class TitleToSlugTest(_TitleToSlug):
    """The preset tries ``title`` before ``slug``. A reference that is part of
    several titles goes on to the slug form, which opens the one page whose
    URL ends in it (``openai`` is part of eight titles of the AI SDK, and one
    URL ends in ``/openai``); stderr says how many titles matched and names
    the others, with the command for the first. When no URL or several end
    in it, the titles are listed as before (exit 1). A reference that opens
    a page today opens the same page."""

    CORPUS = (_page("Azure OpenAI", "https://ai-sdk.dev/providers/ai-sdk-providers/azure", "azure body")
              + _page("OpenAI Compatible", "https://ai-sdk.dev/providers/openai-compatible-providers", "compatible body")
              + _page("OpenAI", "https://ai-sdk.dev/providers/ai-sdk-providers/openai", "openai body")
              + _page("Overview", "https://ai-sdk.dev/docs/agents/overview", "agents overview")
              + _page("Overview", "https://ai-sdk.dev/docs/foundations/overview", "foundations overview")
              + _page("Provider A", "https://ai-sdk.dev/docs/a", "provider a")
              + _page("Provider B", "https://ai-sdk.dev/docs/b", "provider b")
              + _page("Registry", "https://ai-sdk.dev/docs/reference/provider", "registry body"))

    def test_a_title_word_whose_url_end_is_one_page_opens_that_page(self):
        for label, corpus in self.corpora().items():
            with self.subTest(corpus=label):
                code, out, err = self.run_on(corpus, "sections", tail=("openai",))
                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines()[0], 'Sections in [2] "OpenAI"')
                note = self.note(err, "openai")
                self.assertTrue(note.startswith(
                    "Note: 'openai' is part of 3 titles, and the URL of [2] ends in it; opened [2]. "
                    "The other titles: [0] https://ai-sdk.dev/providers/ai-sdk-providers/azure, "
                    "[1] https://ai-sdk.dev/providers/openai-compatible-providers. For [0]: "), note)
                self.assertEqual(err.splitlines(), [note])

    def test_two_url_ends_or_none_stay_ambiguous(self):
        # two URLs end in "overview"; none in "pen" (part of the three OpenAI titles)
        for ref, pages in (("overview", ["3", "4"]), ("pen", ["0", "1", "2"])):
            with self.subTest(ref=ref):
                code, out, err = self.run_on(self.corpora()["file"], "sections", tail=(ref,))
                self.assertEqual(code, 1, err)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith(f"Error: Ambiguous title substring '{ref}'. Matches:"), err)
                self.assertNotIn("Note:", err)
                self.assertEqual(re.findall(r"(?m)^  \[(\d+)\] ", err), pages)

    def test_the_page_need_not_be_one_of_the_titles(self):
        code, out, err = self.run_on(self.corpora()["file"], "sections", tail=("provider",))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'Sections in [7] "Registry"')
        note = self.note(err, "provider")
        self.assertIn("is part of 2 titles, and the URL of [7] ends in it; opened [7]. The other titles: "
                      "[5] https://ai-sdk.dev/docs/a, [6] https://ai-sdk.dev/docs/b. For [5]: ", note)

    def test_the_note_command_runs_as_printed_for_each_subcommand(self):
        """The command at the end of the Note is the whole command, made like
        the ``Next:`` lines (the same options after the subcommand, the page
        after ``--``): split by the shell rules and run as printed, it opens
        the page it names."""
        for label, corpus in self.corpora().items():
            for cmd, tail, reached in (("sections", ("openai",), 'Sections in [0] "Azure OpenAI"'),
                                       ("content", ("openai", "Setup"), "zzq azure body"),
                                       ("search-content", ("zzq",), "zzq azure body")):
                args = (cmd, "--page-ref", "openai") if cmd == "search-content" else (cmd,)
                with self.subTest(corpus=label, cmd=cmd):
                    code, out, err = self.run_on(corpus, *args, tail=tail)
                    self.assertEqual(code, 0, err)
                    self.assertIn("ai-sdk-providers/openai" if cmd == "sections" else "zzq openai body", out)
                    note = self.note(err, "openai")
                    argv = self.note_command(note, 0)
                    self.assertEqual(argv[:2], [SCRIPT, cmd], note)
                    nexts = [shlex.split(ln[len("Next: "):], comments=True)
                             for ln in out.splitlines() if ln.startswith("Next: ")]
                    if nexts:  # the same options as the Next: lines of the same output
                        self.assertEqual(options_of(argv), options_of(nexts[0]), (note, nexts[0]))
                    code, opened, err = _loader.run_cli(generic, argv)
                    self.assertEqual(code, 0, (note, err))
                    self.assertIn(reached, opened)
                    self.assertNotIn("openai body", opened)

    def test_the_note_command_carries_a_hostile_heading_and_query(self):
        """A heading or query that starts with "-" and holds quotes, an escape
        and a shell comment sign comes back from the Note's command as it was
        given (after "--"), and the command reaches the other page."""
        heading = "--dry-run 'quoted' \\_ #x"
        query = "-dry-run 'quoted'"
        two = (_page("Azure OpenAI", "https://ai-sdk.dev/providers/ai-sdk-providers/azure", "azure body")
               + _page("OpenAI", "https://ai-sdk.dev/providers/ai-sdk-providers/openai", "openai body"))
        self.write(two.replace("## Setup", f"## {heading}"))
        for label, corpus in self.corpora().items():
            for cmd, args, tail, after in (("content", ("content",), ("openai", heading), ["0", heading]),
                                           ("search-content", ("search-content", "--page-ref", "openai"),
                                            (query,), [query])):
                with self.subTest(corpus=label, cmd=cmd):
                    code, out, err = self.run_on(corpus, *args, tail=tail)
                    self.assertEqual(code, 0, err)
                    self.assertIn("zzq openai body", out)
                    argv = self.note_command(self.note(err, "openai"), 0)
                    self.assertEqual(argv[:2], [SCRIPT, cmd])
                    self.assertEqual(argv[argv.index("--") + 1:], after)
                    code, opened, err = _loader.run_cli(generic, argv)
                    self.assertEqual(code, 0, err)
                    self.assertIn("https://ai-sdk.dev/providers/ai-sdk-providers/azure", opened)
                    self.assertIn("zzq azure body", opened)
                    self.assertNotIn("openai body", opened)

    def test_more_than_five_other_titles(self):
        many = "".join(_page(f"OpenAI tool {i}", f"https://ai-sdk.dev/docs/t{i}", f"t{i}") for i in range(7))
        self.write(many + self.CORPUS)
        code, out, err = self.run_on(self.corpora()["file"], "sections", tail=("openai",))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'Sections in [9] "OpenAI"')
        note = self.note(err, "openai")
        self.assertIn("is part of 10 titles", note)
        # the title that is the reference first (none other here), then page order
        self.assertIn("The other titles: [0] https://ai-sdk.dev/docs/t0, [1] https://ai-sdk.dev/docs/t1, "
                      "[2] https://ai-sdk.dev/docs/t2, [3] https://ai-sdk.dev/docs/t3, "
                      "[4] https://ai-sdk.dev/docs/t4 (and 4 more). For [0]: ", note)

    def test_another_title_with_no_url_is_named_by_its_title(self):
        self.write(self.CORPUS + "---\ntitle: OpenAI FAQ\n---\n\n## Setup\n\nzzq faq\n\n")
        code, out, err = self.run_on(self.corpora()["file"], "sections", tail=("openai",))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'Sections in [2] "OpenAI"')
        note = self.note(err, "openai")
        self.assertIn("is part of 4 titles, and the URL of [2] ends in it; opened [2]. The other titles: "
                      "[0] https://ai-sdk.dev/providers/ai-sdk-providers/azure, "
                      "[1] https://ai-sdk.dev/providers/openai-compatible-providers, [8] OpenAI FAQ. For [0]: ", note)

    def test_of_several_url_ends_the_one_at_lang_slug(self):
        # as the slug form chooses (prefer_lang_exact), and with its Note too
        self.write(self.CORPUS + _page("Widget tools", "https://ai-sdk.dev/en/guides/widget", "guide")
                   + _page("Widgets", "https://ai-sdk.dev/en/widget", "widgets"))
        code, out, err = self.run_on(self.corpora()["file"], "sections", tail=("widget",))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], 'Sections in [9] "Widgets"')
        # the title step's Note, then the slug form's on the other URL end
        lines = err.splitlines()
        self.assertEqual(len(lines), 2, err)
        title_note, slug_note = lines
        self.assertTrue(title_note.startswith(
            "Note: 'widget' is part of 2 titles, and the URL of [9] ends in it; opened [9]. "
            "The other titles: [8] https://ai-sdk.dev/en/guides/widget. For [8]: "), err)
        self.assertTrue(slug_note.startswith(
            "Note: 'widget' also matches [8] https://ai-sdk.dev/en/guides/widget; resolved to [9] "
            "(exact <lang>/widget). For [8]: "), err)

    def test_profiles_without_a_slug_after_the_title_are_unchanged(self):
        docs = self.docs()
        for forms, want, note in ((("int", "title", "url_substring"), None, False),
                                  (("int", "title"), None, False),
                                  (("int", "slug", "title"), 2, False),
                                  (("int", "url", "slug", "title"), 2, False),
                                  (("int", "title", "url_substring", "slug"), 2, True),
                                  (("int", "title", "url", "slug"), 2, True)):
            with self.subTest(forms=forms):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        idx = generic._resolve_page_ref(docs, "openai", None, forms)
                    except SystemExit as e:
                        self.assertEqual(e.code, 1)
                        idx = None
                self.assertEqual(idx, want, err.getvalue())
                self.assertEqual("is part of 3 titles" in err.getvalue(), note, err.getvalue())
                if want is None:
                    self.assertIn("Ambiguous title substring 'openai'", err.getvalue())

    def test_references_that_open_a_page_open_the_same_page(self):
        # one title (also where a URL ends in it: the Note of the title form);
        # a URL; its end; a longer end; an end no title has
        for ref, want in (("Azure", 0), ("OpenAI Compatible", 1), ("registry", 7),
                          ("https://ai-sdk.dev/providers/ai-sdk-providers/openai", 2),
                          ("ai-sdk-providers/openai", 2), ("agents/overview", 3), ("reference/provider", 7)):
            with self.subTest(ref=ref):
                code, out, err = self.run_on(self.corpora()["file"], "sections", tail=(ref,))
                self.assertEqual(code, 0, err)
                self.assertIn(f"Sections in [{want}] ", out.splitlines()[0])
                self.assertNotIn("is part of", err)

    def test_hostile_references_fail_cleanly(self):
        for word in ("-openai", "--limit", "\\_", "the", "---", "'", "-", "%", "(", "[", "/", "//"):
            with self.subTest(word=word):
                code, out, err = self.run_on(self.corpora()["file"], "sections", tail=(word,))
                self.assertIn(code, (0, 1), err)
                self.assertNotIn("Traceback", err)


# The AI SDK llms-full.txt (fetched 2026-11-03), cut: every page the reference
# "openai" can name (the eight whose title holds it; "OpenAI" is also the one
# page whose URL ends in /openai), in their order, each with its
# frontmatter, its first line and its H2 headings.
AI_SDK_OPENAI_PAGES = """\
---
title: Azure OpenAI Slow to Stream
description: Learn to troubleshoot Azure OpenAI slow to stream issues.
url: "https://ai-sdk.dev/docs/troubleshooting/azure-stream-slow"
docs_index: /llms.txt
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## Issue
## Cause
## Solution
---
title: Object generation failed with OpenAI
description: Troubleshooting NoObjectGeneratedError with finish-reason content-filter caused by incompatible Zod schema types when using OpenAI structured outputs
url: "https://ai-sdk.dev/docs/troubleshooting/no-object-generated-content-filter"
docs_index: /llms.txt
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## Issue
## Background
## Solution
## Related Information
---
title: Azure OpenAI
description: Learn how to use Azure OpenAI with AI Gateway and the direct provider.
url: "https://ai-sdk.dev/providers/ai-sdk-providers/azure"
docs_index: /llms.txt
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## Using AI Gateway
## Using `@ai-sdk/azure`
## Provider Instance
## Language Models
## Embedding Models
## Image Models
## Transcription Models
## Speech Models
---
title: OpenAI
description: Learn how to use OpenAI with AI Gateway and the direct provider.
url: "https://ai-sdk.dev/providers/ai-sdk-providers/openai"
docs_index: /llms.txt
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## Using AI Gateway
## Using `@ai-sdk/openai`
## Provider Instance
## Language Models
## Realtime Models
## Embedding Models
## Image Models
## Transcription Models
## Translation Models
## Speech Models
## Decision Models
---
title: OpenAI Compatible Providers
description: Use OpenAI compatible providers with the AI SDK.
url: "https://ai-sdk.dev/providers/openai-compatible-providers"
docs_index: /llms.txt
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## Setup
## Provider Instance
## Language Models
## Image Models
## Embedding Models
## Completion Models
## Chat Model Options
## Provider-specific options
## Custom Metadata Extraction
---
title: Get started with OpenAI o1
description: Get started with OpenAI o1 using the AI SDK.
url: "https://ai-sdk.dev/cookbook/guides/o1"
docs_index: /llms.txt
tags:
  - getting-started
  - reasoning
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## OpenAI o1
## Getting Started with the AI SDK
## Get Started
---
title: Get started with OpenAI o3-mini
description: Get started with OpenAI o3-mini using the AI SDK.
url: "https://ai-sdk.dev/cookbook/guides/o3"
docs_index: /llms.txt
tags:
  - getting-started
  - reasoning
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## OpenAI o3-mini
## Getting Started with the AI SDK
## Get Started
---
title: OpenAI Responses API
description: Get started with the OpenAI Responses API using the AI SDK.
url: "https://ai-sdk.dev/cookbook/guides/openai-responses"
docs_index: /llms.txt
tags:
  - getting-started
  - agents
---
> For an index of all documentation, see [/llms.txt](/llms.txt).
## OpenAI Responses API
## Getting Started with the AI SDK
## Using Persistence
## Migrating from Completions API
## Get Started
"""


class TitleToSlugRealPagesTest(_TitleToSlug):
    """The title → slug step on pages cut from the real corpus: ``openai`` and
    ``azure`` open the provider pages. ``OpenAI`` (the slug form matches
    letter case, and the URL ends in ``/openai``) and ``Azure OpenAI`` (no
    URL ends in it) stay ambiguous, with the page whose title is the
    reference first."""

    CORPUS = AI_SDK_OPENAI_PAGES

    def test_openai_and_azure_open_the_provider_pages(self):
        cases = {"openai": ("8 titles", "3", "OpenAI",
                            "[0] https://ai-sdk.dev/docs/troubleshooting/azure-stream-slow, "
                            "[1] https://ai-sdk.dev/docs/troubleshooting/no-object-generated-content-filter, "
                            "[2] https://ai-sdk.dev/providers/ai-sdk-providers/azure, "
                            "[4] https://ai-sdk.dev/providers/openai-compatible-providers, "
                            "[5] https://ai-sdk.dev/cookbook/guides/o1 (and 2 more)"),
                 "azure": ("2 titles", "2", "Azure OpenAI", "[0] https://ai-sdk.dev/docs/troubleshooting/azure-stream-slow")}
        for label, corpus in self.corpora().items():
            for ref, (count, page, title, others) in cases.items():
                with self.subTest(corpus=label, ref=ref):
                    code, out, err = self.run_on(corpus, "sections", tail=(ref,))
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out.splitlines()[0], f'Sections in [{page}] "{title}"')
                    note = self.note(err, ref)
                    self.assertTrue(note.startswith(
                        f"Note: '{ref}' is part of {count}, and the URL of [{page}] ends in it; opened [{page}]. "
                        f"The other titles: {others}. For [0]: "), note)
                    self.assertEqual(err.splitlines(), [note])

    def test_the_note_command_runs_as_printed_for_each_subcommand(self):
        for label, corpus in self.corpora().items():
            for cmd, args, tail in (("sections", ("sections",), ("openai",)),
                                    ("content", ("content",), ("openai",)),
                                    ("search-content", ("search-content", "--page-ref", "openai"),
                                     ("index of all documentation",))):
                with self.subTest(corpus=label, cmd=cmd):
                    code, out, err = self.run_on(corpus, *args, tail=tail)
                    self.assertEqual(code, 0, err)
                    self.assertIn("OpenAI", out)
                    note = self.note(err, "openai")
                    argv = self.note_command(note, 0)
                    self.assertEqual(argv[:2], [SCRIPT, cmd], note)
                    code, opened, err = _loader.run_cli(generic, argv)
                    self.assertEqual(code, 0, (note, err))
                    self.assertIn("Azure OpenAI Slow to Stream", opened)
                    self.assertNotIn("ai-sdk-providers/openai", opened)

    def test_references_no_url_ends_in_stay_ambiguous_with_the_title_first(self):
        for ref, pages in (("OpenAI", ["3", "0", "1", "2", "4", "5", "6", "7"]), ("Azure OpenAI", ["2", "0"])):
            with self.subTest(ref=ref):
                code, out, err = self.run_on(self.corpora()["file"], "sections", tail=(ref,))
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith(f"Error: Ambiguous title substring '{ref}'. Matches:"), err)
                self.assertNotIn("Note:", err)
                self.assertNotIn("(showing", err)
                self.assertEqual(re.findall(r"(?m)^  \[(\d+)\] ", err), pages)
                lines = re.findall(r"(?m)^      (parse-llms-txt\.py .+)$", err)
                self.assertEqual(len(lines), len(pages), err)
                for line, page in zip(lines, pages):
                    code, opened, e2 = _loader.run_cli(generic, shlex.split(line, comments=True))
                    self.assertEqual(code, 0, (line, e2))
                    self.assertEqual(opened.splitlines()[0].split('"')[0], f"Sections in [{page}] ", line)

    def test_references_that_open_a_page_open_the_same_page(self):
        urls = re.findall(r'(?m)^url: "(.+)"$', AI_SDK_OPENAI_PAGES)
        self.assertEqual(len(urls), 8)
        refs = [("o1", 5), ("openai-responses", 7), ("openai compatible", 4), ("guides/o3", 6),
                *((u, i) for i, u in enumerate(urls))]
        for ref, want in refs:
            with self.subTest(ref=ref):
                code, out, err = self.run_on(self.corpora()["file"], "sections", tail=(ref,))
                self.assertEqual(code, 0, err)
                self.assertIn(f"Sections in [{want}] ", out.splitlines()[0])
                self.assertNotIn("is part of", err)


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
