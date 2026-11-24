"""A page URL whose host ``urlparse`` cannot read, in a source with ``url_base``.

``split_documents`` joins each relative page URL onto ``url_base`` (Vite and
Vitest write ``url: /guide/x.md``). ``urljoin`` raises ``ValueError`` for a
URL such as ``//[name].example.com/x.md`` (a bracketed host that is not an
IPv6 address, an unclosed ``[``, a host NFKC turns into a ``/``), and nothing
caught it, so one such page stopped every command. The URL is now kept as
written: only fetching that page can fail, and these two presets read
``llms-full.txt`` without fetching a page. No network: ``urlopen`` is
patched to fail.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"

# each form makes urljoin raise ValueError (checked by test_the_forms_raise)
UNREADABLE = {
    "bracketed host": "//[name].example.com/guide/x.md",
    "unclosed bracket": "//[name/guide/x.md",
    "stray closing bracket": "//name]/guide/x.md",
    "NFKC slash": "//a／b.example/guide/x.md",
    "bracket only": "//[name]",
}
# forms with a readable host or none: joined as before
READABLE = {
    "/guide/ok.md": "{base}/guide/ok.md",
    "guide/ok.md": "{base}/guide/ok.md",
    "//cdn.example.com/guide/ok.md": "https://cdn.example.com/guide/ok.md",
    "//[::1]/guide/ok.md": "https://[::1]/guide/ok.md",
    "https://other.example/guide/ok.md": "https://other.example/guide/ok.md",
}
PRESETS = ("vite", "vitest")


def _split(text: str, profile: dict) -> list[dict]:
    try:
        return g.split_documents(text.splitlines(keepends=True), profile)
    except Exception as e:  # a traceback is the failure under test, not an error of the test
        raise AssertionError(f"{type(e).__name__}: {e}") from e


def _run(argv: list[str]) -> tuple[int, str, str]:
    try:
        return _loader.run_cli(g, argv)
    except Exception as e:  # a traceback is the failure under test, not an error of the test
        raise AssertionError(f"{type(e).__name__}: {e}") from e


def _file(bad: str) -> str:
    return (
        f"---\nurl: {bad}\n---\n\n# Broken Page\n\nZebra mentions the quokka.\n\n## Broken Heading\n\nText.\n\n---\n\n"
        "---\nurl: /guide/ok.md\n---\n\n# Good Page\n\nGood page talks about the quokka too.\n\n## Good Heading\n\nText.\n"
    )


class UnreadableUrlBaseTest(unittest.TestCase):
    def setUp(self):
        self.sources = _loader.without_user_sources(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch("urllib.request.urlopen", side_effect=OSError("no network in tests"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def opts(self, preset: str, text: str) -> list[str]:
        f = Path(self.tmp, f"{preset}.txt")
        f.write_text(text, encoding="utf-8")
        return ["--source", preset, "--sources-file", self.sources, "--cache-dir", self.tmp, "--file", str(f)]

    def test_the_forms_raise(self):
        from urllib.parse import urljoin
        for label, bad in UNREADABLE.items():
            with self.subTest(form=label), self.assertRaises(ValueError):
                urljoin("https://vite.dev", bad)

    def test_the_url_is_kept_as_written_and_other_pages_are_joined(self):
        for preset in PRESETS:
            profile = g._read_sources_file(g.PRESETS_FILE)[preset]
            self.assertTrue(profile["url_base"], preset)
            for label, bad in UNREADABLE.items():
                with self.subTest(preset=preset, form=label):
                    docs = _split(_file(bad), profile)
                    self.assertEqual(len(docs), 2)
                    self.assertEqual(docs[0]["url"], bad)
                    self.assertEqual(docs[1]["url"], profile["url_base"] + "/guide/ok.md")

    def test_readable_forms_join_as_before(self):
        for preset in PRESETS:
            profile = g._read_sources_file(g.PRESETS_FILE)[preset]
            for raw, want in READABLE.items():
                with self.subTest(preset=preset, url=raw):
                    docs = _split(f"---\nurl: {raw}\n---\n\n# P\n\nBody.\n", profile)
                    self.assertEqual(len(docs), 1)
                    self.assertEqual(docs[0]["url"], want.format(base=profile["url_base"]))

    def test_every_command_runs_and_the_good_page_stays_usable(self):
        for preset in PRESETS:
            for label, bad in UNREADABLE.items():
                opts = self.opts(preset, _file(bad))
                for argv in (["fetch-index"], ["search-index", "--", "page"], ["search-index", "--show-sections", "--", "page"],
                             ["search", "--", "quokka"], ["search-content", "--", "quokka"],
                             ["sections", "--", "1"], ["sections", "--", "Good Page"], ["content", "--", "1", "Good Page"],
                             ["sections", "--", "0"], ["content", "--", "0", "Broken Page"]):
                    with self.subTest(preset=preset, form=label, command=argv):
                        code, out, err = _run([SCRIPT, argv[0], *opts, *argv[1:]])
                        self.assertEqual(code, 0, err)
                        self.assertNotIn("Traceback", err)
                        if argv[0] == "fetch-index":
                            self.assertEqual(out.count("Broken Page"), 1, out)
                            self.assertIn("Good Page", out)
                        if argv[0] in ("search", "search-index", "search-content") and "--show-sections" not in argv:
                            self.assertIn("Good Page", out)
                            self.assertIn("Broken Page", out)

    def test_the_other_pages_index_is_the_same_with_and_without_the_bad_page(self):
        # the bad page changes only its own entry: the good page's listing is the same
        good = _file("/guide/bad.md")
        for preset in PRESETS:
            for label, bad in UNREADABLE.items():
                with self.subTest(preset=preset, form=label):
                    outs = []
                    for text in (good, _file(bad)):
                        code, out, err = _run([SCRIPT, "search-index", *self.opts(preset, text), "--", "page"])
                        self.assertEqual(code, 0, err)
                        outs.append(out)
                    self.assertEqual(self._entry(outs[0], "Good Page"), self._entry(outs[1], "Good Page"))

    @staticmethod
    def _entry(out: str, title: str) -> str:
        lines = out.splitlines()
        hits = [i for i, ln in enumerate(lines) if title in ln and ln.startswith("[")]
        assert len(hits) == 1, out
        return lines[hits[0]]


if __name__ == "__main__":
    unittest.main()
