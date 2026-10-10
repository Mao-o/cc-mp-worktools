"""Parity harness for retiring the three dedicated parsers.

``parse-claude-docs.py`` / ``parse-ai-sdk.py`` / ``parse-firebase.py`` are
being folded into the generic loader ``parse-llms-txt.py`` (profile keys in
``presets.json`` + CLI flags). Every step of that migration must keep their
output the same, so this module pins it first:

* ``fixtures/parity/<corpus>/`` holds synthetic cache directories written by
  ``fixtures/parity/build_fixtures.py`` (made-up pages and product names
  that reproduce the shape of the upstream files, including the edge cases
  listed in ``CORPORA``; no upstream text is copied — its module docstring
  says how to regenerate them). A run copies one into a temp dir and passes it as
  ``--cache-dir``, so no network is touched: ``urllib.request.urlopen`` is
  patched to fail and every call is counted (the count is part of the
  golden, so a later step that fetches more than the old script did shows up).
* ``fixtures/parity/golden/`` holds what the old scripts print for ``CASES``
  (every subcommand x representative queries, zero-hit and not-found paths).
  ``GoldenSelfCheckTest`` re-runs the old scripts and requires a byte-for-byte
  match (stdout, stderr, exit code, fetch count) — the harness is only useful
  while the golden is exactly what the old scripts print.
* ``normalize`` / ``compare`` are the comparator later steps use against the
  generic loader's output. They drop or canonicalise only the lines the
  migration is allowed to change (listed above ``normalize``) and require
  everything else — page list and order, refs, titles, URLs, descriptions,
  section lists, page bodies, hit blocks, the query in the header, the
  ``Next:`` commands minus the script name and corpus options — to match
  byte for byte. ``compare`` looks at stdout, and at the first line of the
  normalised stderr when the expected stdout is empty (error paths).
* ``GENERIC_PARITY_EXPECTED`` lists the cases the generic loader already
  matches; each migration step adds the cases it brings to parity. Cases
  taken out when the comparator was tightened are listed next to it.
* ``CorpusParityGateTest`` runs the same cases on the full local cache
  (``$LLMS_DOCS_CACHE_DIR`` / ``$XDG_CACHE_HOME/llms-docs`` /
  ``~/.cache/llms-docs``); it is a local-only gate enabled by
  ``LLMS_DOCS_PARITY_CORPUS=1`` and skipped otherwise (CI). Parity on the
  small fixture can be a coincidence of its size (a slug or title substring
  that is unique among a dozen pages is ambiguous among hundreds), so a step
  that adds to ``GENERIC_PARITY_EXPECTED`` runs this gate too. The fixture
  carries namesakes, links between pages and navigation headings so that
  the cases matching on it are the ones matching on the full corpus.

Regenerating the golden is a deliberate act: run this file as a script with
``--update-golden`` (only while the old scripts are the reference), and
review the diff. ``--report-generic`` prints, per case, how far the generic
loader with the provisional profiles in ``GENERIC_PROFILES`` is from the
golden after normalisation.
"""

import difflib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parity"
GOLDEN_DIR = FIXTURES / "golden"
MANIFEST = GOLDEN_DIR / "manifest.json"

# Placeholder for the per-run temp cache dir in recorded output. Paths are
# substituted, not dropped: a hint that loses ``--cache-dir`` is a real diff.
CACHE_TOKEN = "<CACHE_DIR>"

CORPUS_ENV = "LLMS_DOCS_PARITY_CORPUS"

# corpus dir -> the shapes its synthetic pages reproduce (design notes for
# the reader; ``build_fixtures.py`` writes them, the cases below exercise them).
CORPORA = {
    # claude-code: H1 + ``Source:`` pages; the last page never closes its
    # final fence, so its later headings sit inside an unclosed fence.
    # The index carries entries with no page in the full text (join misses).
    # Pages link to each other (``→ [doc_idx N]`` annotations) and come with
    # namesakes (``snapshotting`` / ``kit/file-snapshotting``, ``hub`` / ``hubs`` /
    # ``hub-rollout``) so slug and substring refs resolve as on the
    # full corpus.
    # claude-platform: frontmatter pages with the navigation headings that
    # sit between pages, ``# Title`` lines outside fences in the body, two
    # pages with the same title, a ``(Beta)`` variant group in the index, and
    # namesakes (``folding`` / ``folding-background``).
    "claude-docs": ("claude-code-llms.txt", "claude-code-llms-full.txt",
                    "claude-platform-llms.txt", "claude-platform-llms-full.txt"),
    # ai-sdk: frontmatter with ``tags:`` block lists and ``\"`` escapes.
    "ai-sdk": ("ai-sdk-llms-full.txt",),
    # firebase: an index of a few dozen entries; nine pages are in the page
    # cache, the rest are fetched through the failing mock (skipped).
    # Pages printed by ``content`` cases have body lines starting with
    # ``Note:`` / ``Tip:`` and the words ``page`` / ``document``.
    "firebase": ("firebase-llms.txt", "firebase-docs"),
}

