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
