"""In-body link annotations ``→ [doc_idx N]`` printed by ``content``.

``parse-claude-docs.py content`` appends `` → [doc_idx N]`` after a Markdown
link that points at another page of the corpus, so the reader can open it
with ``content N``. Links inside code fences and on table rows, links to the
page itself and links to URLs that are no page are left as they are;
``/docs/en/…`` and ``/en/…`` name the same page; ``--no-link-annotations``
turns the annotations off.

The cases below pin that behaviour on the old script first. They run on
both routes the page bodies come from (``--file`` and the cache), with the
network patched to fail.
"""

import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _loader

CD_SCRIPT = "parse-claude-docs.py"

HOST = "https://code.claude.com"

# page 0 is the page under test; every line of its body that carries a link
# starts with a label naming the case.
PAGES = [
    ("Alpha", f"{HOST}/docs/en/alpha", [
        "Path [b](/docs/en/beta) end.",
        f"Abs [b]({HOST}/docs/en/beta) end.",
        f"Md [b]({HOST}/docs/en/beta.md) end.",
        "PathMd [b](/docs/en/beta.md) end.",
        "Frag [b](/docs/en/beta#part-two) end.",
        "Slash [b](/docs/en/beta/) end.",
        "Query [b](/docs/en/beta?tab=one) end.",
        "Alias [b](/en/beta) end.",
        "AliasRev [g](/docs/en/gamma) end.",
        "AliasPage [g](/en/gamma) end.",
        "Two [b](/docs/en/beta) and [d](/docs/en/delta) end.",
        "Unknown [x](/docs/en/nope) end.",
        "External [x](https://example.invalid/docs/en/beta) end.",
        "Relative [b](beta) end.",
        "Self [a](/docs/en/alpha) end.",
        f"SelfAbs [a]({HOST}/docs/en/alpha#top) end.",
        "",
        "```",
        "Fence [b](/docs/en/beta) end.",
        "```",
        "",
        "~~~md",
        "Tilde [b](/docs/en/beta) end.",
        "~~~",
        "",
        "| Table | [b](/docs/en/beta) |",
        "|---|---|",
        "",
        "## Second part",
        "",
        "Section [d](/docs/en/delta) end.",
    ]),
    ("Beta", f"{HOST}/docs/en/beta", ["Beta body. Back to [a](/docs/en/alpha)."]),
    # a page published under the short ``/en/`` form
    ("Gamma", f"{HOST}/en/gamma", ["Gamma body."]),
    ("Delta", f"{HOST}/docs/en/delta", ["Delta body."]),
]

# label -> the line as ``content`` prints it (annotations on)
EXPECTED = {
    "Path": "Path [b](/docs/en/beta) → [doc_idx 1] end.",
    "Abs": f"Abs [b]({HOST}/docs/en/beta) → [doc_idx 1] end.",
    "Md": f"Md [b]({HOST}/docs/en/beta.md) → [doc_idx 1] end.",
    "PathMd": "PathMd [b](/docs/en/beta.md) → [doc_idx 1] end.",
    "Frag": "Frag [b](/docs/en/beta#part-two) → [doc_idx 1] end.",
    "Slash": "Slash [b](/docs/en/beta/) → [doc_idx 1] end.",
    "Query": "Query [b](/docs/en/beta?tab=one) → [doc_idx 1] end.",
    "Alias": "Alias [b](/en/beta) → [doc_idx 1] end.",
    "AliasRev": "AliasRev [g](/docs/en/gamma) → [doc_idx 2] end.",
    "AliasPage": "AliasPage [g](/en/gamma) → [doc_idx 2] end.",
    "Two": "Two [b](/docs/en/beta) → [doc_idx 1] and [d](/docs/en/delta) → [doc_idx 3] end.",
    "Unknown": "Unknown [x](/docs/en/nope) end.",
    "External": "External [x](https://example.invalid/docs/en/beta) end.",
    "Relative": "Relative [b](beta) end.",
    "Self": "Self [a](/docs/en/alpha) end.",
    "SelfAbs": f"SelfAbs [a]({HOST}/docs/en/alpha#top) end.",
    "Fence": "Fence [b](/docs/en/beta) end.",
    "Tilde": "Tilde [b](/docs/en/beta) end.",
    "|": "| Table | [b](/docs/en/beta) |",
    "Section": "Section [d](/docs/en/delta) → [doc_idx 3] end.",
}

