#!/usr/bin/env python3
"""Progressive loader for Firebase documentation (firebase.google.com/docs/llms.txt).

Firebase publishes a lightweight ``llms.txt`` index of per-page ``.md.txt``
documents. Unlike AI SDK or Claude docs, there is **no ``llms-full.txt``**, so
individual pages are fetched on demand and cached alongside the index.

Subcommands:

  fetch-index  — Fetch (if uncached) and print page index (paginated)
  sections     — List sections within a specific page (auto-fetches the page)
  content      — Print content of a page or section (auto-fetches the page)
"""

import argparse
import hashlib
import os
import re
import sys
import urllib.request
import urllib.error

LLMS_TXT_URL = "https://firebase.google.com/docs/llms.txt"
DEFAULT_CACHE_DIR = "/tmp"
INDEX_CACHE_NAME = "firebase-llms.txt"
PAGES_CACHE_SUBDIR = "firebase-docs"


# ---------------------------------------------------------------------------
# Core parser: code-fence-aware line scanner
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
# Section (heading) extraction — H2 and below
# ---------------------------------------------------------------------------

def extract_sections(body_lines: list[str]) -> list[dict]:
    """Extract Markdown headings (H2+) from *body_lines*."""
    fence = FenceTracker()
    headings: list[dict] = []

    for idx, line in enumerate(body_lines):
        was_in_fence = fence.in_fence
        fence.update(line)
        if was_in_fence or fence.in_fence:
            continue

        m = re.match(r"^(#{2,6})\s+(.+)", line)
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

    path_stack: list[tuple[int, str]] = []
    for h in headings:
        while path_stack and path_stack[-1][0] >= h["level"]:
            path_stack.pop()
        path_stack.append((h["level"], h["title"]))
        h["heading_path"] = "/".join(t for _, t in path_stack)

    return headings


# ---------------------------------------------------------------------------
# Content extraction with code-fence and table protection
# ---------------------------------------------------------------------------

def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped.startswith("|") and stripped.endswith("|")


def extract_content(body_lines: list[str], heading_path: str | None = None) -> str:
    """Extract content; if *heading_path* is given, return that section's content.

    Extends the slice to include any unclosed code fence or unfinished table.
    """
    if heading_path is None:
        return "".join(body_lines)

    sections = extract_sections(body_lines)
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
        available = "\n".join(f"  - {s['heading_path']}" for s in sections)
        print(
            f"Error: heading '{heading_path}' not found.\n\nAvailable sections:\n{available}",
            file=sys.stderr,
        )
        sys.exit(1)

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

    if content_lines and _is_table_line(content_lines[-1]):
        i = end_line
        while i < len(body_lines) and _is_table_line(body_lines[i]):
            content_lines.append(body_lines[i])
            i += 1

    return "".join(content_lines)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, cache_path: str) -> str:
    """Return path to cached file, fetching from *url* if necessary."""
    if os.path.exists(cache_path):
        return cache_path

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "claude-code-firebase-researcher/1.0"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        parent = os.path.dirname(cache_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path
    except urllib.error.URLError as e:
        print(f"Error: Failed to fetch {url}: {e}", file=sys.stderr)
        sys.exit(1)


def load_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


# ---------------------------------------------------------------------------
# URL → cache filename
# ---------------------------------------------------------------------------

_URL_PREFIX = "https://firebase.google.com/"


def _url_to_cache_filename(url: str) -> str:
    """Convert a Firebase ``.md.txt`` URL to a cache filename.

    Strips the firebase.google.com host and replaces ``/`` with ``_`` so the
    original URL structure stays legible in /tmp. Falls back to a sha1 hash
    for unexpected URL patterns or extremely long paths. Note: the index page
    URL is ``firebase.google.com/docs.md.txt`` (a sibling of ``/docs/...``),
    not ``/docs/index.md.txt``, so the prefix must be the host root.
    """
    if url.startswith(_URL_PREFIX):
        path = url[len(_URL_PREFIX):]
        if path:
            safe = path.replace("/", "_")
            if len(safe) <= 200:
                return safe
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return f"{h}.md.txt"


# ---------------------------------------------------------------------------
# llms.txt index parser
# ---------------------------------------------------------------------------

def parse_llms_index(lines: list[str]) -> list[dict]:
    """Parse firebase llms.txt into a list of page entries.

    Format: ``- [Title](URL): Description``
    """
    entries: list[dict] = []
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
# Cache path + index loading helpers
# ---------------------------------------------------------------------------

def _index_cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir.rstrip("/"), INDEX_CACHE_NAME)


