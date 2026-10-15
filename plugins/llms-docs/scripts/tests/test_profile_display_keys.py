"""Structure / display profile keys of parse-llms-txt.py, all off by default:
``strip_trailing_headings`` / ``min_heading_level`` / ``protect_tables`` /
``compact_pack`` / ``untitled_warn_ratio`` / ``label`` / ``cache_name``.

Each key is checked for: its default (the output before the key existed),
the value that turns it on, and the values it rejects. ``cache_name`` is
accepted only from the bundled presets, so its tests point ``PRESETS_FILE``
at a temporary presets file. No network: fetches go to a fake.
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

generic = _loader.load_script("parse-llms-txt.py")

URL = "https://example.com/llms-full.txt"

# frontmatter pages: a body ``# comment`` outside any fence (a section at
# floor 1, not at floor 2), navigation headings dangling at the end of a
# page, a page whose title comes from its first heading (no ``title:``).
CORPUS = """\
---
title: Alpha
url: https://example.com/alpha
---

Alpha intro.

## Setup

Run it.

# a shell comment outside any fence

More setup.

## Nav Group

### Nav Child

---
title: Beta
url: https://example.com/beta
---

Beta body text.

| a | b |
| - | - |
| 1 | 2 |

## Footer
---
url: https://example.com/gamma
description: no title key here
---

# Gamma From Heading

