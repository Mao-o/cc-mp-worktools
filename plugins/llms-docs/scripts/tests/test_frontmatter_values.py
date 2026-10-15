"""Frontmatter values in parse-llms-txt.py: ``frontmatter_yaml_values``
(quotes, escapes, indented continuation, ``|`` / ``>`` block scalars) and
``tags_key`` (block list / flow list / comma-separated tags, shown and
ranked). Both are off by default; the default reproduces the values the
loader read before the keys existed.

The page shapes copy the real files' frontmatter (AI SDK's ``tags:`` block
lists and ``\\"`` in titles, Vite's ``description: >-``) with made-up words.
"""

import json
import shlex
import tempfile
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")

URL = "https://example.com/llms-full.txt"

CORPUS = """\
---
title: "Error \\"Cannot find module 'LoomJSX'\\""
description: "A back\\\\slash \\\\ page"
url: https://example.com/docs/fixes/loomjsx
tags:
  - api servers
  - "streaming"
---

# Error page

Body about widgets.

---
title: Folded Page
description: >-
  Folded words that
  run on to a second line.
url: https://example.com/docs/folded
tags: [one, "two words"]
meta:
  nested: value
---

# Folded Page

Folded body about gadgets.

---
title: Literal Page
description: |
  First literal line.

  After a blank line.
url: https://example.com/docs/literal
tags: alpha, beta
---

Literal body.

---
title: Continued Page
description: first part
  second part
url: https://example.com/docs/continued
tags:
- unindented
- items
---

Continued body about sprockets.

## Usage

Sprockets usage.

---
title: Empty Value Page
description:
  filled from the next line
url: https://example.com/docs/empty
---

Empty value body.

---
title: 'It''s Plain'
description: Nothing special here
url: https://example.com/docs/plain
---

Plain body about zebracorn only.
"""

# the values before the keys existed (and still the default)
DEFAULT_VALUES = [
    ("Error \\\"Cannot find module 'LoomJSX'\\", "A back\\\\slash \\\\ page"),
    ("Folded Page", ">-"),
    ("Literal Page", "|"),
    ("Continued Page", "first part"),
    ("Empty Value Page", ""),
    ("It''s Plain", "Nothing special here"),
]
YAML_VALUES = [
    ("Error \"Cannot find module 'LoomJSX'\"", "A back\\slash \\ page"),
    ("Folded Page", "Folded words that run on to a second line."),
    ("Literal Page", "First literal line. After a blank line."),
    ("Continued Page", "first part second part"),
    ("Empty Value Page", "filled from the next line"),
    ("It's Plain", "Nothing special here"),
]
TAGS = [["api servers", "streaming"], ["one", "two words"], ["alpha", "beta"],
        ["unindented", "items"], [], []]


def _profile(path="sources.json", **raw) -> dict:
    return generic._validate_profile(path, "site", {"url": URL, "split": "frontmatter",
                                                    "page_url": "frontmatter:url", **raw})


def _split(text: str = CORPUS, **raw) -> list[dict]:
    return generic.split_documents(text.splitlines(keepends=True), _profile(**raw))


class _CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.corpus = self.dir / "full.txt"
        self.corpus.write_text(CORPUS, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, profile: dict, *argv) -> tuple[int, str, str]:
        sources = self.dir / "sources.json"
        sources.write_text(json.dumps({"sources": {"site": {
            "url": URL, "split": "frontmatter", "page_url": "frontmatter:url", **profile}}}),
            encoding="utf-8")
        return _loader.run_cli(generic, ["parse-llms-txt.py", *argv, "--source", "site",
                                         "--sources-file", str(sources), "--file", str(self.corpus)])


