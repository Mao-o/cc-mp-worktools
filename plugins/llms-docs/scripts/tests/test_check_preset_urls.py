"""Offline tests for scripts/check-preset-urls.py (the hand-run preset checker).

The tool itself goes to the network, so it is not run here: ``probe`` is
replaced by a table of answers and only the redirect-following, the verdicts
and the exit code are checked.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

checker = _loader.load_script("check-preset-urls.py")


def answers(table):
    def fake_probe(url, timeout=20.0):
        return table[url]
    return fake_probe


class CheckTest(unittest.TestCase):
    def test_a_direct_200_is_ok(self):
        with mock.patch.object(checker, "probe", answers({"https://a.example/x": (200, None)})):
            result = checker.check("https://a.example/x")
        self.assertEqual(checker.verdict(result), "ok")
        self.assertEqual(result["hops"], [(200, None)])

    def test_a_redirect_is_followed_to_its_final_status(self):
        table = {
            "https://a.example/old": (301, "https://a.example/new"),
            "https://a.example/new": (200, None),
        }
        with mock.patch.object(checker, "probe", answers(table)):
            result = checker.check("https://a.example/old")
        self.assertEqual(checker.verdict(result), "moved")
        self.assertEqual(result["hops"], [(301, "https://a.example/new"), (200, None)])
        self.assertEqual(result["final"], 200)

    def test_a_redirect_to_a_dead_page_is_not_reported_as_moved(self):
        table = {
            "https://a.example/old": (308, "https://b.example/gone"),
            "https://b.example/gone": (404, None),
        }
        with mock.patch.object(checker, "probe", answers(table)):
            result = checker.check("https://a.example/old")
        self.assertEqual(checker.verdict(result), "moved-broken")

    def test_404_and_network_errors_are_broken(self):
        for first in ((404, None), (0, "timed out")):
            with self.subTest(first=first):
                with mock.patch.object(checker, "probe", answers({"https://a.example/x": first})):
                    self.assertEqual(checker.verdict(checker.check("https://a.example/x")), "broken")

    def test_a_redirect_loop_stops(self):
        table = {"https://a.example/a": (302, "https://a.example/b"),
                 "https://a.example/b": (302, "https://a.example/a")}
        with mock.patch.object(checker, "probe", answers(table)):
            result = checker.check("https://a.example/a")
        self.assertEqual(len(result["hops"]), 5)  # the documented hop limit
        self.assertEqual(checker.verdict(result), "moved-broken")


class MainTest(unittest.TestCase):
    def run_main(self, sources, table):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sources.json")
            path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(checker, "probe", answers(table)), contextlib.redirect_stdout(out):
                code = checker.main(["--file", str(path), "--workers", "1"])
        return code, out.getvalue()

    def test_checks_both_url_fields_and_exits_1_when_one_moved(self):
        sources = {"site": {"url": "https://a.example/llms-full.txt", "index_url": "https://a.example/llms.txt"}}
        table = {
            "https://a.example/llms-full.txt": (200, None),
            "https://a.example/llms.txt": (301, "https://a.example/new/llms.txt"),
            "https://a.example/new/llms.txt": (200, None),
        }
        code, out = self.run_main(sources, table)
        self.assertEqual(code, 1)
        self.assertIn("ok            site url: https://a.example/llms-full.txt", out)
        self.assertIn("moved         site index_url: https://a.example/llms.txt", out)
        self.assertIn("301 https://a.example/new/llms.txt", out)
        self.assertIn("(2 URLs, 1 not answering 200 directly)", out)

    def test_exits_0_when_everything_answers_200(self):
        sources = {"site": {"url": "https://a.example/llms-full.txt"}}
        code, out = self.run_main(sources, {"https://a.example/llms-full.txt": (200, None)})
        self.assertEqual(code, 0)
        self.assertIn("(1 URLs, 0 not answering 200 directly)", out)

    def test_every_bundled_preset_url_is_listed(self):
        # the default file is the bundled presets: each url / index_url is one job
        presets = json.loads(Path(checker.DEFAULT_FILE).read_text(encoding="utf-8"))["sources"]
        expected = sum(1 for p in presets.values() for f in checker.URL_FIELDS if p.get(f))
        self.assertGreater(expected, len(presets))
        seen = []

        def fake_check(url):
            seen.append(url)
            return {"url": url, "hops": [(200, None)], "final": 200}

        out = io.StringIO()
        with mock.patch.object(checker, "check", fake_check), contextlib.redirect_stdout(out):
            code = checker.main(["--workers", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), expected)


if __name__ == "__main__":
    unittest.main()
