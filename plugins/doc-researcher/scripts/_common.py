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

import os
import re
import sys
import urllib.error
import urllib.request


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


def extract_content(body_lines, heading_path=None, *,
                    protect_tables: bool = True, min_level: int = 2) -> str:
    """Extract content from *body_lines*.

    If *heading_path* is None, return the entire body. Otherwise, find the
    matching section and return its content, extending the slice to include
    any unclosed code fence. When *protect_tables* is True, also extend to
    include a Markdown table that straddles the section boundary.

    *min_level* must match what the caller's ``cmd_sections`` prints — otherwise
    the AI agent sees one heading hierarchy but searches another, which lets
    a stray H1/H2 of the same title silently match the wrong section. AI SDK
    passes ``min_level=1`` (its body_lines include the H1); Claude/Firebase
    keep the default of 2 because Firebase hands the full page (H1 included)
    to this function and must not collapse an H1 onto an identically-named H2.
    """
    if heading_path is None:
        return "".join(body_lines)

    sections = extract_sections(body_lines, min_level=min_level)
    target = None
    for s in sections:
        if s["heading_path"] == heading_path or s["title"] == heading_path:
            target = s
            break

    if target is None:
        heading_lower = heading_path.lower()
        for s in sections:
            if heading_lower in s["heading_path"].lower() or heading_lower in s["title"].lower():
                target = s
                break

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

    content_lines = list(body_lines[target["line_start"]:end_line])

    # Code-fence protection: if we end inside a fence, extend to closing
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

    # Table protection: if we end inside a table, extend to table end
    if protect_tables and content_lines and _is_table_line(content_lines[-1]):
        i = end_line
        while i < len(body_lines) and _is_table_line(body_lines[i]):
            content_lines.append(body_lines[i])
            i += 1

    return "".join(content_lines)


# ---------------------------------------------------------------------------
# Core: llms.txt lightweight index parser
# ---------------------------------------------------------------------------

def parse_llms_index(lines):
    """Parse a llms.txt lightweight index into page entries.

    Handles:
        - ``- [Title](URL): Description``
        - ``- [Title](URL) - Description``
        - ``- [Title](URL)``

    Returns a list of dicts: ``{"title": str, "url": str, "description": str}``.
    """
    entries = []
    for line in lines:
        m = re.match(r"^- \[(.+?)\]\((https?://\S+?)\)(?:(?::\s*|\s+-\s+)(.+))?$", line.strip())
        if m:
            entries.append({
                "title": m.group(1),
                "url": m.group(2),
                "description": (m.group(3) or "").strip(),
            })
    return entries


# ---------------------------------------------------------------------------
# Core: HTTP fetch + file IO
# ---------------------------------------------------------------------------