class ValidationTest(unittest.TestCase):
    def load(self, raw: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "sources.json")
            f.write_text(json.dumps({"sources": {"x": {"url": URL, "split": "frontmatter", **raw}}}),
                         encoding="utf-8")
            code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", str(f)])
            return code, err

    def test_defaults_are_off(self):
        p = _profile()
        self.assertEqual((p["tags_key"], p["frontmatter_yaml_values"]), (None, False))

    def test_accepted_values(self):
        p = _profile(tags_key="keywords", frontmatter_yaml_values=True)
        self.assertEqual((p["tags_key"], p["frontmatter_yaml_values"]), ("keywords", True))
        self.assertIsNone(_profile(tags_key=None)["tags_key"])

    def test_rejected_values(self):
        cases = {
            "tags_key not a key name": ({"tags_key": "two words"}, "YAML key name"),
            "tags_key empty": ({"tags_key": ""}, "YAML key name"),
            "tags_key not a string": ({"tags_key": ["tags"]}, "YAML key name"),
            "tags_key with split h1": ({"split": "h1", "tags_key": "tags"}, 'only valid with split "frontmatter"'),
            "yaml values not bool": ({"frontmatter_yaml_values": "yes"}, "true or false"),
            "yaml values with split line": ({"split": "line", "line_prefix": "Source:",
                                             "frontmatter_yaml_values": True},
                                            'only valid with split "frontmatter"'),
        }
        for label, (raw, message) in cases.items():
            with self.subTest(label):
                code, err = self.load(raw)
                self.assertEqual(code, 1, err)
                key = "tags_key" if "tags_key" in raw else "frontmatter_yaml_values"
                self.assertIn(f".{key} ", err)
                self.assertIn(message, err)

    def test_yaml_values_false_is_accepted_with_any_split(self):
        code, err = self.load({"split": "h1", "frontmatter_yaml_values": False})
        self.assertEqual(code, 0, err)


