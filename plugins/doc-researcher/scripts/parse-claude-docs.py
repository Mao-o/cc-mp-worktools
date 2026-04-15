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

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _common import (
    FenceTracker,
    add_cache_dir_arg,
    add_doc_index_arg,
    add_heading_path_arg,
    die_index_out_of_range,
    extract_content,
    extract_sections,
    fetch_url,
    load_lines,
    next_hint,
    parse_llms_index,
    print_metadata_header,
)

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

USER_AGENT = "claude-docs-researcher/1.0"


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
    path = fetch_url(src["index_url"], index_cache, user_agent=USER_AGENT)
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
    next_hint("sections", full_cache, "<doc_index>")
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
            file_arg = fetch_url(src["full_url"], file_arg, user_agent=USER_AGENT)
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
        die_index_out_of_range(idx, len(docs))

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
    next_hint("content", args.file, str(idx), '"<heading_path>"')


def cmd_content(args):
    """Print content of a specific page or section."""
    file_path, lines = _load_full_txt(args.file)
    docs = split_documents(lines)

    idx = args.doc_index
    if idx < 0 or idx >= len(docs):
        die_index_out_of_range(idx, len(docs))

    doc = docs[idx]
    content = extract_content(doc["body_lines"], args.heading_path)

    print_metadata_header(
        doc["title"],
        source=doc["source_url"] or None,
        heading_path=args.heading_path,
    )
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
    add_cache_dir_arg(p_index)
    p_index.set_defaults(func=cmd_fetch_index)

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a page")
    p_sections.add_argument("file", help="Path to llms-full.txt file")
    add_doc_index_arg(p_sections, help="Page index (from fetch-index)")
    p_sections.set_defaults(func=cmd_sections)

    # content
    p_content = sub.add_parser("content", help="Print page/section content")
    p_content.add_argument("file", help="Path to llms-full.txt file")
    add_doc_index_arg(p_content, help="Page index")
    add_heading_path_arg(p_content, help="Heading path (omit for full page)")
    p_content.set_defaults(func=cmd_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
