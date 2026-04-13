#!/usr/bin/env python3
"""Progressive loader for Claude documentation (llms-full.txt).

Supports two documentation sources:
  - code     : code.claude.com/docs      (Claude Code)
  - platform : platform.claude.com/docs  (Claude Developer Platform)

Parses the concatenated H1-delimited Markdown pages and provides
subcommands for progressive (layered) access:

  fetch-index  — Fetch (if uncached) and print page index
  sections     — List sections (headings) within a specific page
  content      — Print content of a specific page or section
"""

import argparse
import os
import re
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Source profiles
# ---------------------------------------------------------------------------

SOURCES = {
    "code": {
        "label": "Claude Code",
        "index_url": "https://code.claude.com/docs/llms.txt",
        "full_url": "https://code.claude.com/docs/llms-full.txt",
        "index_cache": "claude-code-llms.txt",
        "full_cache": "claude-code-llms-full.txt",
    },
    "platform": {
        "label": "Claude Developer Platform",
        "index_url": "https://platform.claude.com/llms.txt",
        "full_url": "https://platform.claude.com/llms-full.txt",
        "index_cache": "claude-platform-llms.txt",
        "full_cache": "claude-platform-llms-full.txt",
    },
}

DEFAULT_SOURCE = "code"


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
# Document splitting (H1 boundaries)
# ---------------------------------------------------------------------------

def _is_h1(line: str) -> re.Match | None:
    """Return match if *line* is an H1 heading (``# Title``)."""
    return re.match(r"^# (.+)", line)


def _extract_source_url(body_lines: list[str], limit: int = 10) -> str:
    """Extract Source:/URL: from the first *limit* lines of a page body."""
    for line in body_lines[:limit]:
        m = re.match(r"^(?:Source|URL):\s*(https?://\S+)", line.strip())
        if m:
            return m.group(1)
    return ""


def split_documents(lines: list[str]) -> list[dict]:
    """Split *lines* into page documents delimited by H1 headings.

    Handles both Claude Code format (single H1) and Platform format
    (duplicate H1 with URL: line between them) by merging consecutive
    documents with the same title when the first one is very short.

    Returns a list of dicts:
        {
            "title": str,
            "source_url": str,
            "body_lines": [...],     # lines after H1 until next H1
            "line_start": int,       # 0-based index into *lines*
            "line_end": int,
        }
    """
    raw_docs: list[dict] = []
    fence = FenceTracker()

    current_title: str | None = None
    current_start: int = 0
    current_body_start: int = 0

    for i, line in enumerate(lines):
        was_in_fence = fence.in_fence
        fence.update(line)

        # Only detect H1 outside code fences
        if was_in_fence or fence.in_fence:
            continue

        m = _is_h1(line)
        if m:
            # Finalize previous document if exists
            if current_title is not None:
                body = lines[current_body_start:i]
                raw_docs.append({
                    "title": current_title,
                    "source_url": _extract_source_url(body),
                    "body_lines": body,
                    "line_start": current_start,
                    "line_end": i,
                })

            current_title = m.group(1).strip()
            current_start = i
            current_body_start = i + 1  # skip H1 line itself

    # Finalize last document
    if current_title is not None:
        body = lines[current_body_start:]
        raw_docs.append({
            "title": current_title,
            "source_url": _extract_source_url(body),
            "body_lines": body,
            "line_start": current_start,
            "line_end": len(lines),
        })

    # Merge consecutive docs with the same title (Platform duplicate-H1 fix).
    # Pattern: short doc (URL-only) followed by same-title doc (real content).
    docs: list[dict] = []
    i = 0
    while i < len(raw_docs):
        doc = raw_docs[i]
        # Check for merge candidate: same title, first doc is short (≤5 non-blank lines)
        if i + 1 < len(raw_docs) and raw_docs[i + 1]["title"] == doc["title"]:
            non_blank = sum(1 for l in doc["body_lines"] if l.strip())
            if non_blank <= 5:
                next_doc = raw_docs[i + 1]
                # Merge: keep source_url from first (has URL: line), body from second
                merged_url = doc["source_url"] or next_doc["source_url"]
                docs.append({
                    "title": doc["title"],
                    "source_url": merged_url,
                    "body_lines": next_doc["body_lines"],
                    "line_start": doc["line_start"],
                    "line_end": next_doc["line_end"],
                })
                i += 2
                continue
        docs.append(doc)
        i += 1

    return docs


# ---------------------------------------------------------------------------
# Section (heading) extraction — H2 and below
# ---------------------------------------------------------------------------

