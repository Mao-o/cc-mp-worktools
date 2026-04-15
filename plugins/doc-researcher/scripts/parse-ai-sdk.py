#!/usr/bin/env python3
"""Progressive loader for ai-sdk.dev/llms.txt.

Parses the concatenated frontmatter-delimited Markdown documents in llms.txt
and provides subcommands for progressive (layered) access:

  fetch-index  — Fetch (if uncached) and print document index
  sections     — List sections (headings) within a specific document
  content      — Print content of a specific document or section
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
    die,
    die_index_out_of_range,
    extract_content,
    extract_sections,
    fetch_url,
    load_lines,
    next_hint,
    print_metadata_header,
)

LLMS_TXT_URL = "https://ai-sdk.dev/llms.txt"
DEFAULT_CACHE_PATH = "/tmp/ai-sdk-llms.txt"


# ---------------------------------------------------------------------------
# Document splitting (frontmatter boundaries)
# ---------------------------------------------------------------------------

def _is_frontmatter_delimiter(line: str) -> bool:
    """Return True if *line* is a YAML frontmatter delimiter (``---``)."""
    return line.rstrip("\n\r") == "---"


def split_documents(lines: list[str]) -> list[dict]:
    """Split *lines* into documents delimited by frontmatter boundaries.

    Returns a list of dicts:
        {
            "frontmatter_lines": [...],
            "body_lines": [...],
            "line_start": int,   # 0-based index into *lines*
            "line_end": int,
        }
    """
    docs: list[dict] = []
    fence = FenceTracker()
    i = 0
    n = len(lines)

    while i < n:
        fence_snapshot = fence.in_fence
        fence.update(lines[i])

        # Look for opening ``---`` outside a code fence
        if not fence_snapshot and _is_frontmatter_delimiter(lines[i]):
            fm_start = i
            i += 1
            # Collect frontmatter lines until closing ``---``
            fm_lines: list[str] = []
            while i < n:
                if _is_frontmatter_delimiter(lines[i]):
                    i += 1  # skip closing ---
                    break
                fm_lines.append(lines[i])
                i += 1

            # Collect body lines until next frontmatter opening or EOF
            body_start = i
            while i < n:
                fence.update(lines[i])
                if not fence.in_fence and _is_frontmatter_delimiter(lines[i]):
                    if _looks_like_frontmatter_start(lines, i):
                        break
                i += 1

            docs.append({
                "frontmatter_lines": fm_lines,
                "body_lines": lines[body_start:i],
                "line_start": fm_start,
                "line_end": i,
            })
        else:
            i += 1

    return docs


def _looks_like_frontmatter_start(lines: list[str], pos: int) -> bool:
    """Heuristic: does ``---`` at *pos* start a frontmatter block?

    We check if there's a closing ``---`` within the next 30 lines that
    contains at least one ``key: value`` pattern between them.
    """
    if not _is_frontmatter_delimiter(lines[pos]):
        return False
    n = len(lines)
    limit = min(pos + 30, n)
    has_kv = False
    for j in range(pos + 1, limit):
        if _is_frontmatter_delimiter(lines[j]):
            return has_kv
        if re.match(r"^[a-zA-Z_][\w-]*\s*:", lines[j]):
            has_kv = True
    return False


# ---------------------------------------------------------------------------
# Frontmatter field extraction
# ---------------------------------------------------------------------------

def parse_frontmatter(fm_lines: list[str]) -> dict:
    """Extract title, description, and tags from frontmatter lines."""
    result: dict = {"title": "", "description": "", "tags": []}
    current_key = None
    current_value_lines: list[str] = []

    def _flush():
        nonlocal current_key, current_value_lines
        if current_key and current_value_lines:
            value = " ".join(current_value_lines).strip()
            if current_key == "tags":
                # Parse [tag1, tag2, ...] or bare comma-separated
                m = re.match(r"\[(.+)\]", value)
                inner = m.group(1) if m else value
                result["tags"] = [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
            else:
                result[current_key] = value.strip("'\"")
        current_key = None
        current_value_lines = []

    for line in fm_lines:
        m = re.match(r"^(title|description|tags)\s*:\s*(.*)", line)
        if m:
            _flush()
            current_key = m.group(1)
            rest = m.group(2).strip()
            if rest and rest != "|" and rest != ">":
                current_value_lines.append(rest)
        elif current_key and line.startswith("  "):
            # Continuation of multi-line value
            current_value_lines.append(line.strip())
    _flush()
    return result


# ---------------------------------------------------------------------------
# Search scoring
# ---------------------------------------------------------------------------

def _extract_h1_h2_titles(body_lines: list[str]) -> list[str]:
    """Extract H1/H2 heading titles from body_lines (lightweight, no full parse)."""
    fence = FenceTracker()
    titles: list[str] = []
    for line in body_lines:
        was_in_fence = fence.in_fence
        fence.update(line)
        if was_in_fence or fence.in_fence:
            continue
        m = re.match(r"^(#{1,2})\s+(.+)", line)
        if m:
            titles.append(m.group(2).strip())
    return titles


def score_document(fm: dict, heading_titles: list[str], keywords: list[str]) -> int:
    """Score a document against search keywords.

    Returns total score (0 means no match).
    """
    title_lower = (fm["title"] or "").lower()
    desc_lower = (fm["description"] or "").lower()
    tags_lower = [t.lower() for t in fm["tags"]]
    headings_lower = [h.lower() for h in heading_titles]

    total = 0
    matched_keywords = 0

    for kw in keywords:
        kw_lower = kw.lower()
        kw_score = 0

        # Title matching
        if kw_lower == title_lower:
            kw_score += 10
        elif kw_lower in title_lower:
            kw_score += 5

        # Tag matching
        if any(kw_lower == t or kw_lower in t for t in tags_lower):
            kw_score += 4

        # Description matching
        if kw_lower in desc_lower:
            kw_score += 2

        # Heading matching
        if any(kw_lower in h for h in headings_lower):
            kw_score += 1

        if kw_score > 0:
            matched_keywords += 1
        total += kw_score

    # Bonus for matching all keywords
    if len(keywords) > 1 and matched_keywords == len(keywords):
        total += 10

    return total


# ---------------------------------------------------------------------------
# Fetch helper (AI SDK uses a 60s timeout for the compact llms.txt index)
# ---------------------------------------------------------------------------

def fetch_llms_txt(cache_path: str) -> str:
    """Return path to cached llms.txt, fetching if necessary."""
    return fetch_url(
        LLMS_TXT_URL,
        cache_path,
        user_agent="claude-code-ai-sdk-researcher/1.0",
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_fetch_index(args):
    """Fetch (if needed) and print document index."""
    cache_path = args.cache_dir.rstrip("/") + "/ai-sdk-llms.txt"
    path = fetch_llms_txt(cache_path)
    lines = load_lines(path)
    docs = split_documents(lines)

    print(f"AI SDK llms.txt Document Index (file: {cache_path})")
    print("=" * 60)
    print()

    if args.compact:
        # Compact mode: titles only, multiple per line
        entries = []
        for i, doc in enumerate(docs):
            fm = parse_frontmatter(doc["frontmatter_lines"])
            title = fm["title"] or "(untitled)"
            entries.append(f"[{i}] {title}")
        # Print entries, ~2 per line (max 80 chars)
        line_buf = ""
        for entry in entries:
            if line_buf and len(line_buf) + len(entry) + 2 > 80:
                print(line_buf)
                line_buf = entry
            else:
                line_buf = f"{line_buf}  {entry}" if line_buf else entry
        if line_buf:
            print(line_buf)
    else:
        for i, doc in enumerate(docs):
            fm = parse_frontmatter(doc["frontmatter_lines"])
            title = fm["title"] or "(untitled)"
            desc = fm["description"] or ""
            tags = ", ".join(fm["tags"]) if fm["tags"] else ""

            print(f"[{i}] {title}")
            if desc:
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            if tags:
                print(f"    tags: {tags}")
            print()

    print()
    print(f"({len(docs)} documents total)")
    print()
    print(f"Tip: use 'search' to find specific documents by keyword")
    next_hint("sections", cache_path, "<doc_index>")


def cmd_sections(args):
    """List sections within a specific document."""
    lines = load_lines(args.file)
    docs = split_documents(lines)

    idx = args.doc_index
    if idx < 0 or idx >= len(docs):
        die_index_out_of_range(idx, len(docs))

    doc = docs[idx]
    fm = parse_frontmatter(doc["frontmatter_lines"])
    title = fm["title"] or "(untitled)"
    sections = extract_sections(doc["body_lines"], min_level=1)

    print(f'Sections in [{idx}] "{title}"')
    print("=" * 60)

    for s in sections:
        indent = "  " * (s["level"] - 1)
        code_marker = " [code]" if s["has_code_blocks"] else ""
        print(f"{indent}[L{s['level']}] {s['title']}{code_marker}")

    print()
    print(f"({len(sections)} sections)")
    print()
    next_hint("content", args.file, str(idx), '"<heading_path>"')


def cmd_content(args):
    """Print content of a specific document or section."""
    lines = load_lines(args.file)
    docs = split_documents(lines)

    idx = args.doc_index
    if idx < 0 or idx >= len(docs):
        die_index_out_of_range(idx, len(docs))

    doc = docs[idx]
    fm = parse_frontmatter(doc["frontmatter_lines"])

    content = extract_content(doc["body_lines"], args.heading_path,
                              protect_tables=False, min_level=1)

    print_metadata_header(
        fm["title"] or "(untitled)",
        tags=fm["tags"] or None,
        heading_path=args.heading_path,
    )
    print(content, end="")


def cmd_search(args):
    """Search documents by keyword."""
    file_path = args.file
    # Auto-fetch if file doesn't exist
    if not os.path.exists(file_path):
        file_path = fetch_llms_txt(file_path)

    lines = load_lines(file_path)
    docs = split_documents(lines)
    keywords = args.query.split()

    if not keywords:
        die("query must not be empty")

    # Score all documents
    scored: list[tuple[int, int, dict]] = []  # (score, index, fm)
    doc_headings: dict[int, list[str]] = {}
    for i, doc in enumerate(docs):
        fm = parse_frontmatter(doc["frontmatter_lines"])
        h_titles = _extract_h1_h2_titles(doc["body_lines"])
        doc_headings[i] = h_titles
        s = score_document(fm, h_titles, keywords)
        if s > 0:
            scored.append((s, i, fm))

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])
    scored = scored[:args.limit]

    print(f'Search results for "{args.query}" (file: {file_path})')
    print("=" * 60)
    print()

    if not scored:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords or use fetch-index --compact to browse all documents")
    else:
        for s, idx, fm in scored:
            title = fm["title"] or "(untitled)"
            desc = fm["description"] or ""
            tags = ", ".join(fm["tags"]) if fm["tags"] else ""

            print(f"[{idx}] {title} (score: {s})")
            if desc:
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            if tags:
                print(f"    tags: {tags}")
            if args.show_sections:
                for h in doc_headings.get(idx, []):
                    print(f"      - {h}")
            print()

    print(f"({len(scored)} results, {len(docs)} documents searched)")
    print()
    next_hint("sections", file_path, "<doc_index>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Progressive loader for ai-sdk.dev/llms.txt",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-index
    p_index = sub.add_parser("fetch-index", help="Fetch and print document index")
    add_cache_dir_arg(p_index, help="Directory to cache llms.txt (default: /tmp)")
    p_index.add_argument(
        "--compact", action="store_true",
        help="Print titles only in compact format",
    )
    p_index.set_defaults(func=cmd_fetch_index)

    # search
    p_search = sub.add_parser("search", help="Search documents by keyword")
    p_search.add_argument("file", help="Path to llms.txt file (auto-fetched if missing)")
    p_search.add_argument("query", help="Space-separated keywords (AND search)")
    p_search.add_argument("--limit", type=int, default=15,
                          help="Max results to show (default: 15)")
    p_search.add_argument("--show-sections", action="store_true",
                          help="Show H1/H2 headings for each result")
    p_search.set_defaults(func=cmd_search)

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a document")
    p_sections.add_argument("file", help="Path to llms.txt file")
    add_doc_index_arg(p_sections)
    p_sections.set_defaults(func=cmd_sections)

    # content
    p_content = sub.add_parser("content", help="Print document/section content")
    p_content.add_argument("file", help="Path to llms.txt file")
    add_doc_index_arg(p_content, help="Document index")
    add_heading_path_arg(p_content)
    p_content.set_defaults(func=cmd_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