SCRIPTS = {
    "claude-docs": "parse-claude-docs.py",
    "ai-sdk": "parse-ai-sdk.py",
    "firebase": "parse-firebase.py",
}

_P = ("--source", "platform")

# (case id, corpus, argv after the script name). ``--cache-dir`` is appended.
CASES = [
    # --- claude-docs: code (default source)
    ("cd-code-fetch-index", "claude-docs", ["fetch-index"]),
    ("cd-code-search-index", "claude-docs", ["search-index", "snapshot"]),
    ("cd-code-search-index-multi", "claude-docs", ["search-index", "workers side"]),
    ("cd-code-search-index-zero", "claude-docs", ["search-index", "zzzparitynohit"]),
    ("cd-code-search-content", "claude-docs", ["search-content", "hub"]),
    ("cd-code-search-content-page", "claude-docs", ["search-content", "restore", "--page-ref", "snapshotting"]),
    ("cd-code-search-content-zero", "claude-docs", ["search-content", "zzzparitynohit"]),
    ("cd-code-sections", "claude-docs", ["sections", "snapshotting"]),
    ("cd-code-sections-int", "claude-docs", ["sections", "2"]),
    ("cd-code-content", "claude-docs", ["content", "quillon-tag"]),
    ("cd-code-content-heading", "claude-docs", ["content", "hub", "Roll out a hub"]),
    ("cd-code-content-unclosed-fence", "claude-docs", ["content", "snapshotting", "--max-chars", "3000"]),
    ("cd-code-content-missing", "claude-docs", ["content", "no-such-page-slug"]),
    ("cd-code-search", "claude-docs", ["search", "snapshot restore"]),
    ("cd-code-search-body-only", "claude-docs", ["search", "widgets"]),
    ("cd-code-search-zero", "claude-docs", ["search", "zzzparitynohit"]),
    # --- claude-docs: platform
    ("cd-plat-fetch-index", "claude-docs", ["fetch-index", *_P]),
    ("cd-plat-search-index", "claude-docs", ["search-index", "fetch blob", *_P]),
    ("cd-plat-search-index-variants", "claude-docs", ["search-index", "releases", *_P]),
    ("cd-plat-search-content", "claude-docs", ["search-content", "vantry-version", *_P]),
    ("cd-plat-sections", "claude-docs", ["sections", "get-access-token", *_P]),
    ("cd-plat-sections-dup-title", "claude-docs", ["sections", "https://platform.claude.com/docs/en/api/beta/blobs/fetch", *_P]),
    ("cd-plat-content", "claude-docs", ["content", "glossary", *_P]),
    ("cd-plat-content-heading", "claude-docs", ["content", "ip-addresses", "Inbound IP addresses", *_P]),
    ("cd-plat-content-nav-tail", "claude-docs", ["content", "folding", *_P]),
    ("cd-plat-search", "claude-docs", ["search", "access token", *_P]),
    ("cd-search-both", "claude-docs", ["search", "releases", "--source", "both"]),
    # --- ai-sdk
    ("as-fetch-index", "ai-sdk", ["fetch-index"]),
    ("as-fetch-index-compact", "ai-sdk", ["fetch-index", "--compact"]),
    ("as-search-index", "ai-sdk", ["search-index", "weaveText"]),
    ("as-search-index-tags", "ai-sdk", ["search-index", "embed", "--show-sections"]),
    ("as-search-index-zero", "ai-sdk", ["search-index", "zzzparitynohit"]),
    ("as-search-content", "ai-sdk", ["search-content", "JSX"]),
    ("as-search-content-page", "ai-sdk", ["search-content", "stream", "--page-ref", "1"]),
    ("as-sections", "ai-sdk", ["sections", "useCompletion"]),
    ("as-content", "ai-sdk", ["content", "Loom is not assignable"]),
    ("as-content-heading", "ai-sdk", ["content", "3", "API Signature"]),
    ("as-content-missing", "ai-sdk", ["content", "stream-shape"]),
    ("as-search", "ai-sdk", ["search", "stream shape"]),
    ("as-search-zero", "ai-sdk", ["search", "zzzparitynohit"]),
    # --- firebase
    ("fb-fetch-index", "firebase", ["fetch-index"]),
    ("fb-fetch-index-page", "firebase", ["fetch-index", "--offset", "5", "--limit", "5"]),
    ("fb-search-index", "firebase", ["search-index", "projects"]),
    ("fb-search-index-zero", "firebase", ["search-index", "zzzparitynohit"]),
    ("fb-search-content", "firebase", ["search-content", "ship labels"]),
    ("fb-sections", "firebase", ["sections", "14"]),
    ("fb-sections-url", "firebase", ["sections", "https://firebase.google.com/docs/cli/labels"]),
    ("fb-content", "firebase", ["content", "labels"]),
    ("fb-content-heading", "firebase", ["content", "14", "Get started"]),
    ("fb-content-dead", "firebase", ["content", "1"]),
    ("fb-search", "firebase", ["search", "arcade hub"]),
    ("fb-search-zero", "firebase", ["search", "zzzparitynohit"]),
]