def extract_sections(body_lines: list[str]) -> list[dict]:
    """Extract Markdown headings (H2+) from *body_lines*.

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
                "line_end": -1,  # filled later
                "has_code_blocks": False,
            })

    # Fill line_end and has_code_blocks
    for i, h in enumerate(headings):
        next_start = headings[i + 1]["line_start"] if i + 1 < len(headings) else len(body_lines)
        h["line_end"] = next_start
        section_text = "\n".join(body_lines[h["line_start"]:next_start])
        h["has_code_blocks"] = "```" in section_text

    # Build heading_path (handle level skips)
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
    """Check if line is part of a Markdown table."""
    stripped = line.strip()
    return bool(stripped) and stripped.startswith("|") and stripped.endswith("|")


def extract_content(body_lines: list[str], heading_path: str | None = None) -> str:
    """Extract content from body_lines.

    If *heading_path* is None, return the entire body.
    Otherwise, find the section matching *heading_path* and return its content,
    extending to include any unclosed code fence or unfinished table.
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
        # Try case-insensitive partial match
        heading_lower = heading_path.lower()
        for s in sections:
            if heading_lower in s["heading_path"].lower() or heading_lower in s["title"].lower():
                target = s
                break

    if target is None:
        available = "\n".join(f"  - {s['heading_path']}" for s in sections)
        print(f"Error: heading '{heading_path}' not found.\n\nAvailable sections:\n{available}", file=sys.stderr)
        sys.exit(1)

    # Find the end: next heading at same or higher level
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
    if content_lines and _is_table_line(content_lines[-1]):
        i = end_line
        while i < len(body_lines) and _is_table_line(body_lines[i]):
            content_lines.append(body_lines[i])
            i += 1

    return "".join(content_lines)


# ---------------------------------------------------------------------------
# Fetch helper
# ---------------------------------------------------------------------------

def _fetch_url(url: str, cache_path: str) -> str:
    """Return path to cached file, fetching from *url* if necessary."""
    if os.path.exists(cache_path):
        return cache_path

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "claude-docs-researcher/1.0"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        with open(cache_path, "wb") as f:
            f.write(data)
        return cache_path
    except urllib.error.URLError as e:
        print(f"Error: Failed to fetch {url}: {e}", file=sys.stderr)
        sys.exit(1)


def load_lines(path: str) -> list[str]:
    """Read file and return lines (preserving newlines)."""
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


# ---------------------------------------------------------------------------
# llms.txt index parser (lightweight)
# ---------------------------------------------------------------------------

def parse_llms_index(lines: list[str]) -> list[dict]:
    """Parse llms.txt (lightweight index) into a list of page entries.

    Handles formats:
        - [Title](URL): Description   (Claude Code)
        - [Title](URL) - Description  (Platform)
        - [Title](URL)                (Platform, no description)

    Returns list of dicts: {"title": str, "url": str, "description": str}
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
# Source resolution helpers
# ---------------------------------------------------------------------------

def _get_source(args) -> dict:
    """Get source profile from args."""
    return SOURCES[args.source]


def _cache_path(cache_dir: str, filename: str) -> str:
    return cache_dir.rstrip("/") + "/" + filename


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _group_index_entries(entries: list[dict]) -> list[dict]:
    """Group entries with the same base title that differ only by parenthetical variant.

    Detects patterns like "Batches (Python)", "Batches (Go)", etc. and collapses
    consecutive same-base entries into a single group entry.

    Returns a list of display items:
        {"type": "single", "index": int, "entry": dict}
        {"type": "group", "base": str, "desc": str, "variants": [(index, variant_name), ...]}
    """
    _VARIANT_RE = re.compile(r"^(.+?)\s*\(([^)]+)\)$")

    items: list[dict] = []
    i = 0
    while i < len(entries):
        m = _VARIANT_RE.match(entries[i]["title"])
        if m:
            base = m.group(1).strip()
            variants = [(i, m.group(2))]
            j = i + 1
            while j < len(entries):
                m2 = _VARIANT_RE.match(entries[j]["title"])
                if m2 and m2.group(1).strip() == base:
                    variants.append((j, m2.group(2)))
                    j += 1
                else:
                    break
            if len(variants) > 1:
                items.append({
                    "type": "group",
                    "base": base,
                    "desc": entries[i].get("description", ""),
                    "variants": variants,
                })
                i = j
                continue
        items.append({"type": "single", "index": i, "entry": entries[i]})
        i += 1
    return items


def cmd_fetch_index(args):
    """Fetch lightweight llms.txt index and print page list with descriptions."""
    src = _get_source(args)
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    path = _fetch_url(src["index_url"], index_cache)
    lines = load_lines(path)
    entries = parse_llms_index(lines)

    full_cache = _cache_path(args.cache_dir, src["full_cache"])

    grouped = _group_index_entries(entries)

    print(f"{src['label']} Document Index (from llms.txt)")
    print("=" * 60)
    print()

    displayed = 0
    grouped_count = 0
    for item in grouped:
        if item["type"] == "single":
            i = item["index"]
            entry = item["entry"]
            print(f"[{i}] {entry['title']}")
            if entry["description"]:
                desc = entry["description"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print()
            displayed += 1
        else:
            variants = item["variants"]
            first_idx = variants[0][0]
            last_idx = variants[-1][0]
            variant_names = [v[1] for v in variants]
            print(f"[{first_idx}-{last_idx}] {item['base']}")
            if item["desc"]:
                desc = item["desc"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print(f"    Variants: {', '.join(variant_names)}")
            print()
            displayed += 1
            grouped_count += len(variants)

    print(f"({len(entries)} pages total, {displayed} entries shown — {grouped_count} pages grouped)")
    print()
    print(f"Next: parse-llms-txt.py sections {full_cache} <doc_index>")
    print(f"  (llms-full.txt will be fetched automatically on first use)")


def _resolve_source_from_path(file_path: str) -> dict | None:
    """Guess source profile from the cache file path."""
    basename = os.path.basename(file_path)
    for key, src in SOURCES.items():
        if src["full_cache"] in basename:
            return src
    return None


def _load_full_txt(file_arg: str) -> tuple[str, list[str]]:
    """Load llms-full.txt, auto-fetching if the file doesn't exist."""
    if not os.path.exists(file_arg):
        src = _resolve_source_from_path(file_arg)
        if src:
            cache_dir = os.path.dirname(file_arg) or "/tmp"
            file_arg = _fetch_url(src["full_url"], file_arg)
        else:
            print(f"Error: File not found: {file_arg}", file=sys.stderr)
            sys.exit(1)
    return file_arg, load_lines(file_arg)


