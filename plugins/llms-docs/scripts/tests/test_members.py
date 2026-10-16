"""``members``: one ``--source`` that ``search`` runs on several sources.

A profile ``{"members": ["claude-code", "claude-platform"]}`` is an alias:
``search --source <alias>`` searches each member in turn (the old
``parse-claude-docs.py search --source both``), groups the results under
``--- <label> (--source <member>) ---``, leads each result with
``[<member>]`` and gives every ``Next:`` line its page's own ``--source``.
Page numbers are per source, so the alias itself opens no page: the other
commands refuse it and name the members.

The checks here:

* validation of the key (shape, self, unknown and nested members, keys that
  cannot go with it, user files) and the refusals (other commands,
  ``--file`` / ``--index-file``);
* the round trip over every combination of member kinds (both ranked on
  ``llms.txt``, both ranked on their pages, one of each) and cache states
  (all cached, none cached and fetched, only one cached): each group shows
  the rows ``search --source <member>`` shows, and every row number and
  every ``Next:`` line, run as printed, opens the row's page in that member;
* a member that cannot be read stops the whole search before anything is
  printed (no cache and no network, a join rate under the guard), while a
  stale cache is used with a warning as for one source;
* awkward queries (leading ``-``, ``\\_``, function words only, symbols
  only) behave as they do on each member alone.

The fixtures are the synthetic Claude docs and AI SDK corpora of the
migration harness; ``MembersCorpusRoundTripTest`` repeats the round trip on
the full local cache when ``LLMS_DOCS_PARITY_CORPUS=1``.
"""

import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _loader
import test_migration_parity as parity

g = _loader.load_script("parse-llms-txt.py")
SCRIPT = "parse-llms-txt.py"

CODE_FULL = "https://code.claude.com/docs/llms-full.txt"
# fixture file per URL, for the mocked network
_SERVED = {
    CODE_FULL: ("claude-docs", "claude-code-llms-full.txt"),
    "https://code.claude.com/docs/llms.txt": ("claude-docs", "claude-code-llms.txt"),
    "https://platform.claude.com/llms-full.txt": ("claude-docs", "claude-platform-llms-full.txt"),
    "https://platform.claude.com/llms.txt": ("claude-docs", "claude-platform-llms.txt"),
    "https://ai-sdk.dev/llms-full.txt": ("ai-sdk", "ai-sdk-llms-full.txt"),
}

PROFILES = {
    **{k: v for k, v in parity.GENERIC_PROFILES.items() if "members" not in v},
    # claude-code read as plain pages (ranked on the pages, no llms.txt)
    "code-pages": {"url": CODE_FULL, "split": "h1", "page_url": "line:Source: ", "label": "Code pages"},
    "claude": {"members": ["claude-code", "claude-platform"]},
    "mixed": {"members": ["claude-code", "ai-sdk"]},
    "pages": {"members": ["code-pages", "ai-sdk"]},
}

# alias -> a query with a result in every member on the fixtures
COMPOSITIONS = {"claude": "sample", "mixed": "text", "pages": "text"}
CACHE_STATES = ("all", "none", "first", "second")


class _Resp:
    def __init__(self, data: bytes, url: str):
        self._data, self.headers, self.url = data, {}, url

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _network(calls: list, root: Path, *, down: bool = False):
    def urlopen(req, *a, **kw):
        url = getattr(req, "full_url", req)
        calls.append(url)
        if down or url not in _SERVED:
            raise OSError("members test: network down")
        corpus, name = _SERVED[url]
        return _Resp((root / corpus / name).read_bytes(), url)
    return urlopen


