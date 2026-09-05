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
    """Tracks whether the current line is inside a fenced code block."""

    def __init__(self):
        self.in_fence = False
        self._fence_len = 0

    def update(self, line: str) -> bool:
        """Update state for *line* and return True if inside a fence AFTER update."""
        stripped = line.lstrip()
        if stripped.startswith("```"):
            backtick_count = len(stripped) - len(stripped.lstrip("`"))
            if not self.in_fence:
                self.in_fence = True
                self._fence_len = backtick_count
            elif backtick_count >= self._fence_len:
                self.in_fence = False
                self._fence_len = 0
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
                    protect_tables: bool = True, min_level: int = 2):
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

    target = None
    for s in sections:
        if s["heading_path"] == heading_path or s["title"] == heading_path:
            target = s
            break

    heading_lower = heading_path.lower()

    if target is None:
        # Case-folded exact match, still a second "exact" tier — not a
        # substring/partial one — so it also wins outright with no
        # ambiguity check. Skipping this and falling straight to the
        # substring tier below would wrongly treat a case-differing exact
        # match as merely "ambiguous" whenever it happens to also be a
        # substring of one of its own descendants' heading_path.
        for s in sections:
            if s["heading_path"].lower() == heading_lower or s["title"].lower() == heading_lower:
                target = s
                break

    if target is None:
        matches = [
            s for s in sections
            if heading_lower in s["heading_path"].lower() or heading_lower in s["title"].lower()
        ]
        if len(matches) > 1:
            die_ambiguous_heading(heading_path, matches)
        if matches:
            target = matches[0]

    if target is None:
        die_heading_not_found(heading_path, sections)

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

def _norm(tok: str) -> str:
    """Lowercase, strip ``-``/``_`` separators, and apply a light plural-to-
    singular stem so ``score_entry`` treats e.g. ``"skills"``/``"Skill"``,
    ``"hooks"``/``"Hook events"``, and ``"stream-text"``/``"streamText"`` as
    equivalent tokens.

    Not a real stemmer (no Porter/Snowball) — just enough to fold the common
    English plural suffixes and hyphen/underscore spelling variants that show
    up across the doc corpora. Order matters: the multi-character sibilant
    endings (``-ses``/``-xes``/``-zes``/``-ches``/``-shes``, e.g. "boxes" →
    "box") are checked *before* the generic single-``s`` strip, because
    stripping only the trailing ``s`` from those would leave a dangling
    ``e`` (e.g. "boxes" → "boxe"). Words already ending in ``ss`` (e.g.
    "process") are left untouched to avoid mangling a non-plural word.

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
    if t.endswith(("ses", "xes", "zes", "ches", "shes")) and len(t) > 4:
        return t[:-2]
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

        if kw_score > 0:
            matched_keywords += 1
        total += kw_score

    if scorable_keywords > 1 and matched_keywords == scorable_keywords:
        total += 10

    return total


def search_index_entries(entries, query: str, *, limit: int = 15, get_extras=None):
    """Score and rank *entries* (dicts with 'title' and 'description') against *query*.

    *query* is split on whitespace and treated as AND keywords. *get_extras*,
    when given, is called as ``get_extras(entry, idx)`` and must return a dict
    that may contain 'tags' and/or 'headings' used for extra scoring signals.

    Returns a list of ``(score, idx, entry)`` tuples sorted by score desc,
    truncated to *limit*. Empty list if query has no tokens.
    """
    keywords = [k for k in query.split() if k]
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
    while their content is kept. Merge-review finding.
    """
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", title)   # image -> alt
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # link -> text
    s = re.sub(r"<[^>]+>", "", s)                          # html tags
    s = html.unescape(s)
    # 強調の区切りとして使われた `_` だけを剥がす (単語境界に接する `_..._` の組)。
    # `snake_case_name` のような識別子内の `_` は見出しの実テキストなので残す
    s = re.sub(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", s)
    s = re.sub(r"[`*~]+", "", s)                          # code / bold / strike markers
    return s.strip()


def heading_anchor_slug(title: str, style: str = "github") -> str:
    """Best-effort anchor slug for one heading title.

    Two platform families generate heading ids differently, so *style*
    selects which one to reproduce:

    - ``"github"`` (default, also matches Mintlify): lowercase, strip
      characters that aren't a letter/digit/underscore/hyphen/space, then
      collapse whitespace runs to a single hyphen. Used by GitHub-flavored
      Markdown renderers and Mintlify docs (e.g. code.claude.com,
      platform.claude.com).
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
    s = re.sub(r"[^\w\s-]", "", s)
    if style == "devsite":
        return re.sub(r"\s+", "_", s).strip("_")
    return re.sub(r"\s+", "-", s).strip("-")


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


def _build_section_results(section_hits, sections, body_lines, keywords,
                           min_coverage, context_lines, max_snippet_chars):
    """Build result list from sections matching >= *min_coverage* keywords."""
    results = []
    total = 0
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

        snippet_lines = []
        for j in range(snippet_start, snippet_end):
            marker = "→ " if j in hit_set else "  "
            snippet_lines.append(f"{marker}{body_lines[j].rstrip()}")
        if truncated:
            snippet_lines.append(f"  ... ({truncated} more hits in this section)")

        snippet = "\n".join(snippet_lines)
        if max_snippet_chars and len(snippet) > max_snippet_chars:
            cut = len(snippet) - max_snippet_chars
            snippet = snippet[:max_snippet_chars] + f"\n  ... ({cut} chars truncated)"

        # "title" is the section's own leaf heading text (or "(top)"),
        # kept separate from "heading_path" (the ancestor breadcrumb) so
        # anchor generation never has to split heading_path on "/" — a
        # heading whose own title contains a slash (e.g. "## CI/CD") would
        # otherwise be misread as a nested breadcrumb (merge-review finding).
        title = sections[si]["title"] if si is not None else "(top)"
        results.append({
            "heading_path": heading_path,
            "title": title,
            "line_offset": hit_line_numbers[0],
            "snippet": snippet,
            "matched_keywords": sorted(all_matched),
            "hit_count": len(hits),
        })
    return results, total


def search_content_in_body(body_lines, query: str, *,
                           context_lines: int = 2,
                           max_matches_per_doc: int = 5,
                           min_level: int = 2,
                           max_snippet_chars: int | None = None):
    """Search *body_lines* for *query* keywords (soft-AND, case-insensitive).

    First tries strict AND (all keywords in same section). If no results and
    len(keywords) > 1, relaxes to partial match (>= ceil(N/2) keywords).
    The ``match_mode`` field indicates which strategy produced results.

    Returns a dict with ``total_matches``, ``results``, and ``match_mode``
    (``"and"`` | ``"partial"`` | ``"none"``).
    """
    keywords = [k.lower() for k in query.split() if k]
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
    build_kw = dict(context_lines=context_lines, max_snippet_chars=max_snippet_chars)

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

    # Rank: keyword coverage desc, hit density desc, position asc
    results.sort(key=lambda r: (-len(r["matched_keywords"]), -r["hit_count"], r["line_offset"]))

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
    }


def full_corpus_body_search(docs_body_lines, query: str, *,
                            context_lines: int = 2,
                            max_matches_per_doc: int = 3,
                            max_snippet_chars: int | None = None,
                            min_level: int = 2,
                            limit: int = 5):
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
    match, sorted by total_matches desc then doc_idx asc, truncated to
    *limit*.
    """
    results = []
    for idx, body_lines in enumerate(docs_body_lines):
        hits = search_content_in_body(
            body_lines, query,
            context_lines=context_lines,
            max_matches_per_doc=max_matches_per_doc,
            min_level=min_level,
            max_snippet_chars=max_snippet_chars,
        )
        if hits["total_matches"] > 0:
            results.append((idx, hits))
    results.sort(key=lambda t: (-t[1]["total_matches"], t[0]))
    return results[:limit]


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    """Print ``Error: {msg}`` to stderr and exit with *code*."""
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def die_heading_not_found(heading_path: str, sections) -> None:
    """Print heading-not-found error with available sections and exit 1."""
    available = "\n".join(f"  - {s['heading_path']}" for s in sections)
    print(
        f"Error: heading '{heading_path}' not found.\n\nAvailable sections:\n{available}",
        file=sys.stderr,
    )
    sys.exit(1)


def die_ambiguous_heading(heading_path: str, matches) -> None:
    """Print an ambiguous-heading error (2+ case-insensitive partial matches
    for the same *heading_path*) listing every candidate, and exit 1.

    Mirrors the ambiguous-slug error the parse-*.py scripts already raise
    from ``_resolve_page_ref`` — silently picking the first partial match
    risks the caller reading (and citing) the wrong section with no
    indication that other candidates existed.
    """
    detail = "\n  ".join(f"- {m['heading_path']}" for m in matches)
    print(
        f"Error: ambiguous heading '{heading_path}'. Matches:\n  {detail}",
        file=sys.stderr,
    )
    sys.exit(1)


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
        help=f"Truncate each snippet to N chars (0 = no limit, default: "
             f"{DEFAULT_MAX_SNIPPET_CHARS})",
    )