def cmd_sections(args):
    """List sections within a specific page."""
    file_path, lines = _load_full_txt(args.file)
    docs = split_documents(lines)

    idx = args.doc_index
    if idx < 0 or idx >= len(docs):
        print(f"Error: doc_index {idx} out of range (0-{len(docs) - 1})", file=sys.stderr)
        sys.exit(1)

    doc = docs[idx]
    sections = extract_sections(doc["body_lines"])

    print(f'Sections in [{idx}] "{doc["title"]}"')
    if doc["source_url"]:
        print(f"  URL: {doc['source_url']}")
    print("=" * 60)

    for s in sections:
        indent = "  " * (s["level"] - 2)  # H2 = no indent, H3 = 2 spaces, etc.
        code_marker = " [code]" if s["has_code_blocks"] else ""
        print(f"{indent}[L{s['level']}] {s['title']}{code_marker}")

    print()
    print(f"({len(sections)} sections)")
    print()
    print(f"Next: parse-llms-txt.py content {args.file} {idx} \"<heading_path>\"")


def cmd_content(args):
    """Print content of a specific page or section."""
    file_path, lines = _load_full_txt(args.file)
    docs = split_documents(lines)

    idx = args.doc_index
    if idx < 0 or idx >= len(docs):
        print(f"Error: doc_index {idx} out of range (0-{len(docs) - 1})", file=sys.stderr)
        sys.exit(1)

    doc = docs[idx]
    content = extract_content(doc["body_lines"], args.heading_path)

    # Print metadata header
    print(f"# doc_title: {doc['title']}")
    if doc["source_url"]:
        print(f"# source: {doc['source_url']}")
    if args.heading_path:
        print(f"# heading_path: {args.heading_path}")
    print("---")
    print(content, end="")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Progressive loader for Claude documentation (llms-full.txt)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-index
    p_index = sub.add_parser("fetch-index", help="Fetch and print page index")
    p_index.add_argument(
        "--source", choices=list(SOURCES.keys()), default=DEFAULT_SOURCE,
        help=f"Documentation source (default: {DEFAULT_SOURCE})",
    )
    p_index.add_argument(
        "--cache-dir", default="/tmp",
        help="Directory to cache files (default: /tmp)",
    )
    p_index.set_defaults(func=cmd_fetch_index)

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a page")
    p_sections.add_argument("file", help="Path to llms-full.txt file")
    p_sections.add_argument("doc_index", type=int, help="Page index (from fetch-index)")
    p_sections.set_defaults(func=cmd_sections)

    # content
    p_content = sub.add_parser("content", help="Print page/section content")
    p_content.add_argument("file", help="Path to llms-full.txt file")
    p_content.add_argument("doc_index", type=int, help="Page index")
    p_content.add_argument("heading_path", nargs="?", default=None,
                           help="Heading path (omit for full page)")
    p_content.set_defaults(func=cmd_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
