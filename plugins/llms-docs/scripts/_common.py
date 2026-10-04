#!/usr/bin/env python3
"""Shared helpers for parse-ai-sdk.py / parse-claude-docs.py / parse-firebase.py.

Imported as a sibling module from each ``parse-*.py``. Callers prepend the
real script directory (``os.path.dirname(os.path.realpath(__file__))``) to
``sys.path`` so ``from _common import ...`` always resolves to this file
even when the parse script is invoked via a symlink. Using ``realpath``
(not ``abspath``) is load-bearing — otherwise a ``_common.py`` sitting next
to the symlink would shadow the real one.
"""

from __future__ import annotations

import difflib
import gzip
import hashlib
import html
import http.client
import json
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib


# ---------------------------------------------------------------------------
# Core: code-fence scanner
# ---------------------------------------------------------------------------

class FenceTracker:
    """Tracks whether the current line is inside a fenced code block.

    Recognises both CommonMark fence styles — backtick (```` ``` ````) and
    tilde (``~~~``). Per the spec a fence is closed only by a run of the
    *same* character at least as long as the opener, so a ``~~~`` inside a
    backtick block (or vice versa) is content, not a closer.

    A closer also carries no info string (CommonMark): inside an open block,
    a line such as ```` ```ts ```` is content. Taking it for a closer turns
    one unclosed block into an open/closed flip that lasts for the rest of
    the file (measured on an MDX-heavy corpus, where the flip hid 299 of 496
    page boundaries); with the rule the state recovers at the next bare
    closer.

    Two deliberate departures from CommonMark, both measured on MDX corpora:

    - Indentation is not limited to 0-3 spaces. Code blocks nested in JSX
      (``<Steps>`` / ``<Tabs>``) are indented 4+ spaces and still render as
      code. With the CommonMark limit, ``truncate_content`` cut inside them,
      and a block opened at column 0 but closed by an indented closer stayed
      open and hid the headings after it.
    - A run followed only by ``*/}`` ends a code block written inside an MDX
      comment (``{/* ... */}``) that opened outside any code block. It
      closes such a block and never opens one: the comment's opening line
      (``{/* ```sql``) is not a fence line, so opening on its last line
      would hide the text that follows. A ``{/*`` inside a code block is
      content (a page showing how to write a comment), and so is the
      ```` ``` */} ```` after it: that block closes at a bare closer.

    One narrow guard for the indentation departure: a bare run (no info
    string) indented 4+ spaces opens a block only once the next non-blank
    line confirms it by being indented at least as deep. When that line is
    shallower, the block would have no content at its own depth: the run is
    a stray closer left after an indented code block in a list item
    (Firebase writes ``       firebase::...`` code lines and then a lone
    ``       ```  ``), so no block opens. Until confirmed, the run and the
    blank lines after it are reported as outside (they hold no heading),
    so a caller that skips a line when the state before *or* after it is
    "inside" still sees the shallower line.
    """

    def __init__(self):
        self.in_fence = False
        self._fence_len = 0
        self._fence_char = ""
        self._in_mdx_comment = False  # a ``{/*`` seen outside a fence, not yet closed
        self._pending = None  # (indent, run, char) of a bare 4+-indented opener not yet confirmed

    def update(self, line: str) -> bool:
        """Update state for *line* and return True if inside a fence AFTER update."""
        if self._pending is not None:
            if not line.strip():
                return self.in_fence
            (indent, run, ch), self._pending = self._pending, None
            if len(line) - len(line.lstrip(" ")) >= indent:
                # content at the opener's depth: the block did open
                self.in_fence = True
                self._fence_len = run
                self._fence_char = ch
            # else: a stray closer, nothing opened; read this line afresh
        stripped = line.lstrip()
        for ch in ("`", "~"):
            if stripped.startswith(ch * 3):
                run = len(stripped) - len(stripped.lstrip(ch))
                after = stripped[run:].strip()
                mdx_comment_end = after == "*/}"
                if not self.in_fence:
                    if mdx_comment_end:
                        self._in_mdx_comment = False
                    else:
                        indent = len(line) - len(line.lstrip(" "))
                        if not after and indent >= 4:
                            self._pending = (indent, run, ch)
                        else:
                            self.in_fence = True
                            self._fence_len = run
                            self._fence_char = ch
                elif (ch == self._fence_char and run >= self._fence_len
                        and (not after or (mdx_comment_end and self._in_mdx_comment))):
                    self.in_fence = False
                    self._fence_len = 0
                    self._fence_char = ""
                    if mdx_comment_end:
                        self._in_mdx_comment = False
                break
        else:
            # not a fence line: track an MDX comment opened outside any block
            if not self.in_fence:
                start = line.rfind("{/*")
                if start >= 0 and "*/}" not in line[start:]:
                    self._in_mdx_comment = True
                elif "*/}" in line:
                    self._in_mdx_comment = False
        return self.in_fence


# ---------------------------------------------------------------------------
# Core: section (heading) extraction
# ---------------------------------------------------------------------------

def extract_sections(body_lines, min_level: int = 2):
    """Extract Markdown headings from *body_lines*.

    *min_level* sets the minimum heading level to collect: AI SDK uses 1 to
    capture H1 inside frontmatter-delimited documents, while Claude /
    Firebase use the default 2 because the page H1 is the document title
    itself and not part of the body.

    Returns a list of dicts:
        {
            "level": int,
            "title": str,
            "heading_path": str,       # slash-separated ancestor path
            "line_start": int,         # relative to body_lines
            "line_end": int,
            "has_code_blocks": bool,
        }
    """
    fence = FenceTracker()
    pattern = re.compile(r"^(#{%d,6})\s+(.+)" % min_level)
    headings = []

    for idx, line in enumerate(body_lines):
        was_in_fence = fence.in_fence
        fence.update(line)
        if was_in_fence or fence.in_fence:
            continue

        m = pattern.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            headings.append({
                "level": level,
                "title": title,
                "line_start": idx,
                "line_end": -1,
                "has_code_blocks": False,
            })

    for i, h in enumerate(headings):
        next_start = headings[i + 1]["line_start"] if i + 1 < len(headings) else len(body_lines)
        h["line_end"] = next_start
        section_text = "\n".join(body_lines[h["line_start"]:next_start])
        h["has_code_blocks"] = "```" in section_text

    path_stack = []
    for h in headings:
        while path_stack and path_stack[-1][0] >= h["level"]:
            path_stack.pop()
        path_stack.append((h["level"], h["title"]))
        h["heading_path"] = "/".join(t for _, t in path_stack)

    return headings


# ---------------------------------------------------------------------------
# Core: content extraction with code-fence and optional table protection
# ---------------------------------------------------------------------------

def _is_table_line(line: str) -> bool:
    """Check if line is part of a Markdown table."""
    stripped = line.strip()
    return bool(stripped) and stripped.startswith("|") and stripped.endswith("|")


def _extend_for_fence_and_table(content_lines, end_line, body_lines, protect_tables):
    """Extend *content_lines* (already sliced up to *end_line*) so it never
    ends mid code-fence or mid Markdown-table.

    Shared by every ``extract_content`` return path (the normal
    heading-section slice and the ``(top)`` preamble slice) so the two
    can't drift into inconsistent fence/table handling.
    """
    fence = FenceTracker()
    for line in content_lines:
        fence.update(line)
    if fence.in_fence:
        i = end_line
        while i < len(body_lines):
            content_lines.append(body_lines[i])
            fence.update(body_lines[i])
            i += 1
            if not fence.in_fence:
                break

    if protect_tables and content_lines and _is_table_line(content_lines[-1]):
        i = end_line
        while i < len(body_lines) and _is_table_line(body_lines[i]):
            content_lines.append(body_lines[i])
            i += 1

    return content_lines


def extract_content(body_lines, heading_path=None, *,
                    protect_tables: bool = True, min_level: int = 2,
                    retry=None):
    """Extract content from *body_lines*.

    If *heading_path* is None, return the entire body. Otherwise, find the
    matching section and return its content, extending the slice to include
    any unclosed code fence. When *protect_tables* is True, also extend to
    include a Markdown table that straddles the section boundary.

    *heading_path* of ``"(top)"`` is a reserved sentinel meaning "the
    preamble before the first heading" — the same value ``search`` prints
    as ``Section: (top)`` for a body hit above the first heading (see
    ``_build_section_results``). Passing that value back here (as the
    ``Next:`` hint tells the caller to) returns everything from the start
    of *body_lines* up to (not including) the first heading, rather than
    falling through to the heading-lookup below where no section is ever
    literally titled ``"(top)"``.

    *min_level* must match what the caller's ``cmd_sections`` prints — otherwise
    the AI agent sees one heading hierarchy but searches another, which lets
    a stray H1/H2 of the same title silently match the wrong section. AI SDK
    passes ``min_level=1`` (its body_lines include the H1); Claude/Firebase
    keep the default of 2 because Firebase hands the full page (H1 included)
    to this function and must not collapse an H1 onto an identically-named H2.

    Returns a tuple ``(content, resolved_heading_path)``. *resolved_heading_path*
    is ``None`` when *heading_path* was ``None`` (whole document), ``"(top)"``
    for the preamble case, or the matched section's canonical ``heading_path``
    otherwise. A caller-supplied *heading_path* that only partially matched
    (e.g. a bare title, or a case-insensitive substring) resolves to a
    *different*, fuller string here — callers should display the returned
    value in headers/hints, not echo back the raw argument, so a reader can
    tell which section was actually picked.

    A partial match (no exact ``heading_path``/``title`` hit, case-sensitive
    or not) that is ambiguous — two or more sections match the same
    case-insensitive substring — exits with an ``Error: ambiguous heading
    ...`` listing every candidate instead of silently picking the first one
    (mirroring how ``_resolve_page_ref``'s slug lookup handles an ambiguous
    slug). Exact matches are checked first and always win outright, so a
    ``heading_path`` copied verbatim from a previous
    ``sections``/``content``/``search`` call is never subject to this check
    even if it would *also* satisfy other sections' partial-match pattern.
    Heading matching is documented as case-insensitive, so the exact-match
    tier itself has two passes: case-sensitive first, then case-folded —
    both take the first matching section outright with no ambiguity check,
    the same as the case-sensitive form always has. Without the second
    pass, a case-differing exact match (e.g. querying ``"configuration"``
    against a page with ``## Configuration`` followed by a nested
    ``### Options``) would fall through to the substring tier and could
    die as "ambiguous" against its own descendant (``"Configuration/
    Options"`` also contains the substring ``"configuration"``), even
    though this exact heading exists and is not actually ambiguous.

    *retry*, when given, maps a heading path to the full command that reads
    it; the not-found and ambiguous errors use it to print candidates as
    commands that can be run as they are.
    """
    if heading_path is None:
        return "".join(body_lines), None

    sections = extract_sections(body_lines, min_level=min_level)

    if heading_path == "(top)":
        end_line = sections[0]["line_start"] if sections else len(body_lines)
        content_lines = _extend_for_fence_and_table(
            list(body_lines[:end_line]), end_line, body_lines, protect_tables
        )
        return "".join(content_lines), "(top)"

    # A full heading_path match is looked for in the whole page before a
    # bare title match: in one pass, an earlier nested ``### Error handling``
    # (title) would win over a later top-level ``## Error handling`` whose
    # heading_path is exactly the argument, and the commands this module
    # prints (always a full heading_path) would read another section.
    target = next((s for s in sections if s["heading_path"] == heading_path), None)
    if target is None:
        target = next((s for s in sections if s["title"] == heading_path), None)

    heading_lower = heading_path.lower()

    if target is None:
        # Case-folded exact match, still a second "exact" tier — not a
        # substring/partial one — so it also wins outright with no
        # ambiguity check. Skipping this and falling straight to the
        # substring tier below would wrongly treat a case-differing exact
        # match as merely "ambiguous" whenever it happens to also be a
        # substring of one of its own descendants' heading_path. Same
        # order as above: heading_path first, then title.
        target = next((s for s in sections if s["heading_path"].lower() == heading_lower), None)
        if target is None:
            target = next((s for s in sections if s["title"].lower() == heading_lower), None)

    if target is None:
        matches = [
            s for s in sections
            if heading_lower in s["heading_path"].lower() or heading_lower in s["title"].lower()
        ]
        if len(matches) > 1:
            die_ambiguous_heading(heading_path, matches, retry=retry)
        if matches:
            target = matches[0]

    if target is None:
        die_heading_not_found(heading_path, sections, retry=retry)

    target_level = target["level"]
    end_line = len(body_lines)
    found_target = False
    for s in sections:
        if s is target:
            found_target = True
            continue
        if found_target and s["level"] <= target_level:
            end_line = s["line_start"]
            break

    content_lines = _extend_for_fence_and_table(
        list(body_lines[target["line_start"]:end_line]), end_line, body_lines, protect_tables
    )

    return "".join(content_lines), target["heading_path"]