CASE_IDS = [c[0] for c in CASES]


# ---------------------------------------------------------------------------
# Running the old scripts
# ---------------------------------------------------------------------------

class _Run:
    __slots__ = ("code", "stdout", "stderr", "fetches")

    def __init__(self, code, stdout, stderr, fetches):
        self.code, self.stdout, self.stderr, self.fetches = code, stdout, stderr, fetches

    def as_record(self) -> dict:
        return {"exit": self.code, "fetches": self.fetches, "stderr": self.stderr}


def _no_network(calls: list):
    def fake_urlopen(req, *a, **kw):
        calls.append(getattr(req, "full_url", req))
        raise OSError("parity harness: network disabled")
    return fake_urlopen


def _scrub(text: str, cache_dir: str) -> str:
    return text.replace(cache_dir, CACHE_TOKEN)


def stage_corpus(corpus: str, dest: str, *, source_dir: Path | None = None) -> None:
    """Copy the corpus's cache files into *dest* with a fresh mtime (so the
    scripts' ``--max-age`` sees a cache hit and never tries to refetch)."""
    src = source_dir or (FIXTURES / corpus)
    for name in CORPORA[corpus]:
        s = src / name
        if s.is_dir():
            shutil.copytree(s, Path(dest) / name, copy_function=shutil.copyfile)
        elif s.exists():
            shutil.copyfile(s, Path(dest) / name)


def run_script(script: str, argv: list, cache_dir: str) -> _Run:
    """Run *script* in-process on *cache_dir* with the network disabled."""
    module = _loader.load_script(script)
    calls: list = []
    env = {k: v for k, v in os.environ.items()
           if k not in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME")}
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch("urllib.request.urlopen", side_effect=_no_network(calls)):
        code, out, err = _loader.run_cli(module, [script, *argv, "--cache-dir", cache_dir])
    return _Run(code, _scrub(out, cache_dir), _scrub(err, cache_dir), len(calls))


def run_legacy_case(case_id: str, *, source_dir: Path | None = None) -> _Run:
    _, corpus, argv = next(c for c in CASES if c[0] == case_id)
    tmp = tempfile.mkdtemp(prefix="llms-parity-")
    try:
        stage_corpus(corpus, tmp, source_dir=source_dir)
        return run_script(SCRIPTS[corpus], argv, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_golden(case_id: str) -> tuple[str, dict]:
    stdout = (GOLDEN_DIR / f"{case_id}.stdout").read_text(encoding="utf-8")
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))[case_id]
    return stdout, record


def update_golden() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for case_id in CASE_IDS:
        run = run_legacy_case(case_id)
        (GOLDEN_DIR / f"{case_id}.stdout").write_text(run.stdout, encoding="utf-8")
        manifest[case_id] = run.as_record()
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------

