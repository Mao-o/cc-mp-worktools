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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (
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
    parse_llms_index,
    print_metadata_header,
)

LLMS_TXT_URL = "https://firebase.google.com/docs/llms.txt"
DEFAULT_CACHE_DIR = "/tmp"
INDEX_CACHE_NAME = "firebase-llms.txt"
PAGES_CACHE_SUBDIR = "firebase-docs"

USER_AGENT = "claude-code-firebase-researcher/1.0"


# ---------------------------------------------------------------------------
# URL → cache filename
# ---------------------------------------------------------------------------

_URL_PREFIX = "https://firebase.google.com/"


def _url_to_cache_filename(url: str) -> str:
    """Convert a Firebase ``.md.txt`` URL to a cache filename.

    Strips the firebase.google.com host, replaces ``/`` with ``_`` for
    legibility, and appends a short sha1 hash suffix to guarantee uniqueness.
    Without the hash, distinct URLs that differ only by ``/`` vs ``_``
    (e.g. ``.../auth/user.md.txt`` vs ``.../auth_user.md.txt``) would collapse
    to the same filename and silently share a cache file. Note: the index page
    URL is ``firebase.google.com/docs.md.txt`` (a sibling of ``/docs/...``),
    not ``/docs/index.md.txt``, so the prefix must be the host root.
    """
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    if url.startswith(_URL_PREFIX):
        path = url[len(_URL_PREFIX):]
        if path:
            base = path[:-len(".md.txt")] if path.endswith(".md.txt") else path
            safe = base.replace("/", "_")
            return f"{safe[:180]}-{h}.md.txt"
    return f"{h}.md.txt"


# ---------------------------------------------------------------------------
# Cache path + index loading helpers
# ---------------------------------------------------------------------------

def _index_cache_path(cache_dir: str) -> str:
    return os.path.join(cache_dir.rstrip("/"), INDEX_CACHE_NAME)


def _pages_cache_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir.rstrip("/"), PAGES_CACHE_SUBDIR)


def _load_index(cache_dir: str) -> list[dict]:
    """Fetch (if needed) the llms.txt index and return parsed entries."""
    idx_path = fetch_url(LLMS_TXT_URL, _index_cache_path(cache_dir), user_agent=USER_AGENT)
    entries = parse_llms_index(load_lines(idx_path))
    if not entries:
        die(f"no entries parsed from {idx_path}. Format may have changed.")
    return entries


def _fetch_page(url: str, cache_dir: str) -> str:
    """Fetch an individual page ``.md.txt`` on demand and return its cache path."""
    filename = _url_to_cache_filename(url)
    page_path = os.path.join(_pages_cache_dir(cache_dir), filename)
    return fetch_url(url, page_path, user_agent=USER_AGENT)


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
    next_hint("sections", "<doc_index>")


def cmd_sections(args):
    entries = _load_index(args.cache_dir)
    if args.doc_index < 0 or args.doc_index >= len(entries):
        die_index_out_of_range(args.doc_index, len(entries))

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
    next_hint("content", str(args.doc_index), '"<heading_path>"')


def cmd_content(args):
    entries = _load_index(args.cache_dir)
    if args.doc_index < 0 or args.doc_index >= len(entries):
        die_index_out_of_range(args.doc_index, len(entries))

    entry = entries[args.doc_index]
    page_path = _fetch_page(entry["url"], args.cache_dir)
    lines = load_lines(page_path)

    content = extract_content(lines, args.heading_path)

    print_metadata_header(
        entry["title"],
        source=entry["url"],
        heading_path=args.heading_path,
    )
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
    add_cache_dir_arg(p_index, default=DEFAULT_CACHE_DIR)
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
    add_doc_index_arg(p_sections, help="Page index (from fetch-index)")
    add_cache_dir_arg(p_sections, default=DEFAULT_CACHE_DIR)
    p_sections.set_defaults(func=cmd_sections)

    p_content = sub.add_parser("content", help="Print page/section content")
    add_doc_index_arg(p_content, help="Page index")
    add_heading_path_arg(p_content, help="Heading path (omit for full page)")
    add_cache_dir_arg(p_content, default=DEFAULT_CACHE_DIR)
    p_content.set_defaults(func=cmd_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