_ANNOTATION_RE = re.compile(r"\(([^\s)]+)\) → \[doc_idx (\d+)\]")


def full_text(pages=PAGES) -> str:
    out = []
    for title, url, body in pages:
        out += [f"# {title}", f"Source: {url}", "", *body, "", ""]
    return "\n".join(out)


def _no_network(*_a, **_kw):
    raise OSError("test: network disabled")


def labelled_lines(stdout: str) -> dict:
    """label -> the printed line, for the lines of page 0's body."""
    out = {}
    for line in stdout.split("\n"):
        for label in EXPECTED:
            if line.startswith(label + " "):
                out[label] = line
    return out


class _LinkAnnotationCases:
    """The behaviour pinned on every route. Subclasses say how to run
    ``content`` (``invoke(argv)`` -> (exit, stdout, stderr)) and with which
    route (``route``: ``"file"`` or ``"cache"``)."""

    route = "file"

    def invoke(self, argv):
        raise NotImplementedError

    def content(self, *argv):
        code, out, err = self.invoke(["content", *argv])
        self.assertEqual(code, 0, err)
        return out

    def test_each_link_shape_is_annotated_as_pinned(self):
        lines = labelled_lines(self.content("0"))
        for label, expected in EXPECTED.items():
            with self.subTest(label=label):
                self.assertEqual(lines.get(label), expected)

    def test_flag_turns_every_annotation_off(self):
        out = self.content("0", "--no-link-annotations")
        self.assertNotIn("doc_idx", out)
        self.assertIn("Path [b](/docs/en/beta) end.", out)
        self.assertIn("Two [b](/docs/en/beta) and [d](/docs/en/delta) end.", out)

    def test_a_narrowed_section_is_annotated_too(self):
        out = self.content("0", "Second part")
        self.assertIn(EXPECTED["Section"], out)
        self.assertNotIn("Path [b]", out)

    def test_annotation_round_trips_to_the_linked_page(self):
        """Every ``[doc_idx N]`` printed opens, as ``content N``, the page the
        link names (its ``# source:`` is the link's URL, ``/en/`` and
        ``/docs/en/`` taken as one)."""
        out = self.content("0")
        found = _ANNOTATION_RE.findall(out)
        self.assertGreaterEqual(len(found), 12)
        for link, n in found:
            with self.subTest(link=link, n=n):
                target = self.content(n)
                source = re.search(r"^# source: (\S+)$", target, re.M).group(1)
                self.assertEqual(_page_key(source), _page_key(link))
                self.assertNotEqual(n, "0")


def _page_key(url: str) -> str:
    """The path of a page URL or link with the forms that name one page
    folded together (host, ``#…``, ``?…``, a trailing ``/``, ``.md``,
    ``/docs`` before ``/en/``)."""
    u = re.sub(r"^https?://[^/]+", "", url).split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if u.endswith(".md"):
        u = u[:-3]
    if u.startswith("/docs/en/"):
        u = u[len("/docs"):]
    return u


class _LegacyRunner:
    """``parse-claude-docs.py`` on a temp copy of the fixture."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="llms-link-annot-")
        cls.module = _loader.load_script(CD_SCRIPT)
        cls.file = os.path.join(cls.tmp, "fixture-llms-full.txt")
        with open(cls.file, "w", encoding="utf-8") as f:
            f.write(full_text())
        cls.cache_dir = os.path.join(cls.tmp, "cache")
        os.makedirs(cls.cache_dir)
        shutil.copyfile(cls.file, os.path.join(cls.cache_dir, "claude-code-llms-full.txt"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def invoke(self, argv):
        cached = os.path.join(self.cache_dir, "claude-code-llms-full.txt")
        now = time.time()
        os.utime(cached, (now, now))
        where = (["--file", self.file] if self.route == "file" else [])
        with mock.patch("urllib.request.urlopen", side_effect=_no_network):
            return _loader.run_cli(self.module, [CD_SCRIPT, *argv, *where,
                                                 "--cache-dir", self.cache_dir])


class LegacyFileTest(_LegacyRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "file"


class LegacyCacheTest(_LegacyRunner, _LinkAnnotationCases, unittest.TestCase):
    route = "cache"


if __name__ == "__main__":
    unittest.main()