# The lines the migration may change. Everything not listed here must stay
# byte-identical.
#  1. the header: a first line of the form ``… results for "q" (…)`` keeps
#     everything but its trailing parenthesis (the corpus label; the query
#     still has to match), a first line of the form ``… Document Index (…)``
#     is dropped, and so are the ``(index: …)`` / ``(file: …)`` /
#     ``(source: …)`` / ``Cache:`` lines in the header block (up to the first
#     blank or ``===`` line; ``sections`` prints its ``URL:`` line before the
#     generic loader's ``(file: …)``)
#  2. ``Next:`` lines are kept with the script name replaced by ``<script>``
#     and the corpus-selecting options (``--source`` / ``--cache-dir`` /
#     ``--file`` / ``--index-file`` / ``--sources-file`` and their value)
#     removed; the subcommand, page ref, heading and other options still count
#  3. the trailing count line ``(N … total …)`` (firebase's paged index
#     says ``(N of M pages shown, …)``)
#  4. ``Tip:`` / ``Note:`` lines, except in ``content`` output (first line
#     ``# doc_title:``), where they are page body and must match
#  5. the unit word ``page`` / ``document`` as a whole word (not inside a
#     flag name such as ``--page-ref``, an identifier or a path) and the
#     ``URL:`` / ``url:`` case
# ``compare`` looks at stdout; when the expected stdout is empty (an error
# path) it also compares the first line of the normalised stderr.
_HEADER_FOLLOW_RE = re.compile(r"^\s*(\((index|file|source): .*\)|Cache: .*)$")
_HEADER_TAIL_RE = re.compile(r"\s*\((?:[^()]|\([^()]*\))*\)\s*$")
_NEXT_RE = re.compile(r"^\s*Next: ")
_NEXT_SCRIPT_RE = re.compile(r"^(\s*Next: )\S+\.py ")
_NEXT_CORPUS_OPT_RE = re.compile(r" --(?:source|cache-dir|file|index-file|sources-file) \S+")
_TIP_RE = re.compile(r"^\s*(Tip|Note): ")
_COUNT_RE = re.compile(r"^\(\d+ [^()]*\b(?:total|shown)\b[^()]*\)$")
_URL_LABEL_RE = re.compile(r"^(\s*)(?:URL|url): ")
_UNIT_RE = re.compile(r"(?<![-\w/<])(?:page|document)(s?)(?![-\w/>])")


def _is_index_header(line: str) -> bool:
    """A first line that names the corpus index (``… Document Index (…)``)."""
    return bool(re.search(r"Document(?:ation)? Index", line))


def normalize(text: str) -> list[str]:
    lines = text.split("\n")
    out: list[str] = []
    head_end = 1  # the header block: line 1 up to the first blank / ``===`` line
    while (head_end < len(lines) and lines[head_end].strip()
           and not lines[head_end].startswith("===")):
        head_end += 1
    head = [line for line in lines[1:head_end] if not _HEADER_FOLLOW_RE.match(line)]
    if lines and re.search(r'results? for "', lines[0]):
        head.insert(0, _HEADER_TAIL_RE.sub("", lines[0]))
    elif lines and not _is_index_header(lines[0]):
        head.insert(0, lines[0])
    body = head + lines[head_end:]
    if not (lines and lines[0].startswith("# doc_title:")):
        body = [line for line in body if not _TIP_RE.match(line)]
    # the count line: the last non-empty line that is not a Next: line
    k = len(body) - 1
    while k >= 0 and (not body[k].strip() or _NEXT_RE.match(body[k])):
        k -= 1
    if k >= 0 and _COUNT_RE.match(body[k]):
        del body[k]
    for line in body:
        if _NEXT_RE.match(line):
            line = _NEXT_SCRIPT_RE.sub(r"\1<script> ", line)
            line = _NEXT_CORPUS_OPT_RE.sub("", line)
        line = _URL_LABEL_RE.sub(r"\1url: ", line)
        out.append(_UNIT_RE.sub(r"<unit>\1", line))
    return out


def compare(expected: str, actual: str, expected_err: str = "", actual_err: str = "") -> list[str]:
    """Unified diff of the normalised stdout; empty when they match. When the
    expected stdout is empty, the first line of the normalised stderr is
    compared too (the error message is then the whole answer)."""
    exp, act = normalize(expected), normalize(actual)
    if not expected.strip():
        exp = exp + ["stderr: " + (normalize(expected_err) or [""])[0]]
        act = act + ["stderr: " + (normalize(actual_err) or [""])[0]]
    return list(difflib.unified_diff(exp, act, "golden", "actual", lineterm="", n=1))


# ---------------------------------------------------------------------------
# Generic loader with provisional profiles (for later migration steps)
# ---------------------------------------------------------------------------