Gamma text.
"""


def _profile(path="sources.json", **raw) -> dict:
    return generic._validate_profile(path, "site", {"url": URL, "split": "frontmatter",
                                                    "page_url": "frontmatter:url",
                                                    "frontmatter_key": "url", **raw})


def _split(**raw) -> list[dict]:
    return generic.split_documents(CORPUS.splitlines(keepends=True), _profile(**raw))


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
            "url": URL, "split": "frontmatter", "page_url": "frontmatter:url",
            "frontmatter_key": "url", **profile}}}), encoding="utf-8")
        return _loader.run_cli(generic, ["parse-llms-txt.py", *argv, "--source", "site",
                                         "--sources-file", str(sources), "--file", str(self.corpus)])


class ValidationTest(unittest.TestCase):
    def load(self, raw: dict, path: str = "sources.json") -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "sources.json")
            f.write_text(json.dumps({"sources": {"x": {"url": URL, "split": "frontmatter", **raw}}}),
                         encoding="utf-8")
            code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources", "--sources-file", str(f)])
            return code, err

    def test_defaults_reproduce_the_output_before_the_keys(self):
        p = _profile()
        self.assertEqual(
            {k: p[k] for k in ("strip_trailing_headings", "min_heading_level", "protect_tables",
                               "compact_pack", "untitled_warn_ratio", "label", "cache_name")},
            {"strip_trailing_headings": False, "min_heading_level": None, "protect_tables": True,
             "compact_pack": False, "untitled_warn_ratio": None, "label": None, "cache_name": None})

    def test_accepted_values(self):
        p = _profile(strip_trailing_headings=True, min_heading_level=1, protect_tables=False,
                     compact_pack=True, untitled_warn_ratio=0.1, label="Example Docs")
        self.assertEqual((p["strip_trailing_headings"], p["min_heading_level"], p["protect_tables"],
                          p["compact_pack"], p["untitled_warn_ratio"], p["label"]),
                         (True, 1, False, True, 0.1, "Example Docs"))
        self.assertEqual(_profile(min_heading_level=2)["min_heading_level"], 2)
        self.assertEqual(_profile(untitled_warn_ratio=0)["untitled_warn_ratio"], 0)
        self.assertEqual(_profile(untitled_warn_ratio=1)["untitled_warn_ratio"], 1)

    def test_rejected_values(self):
        cases = {
            "strip_trailing_headings not bool": {"strip_trailing_headings": "yes"},
            "min_heading_level 3": {"min_heading_level": 3},
            "min_heading_level 0": {"min_heading_level": 0},
            "min_heading_level true": {"min_heading_level": True},
            "min_heading_level string": {"min_heading_level": "2"},
            "protect_tables not bool": {"protect_tables": 0},
            "compact_pack not bool": {"compact_pack": 1},
            "untitled_warn_ratio above 1": {"untitled_warn_ratio": 1.5},
            "untitled_warn_ratio negative": {"untitled_warn_ratio": -0.1},
            "untitled_warn_ratio string": {"untitled_warn_ratio": "0.1"},
            "untitled_warn_ratio true": {"untitled_warn_ratio": True},
            "label empty": {"label": " "},
            "label not string": {"label": 3},
            "label two lines": {"label": "a\nb"},
            "cache_name in a user file": {"cache_name": "site"},
        }
        for label, raw in cases.items():
            with self.subTest(label):
                code, err = self.load(raw)
                self.assertEqual(code, 1, err)
                self.assertIn("README", err)
                self.assertIn(next(iter(raw)), err)

    def test_untitled_warn_ratio_needs_split_frontmatter(self):
        code, err = self.load({"split": "h1", "untitled_warn_ratio": 0.1})
        self.assertEqual(code, 1)
        self.assertIn('only valid with split "frontmatter"', err)


class StripTrailingHeadingsTest(unittest.TestCase):
    def test_dangling_headings_are_dropped_only_when_on(self):
        off, on = _split(), _split(strip_trailing_headings=True)
        self.assertIn("## Nav Group\n", off[0]["body_lines"])
        self.assertIn("### Nav Child\n", off[0]["body_lines"])
        self.assertEqual(on[0]["body_lines"], off[0]["body_lines"][:off[0]["body_lines"].index("## Nav Group\n")])
        self.assertEqual([x for x in on[0]["body_lines"] if x.strip()][-1], "More setup.\n")
        # a heading with text under it stays, and a page's title source is untouched
        self.assertNotIn("## Footer\n", on[1]["body_lines"])
        self.assertIn("| 1 | 2 |\n", on[1]["body_lines"])
        self.assertEqual(on[2]["body_lines"], off[2]["body_lines"])

    def test_runs_after_drop_lines(self):
        # the footer line under the nav heading is boilerplate: once it is
        # dropped, the heading dangles and goes too
        lines = CORPUS.replace("### Nav Child\n", "### Nav Child\nWas this helpful?\n").splitlines(keepends=True)
        docs = generic.split_documents(lines, _profile(strip_trailing_headings=True,
                                                       drop_lines=["^Was this helpful"]))
        self.assertEqual([x for x in docs[0]["body_lines"] if x.strip()][-1], "More setup.\n")

    def test_a_trailing_h1_is_dropped_too(self):
        lines = ["---\n", "title: Delta\n", "url: https://example.com/delta\n", "---\n", "\n", "Delta text.\n", "\n", "# X\n", "\n"]
        off = generic.split_documents(lines, _profile())
        on = generic.split_documents(lines, _profile(strip_trailing_headings=True))
        self.assertIn("# X\n", off[0]["body_lines"])
        self.assertNotIn("# X\n", on[0]["body_lines"])
        self.assertEqual([x for x in on[0]["body_lines"] if x.strip()][-1], "Delta text.\n")


class MinHeadingLevelTest(_CliCase):
    def test_level_reaches_every_page(self):
        self.assertEqual({d["min_level"] for d in _split()}, {1})  # frontmatter default
        self.assertEqual({d["min_level"] for d in _split(min_heading_level=2)}, {None})
        h1 = generic._validate_profile("s", "site", {"url": URL, "split": "h1", "min_heading_level": 1})
        docs = generic.split_documents(["# A\n", "## x\n", "# B\n", "text\n"], h1)
        self.assertEqual({d["min_level"] for d in docs}, {1})

    def test_sections_and_hits_use_the_floor(self):
        code, out, err = self.run_cli({}, "sections", "Alpha")
        self.assertEqual(code, 0, err)
        self.assertIn("[L1] a shell comment outside any fence", out)
        code, out, err = self.run_cli({"min_heading_level": 2}, "sections", "Alpha")
        self.assertEqual(code, 0, err)
        self.assertNotIn("shell comment", out)
        self.assertRegex(out, r"(?m)^\[L2\] Setup$")  # floor 2: H2 is not indented
        # body hits name their section by the same floor
        code, out, err = self.run_cli({"min_heading_level": 2}, "search-content", "more setup")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: Setup ", out)
        code, out, err = self.run_cli({}, "search-content", "more setup")
        self.assertEqual(code, 0, err)
        self.assertIn("Section: a shell comment outside any fence", out)

    def test_full_corpus_fallback_uses_the_floor(self):
        # "more" is in no title, description or heading, so no candidate is
        # ranked and only the full-corpus search finds the body line; it runs
        # with the profile floor
        for profile, section in (({"min_heading_level": 2}, "Section: Setup  (x1)"),
                                 ({}, "Section: a shell comment outside any fence  (x1)")):
            with self.subTest(profile=profile):
                code, out, err = self.run_cli(profile, "search", "more")
                self.assertEqual(code, 0, err)
                self.assertIn("[body-only]", out)
                self.assertIn(section, out)


class ProtectTablesTest(_CliCase):
    def _kwargs_of_extract_content(self, profile: dict) -> dict:
        import _commands
        real = _commands.extract_content
        seen = {}

        def spy(*a, **kw):
            seen.update(kw)
            return real(*a, **kw)
        with mock.patch.object(_commands, "extract_content", side_effect=spy):
            code, _out, err = self.run_cli(profile, "content", "Beta")
        self.assertEqual(code, 0, err)
        return seen

    def test_false_reaches_extract_content_and_default_keeps_its_default(self):
        self.assertIs(self._kwargs_of_extract_content({"protect_tables": False}).get("protect_tables"), False)
        self.assertNotIn("protect_tables", self._kwargs_of_extract_content({}))
        self.assertNotIn("protect_tables", self._kwargs_of_extract_content({"protect_tables": True}))


class CompactPackTest(_CliCase):
    def test_packs_only_with_compact(self):
        code, out, err = self.run_cli({"compact_pack": True}, "fetch-index", "--compact")
        self.assertEqual(code, 0, err)
        self.assertIn("[0] Alpha  [1] Beta  [2] Gamma From Heading\n", out)
        code, out, _ = self.run_cli({}, "fetch-index", "--compact")
        self.assertIn("[0] Alpha\n[1] Beta\n[2] Gamma From Heading\n", out)
        # without --compact the key changes nothing
        _, on, _ = self.run_cli({"compact_pack": True}, "fetch-index")
        _, off, _ = self.run_cli({}, "fetch-index")
        self.assertEqual(on, off)

    def test_lines_stay_within_80_columns(self):
        rows = [f"[{i}] {'Title %d' % i}{'x' * 20}" for i in range(12)]
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            generic._print_packed(rows)
        lines = buf.getvalue().splitlines()
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)
        self.assertEqual(" ".join(lines).split(), " ".join(rows).split())
        self.assertGreater(len(lines), 1)
        self.assertLess(len(lines), len(rows))
        # exactly 80 columns still fits; one more does not
        for width, expect in ((39, 1), (40, 2)):
            with self.subTest(width=width):
                pair = ["a" * width, "b" * width]  # 2*width + 2 columns
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    generic._print_packed(pair)
                self.assertEqual(len(buf.getvalue().splitlines()), expect)


class UntitledWarnRatioTest(_CliCase):
    # 1 of 3 pages (Gamma) has no title: key -> ratio 1/3
    def test_warns_above_the_ratio_on_the_corpus_wide_commands(self):
        for argv in (("fetch-index",), ("search-index", "alpha"), ("search", "alpha")):
            with self.subTest(argv=argv):
                code, _out, err = self.run_cli({"untitled_warn_ratio": 0.3}, *argv)
                self.assertEqual(code, 0, err)
                self.assertIn("WARNING: 1/3 documents (33%) have no title in", err)
                self.assertIn("stray '---' line", err)

    def test_quiet_at_or_under_the_ratio_unset_or_on_page_commands(self):
        cases = [({"untitled_warn_ratio": 0.34}, ("fetch-index",)),
                 ({"untitled_warn_ratio": 1 / 3}, ("fetch-index",)),  # exactly the ratio: not above it
                 ({"untitled_warn_ratio": 1}, ("fetch-index",)),
                 ({}, ("fetch-index",)),
                 ({"untitled_warn_ratio": 0}, ("sections", "Alpha")),
                 ({"untitled_warn_ratio": 0}, ("content", "Alpha")),
                 ({"untitled_warn_ratio": 0}, ("search-content", "alpha"))]
        for profile, argv in cases:
            with self.subTest(profile=profile, argv=argv):
                code, _out, err = self.run_cli(profile, *argv)
                self.assertEqual(code, 0, err)
                self.assertNotIn("WARNING", err)

    def test_title_from_a_heading_counts_as_untitled(self):
        docs = _split()
        self.assertEqual([d["fm_titled"] for d in docs], [True, True, False])
        self.assertEqual(docs[2]["title"], "Gamma From Heading")


class LabelTest(_CliCase):
    def test_label_leads_the_headers(self):
        expect = {
            ("fetch-index",): r"^Example Docs llms-full\.txt Document Index \(file: ",
            ("search-index", "alpha"): r'^Search-index results for "alpha" \(Example Docs, source: site, file: ',
            ("search-content", "alpha"): r'^Search-content results for "alpha" \(Example Docs, source: site, file: ',
            ("search", "alpha"): r'^Search results for "alpha" \(Example Docs, source: site, file: ',
        }
        for argv, pattern in expect.items():
            with self.subTest(argv=argv):
                code, out, err = self.run_cli({"label": "Example Docs"}, *argv)
                self.assertEqual(code, 0, err)
                self.assertRegex(out.splitlines()[0], pattern)
                code, out, err = self.run_cli({}, *argv)
                self.assertNotIn("Example Docs", out)
                self.assertRegex(out.splitlines()[0], r"^site llms-full|\(source: site, file: ")


class CacheNameTest(unittest.TestCase):
    """``cache_name`` names the cache files of a bundled preset."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cache = self.dir / "cache"
        self.cache.mkdir()
        self.presets = self.dir / "presets.json"
        self._patch = mock.patch.object(generic, "PRESETS_FILE", str(self.presets))
        self._patch.start()
        env = {k: v for k, v in os.environ.items() if k != generic.SOURCES_ENV}
        env["XDG_CONFIG_HOME"] = str(self.dir / "config")
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def write_presets(self, sources: dict) -> None:
        self.presets.write_text(json.dumps({"sources": sources}), encoding="utf-8")

    def fetch_paths(self, *extra) -> tuple[int, str, list]:
        calls = []

        def fake_fetch(url, cache_path, **kw):
            calls.append((url, cache_path))
            Path(cache_path).write_text("# Page\n\ntext\n" if url.endswith("full.txt") else
                                        "- [Page](https://example.com/page): d\n", encoding="utf-8")
            return cache_path
        with mock.patch.object(generic, "fetch_url", side_effect=fake_fetch):
            code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "fetch-index", "--source", "site",
                                                        "--cache-dir", str(self.cache), *extra])
        return code, err, calls

    def test_preset_cache_name_names_both_cache_files(self):
        self.write_presets({"site": {"url": URL, "split": "h1", "index_url": "https://example.com/llms.txt",
                                     "cache_name": "site-docs"}})
        code, err, calls = self.fetch_paths()
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [(URL, str(self.cache / "site-docs-llms-full.txt")),
                                 ("https://example.com/llms.txt", str(self.cache / "site-docs-llms.txt"))])

    def test_without_cache_name_the_url_hash_names_the_files(self):
        self.write_presets({"site": {"url": URL, "split": "h1", "index_url": "https://example.com/llms.txt"}})
        code, err, calls = self.fetch_paths()
        self.assertEqual(code, 0, err)
        full = hashlib.sha256(URL.encode("utf-8")).hexdigest()[:12]
        index = hashlib.sha256(b"https://example.com/llms.txt").hexdigest()[:12]
        self.assertEqual(calls, [(URL, str(self.cache / f"generic-site-{full}-llms-full.txt")),
                                 ("https://example.com/llms.txt",
                                  str(self.cache / f"generic-site-{index}-llms.txt"))])

    def test_user_entry_of_the_same_name_does_not_inherit_the_cache_name(self):
        # a user profile that reuses the preset's name for another URL must
        # not read or overwrite the preset's named cache file
        self.write_presets({"site": {"url": URL, "split": "h1", "index_url": "https://example.com/llms.txt",
                                     "cache_name": "site-docs"}})
        user = self.dir / "user.json"
        mirror = "https://mirror.example/llms-full.txt"
        user.write_text(json.dumps({"sources": {"site": {"url": mirror, "split": "h1",
                                                         "index_url": "https://mirror.example/llms.txt"}}}),
                        encoding="utf-8")
        code, err, calls = self.fetch_paths("--sources-file", str(user))
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][0], mirror)
        for _url, path in calls:
            self.assertNotIn("site-docs", path)
            self.assertRegex(os.path.basename(path), r"^generic-site-[0-9a-f]{12}-llms(-full)?\.txt$")

    def test_rejected_in_presets(self):
        cases = {
            "path traversal": {"site": {"url": URL, "split": "h1", "cache_name": "../x"}},
            "slash": {"site": {"url": URL, "split": "h1", "cache_name": "a/b"}},
            "upper case": {"site": {"url": URL, "split": "h1", "cache_name": "Site"}},
            "not a string": {"site": {"url": URL, "split": "h1", "cache_name": 1}},
            "shared by two presets": {"site": {"url": URL, "split": "h1", "cache_name": "same"},
                                      "other": {"url": "https://b.example/llms-full.txt", "split": "h1",
                                                "cache_name": "same"}},
        }
        for label, sources in cases.items():
            with self.subTest(label):
                self.write_presets(sources)
                code, _out, err = _loader.run_cli(generic, ["parse-llms-txt.py", "sources"])
                self.assertEqual(code, 1, err)
                self.assertIn("cache_name", err)


if __name__ == "__main__":
    unittest.main()