def _pages_cache_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir.rstrip("/"), PAGES_CACHE_SUBDIR)


def _load_index(cache_dir: str) -> list[dict]:
    """Fetch (if needed) the llms.txt index and return parsed entries."""
    idx_path = _fetch_url(LLMS_TXT_URL, _index_cache_path(cache_dir))
    entries = parse_llms_index(load_lines(idx_path))
    if not entries:
        print(
            f"Error: no entries parsed from {idx_path}. Format may have changed.",
            file=sys.stderr,
        )
        sys.exit(1)
    return entries


def _fetch_page(url: str, cache_dir: str) -> str:
    """Fetch an individual page ``.md.txt`` on demand and return its cache path."""
    filename = _url_to_cache_filename(url)
    page_path = os.path.join(_pages_cache_dir(cache_dir), filename)
    return _fetch_url(url, page_path)


def _validate_index(idx: int, total: int) -> None:
    if idx < 0 or idx >= total:
        print(f"Error: doc_index {idx} out of range (0-{total - 1})", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_fetch_index(args):
    entries = _load_index(args.cache_dir)
    total = len(entries)

    offset = max(0, args.offset)
    limit = args.limit if args.limit > 0 else total - offset
    end = min(offset + limit, total)

    print("Firebase Documentation Index (from llms.txt)")
    print("=" * 60)
    print()

    for i in range(offset, end):
        entry = entries[i]
        print(f"[{i}] {entry['title']}")
        if entry["description"]:
            desc = entry["description"]
            if len(desc) > 120:
                desc = desc[:117] + "..."
            print(f"    {desc}")
        print()

    shown = end - offset
    print(f"({shown} of {total} pages shown, offset={offset})")
    if end < total:
        print(f"  Next page: --offset {end} --limit {args.limit}")
    print()
    print("Next: parse-firebase.py sections <doc_index>")


def cmd_sections(args):
    entries = _load_index(args.cache_dir)
    _validate_index(args.doc_index, len(entries))

    entry = entries[args.doc_index]
    page_path = _fetch_page(entry["url"], args.cache_dir)
    lines = load_lines(page_path)
    sections = extract_sections(lines)

    print(f'Sections in [{args.doc_index}] "{entry["title"]}"')
    print(f"  URL: {entry['url']}")
    print(f"  Cache: {page_path}")
    print("=" * 60)

    for s in sections:
        indent = "  " * (s["level"] - 2)
        code_marker = " [code]" if s["has_code_blocks"] else ""
        print(f"{indent}[L{s['level']}] {s['title']}{code_marker}")

    print()
    print(f"({len(sections)} sections)")
    print()
    print(f"Next: parse-firebase.py content {args.doc_index} \"<heading_path>\"")


def cmd_content(args):
    entries = _load_index(args.cache_dir)
    _validate_index(args.doc_index, len(entries))

    entry = entries[args.doc_index]
    page_path = _fetch_page(entry["url"], args.cache_dir)
    lines = load_lines(page_path)

    content = extract_content(lines, args.heading_path)

    print(f"# doc_title: {entry['title']}")
    print(f"# source: {entry['url']}")
    if args.heading_path:
        print(f"# heading_path: {args.heading_path}")
    print("---")
    print(content, end="")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Progressive loader for Firebase documentation (firebase.google.com/docs/llms.txt)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("fetch-index", help="Fetch and print page index")
    p_index.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help=f"Directory to cache files (default: {DEFAULT_CACHE_DIR})",
    )
    p_index.add_argument(
        "--limit", type=int, default=100,
        help="Max entries to show (default: 100, use 0 for all)",
    )
    p_index.add_argument(
        "--offset", type=int, default=0,
        help="Skip first N entries (default: 0)",
    )
    p_index.set_defaults(func=cmd_fetch_index)

    p_sections = sub.add_parser("sections", help="List sections in a page")
    p_sections.add_argument("doc_index", type=int, help="Page index (from fetch-index)")
    p_sections.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help=f"Directory to cache files (default: {DEFAULT_CACHE_DIR})",
    )
    p_sections.set_defaults(func=cmd_sections)

    p_content = sub.add_parser("content", help="Print page/section content")
    p_content.add_argument("doc_index", type=int, help="Page index")
    p_content.add_argument(
        "heading_path", nargs="?", default=None,
        help="Heading path (omit for full page)",
    )
    p_content.add_argument(
        "--cache-dir", default=DEFAULT_CACHE_DIR,
        help=f"Directory to cache files (default: {DEFAULT_CACHE_DIR})",
    )
    p_content.set_defaults(func=cmd_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