GENERIC_PROFILES = {
    "claude-code": {"url": "https://code.claude.com/docs/llms-full.txt", "split": "h1",
                    "page_url": "line:Source: ", "index_url": "https://code.claude.com/docs/llms.txt"},
    "claude-platform": {"url": "https://platform.claude.com/llms-full.txt", "split": "frontmatter",
                        "page_url": "frontmatter:url", "index_url": "https://platform.claude.com/llms.txt"},
    "ai-sdk": {"url": "https://ai-sdk.dev/llms-full.txt", "split": "frontmatter",
               "page_url": "frontmatter:url"},
}

# Cases whose generic-loader output already matches the golden (normalised
# stdout, the first stderr line when stdout is empty, and exit code; no more
# fetches). Later steps add to this as they port each behaviour; it does not
# shrink unless the comparator is tightened. Taken out when ``Next:`` lines
# and stderr started to count:
#   cd-code-search-content-zero: the old script prints a second ``Next:``
#     line (``search-content <query>``) the generic loader does not
#   cd-code-content-missing: ``No page found for slug: …`` vs. the generic
#     loader's ``No page found for: …``
GENERIC_PARITY_EXPECTED: frozenset = frozenset({
    "cd-code-sections-int",
    "cd-plat-content-heading",
    "as-search-index-zero",
    "as-search-content-page",
    "as-sections",
    "as-content-heading",
    "as-search-zero",
})


def _generic_argv(corpus: str, argv: list, cache_dir: str, sources_file: str) -> list | None:
    if corpus == "firebase":
        return None  # no per-page fetch mode in the generic loader yet
    argv = list(argv)
    if corpus == "ai-sdk":
        name, full, index = "ai-sdk", "ai-sdk-llms-full.txt", None
    else:
        source = "code"
        if "--source" in argv:
            i = argv.index("--source")
            source = argv[i + 1]
            del argv[i:i + 2]
        if source == "both":
            return None  # multi-source search is a later step
        name = f"claude-{source}"
        full, index = f"{name}-llms-full.txt", f"{name}-llms.txt"
    out = [*argv, "--source", name, "--sources-file", sources_file,
           "--file", os.path.join(cache_dir, full)]
    if index:
        out += ["--index-file", os.path.join(cache_dir, index)]
    return out


def run_generic_case(case_id: str, *, source_dir: Path | None = None) -> _Run | None:
    _, corpus, argv = next(c for c in CASES if c[0] == case_id)
    tmp = tempfile.mkdtemp(prefix="llms-parity-")
    try:
        stage_corpus(corpus, tmp, source_dir=source_dir)
        sources = os.path.join(tmp, "sources.json")
        with open(sources, "w", encoding="utf-8") as f:
            json.dump({"sources": GENERIC_PROFILES}, f)
        g_argv = _generic_argv(corpus, argv, tmp, sources)
        if g_argv is None:
            return None
        return run_script("parse-llms-txt.py", g_argv, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class GoldenSelfCheckTest(unittest.TestCase):
    """The old scripts still print exactly the recorded golden."""

    def test_every_case_has_a_golden(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest), sorted(CASE_IDS))
        self.assertEqual(len(set(CASE_IDS)), len(CASE_IDS))
        for case_id in CASE_IDS:
            self.assertTrue((GOLDEN_DIR / f"{case_id}.stdout").is_file(), case_id)

    def test_old_scripts_match_golden_byte_for_byte(self):
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                stdout, record = load_golden(case_id)
                run = run_legacy_case(case_id)
                self.assertEqual(run.stdout, stdout)
                self.assertEqual(run.as_record(), record)

    def test_cases_cover_every_subcommand_of_every_script(self):
        commands = {"fetch-index", "search-index", "search-content", "sections", "content", "search"}
        for corpus in SCRIPTS:
            seen = {argv[0] for _, c, argv in CASES if c == corpus}
            self.assertEqual(seen, commands, corpus)

    def test_golden_is_not_degenerate(self):
        """Each case printed something, most succeeded, and only the cases
        that are meant to touch the (failing) network did."""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for case_id in CASE_IDS:
            stdout, record = load_golden(case_id)
            with self.subTest(case=case_id):
                self.assertTrue(stdout.strip() or record["stderr"].strip())
                if not case_id.startswith("fb-"):
                    self.assertEqual(record["fetches"], 0)
        ok = sum(1 for r in manifest.values() if r["exit"] == 0)
        self.assertGreaterEqual(ok, len(CASE_IDS) - 6)
        # the firebase cases do exercise the mocked fetch path
        self.assertGreater(sum(r["fetches"] for k, r in manifest.items() if k.startswith("fb-")), 0)