class _Env:
    """A cache dir (with a space in its path, so the hints' quoting is used)
    and a sources file with ``PROFILES``."""

    def __init__(self, testcase, profiles=None, root: Path | None = None):
        self.root = root or parity.FIXTURES
        self.dir = tempfile.mkdtemp(prefix="llms members ")
        testcase.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.profiles = profiles or PROFILES
        self.sources = os.path.join(self.dir, "sources.json")
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": self.profiles}, f)

    def stage(self, name: str) -> None:
        """Put source *name*'s files into the cache under the loader's names."""
        profile = {"name": name, **self.profiles[name]}
        targets = [(profile["url"], g._cache_path(self.dir, profile))]
        if profile.get("index_url"):
            targets.append((profile["index_url"], g._index_cache_path(self.dir, profile)))
        for url, target in targets:
            corpus, file = _SERVED[url]
            shutil.copyfile(self.root / corpus / file, target)

    def run(self, argv: list, *, down: bool = False, full_argv: bool = False):
        """``(exit, stdout, stderr, fetched URLs)``. *argv* gets ``--sources-file``
        and ``--cache-dir`` unless *full_argv* (a command line run as printed)."""
        if not full_argv:
            # before a ``--``, after which everything is positional
            at = argv.index("--") if "--" in argv else len(argv)
            argv = [SCRIPT, *argv[:at], "--sources-file", self.sources, "--cache-dir", self.dir, *argv[at:]]
        calls: list = []
        env = {k: v for k, v in os.environ.items() if k not in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME",
                                                                g.SOURCES_ENV)}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=_network(calls, self.root, down=down)):
            try:
                code, out, err = _loader.run_cli(g, argv)
            except Exception as e:  # a crash fails the assertions instead of erroring the run
                code, out, err = f"crash: {e!r}", "", f"crash: {e!r}"
        return code, out, err, calls


_ROW_RE = re.compile(r"^(?:\[([a-z0-9-]+)\] )?\[(\d+)\] ")


def groups_of(stdout: str) -> dict:
    """member -> its result block (``[member] `` taken off each row) of a
    multi-source search."""
    out: dict = {}
    current = None
    for line in stdout.split("\n"):
        if line == "No matching documents found.":
            break  # the per-member zero-hit notes below repeat the headings
        m = re.match(r"^--- .+ \(--source ([a-z0-9-]+)\) ---$", line)
        if m:
            current = m.group(1)
            out[current] = []
            continue
        if line.startswith(("Note: ", "No matching ", "Next: ")):
            current = None
        if current is not None:
            out[current].append(re.sub(rf"^\[{re.escape(current)}\] (?=\[\d+\] )", "", line))
    return {k: _rows_block(v) for k, v in out.items()}


def single_block(stdout: str) -> list:
    """The result block of a one-source search (between the header and the
    ``Next:`` lines)."""
    lines = stdout.split("\n")
    start = lines.index("=" * 60) + 1
    end = next((i for i, line in enumerate(lines) if line.startswith("Next: ")), len(lines))
    return _rows_block(lines[start:end])


def _rows_block(lines: list) -> list:
    """The rows and hit blocks, without the count line (worded per mode) and
    the blank lines around them."""
    body = [line for line in lines if not re.match(r"^\(\d+ documents", line)]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def expected_group(single_stdout: str, label: str) -> list:
    """What a member's group shows, from ``search --source <member>`` alone:
    its result block, or the ``(no matching …)`` line when it found nothing."""
    if "\nNo matching documents found.\n" in single_stdout:
        return [f"  (no matching documents in {label})"]
    return single_block(single_stdout)


def rows_of(stdout: str) -> list:
    """``[(member, ref, url)]`` of a multi-source search, in display order."""
    rows = []
    lines = stdout.split("\n")
    for i, line in enumerate(lines):
        m = _ROW_RE.match(line)
        if m and m.group(1):
            url = lines[i + 1].strip()[len("URL: "):] if lines[i + 1].strip().startswith("URL: ") else None
            rows.append((m.group(1), int(m.group(2)), url))
    return rows


def next_lines(stdout: str) -> list:
    return [line[len("Next: "):] for line in stdout.split("\n") if line.startswith("Next: ")]


def _page_url(content_stdout: str) -> str | None:
    m = re.search(r"^# source: (\S+)$", content_stdout, re.M)
    return m.group(1) if m else None


def _same_page(a: str, b: str) -> bool:
    return g._ref_url_key_with_query(a, None) == g._ref_url_key_with_query(b, None)