# ---------------------------------------------------------------------------
# Core: llms.txt lightweight index parser
# ---------------------------------------------------------------------------

_INDEX_ENTRY_RE = re.compile(
    r"^[-*+]\s+\[(.+?)\]\((https?://\S+?)\)(?:(?::\s*|\s+-\s+)(.+))?$"
)


def parse_llms_index(lines):
    """Parse a llms.txt lightweight index into page entries.

    Handles:
        - ``- [Title](URL): Description``
        - ``- [Title](URL) - Description``
        - ``- [Title](URL)``

    The bullet marker may be ``-``, ``*``, or ``+`` (all valid Markdown list
    markers) and leading indentation is ignored (line is stripped before
    matching) — an upstream source switching its bullet character between
    releases should not silently drop to 0 parsed entries (see internal
    backlog notes on llms.txt format-change detection).

    Returns a list of dicts: ``{"title": str, "url": str, "description": str}``.
    """
    entries = []
    for line in lines:
        m = _INDEX_ENTRY_RE.match(line.strip())
        if m:
            entries.append({
                "title": m.group(1),
                "url": m.group(2),
                "description": (m.group(3) or "").strip(),
            })
    return entries


# ---------------------------------------------------------------------------
# Core: URL normalization (llms.txt ↔ llms-full.txt join)
# ---------------------------------------------------------------------------

def normalize_doc_url(url: str) -> str:
    """Strip ``.md`` suffix, query, fragment, trailing slash for stable URL matching.

    ``llms.txt`` entries can include ``.md`` suffix (e.g.
    ``https://code.claude.com/docs/en/hooks.md``) while ``llms-full.txt``'s
    ``Source:`` line drops it. Normalising both sides lets us join entries
    1:1 across the two indexes without losing precision.
    """
    if not url:
        return ""
    u = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if u.endswith(".md"):
        u = u[:-3]
    return u


def build_url_to_full_index(docs) -> dict:
    """Map normalised ``source_url`` → index in *docs* (from ``split_documents``).

    Docs lacking a ``source_url`` are skipped silently.
    """
    out: dict = {}
    for i, d in enumerate(docs):
        nu = normalize_doc_url(d.get("source_url", ""))
        if nu:
            out[nu] = i
    return out


# ---------------------------------------------------------------------------
# Core: cache directory resolution
# ---------------------------------------------------------------------------

def default_cache_dir() -> str:
    """Compute the default cache directory.

    Resolution order: ``$LLMS_DOCS_CACHE_DIR`` (full override) >
    ``$XDG_CACHE_HOME/llms-docs`` > ``~/.cache/llms-docs``. The old default
    (``/tmp``) is not used as a fallback: it is cleared on reboot / OS
    housekeeping (defeating the ``--max-age`` cache entirely on a fresh
    session) and is world-writable on multi-user systems (a
    ``PermissionError`` risk). Does not create the directory — callers that
    actually write into it are responsible for that (``fetch_url`` already
    does via ``create_parent``); computing a default should not have the
    side effect of creating an unused directory when the caller passes an
    explicit ``--cache-dir`` instead.

    Per the XDG Base Directory spec, a relative ``$XDG_CACHE_HOME`` is
    invalid and must be ignored (falling back to ``~/.cache``) rather than
    resolved against the current working directory — otherwise the cache
    location would silently depend on whichever directory the script
    happened to be launched from, and could read/write into whatever
    project directory happens to be the cwd. ``$LLMS_DOCS_CACHE_DIR`` is
    this plugin's own escape hatch, not XDG-governed, so it is exempt from
    this check and accepts a relative value as-is (resolved by the OS
    against the cwd, same as any other relative path a user hands us).
    """
    override = os.environ.get("LLMS_DOCS_CACHE_DIR")
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        expanded = os.path.expanduser(xdg)
        if os.path.isabs(expanded):
            return os.path.join(expanded, "llms-docs")
    return os.path.join(os.path.expanduser("~/.cache"), "llms-docs")


# ---------------------------------------------------------------------------
# Core: HTTP fetch + file IO
# ---------------------------------------------------------------------------

def _format_age(seconds: float) -> str:
    """Format a duration in seconds as a short human-readable string."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def _atomic_write(path: str, data: bytes) -> None:
    """Write *data* to *path* atomically.

    Writes to a temp file in the *same directory* as *path* (a cross-
    filesystem temp dir would make ``os.replace`` non-atomic, or raise) and
    ``os.replace``s it into place. This guarantees a concurrent reader (a
    parallel Skill fork, or a second invocation) never observes a partially
    -written cache file — the previous implementation's plain ``open(...,
    "wb")`` truncated the file before writing, so a reader mid-fetch could
    see 0 bytes or a truncated body and silently mis-parse it as "0
    entries" or "corrupt".
    """
    parent = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".fetch-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class FetchError(Exception):
    """Raised by ``fetch_url(..., raise_on_error=True)`` instead of exiting.

    Lets a caller that fetches many independent pages in a loop (Firebase's
    ``search``/``search-content``, which have no single upstream index to
    fail on) catch a single page's failure and skip it, rather than the
    whole process dying on the first dead link in a ~7000-entry index.
    """

    def __init__(self, url: str, cause: Exception):
        super().__init__(f"{url}: {cause}")
        self.url = url
        self.cause = cause


def _meta_path(cache_path: str) -> str:
    return cache_path + ".meta.json"


def _load_fetch_meta(cache_path: str) -> dict:
    """Return the persisted ETag/Last-Modified sidecar for *cache_path*.

    Returns ``{}`` when there is no sidecar, it's unreadable/corrupt, it
    parses to valid JSON that isn't a ``dict`` (e.g. a list or bare string),
    or its ``content_hash``/``etag``/``last_modified`` values aren't strings
    — nothing guarantees the file wasn't hand-edited or written by some
    future version with a different shape. A non-string ``etag``/
    ``last_modified`` would otherwise reach ``fetch_url``'s request headers,
    where ``urllib`` raises an uncaught ``TypeError`` instead of falling
    back cleanly; a non-string ``content_hash`` can't crash the same way
    (it just never equals the current file's hash) but is rejected too so
    every value this function hands back is guaranteed a string — a caller
    shouldn't have to separately re-check that. In every rejected case the
    caller just ends up sending no conditional headers, falling back to the
    plain unconditional GET this always did before conditional support
    existed.
    """
    try:
        with open(_meta_path(cache_path), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    for key in ("content_hash", "etag", "last_modified"):
        if key in meta and not isinstance(meta[key], str):
            return {}
    return meta


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_fetch_meta(cache_path: str, etag: str | None, last_modified: str | None,
                      content_hash: str) -> None:
    """Persist *etag*/*last_modified*/*content_hash* alongside *cache_path*
    for the next fetch's conditional GET.

    *content_hash* (the sha256 of the exact bytes just written to
    *cache_path*) is always stored, independent of what the server sent,
    and is what ``fetch_url`` checks before trusting the ETag/Last-Modified
    to send as conditional headers — see its docstring for why: two
    concurrent writers of the same cache file can interleave their
    (independently atomic) body write and sidecar write, pairing one
    response's body with a *different* response's validators.

    Otherwise always overwrites — including with no ``etag``/
    ``last_modified`` keys when the response sent neither header — so a
    server that stops sending them doesn't leave a stale conditional value
    that would keep being sent on every future fetch (and keep getting
    silently ignored, or worse, matched by coincidence).
    """
    meta = {"content_hash": content_hash}
    if etag:
        meta["etag"] = etag
    if last_modified:
        meta["last_modified"] = last_modified
    _atomic_write(_meta_path(cache_path), json.dumps(meta).encode("utf-8"))


def _handle_fetch_failure(url: str, cache_path: str, error: Exception,
                           raise_on_error: bool) -> str:
    """Shared tail of ``fetch_url``'s failure handling (stale-serve / exit /
    raise), factored out so both the ``HTTPError`` branch (304 aside) and
    the general transport-error branch apply it identically.
    """
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        print(
            f"WARNING: fetch failed ({error}); using cached copy "
            f"({_format_age(age)} old)",
            file=sys.stderr,
        )
        return cache_path
    if raise_on_error:
        raise FetchError(url, error) from error
    print(f"Error: Failed to fetch {url}: {error}", file=sys.stderr)
    sys.exit(1)


class _NoValidatorRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strips conditional-GET headers before forwarding a redirected request.

    ``urllib``'s default redirect handling copies a request's headers
    (``If-None-Match``/``If-Modified-Since`` included) onto the redirected
    request unchanged — verified empirically against a live redirect. Sent
    toward a redirect destination, those validators describe whatever this
    process last cached under *this* function's own bookkeeping, not
    anything the destination server has ever confirmed — a coincidental
    match there can 304 into silently keeping the wrong body indefinitely
    (see ``fetch_url``'s docstring). Installed as the process-wide default
    opener (see below) so every ``urllib.request.urlopen`` call in this
    process is covered, not just calls this module makes directly; safe
    because each ``parse-*.py`` runs as its own single-purpose process with
    no unrelated code sharing it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            for header in ("If-none-match", "If-modified-since"):
                new_req.headers.pop(header, None)
                new_req.unredirected_hdrs.pop(header, None)
        return new_req


urllib.request.install_opener(
    urllib.request.build_opener(_NoValidatorRedirectHandler())
)


def fetch_url(url: str, cache_path: str, *, user_agent: str,
              timeout: int = 120, create_parent: bool = True,
              max_age: int | None = None, raise_on_error: bool = False) -> str:
    """Return path to cached file, fetching from *url* if it doesn't exist yet.

    When *max_age* is given (seconds), re-fetches if the existing cache is
    older than that. ``max_age=None`` (default) keeps the original behaviour
    of using the cache indefinitely once it exists.

    On transport failure:
      * If a cache file already exists (even a stale one past *max_age*),
        print a WARNING to stderr and return that stale copy rather than
        failing outright — a transient network blip shouldn't break an
        otherwise-usable session when a slightly-old copy is on disk.
      * Otherwise (no cache at all) and *raise_on_error* is false (the
        default), print an ``Error: ...`` and exit 1 (mirrors the
        pre-refactor per-script helpers) — appropriate for a single
        upstream index/page that the whole command depends on.
      * Otherwise (no cache, *raise_on_error* true), raise ``FetchError``
        instead of exiting — for a caller fetching many independent pages
        in a loop (one dead link shouldn't kill the other N-1 fetches).

    Catches ``(urllib.error.URLError, OSError, http.client.HTTPException,
    EOFError, zlib.error, ValueError)`` — not just ``URLError`` — so a read
    timeout (``TimeoutError``, an ``OSError`` subclass), a truncated
    transfer (``http.client.IncompleteRead``), a truncated/corrupt gzip
    stream, or a validator string ``http.client.putheader`` rejects at
    send time (a bare newline, or a non-Latin-1 character —
    ``UnicodeEncodeError`` is a ``ValueError`` subclass) hits this handling
    instead of propagating as a raw Python traceback. The ``ValueError``
    entry is a backstop, not the primary defense: ``_load_fetch_meta``
    already rejects a non-string/malformed validator *before* a request is
    even built, purely to skip a pointless network round-trip for a sidecar
    already known to be unusable. This clause exists because a sidecar can
    still hold a *string* that is itself an invalid HTTP field value (hand
    edit, corruption, an upstream that started sending exotic characters)
    — a case `_load_fetch_meta`'s type check can't enumerate in advance.
    The only ``ValueError`` raised *inside* this try block on the success
    path (a malformed ``Content-Length``) is already caught and neutralized
    in its own inner ``try``/``except`` below, so it can never reach here.

    Writes are atomic (see ``_atomic_write``) and validated against
    ``Content-Length`` when the server sends one — a response that reads
    fewer bytes than advertised is treated as a transport failure rather
    than cached as if it were complete. A ``Content-Length`` that isn't a
    valid integer (a malformed or non-conformant server/intermediary) is
    treated the same as no ``Content-Length`` at all: the check is simply
    skipped rather than raising ``ValueError`` — the bytes we already read
    are not in doubt just because the length header describing them is
    garbled, so there is nothing to fall back to a stale cache *for*.

    Sends ``Accept-Encoding: gzip`` and decompresses a ``Content-Encoding:
    gzip`` response before caching (``urllib`` does not auto-decompress).
    The ``Content-Length`` check above runs on the *compressed* bytes
    actually read off the wire — that header describes the transferred
    (encoded) size, not the decompressed size — so it is validated before
    ``gzip.decompress`` is called. A truncated/malformed gzip stream
    (``EOFError``/``zlib.error`` from ``gzip.decompress``, or
    ``gzip.BadGzipFile`` — an ``OSError`` subclass, already covered) is
    treated as the same kind of transport failure as ``IncompleteRead``.

    When an existing cache has a persisted ``ETag``/``Last-Modified`` (see
    ``_load_fetch_meta``/``_save_fetch_meta``), a re-fetch past *max_age*
    sends them as ``If-None-Match``/``If-Modified-Since`` — but only if the
    sidecar's ``content_hash`` still matches the cache file's actual
    current bytes. Without that check, two processes racing to refresh the
    same stale cache could interleave their (independently atomic) body
    write and sidecar write, pairing one response's body with a
    *different* response's validators; a later conditional GET could then
    get a 304 that's valid for the *sidecar's* response but not for the
    body actually on disk, silently locking in a stale/wrong body for
    another *max_age* interval. A hash mismatch instead falls back to an
    unconditional GET, which rewrites both files together and resolves the
    inconsistency. A ``304 Not Modified`` response (raised by ``urllib`` as
    ``HTTPError`` with ``code=304``, not returned as a normal response) is
    treated as success: the cache file's mtime is bumped to now (resetting
    the *max_age* clock) without re-writing its content or the sidecar
    (both are already known-consistent, or the hash check above would have
    prevented the conditional request), and no bytes are re-downloaded.
    Any other HTTP status/transport error falls through to the same
    stale-serve/exit/raise handling as before.

    The process-wide default opener installed just above this function
    (``_NoValidatorRedirectHandler``) strips conditional-GET headers before
    ``urllib`` forwards a redirected request — the primary defense against
    a validator ending up sent toward a destination it was never confirmed
    against. ``etag``/``last_modified`` are *also* persisted only when the
    response's resolved URL (``resp.url``, which ``urllib`` sets to the
    final URL after following any redirects) equals *url* itself, as a
    second, independent layer: even if the redirect-handler stripping were
    ever bypassed or removed, a validator obtained through a redirect
    still wouldn't be saved in the first place. ``content_hash`` is
    recorded either way — it doesn't carry this risk since it's compared
    against the cache file's own bytes, not sent to any server.
    """
    cache_exists = os.path.exists(cache_path)
    if cache_exists:
        if max_age is None:
            return cache_path
        age = time.time() - os.path.getmtime(cache_path)
        if age < max_age:
            return cache_path

    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip"}
    if cache_exists:
        meta = _load_fetch_meta(cache_path)
        try:
            with open(cache_path, "rb") as f:
                current_hash = _content_hash(f.read())
        except OSError:
            current_hash = None
        if meta.get("content_hash") == current_hash:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        # else: sidecar doesn't match the cache file's current bytes (a
        # concurrent writer's interleaved update, or a sidecar left behind
        # by an older version of this function that never wrote one) —
        # send no conditional headers, forcing an unconditional GET that
        # rewrites both files together and resolves the inconsistency.

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            content_length = resp.headers.get("Content-Length")
            expected_length = None
            if content_length is not None:
                try:
                    expected_length = int(content_length)
                except ValueError:
                    expected_length = None  # malformed header; skip the check
            if expected_length is not None and len(data) != expected_length:
                raise http.client.IncompleteRead(
                    data, expected_length - len(data)
                )
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
            # Defense-in-depth alongside _NoValidatorRedirectHandler
            # (module-level, installed above fetch_url): even if header
            # stripping on redirect were ever bypassed or removed, still
            # never persist a validator obtained through a redirect —
            # content_hash is unaffected and still recorded either way.
            redirected = resp.url != url
        if create_parent:
            parent = os.path.dirname(cache_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        _atomic_write(cache_path, data)
        try:
            _save_fetch_meta(
                cache_path,
                None if redirected else etag,
                None if redirected else last_modified,
                _content_hash(data),
            )
        except OSError as e:
            # The fetch itself already succeeded and cache_path is already
            # written — a failure persisting the *sidecar* (disk full, a
            # read-only cache dir that still somehow let the first write
            # through, ...) is a pure optimization loss (no conditional GET
            # next time), never a reason to report this fetch as failed.
            print(
                f"WARNING: could not save fetch metadata for {url}: {e}",
                file=sys.stderr,
            )
        return cache_path
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache_exists:
            try:
                os.utime(cache_path, None)
            except OSError as utime_err:
                # A sibling except clause below can't catch this — it only
                # covers the try block above, not exceptions raised inside
                # this except block. Route it through the same
                # stale-cache-fallback path explicitly instead of letting
                # it escape as a raw traceback despite the fetch itself
                # (the conditional GET) having succeeded.
                return _handle_fetch_failure(
                    url, cache_path, utime_err, raise_on_error
                )
            return cache_path
        return _handle_fetch_failure(url, cache_path, e, raise_on_error)
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            EOFError, zlib.error, ValueError) as e:
        return _handle_fetch_failure(url, cache_path, e, raise_on_error)


def load_lines(path: str):
    """Read file and return lines (preserving newlines).

    Decodes with ``errors="replace"`` rather than the default ``"strict"``:
    a cache file truncated mid-multibyte-character (e.g. by a killed
    process, or the pre-atomic-write race this module now closes) would
    otherwise raise ``UnicodeDecodeError`` and crash every subcommand that
    touches that cache, instead of degrading to a few replacement
    characters in whatever content was cut off.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