class ComparatorTest(unittest.TestCase):
    """The comparator ignores only the listed lines and catches the rest."""

    def test_identical_golden_compares_clean(self):
        for case_id in CASE_IDS:
            stdout, _ = load_golden(case_id)
            self.assertEqual(compare(stdout, stdout), [], case_id)

    # The only lines a one-line change may go unnoticed on, written out here
    # rather than taken from ``normalize``: a ``normalize`` that excludes too
    # much would otherwise vouch for itself. Besides these, line 1 (an index
    # header) may be skipped.
    _EXCLUDED_PREFIXES = ("(index: ", "(file: ", "(source: ", "Cache: ", "Tip: ", "Note: ",
                          *(f"({d}" for d in "0123456789"))

    def test_one_changed_line_is_detected_in_every_case(self):
        """Negative test: append text to any one line of any golden and the
        comparator reports it, unless the line is one of the excluded forms
        above. The number of skipped lines is pinned, so an exclusion that
        grows (even within those forms) fails here."""
        tried = skipped = 0
        for case_id in CASE_IDS:
            stdout, _ = load_golden(case_id)
            lines = stdout.split("\n")
            kept = normalize(stdout)
            for i, line in enumerate(lines):
                mutated = "\n".join(lines[:i] + [line + " [parity-mutation]"] + lines[i + 1:])
                if normalize(mutated) == kept:
                    self.assertTrue(i == 0 or line.lstrip().startswith(self._EXCLUDED_PREFIXES),
                                    f"{case_id}:{i + 1} skipped: {line!r}")
                    skipped += 1
                    continue
                tried += 1
                self.assertNotEqual(compare(stdout, mutated), [], f"{case_id}:{i + 1}")
        # 20 = 6 ``… Document Index (…)`` first lines + 9 ``Tip:`` / 1 ``Note:``
        # hint lines + 1 ``Tip:`` / 1 ``Note:`` page line quoted in a
        # search-content hit (indented; only ``content`` output keeps them)
        # + 2 firebase ``Cache:`` lines in the header block.
        self.assertEqual(skipped, 20)
        self.assertEqual(tried, 1504)

    def test_dropped_and_reordered_lines_are_detected(self):
        stdout, _ = load_golden("cd-code-search-index")
        lines = stdout.split("\n")
        body = [i for i, l in enumerate(lines) if l.strip() and not _NEXT_RE.match(l)][2:]
        self.assertTrue(body)
        dropped = lines[:body[0]] + lines[body[0] + 1:]
        self.assertNotEqual(compare(stdout, "\n".join(dropped)), [])
        swapped = list(lines)
        a, b = body[0], body[-1]
        self.assertNotEqual(swapped[a], swapped[b])
        swapped[a], swapped[b] = swapped[b], swapped[a]
        self.assertNotEqual(compare(stdout, "\n".join(swapped)), [])

    def test_excluded_lines_are_ignored(self):
        golden = (
            'Search results for "q" (Claude Code)\n'
            "  (index: /a/claude-code-llms.txt)\n"
            "[3] Page title\n"
            "    URL: https://example.com/a\n"
            "    3 pages matched\n"
            "Tip: try search-content\n"
            "(12 pages total, 3 entries shown)\n"
            "\n"
            "Next: parse-claude-docs.py content 3 --cache-dir /a\n"
        )
        actual = (
            'Search results for "q" (source: claude-code, file: /b/x.txt)\n'
            "[3] Page title\n"
            "    url: https://example.com/a\n"
            "    3 documents matched\n"
            "Note: something else\n"
            "(12 documents total)\n"
            "\n"
            "Next: parse-llms-txt.py content 3 --source claude-code --sources-file /b/s.json\n"
        )
        self.assertEqual(compare(golden, actual), [])

    def test_file_line_later_in_the_header_block_is_ignored(self):
        """``sections``: the generic loader prints ``(file: …)`` after the
        ``URL:`` line, still above the ``===`` rule."""
        golden = ('Sections in [6] "Checkpointing"\n'
                  "  URL: https://example.com/checkpointing\n"
                  "======\n"
                  "[L2] How it works\n")
        actual = ('Sections in [6] "Checkpointing"\n'
                  "  url: https://example.com/checkpointing\n"
                  "  (file: /b/x.txt)\n"
                  "======\n"
                  "[L2] How it works\n")
        self.assertEqual(compare(golden, actual), [])

    def test_excluded_forms_still_catch_real_differences(self):
        """Each exclusion is narrow: the same shapes with a changed payload
        outside the excluded part are still reported."""
        base = "[3] Page title\n    URL: https://example.com/a\n    3 pages matched\n"
        sections = 'Sections in [6] "A"\n  URL: https://example.com/a\n======\n[L2] B\n'
        header_golden, _ = load_golden("cd-code-search-index")
        cases = {
            "url value": (base, "[3] Page title\n    url: https://example.com/b\n    3 pages matched\n"),
            "number next to unit": (base, "[3] Page title\n    URL: https://example.com/a\n    4 documents matched\n"),
            "title": (base, "[3] Page titles\n    URL: https://example.com/a\n    3 pages matched\n"),
            "Next: not at line start is content": (base, base + "see Next: below\n"),
            "count line not last": (base, "(3 pages total)\n" + base),
            "Tip without colon-space": (base, base + "Tips and tricks\n"),
            "header shape not on line 1": ("[1] x\n", "[1] x\n" + 'Search results for "q"\n'),
            "line 1 that is not a header": (sections, sections.replace("[6]", "[7]")),
            "(file: …) below the header block": (sections, sections + "  (file: /b/x.txt)\n"),
            "content title line": ("# doc_title: A\n---\nbody\n", "# doc_title: B\n---\nbody\n"),
            "parenthesised line that is not a count": ("[1] x\n\n(7 sections)\n", "[1] x\n\n(8 sections)\n"),
            "non-path line in the header block": (
                'Search results for "q" (Firebase)\n  (top-5 candidate pages fetched on demand)\n\n[1] x\n',
                'Search results for "q" (Firebase)\n  (top-6 candidate pages fetched on demand)\n\n[1] x\n'),
            "header query": (header_golden, header_golden.replace('"snapshot"', '"snapshots"', 1)),
            "header query with parentheses": ('Search results for "f(x)" (Claude Code)\n\n[1] x\n',
                                              'Search results for "f(y)" (Claude Code)\n\n[1] x\n'),
            "body Note line in content": ("# doc_title: A\n---\nNote: x\n", "# doc_title: A\n---\n"),
            "body Tip line in content": ("# doc_title: A\n---\nTip: x\n", "# doc_title: A\n---\nTip: y\n"),
            "flag name with unit word": ("Next: x.py search-content q --page-ref 3\n",
                                         "Next: x.py search-content q --document-ref 3\n"),
            "unit word inside an identifier or path": ("[1] x\n    page_ref /docs/page\n",
                                                       "[1] x\n    document_ref /docs/document\n"),
            "Next subcommand": ("[1] x\n\nNext: x.py sections 3 --cache-dir <CACHE_DIR>\n",
                                "[1] x\n\nNext: x.py content 3 --cache-dir <CACHE_DIR>\n"),
            "Next ref": ("[1] x\n\nNext: parse-claude-docs.py content 3 Example --cache-dir <CACHE_DIR>\n",
                         "[1] x\n\nNext: parse-claude-docs.py content 4 Example --cache-dir <CACHE_DIR>\n"),
            "Next option that is not a corpus option": (
                "[1] x\n\nNext: x.py content 3 --max-chars 3000 --cache-dir <CACHE_DIR>\n",
                "[1] x\n\nNext: x.py content 3 --max-chars 4000 --cache-dir <CACHE_DIR>\n"),
            "line starting with Next but not Next:": ("[1] x\n\nNext steps --file a.txt\n",
                                                     "[1] x\n\nNext steps --file b.txt\n"),
            "stderr when stdout is empty": ("", ""),
        }
        stderr = {"stderr when stdout is empty": ("Error: No page found for slug: a\n",
                                                  "Error: No page found for: a\n")}
        for label, (golden, actual) in cases.items():
            with self.subTest(label=label):
                self.assertNotEqual(compare(golden, actual, *stderr.get(label, ("", ""))), [])

    def test_stderr_is_compared_only_on_its_first_line_and_only_without_stdout(self):
        err_a = "Error: No page found for: a\n  (hint one)\n"
        err_b = "Error: No page found for: a\n  (hint two)\n"
        self.assertEqual(compare("", "", err_a, err_b), [])
        self.assertEqual(compare("[1] x\n", "[1] x\n", "WARNING: a\n", "WARNING: b\n"), [])
        # a stderr that normalises to nothing (an index-header-shaped line)
        # compares as an empty first line instead of crashing
        try:
            self.assertEqual(compare("", "", "A Document Index", "A Document Index"), [])
        except IndexError as e:
            self.fail(f"compare crashed on a stderr that normalises away: {e!r}")