class YamlValuesTest(unittest.TestCase):
    def values(self, docs):
        return [(d["title"], d["description"]) for d in docs]

    def test_default_keeps_the_values_as_before(self):
        self.assertEqual(self.values(_split()), DEFAULT_VALUES)

    def test_on_reads_values_as_yaml(self):
        docs = _split(frontmatter_yaml_values=True)
        self.assertEqual(self.values(docs), YAML_VALUES)

    def test_each_value_shape_by_name(self):
        """One assertion per path, so a mutant that breaks one path names it."""
        docs = _split(frontmatter_yaml_values=True)
        cases = {
            "double-quoted title with escaped quotes": (docs[0]["title"], "Error \"Cannot find module 'LoomJSX'\""),
            "double-quoted escaped backslashes": (docs[0]["description"], "A back\\slash \\ page"),
            "single-quoted doubled quote": (docs[5]["title"], "It's Plain"),
            "folded block scalar": (docs[1]["description"], "Folded words that run on to a second line."),
            "literal block scalar with a blank line": (docs[2]["description"],
                                                       "First literal line. After a blank line."),
            "indented continuation": (docs[3]["description"], "first part second part"),
            "key with no value filled by the next line": (docs[4]["description"], "filled from the next line"),
        }
        for label, (got, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(got, want)

    def test_open_double_quote_spanning_lines(self):
        text = '---\ntitle: "Two \\"line\n  title\\""\ndescription: d\n---\nbody\n'
        self.assertEqual(_split(text)[0]["title"], 'Two \\"line title\\')
        self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["title"], 'Two "line title"')

    def test_nested_mapping_is_not_read_into_the_value(self):
        text = "---\ntitle: T\nmeta:\n  nested: value\n  other: x\n---\nbody\n"
        hit = generic._frontmatter_at(text.splitlines(keepends=True), 0, "title", yaml_values=True)
        self.assertEqual(hit[0]["meta"], "")
        # a nested mapping after a value line is still not a page body
        self.assertEqual(hit[1], 6)

    def test_block_scalar_indicators(self):
        for indicator in ("|", ">", "|-", ">-", "|+", ">+", "|2", ">2-", ">-2"):
            with self.subTest(indicator):
                text = f"---\ntitle: T\ndescription: {indicator}\n  a\n  b\n---\nbody\n"
                self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "a b")
        for not_indicator in (">>", "|x", "> text"):
            with self.subTest(not_indicator):
                text = f"---\ntitle: T\ndescription: {not_indicator}\n  a\n---\nbody\n"
                self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"],
                                 f"{not_indicator} a")

    def test_block_scalar_keeps_lines_that_look_like_list_items(self):
        # an indented ``- `` line under a plain value or a block scalar is
        # text in YAML, not a list item that is dropped
        for fm, want in (("description: |\n  - a\n  - b\n", "- a - b"),
                         ("description: >\n  intro\n  - x\n", "intro - x"),
                         ("description: foo\n  - bar\n", "foo - bar")):
            with self.subTest(fm=fm):
                lines = ("---\ntitle: T\n" + fm + "---\nbody\n").splitlines(keepends=True)
                fields, end, _lists = generic._frontmatter_at(lines, 0, "title", yaml_values=True)
                self.assertEqual(fields["description"], want)
                # the page boundary is the same with the key off
                self.assertEqual(end, generic._frontmatter_at(lines, 0, "title")[1])
                self.assertEqual(end, len(lines) - 1)

    def test_unindented_list_line_does_not_continue_a_plain_value(self):
        lines = "---\ntitle: T\ndescription: foo\n- bar\n---\nbody\n".splitlines(keepends=True)
        fields, end, _lists = generic._frontmatter_at(lines, 0, "title", yaml_values=True)
        self.assertEqual((fields["description"], end), ("foo", 5))

    def test_empty_block_scalar_is_empty(self):
        for indicator in ("|", ">-"):
            for after in ("---\n", "url: https://example.com/x\n---\n"):
                with self.subTest(indicator=indicator, after=after):
                    text = f"---\ntitle: T\ndescription: {indicator}\n{after}body\n"
                    self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "")
                    # the default keeps the indicator, as before the key existed
                    self.assertEqual(_split(text)[0]["description"], indicator)

    def test_closed_quoted_value_is_not_continued(self):
        text = "---\ntitle: T\ndescription: \"done\"\n  stray\n---\nbody\n"
        self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "done")

    def test_unindented_continuation_is_kept_and_joins_the_indented_one(self):
        # Cloudflare's unindented continuation is read with the key off and on
        text = "---\ntitle: T\ndescription: one\ntwo\n  three\n---\nbody\n"
        self.assertEqual(_split(text)[0]["description"], "one two")
        self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "one two three")

    def test_open_single_quote_continued_on_an_indented_line(self):
        text = "---\ntitle: T\ndescription: 'first\n  it''s second'\n---\nbody\n"
        self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "first it's second")

    def test_continuation_that_looks_like_a_key_is_text(self):
        # only under a key with no value is an indented ``key:`` a mapping
        text = "---\ntitle: T\ndescription: first\n  Note: second\n---\nbody\n"
        self.assertEqual(_split(text, frontmatter_yaml_values=True)[0]["description"], "first Note: second")

    def test_a_repeated_key_replaces_a_continued_value(self):
        text = "---\ntitle: T\ndescription: a\n  b\ndescription: \"c\"\n---\nbody\n"
        try:
            got = _split(text, frontmatter_yaml_values=True)[0]["description"]
        except KeyError as e:
            self.fail(f"repeated key crashed: {e!r}")
        self.assertEqual(got, "c")

    def test_page_boundaries_do_not_depend_on_the_key(self):
        off, on = _split(), _split(frontmatter_yaml_values=True)
        self.assertEqual([(d["url"], d["body_lines"]) for d in off],
                         [(d["url"], d["body_lines"]) for d in on])
        self.assertEqual(len(off), 6)


class TagsTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual([d["tags"] for d in _split()], [[]] * 6)

    def test_every_form(self):
        self.assertEqual([d["tags"] for d in _split(tags_key="tags")], TAGS)

    def test_each_form_by_name(self):
        tags = [d["tags"] for d in _split(tags_key="tags", frontmatter_yaml_values=True)]
        cases = {
            "indented block list, quoted item": (0, ["api servers", "streaming"]),
            "flow list with a quoted item": (1, ["one", "two words"]),
            "comma-separated value": (2, ["alpha", "beta"]),
            "unindented block list": (3, ["unindented", "items"]),
            "no tags key": (5, []),
        }
        for label, (page, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(tags[page], want)

    def test_other_key_and_empty_items(self):
        text = ("---\ntitle: T\nkeywords:\n  - a\n  - ''\n  - b\ntags: [x]\n---\nbody\n"
                "---\ntitle: U\nkeywords: [ , c, ]\n---\nbody\n")
        self.assertEqual([d["tags"] for d in _split(text, tags_key="keywords")], [["a", "b"], ["c"]])

    def test_list_under_a_key_with_a_value_is_not_its_list(self):
        text = "---\ntitle: T\ntags: solo\n  - stray\n---\nbody\n"
        self.assertEqual(_split(text, tags_key="tags")[0]["tags"], ["solo"])

    def test_block_scalar_tags_are_text(self):
        # ``tags: |`` then ``- a`` is the text ``- a`` in YAML, read as one
        # comma-separated tag (parse-ai-sdk.py reads it as the list ``[a]``)
        text = "---\ntitle: T\ntags: |\n  - a\n---\nbody\n"
        self.assertEqual(_split(text, tags_key="tags", frontmatter_yaml_values=True)[0]["tags"], ["- a"])
        self.assertEqual(_split(text, tags_key="tags")[0]["tags"], ["|"])

    def test_a_later_key_ends_the_list(self):
        text = "---\ntitle: T\ntags:\n  - a\nother: \"v\"\n  - b\n---\nbody\n"
        self.assertEqual(_split(text, tags_key="tags")[0]["tags"], ["a"])

    def test_a_repeated_key_replaces_the_list(self):
        text = "---\ntitle: T\ntags:\n  - a\ntags: [b]\n---\nbody\n"
        self.assertEqual(_split(text, tags_key="tags")[0]["tags"], ["b"])


class TagsOutputTest(_CliCase):
    ON = {"tags_key": "tags"}

    def test_tags_line_under_each_listing(self):
        commands = {
            "fetch-index": ("fetch-index",),
            "search-index": ("search-index", "widgets error"),
            "search-content": ("search-content", "widgets"),
            "search": ("search", "widgets"),
        }
        for label, argv in commands.items():
            with self.subTest(label):
                code, out, err = self.run_cli(self.ON, *argv)
                self.assertEqual(code, 0, err)
                lines = out.splitlines()
                url = lines.index("    URL: https://example.com/docs/fixes/loomjsx")
                self.assertEqual(lines[url + 1], "    tags: api servers, streaming")
                _code, off, _err = self.run_cli({}, *argv)
                self.assertNotIn("tags:", off)

    def test_content_and_sections_meta(self):
        code, out, err = self.run_cli(self.ON, "content", "Continued Page")
        self.assertEqual(code, 0, err)
        self.assertIn("# doc_tags: unindented, items\n", out)
        _code, off, _err = self.run_cli({}, "content", "Continued Page")
        self.assertNotIn("doc_tags", off)

    def test_tags_rank_a_page_whose_title_and_description_miss(self):
        code, out, err = self.run_cli(self.ON, "search-index", "streaming")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Error", out)
        _code, off, _err = self.run_cli({}, "search-index", "streaming")
        self.assertIn("No matching documents found.", off)

    def test_tags_add_to_the_score(self):
        def score(profile):
            _c, out, _e = self.run_cli(profile, "search-index", "error servers")
            rows = [l for l in out.splitlines() if l.startswith("[0] ")]
            if not rows:
                self.fail(f"no [0] row for {profile}: {out}")
            line = rows[0]
            return int(line.rsplit("(score: ", 1)[1].rstrip(")"))
        self.assertGreater(score(self.ON), score({}))

    def test_body_only_note_names_tags_only_when_ranked_on_them(self):
        for profile, want in ((self.ON, "(no title/description/tags match"),
                              ({}, "(no title/description match")):
            with self.subTest(profile=profile):
                code, out, err = self.run_cli(profile, "search", "zebracorn")
                self.assertEqual(code, 0, err)
                self.assertIn(want, out)
                if profile:
                    self.assertNotIn("(no title/description match", out)

    def test_next_lines_still_run(self):
        """The printed ``Next:`` command of a tags-ranked search runs as is."""
        code, out, err = self.run_cli(self.ON, "search", "streaming")
        self.assertEqual(code, 0, err)
        nxt = [l for l in out.splitlines() if l.startswith("Next: ")]
        self.assertTrue(nxt)
        argv = shlex.split(nxt[0][len("Next: "):], comments=True)
        self.assertEqual(argv[0], "parse-llms-txt.py")
        code, out2, err2 = _loader.run_cli(generic, argv)
        self.assertEqual(code, 0, err2)
        self.assertIn("# doc_title: Error \\\"Cannot find module 'LoomJSX'\\", out2)


if __name__ == "__main__":
    unittest.main()