# ---------------------------------------------------------------------------
# Core: keyword search over index entries and body content
# ---------------------------------------------------------------------------

# ``-ses`` plurals whose root really ends in a hard consonant + ``es``
# (alias → aliases, status → statuses). Everything else ending in ``-ses`` is
# treated as a silent-e root + ``s`` (case → cases, response → responses).
_HARD_SES_PLURALS = frozenset({
    "aliases", "statuses", "viruses", "buses", "gases", "bonuses", "lenses",
    "canvases", "campuses", "censuses", "atlases", "biases", "minuses",
    "pluses", "corpuses", "focuses", "cactuses", "irises", "abuses",
})
# ``-ches`` plurals whose root is a silent-e word (cache → caches); the
# default for ``-ches`` is hard consonant + ``es`` (match → matches).
_SILENT_E_CHES_PLURALS = frozenset({
    "caches", "niches", "headaches", "mustaches", "avalanches",
})
# The singular forms of ``_HARD_SES_PLURALS`` end in ``s`` themselves
# (alias, status); the generic trailing-``s`` strip would turn them into
# "alia"/"statu" and break the plural↔singular fold in the other direction.
_HARD_SES_SINGULARS = frozenset(p[:-2] for p in _HARD_SES_PLURALS)


def _norm(tok: str) -> str:
    """Lowercase, strip ``-``/``_`` separators, and apply a light plural-to-
    singular stem so ``score_entry`` treats e.g. ``"skills"``/``"Skill"``,
    ``"hooks"``/``"Hook events"``, and ``"stream-text"``/``"streamText"`` as
    equivalent tokens.

    Not a real stemmer (no Porter/Snowball) — just enough to fold the common
    English plural suffixes and hyphen/underscore spelling variants that show
    up across the doc corpora. Order matters: the multi-character sibilant
    endings are checked *before* the generic single-``s`` strip, because
    stripping only the trailing ``s`` from e.g. "boxes" would leave a
    dangling ``e`` ("boxe"). Words already ending in ``ss`` (e.g. "process")
    are left untouched to avoid mangling a non-plural word.

    The sibilant endings are ambiguous on the surface: "matches" is
    match + es but "caches" is cache + s, "classes" is class + es but
    "cases" is case + s. The rules below pick the reading that dominates
    tech-doc vocabulary per ending (measured over the claude-docs / ai-sdk
    corpora, internal backlog), with tiny exception lists for the
    frequent counter-examples:

    * ``-sses`` / ``-zzes`` → strip ``es`` (class, process, address, quiz)
    * ``-ses`` → strip ``s`` (case, response, database, release, use) unless
      in ``_HARD_SES_PLURALS`` (alias, status → strip ``es``)
    * ``-zes`` → strip ``s`` (size, resize, freeze)
    * ``-ches`` → strip ``es`` (match, batch, search) unless in
      ``_SILENT_E_CHES_PLURALS`` (cache → strip ``s``)
    * ``-xes`` / ``-shes`` → strip ``es`` (box, index, prefix, hash, flush)
    * the singulars behind ``_HARD_SES_PLURALS`` (alias, status) are left
      as-is instead of losing their final ``s``

    A token with an uppercase letter NOT in the first position, mixed
    with at least one lowercase letter elsewhere, in its ORIGINAL spelling
    (checked before lowercasing) skips all of the above stripping: this
    exact shape — "iOS", "macOS", "tvOS" — is how brand/acronym tokens get
    written, and an ordinary English plural is essentially never mixed-
    case like this. Without this guard, a query for "iOS" would strip to
    "io" and substring-match unrelated titles like "Configuration" or
    "Migrations" purely by coincidence — especially harmful for sources
    with no full-corpus search fallback, where a handful of spurious "io"
    hits can crowd the intended page out of a small top-N candidate list.
    Deliberately narrower than "any token with 2+ capitals": an ALL-CAPS
    query like "HOOKS" (a user typing an ordinary plural in shouty case,
    not an acronym) must still stem to "hook" — the same result "Hooks"
    stems to — or an otherwise-exact match silently becomes a miss solely
    because of how the user capitalized it. Still lowercased and
    separator-stripped like any other token, just not stemmed.
    """
    is_acronym_like = (
        any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok)
    )
    t = tok.lower().replace("-", "").replace("_", "")
    if is_acronym_like:
        return t
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if len(t) > 4:
        if t.endswith(("sses", "zzes")):
            return t[:-2]
        if t.endswith("ses"):
            return t[:-2] if t in _HARD_SES_PLURALS else t[:-1]
        if t.endswith("zes"):
            return t[:-1]
        if t.endswith("ches"):
            return t[:-1] if t in _SILENT_E_CHES_PLURALS else t[:-2]
        if t.endswith(("xes", "shes")):
            return t[:-2]
    if t in _HARD_SES_SINGULARS:
        return t
    if t.endswith("s") and not t.endswith("ss") and len(t) > 2:
        return t[:-1]
    return t


def _norm_phrase(text: str) -> str:
    """Apply ``_norm()`` to *text* one whitespace-separated word at a time
    and rejoin with single spaces.

    ``_norm()``'s length gates (``len(t) > 4`` / ``len(t) > 2``) are sized
    for a single word — applying it to a whole multi-word string directly
    would measure those gates against the *combined* length of every word,
    not the last word alone, so the very same suffix on the very same word
    could pass the gate in a short title/query and fail it once embedded in
    a longer phrase (e.g. ``_norm("uses")`` strips to ``"use"``, but a
    naive whole-string ``_norm("Common uses")`` — length 11 — would instead
    strip the trailing ``"es"`` off the *entire string* to ``"common us"``,
    losing the substring match a plain, un-normalized ``"uses" in "common
    uses"`` used to find). Normalizing word-by-word keeps every word's
    stemming decision local to that word, so a phrase and a bare keyword
    that share a final word always normalize that shared word the same way.
    """
    return " ".join(_norm(word) for word in text.split())


_COMPOUND_MAX_WORDS = 3


def _joins_words(kw: str, words: list[str]) -> bool:
    """True when *kw* equals 2 to ``_COMPOUND_MAX_WORDS`` consecutive *words*
    run together ("statusline" vs ["status", "line"])."""
    for i in range(len(words)):
        joined = words[i]
        for j in range(i + 1, min(i + _COMPOUND_MAX_WORDS, len(words))):
            joined += words[j]
            if joined == kw:
                return True
            if len(joined) >= len(kw) or not kw.startswith(joined):
                break
    return False