def truncate_content(content: str, max_chars: int, *, narrow_hint: str) -> str:
    """Truncate *content* to at most *max_chars* characters (``<= 0``
    disables this), cutting at a line boundary that is not inside a
    fenced code block or Markdown table.

    A raw ``content[:max_chars]`` slice can land inside a fence or table
    (undoing ``extract_content``'s own boundary protection, which only
    guards heading-section cuts, not this later character-budget cut) or
    even split a single line in half, leaving both the truncated
    construct and the appended notice malformed. This instead walks
    lines from the start, remembering the end of the last line that (a)
    is itself not a table row and (b) leaves ``FenceTracker`` closed, and
    cuts there — never later than *max_chars*, but possibly a little
    earlier if the naive boundary falls mid-fence/mid-table. Preferring
    an earlier cut (over extending forward to finish the block, the way
    ``extract_content`` does for its own boundary) keeps this a true
    upper bound on output size, which is the whole point of --max-chars.

    Appends a note showing how many characters were cut and a caller-
    supplied *narrow_hint* — typically a fuller ``content <ref>
    "<heading_path>"`` invocation string — telling the reader how to fetch
    a smaller slice instead of the same truncated one again. Callers
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
        if not fence.in_fence and not _is_table_line(line):
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
    return (
        content[:safe_cut]
        + f"\n... ({cut} chars truncated; narrow with {narrow_hint})\n"
    )


# ---------------------------------------------------------------------------
# Subsection hints (``content`` subcommand)
# ---------------------------------------------------------------------------

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

    if heading_path is None:
        children = [s for s in sections if s["level"] == min_level]
        label = "Top-level sections"
    else:
        target = None
        for s in sections:
            if s["heading_path"] == heading_path:
                target = s
                break
        if target is None:
            return
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
        child_level = target["level"] + 1
        children = [
            s for s in sections[target_idx + 1:]
            if s["line_start"] < block_end and s["level"] == child_level
        ]
        label = f"Subsections of '{target['heading_path']}'"

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