def _option(argv: list, name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class MembersValidationTest(unittest.TestCase):
    def _load(self, user: dict) -> tuple:
        """``(exit, stderr)`` of loading the presets plus *user*."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "sources.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"sources": user}, f)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            try:
                g.load_sources(path, explicit=True)
            except SystemExit as e:
                return e.code, err.getvalue()
            except Exception as e:
                self.fail(f"load_sources crashed: {e!r}")
        return 0, err.getvalue()

    def test_valid_alias_loads(self):
        code, err = self._load({**PROFILES})
        self.assertEqual((code, err), (0, ""))

    def test_alias_of_bundled_presets_in_a_user_file(self):
        presets = sorted(g._read_sources_file(g.PRESETS_FILE))
        code, err = self._load({"both": {"members": presets[:2], "description": "two presets"}})
        self.assertEqual((code, err), (0, ""))

    def test_bad_shapes_are_refused(self):
        base = {k: v for k, v in PROFILES.items() if "members" not in v}
        cases = {
            "not a list": ({"members": "claude-code"}, "must be a list of 2 or more"),
            "one member": ({"members": ["claude-code"]}, "must be a list of 2 or more"),
            "empty": ({"members": []}, "must be a list of 2 or more"),
            "duplicate": ({"members": ["claude-code", "claude-code"]}, "distinct"),
            "not a name": ({"members": ["claude-code", "../x"]}, "source names"),
            "not a string": ({"members": ["claude-code", 3]}, "source names"),
            "itself": ({"members": ["claude-code", "x"]}, "must not name x itself"),
            "member that is an alias": ({"members": ["claude-code", "claude"]},
                                        "'claude', which lists members itself"),
            "key of a file profile": ({"members": ["claude-code", "ai-sdk"], "url": CODE_FULL},
                                      "url cannot go with it"),
            "label": ({"members": ["claude-code", "ai-sdk"], "label": "X"}, "label cannot go with it"),
            "description not a string": ({"members": ["claude-code", "ai-sdk"], "description": 1},
                                         "description must be a string"),
        }
        for label, (raw, want) in cases.items():
            with self.subTest(label):
                profiles = {**base, "claude": PROFILES["claude"], "x": raw}
                code, err = self._load(profiles)
                self.assertEqual(code, 1, err)
                self.assertIn(want, err)

    def test_user_override_turning_a_member_into_an_alias_is_refused(self):
        """A member overridden (in a later file) by an alias: the alias that
        names it is refused, naming where the other alias comes from."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        presets = os.path.join(tmp, "presets.json")
        with open(presets, "w", encoding="utf-8") as f:
            json.dump({"sources": {k: PROFILES[k] for k in ("claude-code", "claude-platform", "ai-sdk", "claude")}}, f)
        user = os.path.join(tmp, "user.json")
        with open(user, "w", encoding="utf-8") as f:
            json.dump({"sources": {"claude-platform": {"members": ["claude-code", "ai-sdk"]}}}, f)
        err = io.StringIO()
        with mock.patch.object(g, "PRESETS_FILE", presets), mock.patch("sys.stderr", err):
            with self.assertRaises(SystemExit):
                g.load_sources(user, explicit=True)
        self.assertIn("sources.claude.members names 'claude-platform', which lists members itself", err.getvalue())
        self.assertIn(user, err.getvalue())

    def test_user_profile_replaces_an_alias_of_the_same_name(self):
        """A user profile replaces the bundled one of its name, alias or not."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        presets = os.path.join(tmp, "presets.json")
        with open(presets, "w", encoding="utf-8") as f:
            json.dump({"sources": {k: PROFILES[k] for k in ("claude-code", "claude-platform", "claude")}}, f)
        user = os.path.join(tmp, "user.json")
        with open(user, "w", encoding="utf-8") as f:
            json.dump({"sources": {"claude": PROFILES["claude-code"]}}, f)
        with mock.patch.object(g, "PRESETS_FILE", presets), mock.patch("sys.stderr", io.StringIO()):
            try:
                sources = g.load_sources(user, explicit=True)
            except SystemExit as e:
                self.fail(f"refused a user profile replacing an alias: exit {e.code}")
        self.assertIsNone(sources["claude"].get("members"))
        self.assertEqual(sources["claude"]["url"], CODE_FULL)

    def test_members_is_not_a_split_candidate_key(self):
        raw = {"url": CODE_FULL, "split": "auto", "split_candidates": [
            {"split": "h1", "page_url": "line:Source: ", "members": ["a", "b"]},
            {"split": "frontmatter", "page_url": "frontmatter:url"}]}
        code, err = self._load({"x": raw})
        self.assertEqual(code, 1)
        self.assertIn("has keys shared by the candidates: members", err)

    def test_sources_lists_an_alias_with_its_members(self):
        env = _Env(self)
        argv = [SCRIPT, "sources", "--sources-file", env.sources]
        code, out, err, _calls = env.run(argv, full_argv=True)
        self.assertEqual(code, 0, err)
        self.assertIn("claude  [user, members, search only]  claude-code + claude-platform", out)


class MembersMissingMemberTest(unittest.TestCase):
    """A member that is not a configured source (a bundled preset renamed or
    removed by a plugin update) stops only what uses that alias: loading
    still works, every other source and ``sources`` still run, and a search
    on the alias names the missing member."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llms members missing ")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sources = os.path.join(self.tmp, "sources.json")
        with open(self.sources, "w", encoding="utf-8") as f:
            json.dump({"sources": {"web": {"members": ["hono", "zod-renamed"]}}}, f)
        self.page = os.path.join(self.tmp, "llms-full.txt")
        with open(self.page, "w", encoding="utf-8") as f:
            f.write("# Routing\n\nThe x router matches paths.\n\n# Middleware\n\nRuns before x handlers.\n")

    def _run(self, argv: list):
        argv = [SCRIPT, *argv, "--sources-file", self.sources]
        if argv[1] != "sources":  # the only command without a cache
            argv += ["--cache-dir", self.tmp]
        calls: list = []
        env = {k: v for k, v in os.environ.items() if k not in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME",
                                                                g.SOURCES_ENV)}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=_network(calls, parity.FIXTURES, down=True)):
            try:
                return _loader.run_cli(g, argv)
            except Exception as e:
                self.fail(f"crashed: {e!r}")

    def test_other_source_still_searches(self):
        code, out, err = self._run(["search", "x", "--source", "hono", "--file", self.page])
        self.assertEqual(code, 0, err)
        self.assertIn("Routing", out)

    def test_sources_marks_the_alias_broken(self):
        code, out, err = self._run(["sources"])
        self.assertEqual(code, 0, err)
        self.assertIn("web  [user, members, broken: zod-renamed]  hono + zod-renamed", out)
        self.assertRegex(out, r"(?m)^hono  \[preset, ")

    def test_search_on_the_alias_names_the_missing_member(self):
        code, out, err = self._run(["search", "x", "--source", "web"])
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("--source web lists members that are not configured sources: zod-renamed", err)

    def test_other_command_on_the_alias_names_the_missing_member(self):
        code, out, err = self._run(["content", "1", "--source", "web"])
        self.assertNotEqual(code, 0)
        self.assertIn("not configured sources: zod-renamed", err)

    def test_load_accepts_a_missing_member(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            try:
                sources = g.load_sources(self.sources, explicit=True)
            except SystemExit as e:
                self.fail(f"load refused a missing member: exit {e.code}: {err.getvalue()}")
            except Exception as e:  # a crash fails the assertion instead of erroring the run
                self.fail(f"load crashed on a missing member: {e!r}")
        self.assertEqual(sources["web"]["members"], ("hono", "zod-renamed"))


class MembersRefusalTest(unittest.TestCase):
    def test_other_commands_refuse_an_alias_and_name_its_members(self):
        env = _Env(self)
        for argv in (["fetch-index"], ["search-index", "kit"], ["search-content", "kit"],
                     ["sections", "1"], ["content", "1"]):
            with self.subTest(argv[0]):
                code, out, err, calls = env.run([*argv, "--source", "claude"])
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertEqual(calls, [])
                # this sources file redefines the presets claude / claude-code /
                # claude-platform, so each replacement is named first
                warned = [line for line in err.splitlines() if line.startswith("WARNING: ")]
                self.assertEqual([w.split()[4] for w in warned],
                                 ["sources.claude", "sources.claude-code", "sources.claude-platform"], err)
                self.assertEqual(err.splitlines()[len(warned)],
                                 "Error: --source claude names several sources (claude-code, claude-platform); "
                                 f"only 'search' reads several at once. Give '{argv[0]}' one of them: "
                                 "--source claude-code / --source claude-platform")

    def test_search_refuses_a_file_of_one_source(self):
        env = _Env(self)
        for opt in ("--file", "--index-file"):
            with self.subTest(opt):
                code, out, err, calls = env.run(["search", "kit", "--source", "claude", opt, env.sources])
                self.assertEqual(code, 2)
                self.assertEqual((out, calls), ("", []))
                self.assertIn(f"{opt} is a file of one source, and --source claude names several", err)

    def test_empty_query_is_refused_before_any_fetch(self):
        env = _Env(self)
        code, out, err, calls = env.run(["search", "  ", "--source", "claude"])
        self.assertEqual((code, out, calls), (1, "", []))
        self.assertIn("query must not be empty", err)


class SourceHintArgsTest(unittest.TestCase):
    """The ``_source_hint_args`` branch for an alias search."""

    def _args(self, **kw):
        ns = mock.Mock(spec=["source", "sources_file_explicit", "sources_file", "index_file", "members"])
        ns.source, ns.sources_file_explicit, ns.sources_file, ns.index_file = "claude", True, "/s.json", None
        ns.members = kw.get("members")
        return ns

    def test_branches(self):
        self.assertEqual(g._source_hint_args(self._args()), ("--source", "claude", "--sources-file", "/s.json"))
        self.assertEqual(g._source_hint_args(self._args(members=("a", "b"))), ("--sources-file", "/s.json"))
        self.assertEqual(g._source_hint_args(self._args(members=("a", "b")), "a"),
                         ("--source", "a", "--sources-file", "/s.json"))
        self.assertEqual(g._source_hint_args(self._args(), "b"), ("--source", "b", "--sources-file", "/s.json"))


class MembersRoundTripTest(unittest.TestCase):
    """Every member kind x every cache state: each group is the member's own
    search, and each row and ``Next:`` line opens the row's page."""

    root: Path = parity.FIXTURES
    compositions = COMPOSITIONS
    cache_states = CACHE_STATES

    def _check(self, env: _Env, alias: str, query: str, state: str, *, require_rows: bool = True):
        members = env.profiles[alias]["members"]
        staged = {"all": members, "none": [], "first": members[:1], "second": members[1:]}[state]
        for name in staged:
            env.stage(name)
        code, out, err, calls = env.run(["search", query, "--source", alias])
        self.assertEqual(code, 0, err)
        # what was not cached was fetched, and nothing that was
        for name in members:
            url = env.profiles[name]["url"]
            fetched_full = url in calls
            if name in staged:
                self.assertFalse(fetched_full, f"{name} cached but fetched")
            else:
                self.assertTrue(fetched_full, f"{name} not cached and not fetched")
        groups = groups_of(out)
        self.assertEqual(list(groups), list(members))
        for name in members:
            s_code, s_out, s_err, s_calls = env.run(["search", query, "--source", name])
            self.assertEqual(s_code, 0, s_err)
            self.assertEqual(s_calls, [], f"{name}: the single search fetched again")
            self.assertEqual(groups[name], expected_group(s_out, env.profiles[name].get("label") or name), name)
        rows = rows_of(out)
        if require_rows:
            self.assertEqual({m for m, _r, _u in rows}, set(members), out)
        for member, ref, url in rows:
            c_code, c_out, c_err, c_calls = env.run(["content", str(ref), "--source", member,
                                                     "--max-chars", "0"])
            self.assertEqual((c_code, c_calls), (0, []), c_err)
            self.assertTrue(_same_page(_page_url(c_out), url), (member, ref, url, _page_url(c_out)))
        hints = next_lines(out)
        self.assertTrue(hints)
        for hint in hints:
            argv = shlex.split(hint, comments=True)
            self.assertEqual(argv[0], SCRIPT)
            source = _option(argv, "--source")
            self.assertIn(source, members, hint)
            self.assertEqual(_option(argv, "--cache-dir"), env.dir, hint)
            self.assertEqual(_option(argv, "--sources-file"), env.sources, hint)
            h_code, h_out, h_err, h_calls = env.run(argv, full_argv=True)
            self.assertEqual((h_code, h_calls), (0, []), (hint, h_err))
            # ``content <ref> …``, or ``content <options> -- <ref> <heading>``
            ref = int(argv[argv.index("--") + 1] if "--" in argv else argv[2])
            row_urls = [u for m, r, u in rows if (m, r) == (source, ref)]
            self.assertTrue(row_urls, f"{hint} names no row")
            self.assertTrue(_same_page(_page_url(h_out), row_urls[0]), hint)
        return out

    def test_every_composition_and_cache_state(self):
        for alias, query in self.compositions.items():
            for state in self.cache_states:
                with self.subTest(alias=alias, cache=state):
                    self._check(_Env(self, root=self.root), alias, query, state)


class MembersFixtureCasesTest(MembersRoundTripTest):
    """Cases whose queries and pages are those of the fixtures."""

    def test_every_composition_and_cache_state(self):
        pass  # run by MembersRoundTripTest

    def test_index_rows_and_body_only_rows_and_hint_order(self):
        """``claude`` on "sample": the code group's top index row keeps the
        first ``Next:`` line, the platform body-only row the second."""
        env = _Env(self)
        out = self._check(env, "claude", "sample", "all")
        self.assertEqual([shlex.split(h)[1:3] + [_option(shlex.split(h), "--source")] for h in next_lines(out)],
                         [["content", "5", "claude-code"], ["content", "1", "claude-platform"]])
        self.assertIn("[claude-platform] [1] Memory spans [body-only]", out)

    def test_group_of_body_only_rows_and_group_without_rows(self):
        """"widgets": the code group has only full-body rows (with the notice
        a search of that source alone prints), the platform group none."""
        out = self._check(_Env(self), "claude", "widgets", "all", require_rows=False)
        self.assertIn("--- Claude Code (--source claude-code) ---\n\n  (no title/description match", out)
        self.assertIn("  (no matching documents in Claude Developer Platform)", out)

    def test_top_index_row_of_the_first_member_is_kept(self):
        """The ``Next:`` lines keep the top index row of the first member that
        has one (as one source keeps its own), not of a later member."""
        env = _Env(self)
        for name in ("claude-code", "claude-platform"):
            env.stage(name)
        with mock.patch.object(g, "hit_candidates", wraps=g.hit_candidates) as spy:
            code, _out, err, _calls = env.run(["search", "sample", "--source", "claude"])
        self.assertEqual(code, 0, err)
        self.assertEqual(spy.call_args.kwargs["keep"], (5, ("--source", "claude-code")))

    def test_zero_hits_point_each_member_at_search_index(self):
        env = _Env(self)
        for name in ("claude-code", "ai-sdk"):
            env.stage(name)
        code, out, _err, _calls = env.run(["search", "zzzmembersnohit", "--source", "mixed"])
        self.assertEqual(code, 0)
        self.assertIn("No matching documents found.", out)
        hints = next_lines(out)
        self.assertEqual([_option(shlex.split(h), "--source") for h in hints], ["claude-code", "ai-sdk"])
        for hint in hints:
            argv = shlex.split(hint, comments=True)
            self.assertEqual(argv[1], "search-index")
            h_code, _out, h_err, h_calls = env.run(argv, full_argv=True)
            self.assertEqual((h_code, h_calls), (0, []), h_err)

    def test_zero_hits_with_some_terms_present_suggest_a_search_per_member(self):
        """Terms each source has, never together: the zero-hit notes suggest a
        narrower ``search`` per member, on that member."""
        env = _Env(self)
        for name in ("claude-code", "ai-sdk"):
            env.stage(name)
        code, out, _err, _calls = env.run(["search", "pariatur zzzq zzzr", "--source", "mixed"])
        self.assertEqual(code, 0)
        self.assertIn("No matching documents found.", out)
        searches = [shlex.split(h, comments=True) for h in next_lines(out)]
        searches = [a for a in searches if a[1] == "search"]
        self.assertEqual([_option(a, "--source") for a in searches], ["claude-code", "ai-sdk"])
        for argv in searches:
            h_code, h_out, h_err, h_calls = env.run(argv, full_argv=True)
            self.assertEqual((h_code, h_calls), (0, []), h_err)
            self.assertNotIn("No matching documents found.", h_out)

    def test_awkward_queries_behave_as_on_each_member(self):
        """Leading ``-`` (after ``--``), ``\\_``, function words only, symbols
        only: the alias search exits as the members do and shows their rows;
        a ``Next:`` line that has to put ``--`` before a heading runs."""
        env = _Env(self)
        for name in ("claude-code", "ai-sdk"):
            env.stage(name)
        outcomes = {}
        for query in ("-kit", "weave\\_text", "the of and", "!!! ???", "--", "-"):
            with self.subTest(query=query):
                code, out, err, _c = env.run(["search", "--source", "mixed", "--", query])
                singles = [env.run(["search", "--source", n, "--", query]) for n in ("claude-code", "ai-sdk")]
                if any(s[0] != 0 for s in singles):
                    self.assertNotEqual(code, 0, out)
                    self.assertTrue(err.startswith("Error: "), err)
                    outcomes[query] = "refused"
                    continue
                self.assertEqual(code, 0, err)
                groups = groups_of(out)
                for n, single in zip(("claude-code", "ai-sdk"), singles):
                    self.assertEqual(groups.get(n), expected_group(single[1], PROFILES[n]["label"]), (query, n))
                hints = next_lines(out)
                for hint in hints:
                    argv = shlex.split(hint, comments=True)
                    h_code, _o, h_err, _hc = env.run(argv, full_argv=True)
                    self.assertEqual(h_code, 0, (hint, h_err))
                outcomes[query] = f"{len(rows_of(out))} rows, {len(hints)} hints"
        # the cases are not all refused or all empty
        self.assertEqual(outcomes, {"-kit": "7 rows, 3 hints", "weave\\_text": "0 rows, 2 hints",
                                    "the of and": "6 rows, 3 hints", "!!! ???": "0 rows, 2 hints",
                                    "--": "5 rows, 3 hints", "-": "10 rows, 3 hints"})


class MembersFailureTest(unittest.TestCase):
    """A member that cannot be read stops the search before any output."""

    def test_member_with_no_cache_and_no_network(self):
        env = _Env(self)
        env.stage("claude-code")
        code, out, err, calls = env.run(["search", "sample", "--source", "claude"], down=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("platform.claude.com", err)

    def test_member_with_a_stale_cache_is_used_with_a_warning(self):
        env = _Env(self)
        for name in ("claude-code", "claude-platform"):
            env.stage(name)
        profile = {"name": "claude-platform", **PROFILES["claude-platform"]}
        for path in (g._cache_path(env.dir, profile), g._index_cache_path(env.dir, profile)):
            os.utime(path, (1, 1))
        code, out, err, calls = env.run(["search", "sample", "--source", "claude"], down=True)
        self.assertEqual(code, 0, err)
        self.assertTrue(calls)
        self.assertIn("[claude-platform] [1] Memory spans", out)
        self.assertIn("WARNING", err)

    def test_member_under_the_join_rate_guard(self):
        """A platform index whose entries name no page: exit 2 for the whole
        search, nothing printed (the code group is not shown alone)."""
        env = _Env(self)
        for name in ("claude-code", "claude-platform"):
            env.stage(name)
        profile = {"name": "claude-platform", **PROFILES["claude-platform"]}
        index = g._index_cache_path(env.dir, profile)
        text = Path(index).read_text(encoding="utf-8")
        Path(index).write_text(text.replace("/docs/en/", "/docs/elsewhere/"), encoding="utf-8")
        code, out, err, _calls = env.run(["search", "sample", "--source", "claude"])
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertIn("Claude Developer Platform index↔full-text join rate", err)


@unittest.skipUnless(os.environ.get(parity.CORPUS_ENV) == "1",
                     f"local-only full-corpus gate ({parity.CORPUS_ENV}=1)")
class MembersCorpusRoundTripTest(MembersRoundTripTest):
    """The round trip on the full local cache (the Claude docs pair)."""

    root = None
    compositions = {"claude": "hooks"}
    cache_states = ("all",)

    def setUp(self):
        cache = Path(os.environ.get("LLMS_DOCS_CACHE_DIR")
                     or os.path.join(os.environ.get("XDG_CACHE_HOME")
                                     or os.path.expanduser("~/.cache"), "llms-docs"))
        if not all((cache / f).exists() for f in parity.CORPORA["claude-docs"]):
            self.skipTest(f"no Claude docs cache in {cache}")
        # the full cache keeps both corpora's files side by side
        self.root = _FlatRoot(cache)


class _FlatRoot:
    """``root / corpus / name`` for a cache dir that holds every file at its top."""

    def __init__(self, path: Path):
        self.path = path

    def __truediv__(self, corpus: str):
        return self.path


if __name__ == "__main__":
    unittest.main()