def score_entry(title: str, description: str, keywords,
                *, tags=None, headings=None) -> int:
    """Score a single index entry against *keywords* (case-insensitive substring).

    Returns total score (0 means no match). Scoring weights:

        title exact match  : +10
        title substring    :  +5
        tag match          :  +4 (if *tags* provided)
        description match  :  +2
        heading match      :  +1 (if *headings* provided)
        all-keyword bonus  : +10 (when len(keywords) > 1)

    Both the keyword and every comparison field are normalized before
    matching — so a plural query still finds a singular title (and vice
    versa), and a normalized whole-string match now counts as "exact"
    (+10) rather than merely a substring (+5). *keywords* and *tags* are
    already atomic single tokens (``query.split()`` / a tag list), so they
    go through ``_norm()`` directly; *title*/*description*/*headings* are
    free-text phrases and go through ``_norm_phrase()`` (word-by-word — see
    its docstring for why applying ``_norm()`` to the whole phrase directly
    would be wrong). Two equal raw strings are still equal after the same
    deterministic transform is applied to both, so this cannot turn a
    previously-exact match into a partial one — it only ever adds new
    matches / upgrades partial matches to exact.

    A keyword made up entirely of characters ``_norm()`` strips (``-``,
    ``_``, or a mix, e.g. ``"_"``/``"--"``/``"_-"``) normalizes to ``""``.
    Left unguarded, every ``kw_norm in <field>`` check below would then
    trivially succeed for every entry (the empty string is a substring of
    any string), turning a degenerate keyword into a universal match
    instead of a no-op. Such keywords are skipped entirely — contributing
    no score and not counting toward *keywords* for the all-keyword bonus
    below — so e.g. a lone ``"_"`` keyword scores every entry 0 rather than
    matching the whole corpus.
    """
    title_norm = _norm_phrase(title or "")
    desc_norm = _norm_phrase(description or "")
    tags_norm = [_norm(t) for t in (tags or [])]
    headings_norm = [_norm_phrase(h) for h in (headings or [])]
    # Closed compounds: a query word like "statusline" / "subagent" also
    # matches the open spelling in the index ("Customize your status line").
    # Only whole consecutive words count ("status" + "line"), so a keyword
    # never matches by straddling a word boundary by chance ("handle
    # redirects" does not contain "handler"). Used only for keywords that
    # match nothing as written.
    title_words = title_norm.split()
    desc_words = desc_norm.split()
    headings_words = [h.split() for h in headings_norm]

    total = 0
    matched_keywords = 0
    scorable_keywords = 0

    for kw in keywords:
        kw_norm = _norm(kw)
        if not kw_norm:
            continue
        scorable_keywords += 1
        kw_score = 0

        if kw_norm == title_norm:
            kw_score += 10
        elif kw_norm in title_norm:
            kw_score += 5

        if any(kw_norm == t or kw_norm in t for t in tags_norm):
            kw_score += 4

        if kw_norm in desc_norm:
            kw_score += 2

        if any(kw_norm in h for h in headings_norm):
            kw_score += 1

        if kw_score == 0:
            if kw_norm == "".join(title_words):
                kw_score += 10
            elif _joins_words(kw_norm, title_words):
                kw_score += 5
            if _joins_words(kw_norm, desc_words):
                kw_score += 2
            if any(_joins_words(kw_norm, h) for h in headings_words):
                kw_score += 1

        if kw_score > 0:
            matched_keywords += 1
        total += kw_score

    if scorable_keywords > 1 and matched_keywords == scorable_keywords:
        total += 10

    return total


# Function words dropped from a query before any matching (index score and
# body search alike). Matching is by substring, so these hit nearly every
# section ("in" is inside "within", "the" inside "there") and a query typed as
# a heading ("Run hooks in the background") ranked pages on them. Kept small
# and fixed on purpose: only words that carry no topic in a docs query.
QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "in", "on", "of", "to", "for", "with", "and", "or",
    "is", "are", "be", "when", "what", "how", "do", "does", "from", "by",
    "at", "as", "it", "its", "this", "that",
})


def query_terms(query: str) -> list[str]:
    """The keywords every search matches *query* by.

    Whitespace-separated tokens, duplicates (case-insensitive) dropped, and
    ``QUERY_STOPWORDS`` dropped when at least one other token remains — a
    query made only of function words ("how to") is searched as typed. A
    token typed in capitals ("DO" for Durable Objects) is an abbreviation,
    not a function word, and stays. The original spelling is kept (``_norm``
    reads the case of "iOS").
    """
    tokens = []
    seen = set()
    for tok in query.split():
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            tokens.append(tok)
    content = [t for t in tokens
               if t.lower() not in QUERY_STOPWORDS or (len(t) > 1 and t.isupper() and t.lower() not in {"and", "or"})]
    return content or tokens


def dropped_query_terms(query: str) -> list[str]:
    """The tokens of *query* that ``query_terms`` leaves out (lowercase)."""
    kept = {t.lower() for t in query_terms(query)}
    dropped = []
    for tok in query.split():
        low = tok.lower()
        if low not in kept and low not in dropped:
            dropped.append(low)
    return dropped


def search_index_entries(entries, query: str, *, limit: int = 15, get_extras=None):
    """Score and rank *entries* (dicts with 'title' and 'description') against *query*.

    *query* is split into AND keywords by ``query_terms``. *get_extras*,
    when given, is called as ``get_extras(entry, idx)`` and must return a dict
    that may contain 'tags' and/or 'headings' used for extra scoring signals.

    Returns a list of ``(score, idx, entry)`` tuples sorted by score desc,
    truncated to *limit*. Empty list if query has no tokens.
    """
    keywords = query_terms(query)
    if not keywords:
        return []

    scored = []
    for idx, entry in enumerate(entries):
        extras = get_extras(entry, idx) if get_extras else {}
        score = score_entry(
            entry.get("title", ""),
            entry.get("description", ""),
            keywords,
            tags=extras.get("tags"),
            headings=extras.get("headings"),
        )
        if score > 0:
            scored.append((score, idx, entry))

    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def format_heading_path_for_display(heading_path: str) -> str:
    """Render a ``heading_path`` for a ``Section:`` display line.

    ``"(top)"`` is the reserved sentinel ``_build_section_results`` uses for
    a body hit above a page's first heading, and the same string
    ``extract_content`` recognizes to return that preamble (see its
    docstring for the full round-trip contract). Displayed bare, "(top)"
    reads as cryptic. Annotated inline as e.g. "(top: before first
    heading)" it would read clearly but would no longer be safe to copy
    verbatim into ``content``'s heading_path argument. This appends the
    clarification as a separate bracketed suffix instead — matching the
    existing "[partial match]" / "(x3)" annotation style already used on
    these same result lines — so the text up to the first two spaces is
    always exactly the value ``content`` expects.
    """
    if heading_path == "(top)":
        return "(top)  [before first heading]"
    return heading_path


def _strip_heading_markup(title: str) -> str:
    """Reduce a raw Markdown heading to its rendered text before slugging.

    Both GitHub and DevSite derive the heading id from the *rendered* text,
    so inline markup must go first: image alt / link text replace the whole
    ``![alt](src)`` / ``[text](dest)`` construct (otherwise the URL leaks
    into the slug as ``hookshttpsexamplecomhooks``), HTML tags are dropped,
    entities are decoded (``&amp;`` -> ``&``, which the slug filter then
    removes like any other symbol), and emphasis / code markers are removed
    while their content is kept. Handled forms (everything else is best-
    effort and documented as such in the skills): inline / reference-style
    links, images, footnote markers, HTML tags, HTML entities, code spans
    (contents kept verbatim), `*`/`~` markers and word-boundary `_`
    emphasis. Merge-review findings.
    """
    # コードスパンの中身は逐語 (レンダリングでもそのまま表示される) なので、
    # `<name>` のような角括弧を HTML タグ除去から守る。先に取り出して
    # プレースホルダに置き、タグ除去・実体参照デコードの後で戻す
    code_spans: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        code_spans.append(m.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    s = re.sub(r"`([^`]*)`", _stash, title)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)       # image -> alt
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # inline link -> text
    s = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", s)      # reference link [text][ref] / [text][] -> text
    s = re.sub(r"\[\^[^\]]*\]", "", s)                     # footnote marker [^1]
    s = re.sub(r"<[^>]+>", "", s)                          # html tags
    s = html.unescape(s)
    # 強調の区切りとして使われた `_` だけを剥がす (単語境界に接する `_..._` の組)。
    # `snake_case_name` のような識別子内の `_` は見出しの実テキストなので残す
    s = re.sub(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", s)
    s = re.sub(r"[`*~]+", "", s)                          # bold / strike markers
    # コードスパンの中身は全ての記号除去の **後** に戻す (`__init__` の `_` や `*` を
    # 強調記号と誤認して剥がさない。マージ前レビューの指摘)
    s = re.sub(r"\x00(\d+)\x00", lambda m: code_spans[int(m.group(1))], s)
    return s.strip()


def heading_anchor_slug(title: str, style: str = "github") -> str:
    """Best-effort anchor slug for one heading title.

    Two platform families generate heading ids differently, so *style*
    selects which one to reproduce:

    - ``"github"`` (default, also matches Mintlify): lowercase, drop
      apostrophes, turn every run of characters that aren't a
      letter/digit/underscore into a single hyphen (``loop.md`` becomes
      ``loop-md``, ``Can't`` becomes ``cant``), and trim hyphens at both
      ends. Checked against the in-page ``#anchor`` links of the
      code.claude.com / platform.claude.com corpus. Used by Mintlify docs
      (e.g. code.claude.com, platform.claude.com).
    - ``"devsite"``: Google DevSite (firebase.google.com) joins words with
      an underscore instead of a hyphen. Live-verified on
      https://firebase.google.com/docs/firestore/manage-data/add-data —
      the "Set a document" heading renders as ``id="set_a_document"`` and
      "Add a document" as ``id="add_a_document"``; this tool's prior
      GitHub-style slug (``#set-a-document``) does not resolve on that
      page. Same character strip as ``"github"``, then whitespace runs
      collapse to a single underscore (not hyphen) and the result is
      stripped of leading/trailing underscores.

    Neither style replicates every platform edge case — most notably
    neither reproduces the numeric ``-1``/``-2`` (or DevSite's own)
    duplicate-heading suffix a page gets when two headings share a title,
    since that requires knowing every other heading on the page rather
    than just this one title, nor a page's custom-id override (Mintlify's
    ``{#custom-id}`` syntax has a DevSite equivalent too). Callers display
    the result as a best-effort anchor, not a guarantee that it matches
    the live page exactly.

    Returns ``""`` if *title* normalizes to nothing (e.g. an all-symbol
    heading) — callers should treat that as "no anchor available".
    """
    s = _strip_heading_markup(title).lower()
    if style == "devsite":
        s = re.sub(r"[^\w\s-]", "", s)
        return re.sub(r"\s+", "_", s).strip("_")
    # 記号は削らずハイフンにし、連続は 1 つにまとめる。`loop.md` は `loop-md`、
    # `--bg` は `bg` になる (ページ内リンクの id と突き合わせて確認した形)。`\_` のような
    # バックスラッシュのエスケープは先に外し、アポストロフィは単語を割らないよう削る。
    # `_` は単語文字なのでそのまま残る
    s = re.sub(r"\\(.)", r"\1", s)
    s = re.sub(r"['’]", "", s)
    s = re.sub(r"[^\w]+", "-", s)
    return s.strip("-")


def section_url_anchor(url: str, title: str, style: str = "github") -> str:
    """``  [<url>#<anchor>]`` suffix for a ``Section:`` search result line.

    Bracketed to match this tool's existing annotation convention on these
    same result lines (``[partial match]``, the ``"(top)"`` suffix's
    ``[before first heading]``) rather than reusing ``→``, which the
    snippet lines directly below already use as the per-line hit marker.

    *title* must be the section's own leaf heading title (a section dict's
    ``"title"`` field, or the ``"(top)"`` sentinel) — **not**
    ``heading_path``. ``heading_path`` is this tool's internal breadcrumb
    (ancestor titles joined with ``"/"``) and is not safe to derive the
    leaf from by splitting on ``"/"``: a heading whose own title legitimately
    contains a slash (e.g. ``## CI/CD``, ``## Read / write data``) would
    have that slash misread as a breadcrumb separator, truncating the
    anchor to only the text after the last ``"/"`` (merge-review finding —
    see CHANGELOG). Passing the leaf title directly sidesteps that
    ambiguity entirely since there is nothing left to split.

    *style* is forwarded to ``heading_anchor_slug`` — pass ``"devsite"``
    for Firebase (Google DevSite heading ids join words with ``_``, not
    ``-``; see that function's docstring). Callers on Mintlify-family
    sources (claude-docs) leave it at the ``"github"`` default.

    Returns ``""`` (no suffix) when there's no page *url* to anchor into,
    when *title* is the ``"(top)"`` sentinel (body text above the first
    heading has no heading element to anchor to), or when the title
    normalizes to an empty slug.

    *url* must already be the human-facing page URL (not e.g. Firebase's
    raw ``.md.txt`` fetch URL) — a ``#fragment`` on a plaintext response
    resolves to nothing. Canonicalizing that is the caller's job (each
    ``parse-*.py`` already has its own notion of "the real page URL" for
    its source).
    """
    if not url or title == "(top)":
        return ""
    slug = heading_anchor_slug(title, style=style)
    if not slug:
        return ""
    return f"  [{url}#{slug}]"


def _heading_text(title: str) -> str:
    """Rendered, lowercased text of one heading (or page title) for
    ``names_every_keyword``: a link's URL must not supply the words
    ("[Alarm Handler](https://.../durable-objects/...)"), and a Markdown
    escape (``max\\_tokens``) reads as the character it escapes."""
    return re.sub(r"\\(.)", r"\1", _strip_heading_markup(title)).lower()


def names_every_keyword(text_lower: str, keywords) -> bool:
    """True when each of *keywords* (lowercase) appears in *text_lower* as a
    word: not next to another ASCII letter or digit, a plural ``s`` / ``es``
    allowed ("hook" names "Hooks", but not "Webhooks"; "as" does not name
    "Constructor")."""
    return all(re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?:e?s)?(?![a-z0-9])",
                         text_lower)
               for kw in keywords)