class GenericLoaderHarnessTest(unittest.TestCase):
    """The generic-loader side of the harness runs (parity itself is filled
    in step by step through ``GENERIC_PARITY_EXPECTED``)."""

    def test_provisional_profiles_load_the_fixtures(self):
        for case_id in ("cd-code-fetch-index", "cd-plat-fetch-index", "as-fetch-index"):
            with self.subTest(case=case_id):
                run = run_generic_case(case_id)
                self.assertEqual(run.code, 0, run.stderr)
                self.assertEqual(run.fetches, 0)
                self.assertTrue(run.stdout.strip())

    def test_expected_parity_cases_match(self):
        for case_id in sorted(GENERIC_PARITY_EXPECTED):
            with self.subTest(case=case_id):
                stdout, record = load_golden(case_id)
                run = run_generic_case(case_id)
                self.assertEqual(compare(stdout, run.stdout, record["stderr"], run.stderr), [])
                self.assertEqual(run.code, record["exit"])
                self.assertLessEqual(run.fetches, record["fetches"])


@unittest.skipUnless(os.environ.get(CORPUS_ENV) == "1",
                     f"local-only full-corpus gate ({CORPUS_ENV}=1)")
class CorpusParityGateTest(unittest.TestCase):
    """The same cases on the full local cache (whatever corpora it holds).

    Every case runs on the old scripts without a crash, and every case in
    ``GENERIC_PARITY_EXPECTED`` matches between the old script and the
    generic loader on the full corpus too, not just on the cut-out fixture.
    """

    def setUp(self):
        self.cache = Path(os.environ.get("LLMS_DOCS_CACHE_DIR")
                          or os.path.join(os.environ.get("XDG_CACHE_HOME")
                                          or os.path.expanduser("~/.cache"), "llms-docs"))

    def _cached(self, case_id: str) -> bool:
        corpus = next(c for c in CASES if c[0] == case_id)[1]
        return all((self.cache / n).exists() for n in CORPORA[corpus])

    def test_old_scripts_run_every_case(self):
        for case_id in filter(self._cached, CASE_IDS):
            with self.subTest(case=case_id):
                run = run_legacy_case(case_id, source_dir=self.cache)
                self.assertIn(run.code, (0, 1), run.stderr)

    def test_expected_parity_cases_match_on_the_full_corpus(self):
        for case_id in filter(self._cached, sorted(GENERIC_PARITY_EXPECTED)):
            with self.subTest(case=case_id):
                old = run_legacy_case(case_id, source_dir=self.cache)
                new = run_generic_case(case_id, source_dir=self.cache)
                self.assertEqual(compare(old.stdout, new.stdout, old.stderr, new.stderr), [])
                self.assertEqual(new.code, old.code)


if __name__ == "__main__":
    if "--update-golden" in sys.argv:
        update_golden()
        print(f"golden written to {GOLDEN_DIR}")
    elif "--report-generic" in sys.argv:
        # ``--corpus DIR``: compare the old script and the generic loader on a
        # full cache directory instead of golden vs. generic on the fixture.
        corpus_dir = Path(sys.argv[sys.argv.index("--corpus") + 1]) if "--corpus" in sys.argv else None
        for cid in CASE_IDS:
            r = run_generic_case(cid, source_dir=corpus_dir)
            if r is None:
                print(f"{cid}: (no generic equivalent yet)")
                continue
            if corpus_dir is None:
                stdout, record = load_golden(cid)
                ref = _Run(record["exit"], stdout, record["stderr"], record["fetches"])
            else:
                ref = run_legacy_case(cid, source_dir=corpus_dir)
            diff = compare(ref.stdout, r.stdout, ref.stderr, r.stderr)
            n = sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))
            same = "MATCH" if not diff and r.code == ref.code else "differ"
            print(f"{cid}: {same}: exit {ref.code}->{r.code}, {n} differing lines")
    else:
        unittest.main()