def fetch_url(url: str, cache_path: str, *, user_agent: str,
              timeout: int = 120, create_parent: bool = True) -> str:
    """Return path to cached file, fetching from *url* if it doesn't exist yet.

    On transport failure, prints ``Error: ...`` to stderr and exits 1
    (mirrors the pre-refactor per-script helpers).
    """
    if os.path.exists(cache_path):
        return cache_path

    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if create_parent:
            parent = os.path.dirname(cache_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path
    except urllib.error.URLError as e:
        print(f"Error: Failed to fetch {url}: {e}", file=sys.stderr)
        sys.exit(1)


def load_lines(path: str):
    """Read file and return lines (preserving newlines)."""
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


# ---------------------------------------------------------------------------
# Core: keyword search over index entries and body content
# ---------------------------------------------------------------------------

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
    """
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()
    tags_lower = [t.lower() for t in (tags or [])]
    headings_lower = [h.lower() for h in (headings or [])]

    total = 0
    matched_keywords = 0

    for kw in keywords:
        kw_lower = kw.lower()
        kw_score = 0

        if kw_lower == title_lower:
            kw_score += 10
        elif kw_lower in title_lower:
            kw_score += 5

        if any(kw_lower == t or kw_lower in t for t in tags_lower):
            kw_score += 4

        if kw_lower in desc_lower:
            kw_score += 2

        if any(kw_lower in h for h in headings_lower):
            kw_score += 1

        if kw_score > 0:
            matched_keywords += 1
        total += kw_score

    if len(keywords) > 1 and matched_keywords == len(keywords):
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


def search_content_in_body(body_lines, query: str, *,
                           context_lines: int = 2,
                           max_matches_per_doc: int = 5,
                           min_level: int = 2):
    """Search *body_lines* for *query* keywords (section-level AND, case-insensitive).

    A section is considered a match only when **all** query keywords appear
    somewhere within it. Matches within the same section are grouped into one
    snippet so the Agent sees the full context rather than scattered hits.

    Hit lines are prefixed with ``→``, surrounding context lines with two
    spaces. If a section contains more than ~20 lines of hits, the snippet
    truncates to the first few hits to keep output manageable.

    *min_level* must match what ``extract_sections`` uses elsewhere in the
    caller (1 for AI SDK frontmatter-delimited docs, 2 for Claude/Firebase).

    Returns a dict:
        {
            "total_matches": int,              # total lines that matched in ANY section
                                               # that contains all keywords (pre-truncation)
            "results": [
                {
                    "heading_path": str,       # enclosing section, or "(top)" if before first heading
                    "line_offset": int,        # first hit line in body_lines (0-based)
                    "snippet": str,
                    "matched_keywords": list[str],
                    "hit_count": int,          # number of hit lines within this section
                },
                ...
            ],
        }
    """
    keywords = [k.lower() for k in query.split() if k]
    if not keywords:
        return {"total_matches": 0, "results": []}

    sections = extract_sections(body_lines, min_level=min_level)

    # Map each line to its enclosing section index (None = before first heading)
    line_to_section_idx: list = [None] * len(body_lines)
    for si, s in enumerate(sections):
        for i in range(s["line_start"], s["line_end"]):
            if 0 <= i < len(body_lines):
                line_to_section_idx[i] = si

    # Collect hits bucketed by section
    section_hits: dict = {}
    for i, line in enumerate(body_lines):
        line_lower = line.lower()
        matched = [kw for kw in keywords if kw in line_lower]
        if not matched:
            continue
        si = line_to_section_idx[i]
        section_hits.setdefault(si, []).append((i, matched))

    results = []
    total_matches = 0

    for si, hits in section_hits.items():
        all_matched = set()
        for _, m in hits:
            all_matched.update(m)

        # Section-level AND: require every keyword to appear in this section
        if len(all_matched) < len(keywords):
            continue

        total_matches += len(hits)
        heading_path = sections[si]["heading_path"] if si is not None else "(top)"
        hit_line_numbers = [h[0] for h in hits]

        # Keep the snippet manageable: if too many hits, show first 3 plus trailing marker
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

        results.append({
            "heading_path": heading_path,
            "line_offset": hit_line_numbers[0],
            "snippet": "\n".join(snippet_lines),
            "matched_keywords": sorted(all_matched),
            "hit_count": len(hits),
        })

    # Rank by hit density, then by earliest appearance
    results.sort(key=lambda r: (-r["hit_count"], r["line_offset"]))

    if max_matches_per_doc > 0:
        results = results[:max_matches_per_doc]

    return {"total_matches": total_matches, "results": results}


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


def die_index_out_of_range(idx: int, total: int, name: str = "doc_index") -> None:
    """Print out-of-range error and exit 1."""
    print(f"Error: {name} {idx} out of range (0-{total - 1})", file=sys.stderr)
    sys.exit(1)


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

def add_cache_dir_arg(parser, *, default: str = "/tmp", help=None) -> None:
    """Add ``--cache-dir`` to *parser*."""
    if help is None:
        help = f"Directory to cache files (default: {default})"
    parser.add_argument("--cache-dir", default=default, help=help)


def add_doc_index_arg(parser, *, help: str = "Document index (from fetch-index)") -> None:
    """Add positional ``doc_index`` (int) to *parser*."""
    parser.add_argument("doc_index", type=int, help=help)


def add_heading_path_arg(parser, *, help: str = "Heading path (omit for full document)") -> None:
    """Add optional positional ``heading_path`` to *parser*."""
    parser.add_argument("heading_path", nargs="?", default=None, help=help)