def _section_heading_texts(sections, page_title: str) -> list[tuple[str, str]]:
    """Per section, two texts for ``names_every_keyword``: the page title
    and every heading on its path (the section's own and its ancestors'),
    and the same without the section's own heading (what it inherits).
    Built from the levels, not by splitting ``heading_path`` (a heading may
    contain a slash)."""
    texts = []
    stack: list = []
    base = _heading_text(page_title) if page_title else ""
    for s in sections:
        while stack and stack[-1][0] >= s["level"]:
            stack.pop()
        inherited = "\n".join([base] + [t for _, t in stack])
        stack.append((s["level"], _heading_text(s["title"])))
        texts.append(("\n".join([base] + [t for _, t in stack]), inherited))
    return texts


SNIPPET_MARK_WIDTH = 2  # "→ " / "  " in front of each snippet line


def _cut_hit_line(text: str, width: int, span) -> str:
    """Cut one snippet line (*text*, its ``→ `` mark included) to about
    *width* characters so that the matched word stays in view.

    *span* is ``(start, end)`` of the first matched keyword in the line
    after the mark (``None`` when unknown). When the word ends within the
    first *width* characters the line is cut from its start, as before;
    otherwise a window starting a third of the width before the word is
    cut out, with ``…`` on each side that was cut. The mark stays in
    front of the window."""
    if len(text) <= width:
        return text
    mark, rest = text[:SNIPPET_MARK_WIDTH], text[SNIPPET_MARK_WIDTH:]
    room = width - SNIPPET_MARK_WIDTH
    if span is None or span[1] <= room:
        return text[:width] + "…"
    begin = max(0, span[0] - room // 3)
    window = rest[begin:begin + room]
    return (mark + ("…" if begin > 0 else "") + window
            + ("…" if begin + room < len(rest) else ""))


def _fit_snippet(lines, max_chars, tail: str = "") -> str:
    """Join the snippet *lines* (``(text, is_hit, span)``) within *max_chars*.

    Fits as is when it can (``0`` / ``None`` = no limit). Otherwise the hit
    lines are kept first and the context lines nearest to a hit are added
    while they fit, so a long table row beside the match cannot push the
    matching line out of view. Lines left out are replaced by one ``...``
    line per gap. When the hit lines alone are over the limit, only the
    hit lines are kept and each is cut to ``max(max_chars // n_hits, 80)``
    characters with ``…`` where it was cut, so every hit stays in view (with
    many hits the total can go over *max_chars*: no line is cut below 80
    characters). A hit line whose matched word lies past that width is cut
    around the word instead of from its start (``_cut_hit_line``; *span* is
    where the first matched keyword sits in the line after its mark). *tail*
    (the "more hits" note) is appended after the fit and does not count
    against the limit.
    """
    def finish(text):
        return text + ("\n" + tail if tail else "")

    full = "\n".join(text for text, _hit, _span in lines)
    if not max_chars or len(full) <= max_chars:
        return finish(full)

    hit_pos = [i for i, (_t, hit, _s) in enumerate(lines) if hit]
    keep = set(hit_pos)
    used = sum(len(lines[i][0]) + 1 for i in keep) - 1
    if used > max_chars:
        width = max(max_chars // len(hit_pos), 80)
        lines = [(_cut_hit_line(text, width, span), hit, span)
                 for text, hit, span in lines]
    else:
        by_distance = sorted(
            (i for i in range(len(lines)) if i not in keep),
            key=lambda i: (min(abs(i - h) for h in hit_pos), i))
        for i in by_distance:
            cost = len(lines[i][0]) + 1
            if used + cost <= max_chars:
                keep.add(i)
                used += cost
    out = []
    prev = None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("  ...")
        out.append(lines[i][0])
        prev = i
    if sorted(keep)[0] != 0:
        out.insert(0, "  ...")
    if prev != len(lines) - 1:
        out.append("  ...")
    return finish("\n".join(out))


def _first_keyword_span(line: str, keywords):
    """``(start, end)`` of the earliest of *keywords* (lowercase) in *line*,
    matched as ``search_content_in_body`` matches them (a substring, case
    ignored); ``None`` when none is there."""
    lower = line.lower()
    spans = [(i, i + len(kw)) for kw in keywords
             for i in [lower.find(kw)] if i >= 0]
    return min(spans) if spans else None


def _build_section_results(section_hits, sections, body_lines, keywords,
                           min_coverage, context_lines, max_snippet_chars,
                           page_title=""):
    """Build result list from sections matching >= *min_coverage* keywords.

    ``heading_count`` is how many sections of the page share the result's
    heading_path (a command that names it reads the first of them).
    ``heading_match`` is whether the page title and the section's headings
    (its own and its ancestors') name every keyword (``names_every_keyword``).
    ``heading_own`` is whether that match needs the section's own heading:
    false when the title and the ancestors already name every keyword (then
    every descendant matches as well) or when there is no heading match.
    """
    results = []
    total = 0
    path_counts = heading_path_counts(sections)
    heading_texts = _section_heading_texts(sections, page_title)
    top_text = _heading_text(page_title) if page_title else ""
    for si, hits in section_hits.items():
        all_matched = set()
        for _, m in hits:
            all_matched.update(m)
        if len(all_matched) < min_coverage:
            continue

        total += len(hits)
        heading_path = sections[si]["heading_path"] if si is not None else "(top)"
        hit_line_numbers = [h[0] for h in hits]

        MAX_SNIPPET_HITS = 3
        if len(hit_line_numbers) > MAX_SNIPPET_HITS:
            visible_hits = hit_line_numbers[:MAX_SNIPPET_HITS]
            truncated = len(hit_line_numbers) - MAX_SNIPPET_HITS
        else:
            visible_hits = hit_line_numbers
            truncated = 0

        snippet_start = max(0, visible_hits[0] - context_lines)
        snippet_end = min(len(body_lines), visible_hits[-1] + context_lines + 1)
        hit_set = set(visible_hits)

        snippet = _fit_snippet(
            [(f"{'→ ' if j in hit_set else '  '}{body_lines[j].rstrip()}", j in hit_set,
              _first_keyword_span(body_lines[j], keywords) if j in hit_set else None)
             for j in range(snippet_start, snippet_end)],
            max_snippet_chars,
            tail=f"  ... ({truncated} more hits in this section)" if truncated else "",
        )

        # "title" is the section's own leaf heading text (or "(top)"),
        # kept separate from "heading_path" (the ancestor breadcrumb) so
        # anchor generation never has to split heading_path on "/" — a
        # heading whose own title contains a slash (e.g. "## CI/CD") would
        # otherwise be misread as a nested breadcrumb (merge-review finding).
        title = sections[si]["title"] if si is not None else "(top)"
        names_text, inherited_text = (heading_texts[si] if si is not None
                                      else (top_text, top_text))
        heading_match = names_every_keyword(names_text, keywords)
        results.append({
            "heading_path": heading_path,
            "title": title,
            "line_offset": hit_line_numbers[0],
            "snippet": snippet,
            "matched_keywords": sorted(all_matched),
            "hit_count": len(hits),
            "heading_count": path_counts.get(heading_path, 1),
            "heading_match": heading_match,
            "heading_own": heading_match and not names_every_keyword(inherited_text, keywords),
        })
    return results, total


def section_fit(heading_match: bool) -> int:
    """How well one section fits the query: 0 when the page title and the
    section's headings name every keyword (``heading_match``), else 1."""
    return 0 if heading_match else 1


def section_rank_key(result: dict) -> tuple:
    """Sort key for the sections of one page (best first).

    Most keywords first, then ``section_fit`` (the headings name every
    keyword), then the section whose own heading completes that match
    (``heading_own``) before its descendants, which inherit it, then most
    hit lines, then position. Hit lines alone favour a huge section that
    happens to contain every word somewhere; the headings find the section
    that is about the query.
    """
    return (-len(result["matched_keywords"]),
            section_fit(result.get("heading_match", False)),
            0 if result.get("heading_own", False) else 1,
            -result["hit_count"],
            result["line_offset"])


def page_fit(hits: dict) -> int:
    """``section_fit`` of a page's best section, from a
    ``search_content_in_body`` result. A result built without the field
    counts as 1 (no heading match), so such pages rank as they did before
    the fit."""
    return hits.get("best_fit", 1)


def search_content_in_body(body_lines, query: str, *,
                           context_lines: int = 2,
                           max_matches_per_doc: int = 5,
                           min_level: int = 2,
                           max_snippet_chars: int | None = None,
                           page_title: str = ""):
    """Search *body_lines* for *query* keywords (soft-AND, case-insensitive).

    First tries strict AND (all keywords in same section). If no results and
    len(keywords) > 1, relaxes to partial match (>= ceil(N/2) keywords).
    The ``match_mode`` field indicates which strategy produced results.

    Keywords come from ``query_terms`` (function words dropped). Sections
    are ordered by ``section_rank_key``; *page_title* (the page's H1 when the
    body does not carry it) counts as a heading of every section.

    Returns a dict with ``total_matches``, ``results``, ``match_mode``
    (``"and"`` | ``"partial"`` | ``"none"``), ``overflow_sections`` and
    ``best_fit`` (``section_fit`` of the first section; 1 when none, and
    always 1 for a one-keyword query: a heading that holds one word — a
    migration guide's "`useChat` changes" — says little about the page, so
    the page order of a one-word query stays by hit count, while its
    sections are still ordered by ``section_rank_key``).
    """
    keywords = [k.lower() for k in query_terms(query)]
    if not keywords:
        return {"total_matches": 0, "results": [], "match_mode": "none"}

    sections = extract_sections(body_lines, min_level=min_level)

    line_to_section_idx: list = [None] * len(body_lines)
    for si, s in enumerate(sections):
        for i in range(s["line_start"], s["line_end"]):
            if 0 <= i < len(body_lines):
                line_to_section_idx[i] = si

    section_hits: dict = {}
    for i, line in enumerate(body_lines):
        line_lower = line.lower()
        matched = [kw for kw in keywords if kw in line_lower]
        if not matched:
            continue
        si = line_to_section_idx[i]
        section_hits.setdefault(si, []).append((i, matched))

    build_args = (section_hits, sections, body_lines, keywords)
    build_kw = dict(context_lines=context_lines, max_snippet_chars=max_snippet_chars,
                    page_title=page_title)

    # Strict AND
    results, total_matches = _build_section_results(*build_args, len(keywords), **build_kw)
    match_mode = "and"

    # Soft-AND fallback: relax to >= ceil(N/2) keywords
    if not results and len(keywords) > 1:
        min_kw = max(1, (len(keywords) + 1) // 2)
        results, total_matches = _build_section_results(*build_args, min_kw, **build_kw)
        match_mode = "partial"

    if not results:
        match_mode = "none"

    results.sort(key=section_rank_key)
    best_fit = section_rank_key(results[0])[1] if results and len(keywords) > 1 else 1

    overflow_sections: list = []
    if max_matches_per_doc > 0 and len(results) > max_matches_per_doc:
        overflow_sections = [
            {"heading_path": r["heading_path"], "hit_count": r["hit_count"]}
            for r in results[max_matches_per_doc:]
        ]
        results = results[:max_matches_per_doc]

    return {
        "total_matches": total_matches,
        "results": results,
        "match_mode": match_mode,
        "overflow_sections": overflow_sections,
        "best_fit": best_fit,
    }


def match_rank(hits: dict) -> int:
    """0 for a strict-AND (or single-keyword) hit set, 1 for ``partial``,
    2 for no body hits (an index-only ``search`` row stays below both)."""
    if not hits.get("total_matches"):
        return 2
    return 1 if hits.get("match_mode") == "partial" else 0


def full_corpus_body_search(docs_body_lines, query: str, *,
                            context_lines: int = 2,
                            max_matches_per_doc: int = 3,
                            max_snippet_chars: int | None = None,
                            min_level: int = 2,
                            limit: int = 5,
                            titles=None):
    """Search *query* across every doc's body, ignoring index ranking.

    Fallback for when title/description-based ranking finds no candidates,
    or none of the ranked candidates have any body hits — e.g. the query
    only matches an option/field name that lives in a page body, not its
    title or description. Without this, a query like that returns "no
    matching pages" from the smart ``search`` command even though
    ``search-content`` (which scans every body unconditionally) finds it.

    *docs_body_lines* is a list of ``body_lines`` (one per doc, in doc-index
    order). Returns a list of ``(doc_idx, hits)`` tuples — *hits* being a
    ``search_content_in_body`` result dict — for docs with at least one
    match, sorted by match strength (docs with a strict-AND section before
    ``[partial match]`` docs), then ``page_fit``, then total_matches desc, then doc_idx asc,
    truncated to *limit*. Strength comes first so that a page whose sections
    mention only some keywords many times cannot push the page that has all
    of them together past the limit. *titles* (one per doc) are the page
    titles ``search_content_in_body`` reads as headings.
    """
    results = []
    for idx, body_lines in enumerate(docs_body_lines):
        hits = search_content_in_body(
            body_lines, query,
            context_lines=context_lines,
            max_matches_per_doc=max_matches_per_doc,
            min_level=min_level,
            max_snippet_chars=max_snippet_chars,
            page_title=titles[idx] if titles is not None else "",
        )
        if hits["total_matches"] > 0:
            results.append((idx, hits))
    # No titles here: the changelog bucket never applies (title "").
    results.sort(key=lambda t: search_content_rank_key(t[0], "", t[1]))
    return results[:limit]


# How many pages ``search`` appends when its candidates only have
# ``[partial match]`` hits. Appended pages rank above the partial candidates,
# so more than this would push every candidate out of the Next: lines.
EXTRA_STRICT_LIMIT = 2


def full_corpus_extra_hits(results, docs_body_lines, query: str, *,
                           context_lines: int = 2,
                           max_matches_per_doc: int = 3,
                           max_snippet_chars: int | None = None,
                           min_level: int = 2,
                           limit: int = 5,
                           include_changelog_priority: bool = False,
                           titles=None):
    """Pages to append to ``search`` rows when the drilled candidates fall short.

    *results* are the rows built from the top-N index candidates (each with
    ``doc_idx`` and ``body_hits``). Returns ``(doc_idx, hits)`` tuples to add,
    or ``[]`` when no search is needed:

    - some candidate already has a strict-AND hit set (every keyword in one
      section): nothing to add. A changelog-style candidate (``title`` per
      ``is_low_priority``) does not count unless *include_changelog_priority*:
      it is ranked last, so it would not show the answer anyway;
    - no candidate has any body hit: the full-corpus hits are added as they
      are (strict first, then ``[partial match]``), up to *limit*;
    - candidates have only ``[partial match]`` hits: only pages with a
      strict-AND section are added, at most ``EXTRA_STRICT_LIMIT``. Other
      partial pages would only repeat the kind of row the candidates already
      show, and the existing rows stay.

    - candidates have strict-AND hits, but on none of them do the page title
      or the best section's headings name every keyword (``page_fit`` 1):
      strict pages whose title or headings do (``page_fit`` 0) are added, at
      most ``EXTRA_STRICT_LIMIT``. A huge section that contains every word
      somewhere on an index candidate must not hide the page whose section
      is about the query ("When edits take effect" typed as the heading).
      A one-keyword query never gets here: its pages all have
      ``page_fit`` 1 (see ``search_content_in_body``), so any strict
      candidate is enough, as before the fit.

    Pages already in *results* are never returned twice. With *titles* (one
    per page), the pages are ranked by ``search_content_rank_key`` before the
    cut, so a changelog-style page does not take a slot unless
    *include_changelog_priority*.
    """
    counted = [r for r in results
               if include_changelog_priority or not is_low_priority(r.get("title", ""))]
    strict_fits = [page_fit(r["body_hits"]) for r in counted
                   if match_rank(r["body_hits"]) == 0]
    best_fit = min(strict_fits) if strict_fits else None
    if best_fit == 0:
        return []
    drilled_any = any(r["body_hits"]["total_matches"] for r in results)
    already_shown = {r["doc_idx"] for r in results}
    # Every page is ranked, then the listed ones are dropped, then the list is
    # cut: a listed page (a changelog with every keyword) must not use a slot.
    found = full_corpus_body_search(
        docs_body_lines, query,
        context_lines=context_lines, max_matches_per_doc=max_matches_per_doc,
        max_snippet_chars=max_snippet_chars, min_level=min_level,
        limit=len(docs_body_lines), titles=titles,
    )
    if titles is not None:
        found.sort(key=lambda t: search_content_rank_key(
            t[0], titles[t[0]], t[1], include_changelog_priority=include_changelog_priority))
    cap = min(limit, EXTRA_STRICT_LIMIT) if drilled_any else limit
    return [(idx, hits) for idx, hits in found
            if idx not in already_shown
            and (not drilled_any or match_rank(hits) == 0)
            and (best_fit is None
                 or (match_rank(hits) == 0 and page_fit(hits) < best_fit))][:max(cap, 0)]


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    """Print ``Error: {msg}`` to stderr and exit with *code*."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def _squash(text: str) -> str:
    """Lowercase *text* and drop everything but letters and digits, so
    ``Pre Tool Use`` / ``pre-tool-use`` / ``PreToolUse`` compare equal."""
    return "".join(c for c in text.lower() if c.isalnum())


NEAR_HEADING_LIMIT = 5
# ``difflib`` ratio a title needs to count as a typo of the guess. Measured on
# the Claude Code corpus (4374 distinct titles): one random edit of a title
# stays at or above 0.7 for all but two 3-letter titles, while unrelated title
# pairs at or above it drop from 29 to 7 in 20000 compared with 0.6 (which
# offered ``Change a setting`` for ``nonexistent thing``).
NEAR_HEADING_MIN_RATIO = 0.7


def near_headings(heading_path: str, sections, *, limit: int = NEAR_HEADING_LIMIT):
    """Sections whose heading is close to a *heading_path* that did not resolve.

    Looks at the last element of the path (the part that is usually guessed
    wrong), ignoring case, spaces and punctuation. Ranked: title equal,
    title contains it, path contains it, it contains the title, then
    ``difflib`` closeness on the title for typos. Returns at most *limit*
    sections, best first; a heading_path that appears more than once on the
    page is offered once (its first occurrence, the one a command reads).
    """
    last = _squash(heading_path.rsplit("/", 1)[-1])
    if not last:
        return []
    ranked = []
    for pos, s in enumerate(sections):
        title = _squash(s["title"])
        path = _squash(s["heading_path"])
        if title == last:
            rank = 0
        elif last in title:
            rank = 1
        elif last in path:
            rank = 2
        elif len(title) >= 3 and title in last:
            rank = 3
        else:
            continue
        ranked.append((rank, pos, s))
    if len(ranked) < limit:
        chosen = {id(s) for _r, _p, s in ranked}
        close = []
        for pos, s in enumerate(sections):
            if id(s) in chosen:
                continue
            ratio = difflib.SequenceMatcher(None, last, _squash(s["title"])).ratio()
            if ratio >= NEAR_HEADING_MIN_RATIO:
                close.append((-ratio, pos, s))
        close.sort(key=lambda t: (t[0], t[1]))
        ranked += [(4, pos, s) for _neg, pos, s in close]
    ranked.sort(key=lambda t: (t[0], t[1]))
    picked = []
    seen = set()
    for _r, _p, s in ranked:
        if s["heading_path"] in seen:
            continue
        seen.add(s["heading_path"])
        picked.append(s)
    return picked[:limit]


def duplicate_heading_note(count: int) -> str:
    """Suffix for a command that reads a heading_path found *count* times.

    A heading_path is not unique when a page repeats a heading under the same
    parents; every command resolves to the first one, and no command can reach
    the later ones, so the reader is told rather than sent to the wrong one
    silently.
    """
    if count > 1:
        return f"  # heading appears {count} times; this reads the first"
    return ""


def heading_path_counts(sections) -> dict:
    """``{heading_path: number of sections with it}`` for *sections*."""
    counts: dict = {}
    for s in sections:
        counts[s["heading_path"]] = counts.get(s["heading_path"], 0) + 1
    return counts


def content_command(script: str, ref, heading_path, hint_args: tuple = ()) -> str:
    """The ``content`` command that reads *heading_path* of page *ref*.

    *hint_args* is the same tuple the ``Next:`` hints carry (``--source`` /
    ``--file`` / ``--cache-dir`` ...), already shell-quoted.
    """
    parts = [script, "content", str(ref)]
    if heading_path is not None:
        parts.append(shlex.quote(heading_path))
    parts += list(hint_args)
    return " ".join(parts)


def search_in_page_keyword(title: str) -> str:
    """The stand-in keyword ``search_in_page_command`` searches with: the
    heading's rendered text, lowercased, as ``query_terms`` reads it. A
    Markdown escape (``max\\_tokens``) is kept as written, because the
    search compares the keyword with the raw lines of the body, where the
    escape is still there. ``""`` when *title* has no words."""
    return " ".join(query_terms(_strip_heading_markup(title).lower()))


def quoted_keyword(keyword: str) -> str:
    """*keyword* as the note naming the stand-in keyword prints it: in
    quotes, and as the same shell word the ``search_in_page_command`` line
    carries when that word needs quoting (``'don'"'"'t ask'`` for a heading
    with an apostrophe), so the reader finds the note's word in the line."""
    quoted = shlex.quote(keyword)
    return quoted if quoted.startswith("'") else f"'{keyword}'"


def search_in_page_command(script: str, ref, title: str, hint_args: tuple = ()) -> str | None:
    """A runnable ``search-content`` command over page *ref* that shows only
    the matching lines (``--context 0``), with *title* (the section's or
    page's own heading, as a stand-in keyword, see
    ``search_in_page_keyword``) as the keyword. The reader swaps in the term
    they need. ``None`` when *title* has no words.

    A keyword that starts with ``-`` (a heading such as ``--bg``) would be
    read as an option, so the options come first and the keyword is put
    last after ``--``."""
    keyword = search_in_page_keyword(title)
    if not keyword:
        return None
    # The keyword always goes last, after "--": it is a stand-in the reader
    # replaces, often with an option name such as --resume, which argparse
    # would otherwise read as an option.
    parts = [script, "search-content", "--page-ref", str(ref), "--context", "0",
             *hint_args, "--", shlex.quote(keyword)]
    return " ".join(parts)


def die_heading_not_found(heading_path: str, sections, retry=None) -> None:
    """Print heading-not-found error and exit 1.

    The nearest headings come first (each with the command that reads it,
    when *retry* is given), then every section, as before.
    """
    lines = [f"Error: heading '{heading_path}' not found."]
    near = near_headings(heading_path, sections)
    counts = heading_path_counts(sections)
    if near:
        lines += ["", "Closest sections:"]
        for s in near:
            note = duplicate_heading_note(counts[s["heading_path"]])
            lines.append(f"  - {s['heading_path']}")
            if retry is not None:
                lines.append(f"      {retry(s['heading_path'])}{note}")
            elif note:
                lines[-1] += note
    available = "\n".join(f"  - {s['heading_path']}" for s in sections)
    lines += ["", "Available sections:", available]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)


def die_ambiguous_heading(heading_path: str, matches, retry=None) -> None:
    """Print an ambiguous-heading error (2+ case-insensitive partial matches
    for the same *heading_path*) listing every candidate, and exit 1.

    Mirrors the ambiguous-slug error the parse-*.py scripts already raise
    from ``_resolve_page_ref`` — silently picking the first partial match
    risks the caller reading (and citing) the wrong section with no
    indication that other candidates existed. With *retry*, each candidate
    is followed by the command that reads it. A heading_path matched more
    than once is listed once, with how many times it appears.
    """
    counts = heading_path_counts(matches)
    rows = []
    seen = set()
    for m in matches:
        if m["heading_path"] in seen:
            continue
        seen.add(m["heading_path"])
        note = duplicate_heading_note(counts[m["heading_path"]])
        rows.append(f"- {m['heading_path']}")
        if retry is not None:
            rows.append(f"    {retry(m['heading_path'])}{note}")
        elif note:
            rows[-1] += note
    detail = "\n  ".join(rows)
    print(
        f"Error: ambiguous heading '{heading_path}'. Matches:\n  {detail}",
        file=sys.stderr,
    )
    sys.exit(1)


_LANG_SEGMENT = r"[a-z]{2}(?:-[A-Za-z]{2,4})?"


def prefer_lang_exact(candidates, page_ref: str):
    """Narrow ambiguous slug *candidates* ``[(idx, url), ...]`` to the one
    page whose URL is exactly ``/<lang>/<page_ref>`` (after an optional
    ``/docs``), when there is exactly one such page; else return them all.

    ``hooks`` ends both ``/en/hooks`` and ``/en/agent-sdk/hooks``. The first
    is the page a bare slug names (the others need their longer form), so it
    wins. Needs a language segment: without one, a longer path is not more
    or less the "exact" page, and the reader has to choose.
    """
    pattern = re.compile(
        rf"^https?://[^/]+(?:/docs)?/{_LANG_SEGMENT}/{re.escape(page_ref.strip('/'))}$"
    )
    exact = [c for c in candidates if pattern.match(normalize_doc_url(c[1]))]
    return exact if len(exact) == 1 else list(candidates)


def die_ambiguous_page(kind: str, page_ref: str, rows, retry=None) -> None:
    """Print an ambiguous page reference error and exit 1.

    *rows* is ``[(idx, label), ...]``. With *retry* (``idx -> command``) each
    candidate is followed by the full command that reads that page.
    """
    lines = []
    for idx, label in rows:
        lines.append(f"[{idx}] {label}")
        if retry is not None:
            lines.append(f"    {retry(idx)}")
    detail = "\n  ".join(lines)
    die(f"Ambiguous {kind} '{page_ref}'. Matches:\n  {detail}")


def note_other_candidates(page_ref: str, chosen, others, retry=None) -> None:
    """One stderr line saying *page_ref* also matched *others* (not chosen).

    With *retry* (``idx -> command``), the line ends with the command that
    runs the same invocation on the first of the other pages.
    """
    rest = [(i, u) for i, u in others if i != chosen]
    if not rest:
        return
    listed = ", ".join(f"[{i}] {u}" for i, u in rest)
    tail = (f"For [{rest[0][0]}]: {retry(rest[0][0])}" if retry is not None
            else "Pass the index or the longer slug for another.")
    print(f"Note: '{page_ref}' also matches {listed}; resolved to [{chosen}] "
          f"(exact <lang>/{page_ref}). {tail}", file=sys.stderr)


# Options (other than the corpus ones) a retried command keeps, per
# subcommand: ``(attribute, flag, default)``. A value equal to the default is
# not echoed (the same rule as ``corpus_hint_args``); ``True`` / ``False``
# defaults are store_true flags. An attribute the script does not define is
# skipped, so one table serves all four scripts. A function, because the
# defaults are defined further down this module.
def _retry_options() -> dict:
    return {
        "content": (
            ("max_chars", "--max-chars", DEFAULT_MAX_CONTENT_CHARS),
            ("no_subsection_hints", "--no-subsection-hints", False),
            ("no_link_annotations", "--no-link-annotations", False),
        ),
        "search-content": (
            ("limit", "--limit", 10),
            ("context", "--context", 2),
            ("max_hits", "--max-hits", 5),
            ("max_snippet_chars", "--max-snippet-chars", DEFAULT_MAX_SNIPPET_CHARS),
            ("include_changelog_priority", "--include-changelog-priority", False),
        ),
    }


def retry_option_args(args, command: str | None = None) -> tuple:
    """The non-corpus options of *args* to echo when re-running *command*.

    *command* defaults to ``args.command``. Only values that differ from the
    default are returned, shell-quoted, so a retry keeps ``--max-chars 0`` or
    ``--limit 3`` instead of silently going back to the defaults.
    """
    command = command or getattr(args, "command", None)
    out = []
    for attr, flag, default in _retry_options().get(command, ()):
        if not hasattr(args, attr):
            continue
        value = getattr(args, attr)
        if value is None or value == default:
            continue
        if isinstance(default, bool):
            if value:
                out.append(flag)
            continue
        out += [flag, shlex.quote(str(value))]
    return tuple(out)


def retry_for_page_ref(args, hint_args: tuple = ()):
    """Build ``idx -> command`` re-running the current invocation on page *idx*.

    Covers the three subcommands that take a page reference: ``sections``,
    ``content`` (heading path kept) and ``search-content`` (``--page-ref``).
    Non-default options of that subcommand (``--max-chars``, ``--limit`` ...)
    are kept too (``retry_option_args``).
    """
    script = os.path.basename(sys.argv[0])
    command = getattr(args, "command", None)

    def retry(idx) -> str:
        if command == "search-content":
            parts = [script, command, shlex.quote(args.query), "--page-ref", str(idx)]
        else:
            parts = [script, command or "content", str(idx)]
            heading = getattr(args, "heading_path", None)
            if heading:
                parts.append(shlex.quote(heading))
        return " ".join(parts + list(retry_option_args(args, command)) + list(hint_args))

    return retry


def die_index_out_of_range(idx: int, total: int, name: str = "doc_index") -> None:
    """Print out-of-range error and exit 1."""
    print(f"Error: {name} {idx} out of range (0-{total - 1})", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Upstream format-change detection
# ---------------------------------------------------------------------------

def assert_parsed(label: str, count: int, path: str) -> None:
    """Exit 2 when *count* parsed items is 0.

    Distinguishes "the parser found nothing because the upstream format
    changed" (exit 2) from "the parser worked fine but this particular
    query had 0 hits" (exit 0). Call this immediately after parsing an
    index or full-text file, before any query-dependent logic runs, so a
    malformed/empty source fails loudly instead of silently behaving like
    an empty search result.
    """
    if count == 0:
        print(
            f"Error: {label} format may have changed (0 entries parsed "
            f"from {path}). Retry with --max-age 0; if it persists, "
            f"report an issue.",
            file=sys.stderr,
        )
        sys.exit(2)


def check_join_rate(label: str, joinable: int, total: int, *,
                    warn_threshold: float = 0.8, fail_threshold: float = 0.5) -> None:
    """Check the fraction of index entries that joined to a full-text doc.

    Below *fail_threshold* the join is broken badly enough to indicate an
    upstream format change rather than a handful of legitimately-missing
    pages — exit 2 (same failure class as ``assert_parsed``). Between
    *fail_threshold* and *warn_threshold*, print a WARNING and continue
    (some pages can legitimately lack a full-text counterpart).
    """
    if total == 0:
        return
    ratio = joinable / total
    if ratio < fail_threshold:
        print(
            f"Error: {label} join rate is {ratio:.0%} ({joinable}/{total}), "
            f"far below expected. The upstream format may have changed. "
            f"Retry with --max-age 0; if it persists, report an issue.",
            file=sys.stderr,
        )
        sys.exit(2)
    if ratio < warn_threshold:
        print(
            f"WARNING: {label} join rate is {ratio:.0%} ({joinable}/{total}). "
            f"Some index entries cannot be joined to full text.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Search ranking (shared by the ``search`` subcommand of all three scripts)
# ---------------------------------------------------------------------------

# Pages whose body text reliably overwhelms keyword searches (release notes,
# changelogs) get pushed below higher-signal pages in search results.
DEPRIORITIZE_PAGE_PATTERNS = ("changelog", "release-notes", "release notes")


def is_low_priority(title: str) -> bool:
    """True if *title* names a changelog / release-notes style page."""
    t = (title or "").lower()
    return any(p in t for p in DEPRIORITIZE_PAGE_PATTERNS)


def add_include_changelog_priority_arg(parser) -> None:
    """Add ``--include-changelog-priority`` (opt out of the deprioritise)."""
    parser.add_argument(
        "--include-changelog-priority", action="store_true",
        help="Do not deprioritize Changelog / release-notes pages",
    )


def search_content_rank_key(idx: int, title: str, hits: dict, *,
                            include_changelog_priority: bool = False) -> tuple:
    """Sort key for ``search-content`` pages, shared by all scripts.

    Order: changelog-style pages last (unless *include_changelog_priority*),
    then strict-AND pages before ``[partial match]`` pages, then the page's
    best section (``page_fit``: the page title and the section's headings
    name every keyword), then most hits, then lowest *idx*.
    ``--limit`` cuts only after this ordering.
    """
    bucket = 0 if include_changelog_priority else (1 if is_low_priority(title) else 0)
    return (bucket, match_rank(hits), page_fit(hits), -hits["total_matches"], idx)


def search_rank_key(result: dict, *, include_changelog_priority: bool = False) -> tuple:
    """Sort key for ``search`` results, identical across all three scripts.

    Order: changelog-style pages last (unless *include_changelog_priority*),
    then pages whose body hits are a strict AND before ``[partial match]``
    pages, then the best section (``page_fit``, as in
    ``search_content_rank_key``), then most body hits, then highest index score, then lowest
    ``doc_idx`` (stable, deterministic tie-break). A partial page can have
    many more hits (each keyword alone is common) while answering the query
    less well than a page that has every keyword in one section. Body hits outrank the index score
    because the index score only reflects title/description keywords while
    body hits reflect how much of the page is actually about the query —
    a page that merely names the term in its title but never discusses it
    should not outrank a page whose body covers it (internal backlog).

    *result* needs ``title``, ``doc_idx``, ``body_hits["total_matches"]``
    and optionally ``index_score`` (``None`` for body-only fallback rows).
    """
    bucket = 0 if include_changelog_priority else (
        1 if is_low_priority(result.get("title", "")) else 0
    )
    return (
        bucket,
        match_rank(result["body_hits"]),
        page_fit(result["body_hits"]),
        -result["body_hits"]["total_matches"],
        -(result.get("index_score") or 0),
        result["doc_idx"],
    )


# ---------------------------------------------------------------------------
# Metadata header (used by ``cmd_content``)
# ---------------------------------------------------------------------------

def print_metadata_header(title: str, *, source=None, tags=None, heading_path=None) -> None:
    """Print a standard ``# doc_title: ...`` block followed by ``---``.

    Output order: ``doc_title`` / ``source`` / ``doc_tags`` / ``heading_path``
    / ``---``. Lines with a falsy value (None, empty string, empty list) are
    skipped.
    """
    print(f"# doc_title: {title}")
    if source:
        print(f"# source: {source}")
    if tags:
        print(f"# doc_tags: {', '.join(tags)}")
    if heading_path:
        print(f"# heading_path: {heading_path}")
    print("---")


# ---------------------------------------------------------------------------
# Next hint
# ---------------------------------------------------------------------------

def next_hint(subcommand: str, *args: str) -> None:
    """Print ``Next: {basename(sys.argv[0])} {subcommand} {args...}``.

    ``os.path.basename`` normalises the script name so the hint always points
    to the file the user invoked, even via a symlink or absolute path.
    *args* are joined verbatim; callers are responsible for quoting
    placeholders like ``'"<heading_path>"'``.
    """
    basename = os.path.basename(sys.argv[0])
    extra = (" " + " ".join(args)) if args else ""
    print(f"Next: {basename} {subcommand}{extra}")


# ---------------------------------------------------------------------------
# argparse skeleton helpers
# ---------------------------------------------------------------------------

DEFAULT_MAX_AGE_SECONDS = 604800  # 7 days


def add_cache_dir_arg(parser, *, default: str | None = None, help=None) -> None:
    """Add ``--cache-dir`` to *parser*.

    *default* resolves via ``default_cache_dir()`` (env-overridable XDG
    cache dir) unless the caller passes an explicit value.
    """
    if default is None:
        default = default_cache_dir()
    if help is None:
        help = f"Directory to cache files (default: {default})"
    parser.add_argument("--cache-dir", default=default, help=help)


def corpus_hint_args(args) -> tuple:
    """Return ``--file``/``--cache-dir``/``--max-age`` CLI args needed to
    keep a generated follow-up command hint pointed at the same corpus as
    the current invocation, instead of silently falling back to defaults.

    A hint (e.g. the ``--max-chars`` truncation notice's "narrow with
    ..." suggestion, or ``print_subsection_hints``'s ``Next:`` line) that
    drops a non-default ``--file``/``--cache-dir``/``--max-age`` selection
    can point the *same numeric page index* at an entirely different
    document once the reader follows it: it re-resolves against the
    default corpus instead of the snapshot/cache dir just displayed, or
    (with a nondefault ``--max-age`` that intentionally accepts an older
    cache) re-fetches and can land on a different upstream ordering.
    ``--file`` is only present on scripts that support a read-only
    snapshot override (checked via ``getattr`` so scripts without it,
    e.g. firebase, are unaffected).

    ``--file`` makes ``--cache-dir``/``--max-age`` irrelevant in every
    loader that supports it (``_load_docs``/``_load_full_txt``): once
    ``--file`` is given, neither is ever even read. Echoing them anyway
    would mislead a reader into thinking they still matter, so once
    ``--file`` is present it is returned alone. ``--cache-dir`` is
    included only when it differs from ``default_cache_dir()`` (itself
    env-overridable, so a plain string default would false-positive under
    ``$XDG_CACHE_HOME``/``$LLMS_DOCS_CACHE_DIR``); ``--max-age`` only when
    it differs from ``DEFAULT_MAX_AGE_SECONDS``. Both a nondefault path
    value are shell-quoted (``shlex.quote``) since these strings are
    spliced verbatim into a copy-pasteable shell command line — an
    unquoted path containing a space or shell metacharacter would
    otherwise split into extra arguments or trigger shell expansion when
    the reader actually runs the generated command.
    """
    file_val = getattr(args, "file", None)
    if file_val:
        return ("--file", shlex.quote(file_val))
    result = []
    cache_dir = getattr(args, "cache_dir", None)
    if cache_dir and cache_dir != default_cache_dir():
        result += ["--cache-dir", shlex.quote(cache_dir)]
    max_age = getattr(args, "max_age", None)
    if max_age is not None and max_age != DEFAULT_MAX_AGE_SECONDS:
        result += ["--max-age", str(max_age)]
    return tuple(result)


def add_max_age_arg(parser) -> None:
    """Add ``--max-age`` to *parser* with a 7-day default."""
    parser.add_argument(
        "--max-age", type=int, default=DEFAULT_MAX_AGE_SECONDS,
        help=f"Re-fetch cache if older than N seconds (default: {DEFAULT_MAX_AGE_SECONDS} = 7 days, 0 = always re-fetch)",
    )


def add_heading_path_arg(parser, *, help: str = "Heading path (omit for full document)") -> None:
    """Add optional positional ``heading_path`` to *parser*."""
    parser.add_argument("heading_path", nargs="?", default=None, help=help)


# ---------------------------------------------------------------------------
# Content-size cap (``content`` subcommand)
# ---------------------------------------------------------------------------

DEFAULT_MAX_CONTENT_CHARS = 24000


def add_max_chars_arg(parser) -> None:
    """Add ``--max-chars`` to *parser* (``content`` subcommand only).

    The 24000-char default keeps a typical ``content`` call's total output
    (metadata header + body + subsection hints) comfortably under the
    Claude Code Bash tool's ~30KB threshold for showing output inline
    rather than diverting it to a file and showing only the first ~2KB —
    past that threshold the subsection hint / ``Next:`` line at the end of
    the output becomes invisible, defeating progressive drill-down.
    """
    parser.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CONTENT_CHARS,
        help=f"Truncate the content body to N chars (default: "
             f"{DEFAULT_MAX_CONTENT_CHARS}, 0 = no limit)",
    )


DEFAULT_MAX_SNIPPET_CHARS = 500


def add_max_snippet_chars_arg(parser) -> None:
    """Add ``--max-snippet-chars`` to *parser* (any ``search``/``search-content``
    subcommand that renders per-hit snippets via ``search_content_in_body``).
    """
    parser.add_argument(
        "--max-snippet-chars", type=int, default=DEFAULT_MAX_SNIPPET_CHARS,
        help=f"Fit each snippet in N chars, keeping the matching lines first; a "
             f"matching line is not cut below 80 chars (0 = no limit, default: "
             f"{DEFAULT_MAX_SNIPPET_CHARS})",
    )


def truncate_content(content: str, max_chars: int, *, narrow_hint: str,
                     next_command: str | None = None, next_note: str = "",
                     no_narrow_reason: str = "this section has no subsections to narrow to"
                     ) -> str:
    """Truncate *content* to at most *max_chars* characters (``<= 0``
    disables this), cutting at a line boundary that is not inside a
    fenced code block or Markdown table.

    A raw ``content[:max_chars]`` slice can land inside a fence or table
    (undoing ``extract_content``'s own boundary protection, which only
    guards heading-section cuts, not this later character-budget cut) or
    even split a single line in half, leaving both the truncated
    construct and the appended notice malformed. This instead walks
    lines from the start, remembering the end of the last line that (a)
    is itself not a table row and (b) leaves ``FenceTracker`` closed (and
    not waiting to confirm an indented opener), and cuts there — never later than *max_chars*, but possibly a little
    earlier if the naive boundary falls mid-fence/mid-table. Preferring
    an earlier cut (over extending forward to finish the block, the way
    ``extract_content`` does for its own boundary) keeps this a true
    upper bound on output size, which is the whole point of --max-chars.

    Appends a note showing how many characters were cut and a caller-
    supplied *narrow_hint* — typically a fuller ``content <ref>
    "<heading_path>"`` invocation string — telling the reader how to fetch
    a smaller slice instead of the same truncated one again. When the
    content has no subsection to narrow to, the caller passes
    *next_command* (a complete, runnable ``search-content`` command) and it
    is printed as a ``Next:`` line in place of *narrow_hint*, after
    *no_narrow_reason* in the notice and a line of its own carrying
    *next_note* (which names the stand-in keyword to replace). Callers
    should apply this AFTER every other content transform (link
    annotation, where applicable, adds text too) so the truncation point
    reflects the actual length of what the reader receives, not a
    pre-annotation length that the annotations could then push back over
    the limit.
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content

    fence = FenceTracker()
    cumulative = 0
    safe_cut = 0
    for line in content.splitlines(keepends=True):
        line_end = cumulative + len(line)
        fence.update(line)
        if line_end > max_chars:
            break
        if not fence.in_fence and fence._pending is None and not _is_table_line(line):
            safe_cut = line_end
        cumulative = line_end
    # safe_cut deliberately stays 0 (rather than falling back to a raw
    # content[:max_chars] slice) when no line boundary is safe within
    # budget — e.g. the very first line alone exceeds max_chars, or an
    # unclosed fence/table runs from the start past it. A raw slice in
    # that case would reintroduce exactly the malformed-cut failure this
    # function exists to prevent; emitting no body at all before the
    # notice is always well-formed, if less useful.
    cut = len(content) - safe_cut
    if next_command:
        # A section with no subsections cannot be narrowed by heading: point
        # at a search inside the page instead, as a line of its own that can
        # be run as printed (like every ``Next:`` line)
        return (
            content[:safe_cut]
            + f"\n... ({cut} chars truncated; {no_narrow_reason})\n"
            + (f"{next_note}\n" if next_note else "")
            + f"Next: {next_command}\n"
        )
    return (
        content[:safe_cut]
        + f"\n... ({cut} chars truncated; narrow with {narrow_hint})\n"
    )


# ---------------------------------------------------------------------------
# Subsection hints (``content`` subcommand)
# ---------------------------------------------------------------------------

def _shallowest(candidates):
    """The sections of *candidates* at the shallowest level among them (the
    direct children of whatever contains them, even when a level is skipped:
    an H2 followed only by H4s has those H4s as its children)."""
    if not candidates:
        return []
    level = min(s["level"] for s in candidates)
    return [s for s in candidates if s["level"] == level]


def subsection_children(sections, body_lines, heading_path):
    """``(label, children, target)`` for the direct child sections of
    *heading_path* (the top-level sections when it is ``None``), or ``None``
    when *heading_path* is not one of *sections*. *target* is the section
    itself (``None`` for the page top).

    A direct child is a section at the shallowest level below its parent,
    not at exactly one level deeper: the top of a page whose first heading
    is an H2 (AI SDK pages carry the title in the frontmatter, not as an
    H1) has those H2s as its children, and so does an H2 whose only
    subsections are H4s. A page top is childless only when the page has no
    heading at all."""
    if heading_path is None:
        return "Top-level sections", _shallowest(sections), None
    target = None
    for s in sections:
        if s["heading_path"] == heading_path:
            target = s
            break
    if target is None:
        return None
    target_idx = sections.index(target)
    # Block extent = up to the next section at same or higher (smaller-
    # number) level. ``target["line_end"]`` only spans until the
    # immediate next heading, which would stop at the first child and
    # miss the rest of the block.
    block_end = len(body_lines)
    for s in sections[target_idx + 1:]:
        if s["level"] <= target["level"]:
            block_end = s["line_start"]
            break
    children = _shallowest([
        s for s in sections[target_idx + 1:]
        if s["line_start"] < block_end and s["level"] > target["level"]
    ])
    return f"Subsections of '{target['heading_path']}'", children, target


def print_subsection_hints(body_lines, page_ref, heading_path, *,
                            min_level: int = 2, extra_hint_args: tuple = ()) -> None:
    """Print direct child sections of *heading_path* (or top-level if
    ``None``) as a hint block, followed by a ``Next: <script> content
    <page_ref> "..."`` line. Lets the caller drill down further without
    re-running ``sections``.

    Shared by all three ``parse-*.py`` scripts' ``cmd_content`` (moved out
    of claude-docs-only ``_print_subsection_hints`` so ai-sdk/firebase get
    the same drill-down experience claude-docs already had).

    *heading_path* must already be the *resolved* canonical path returned
    by ``extract_content`` (or ``None``/``"(top)"``) — not the caller's
    raw, possibly-partial input; resolution (including the ambiguous-
    partial-match check) happens exactly once, in ``extract_content``.
    Looking it up again here from a raw argument would risk silently
    disagreeing with what the content body actually displayed. No section
    is ever literally titled ``"(top)"``, so that sentinel falls through
    to "no hint" below — the preamble before the first heading has no
    child sections to hint at.

    *min_level* must match the value the caller's ``extract_content``/
    ``extract_sections`` calls use for the same document (2 for
    claude-docs/firebase, 1 for ai-sdk) — see ``extract_content``'s own
    docstring for why a mismatch here would show one heading hierarchy
    while resolving against another.
    """
    sections = extract_sections(body_lines, min_level=min_level)
    if not sections:
        return
    found = subsection_children(sections, body_lines, heading_path)
    if found is None:
        return
    label, children, _target = found
    if not children:
        return

    print()
    print(f"--- {label} ({len(children)}) ---")
    for s in children:
        code_marker = " [code]" if s["has_code_blocks"] else ""
        print(f"  - {s['heading_path']}{code_marker}")
    print()
    basename = os.path.basename(sys.argv[0])
    extra_suffix = (" " + " ".join(extra_hint_args)) if extra_hint_args else ""
    print(
        f'Next: {basename} content {page_ref} '
        f'"<heading_path from above>"{extra_suffix}'
    )
