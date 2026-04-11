#!/usr/bin/env python3
"""Progressive loader for Firebase documentation (firebase.google.com/docs/llms.txt).

Firebase publishes a lightweight ``llms.txt`` index of per-page ``.md.txt``
documents. Unlike AI SDK or Claude docs, there is **no ``llms-full.txt``**, so
individual pages are fetched on demand and cached alongside the index.

Subcommands:

  fetch-index     — Fetch (if uncached) and print page index (paginated)
  search-index    — Rank pages by keyword (from llms.txt title/description)
  search-content  — Keyword search across explicitly specified pages (lazy fetch)
  sections        — List sections within a specific page (auto-fetches the page)
  content         — Print content of a page or section (auto-fetches the page)
"""

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

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
    search_content_in_body,
    search_index_entries,
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


def cmd_search_index(args):
    """Rank pages by keyword against llms.txt title/description."""
    entries = _load_index(args.cache_dir)

    if not args.query.strip():
        die("query must not be empty")

    scored = search_index_entries(entries, args.query, limit=args.limit)
    idx_path = _index_cache_path(args.cache_dir)

    print(f'Search-index results for "{args.query}" (Firebase)')
    print(f"  (index: {idx_path})")
    print("=" * 60)
    print()

    if not scored:
        print("No matching pages found.")
        print()
        print("Tip: try broader keywords or 'search-content --pages <idx,idx,...>' for body matches")
    else:
        for score, idx, entry in scored:
            print(f"[{idx}] {entry['title']} (score: {score})")
            if entry["description"]:
                desc = entry["description"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print(f"    URL: {entry['url']}")
            print()

    print(f"({len(scored)} results, {len(entries)} pages searched)")
    print()
    next_hint("search-content", '"<query>"', "--pages", "<idx,idx,...>")


def cmd_search_content(args):
    """Keyword search across explicitly specified page bodies (lazy fetch).

    Firebase has no ``llms-full.txt``, so the caller must list target pages via
    ``--pages 1,5,12`` (obtained from fetch-index or search-index). Unfetched
    pages are downloaded on demand; already-cached pages are re-used.
    """
    entries = _load_index(args.cache_dir)

    if not args.query.strip():
        die("query must not be empty")

    pages_str = args.pages.strip()
    if not pages_str:
        die("--pages must not be empty (provide comma-separated doc_indexes)")
    try:
        page_indexes = [int(p.strip()) for p in pages_str.split(",") if p.strip()]
    except ValueError as e:
        die(f"invalid --pages format: {e}")

    for idx in page_indexes:
        if idx < 0 or idx >= len(entries):
            die_index_out_of_range(idx, len(entries))

    print(f'Search-content results for "{args.query}" (Firebase, {len(page_indexes)} pages)')
    print("=" * 60)
    print()

    total_hits = 0
    docs_matched = 0
    printed_docs = 0

    for idx in page_indexes:
        entry = entries[idx]
        page_path = _fetch_page(entry["url"], args.cache_dir)
        lines = load_lines(page_path)

        hits = search_content_in_body(
            lines, args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=2,
        )

        if hits["total_matches"] == 0:
            continue

        total_hits += hits["total_matches"]
        docs_matched += 1

        if printed_docs >= args.limit:
            continue
        printed_docs += 1

        shown = len(hits["results"])
        print(f"[{idx}] {entry['title']}")
        print(f"    URL: {entry['url']}")
        print(f"    ({hits['total_matches']} hits in this page, showing {shown})")
        for r in hits["results"]:
            print(f"    Section: {r['heading_path']}  (x{r['hit_count']})")
            for snippet_line in r["snippet"].splitlines():
                print(f"      {snippet_line}")
            print()
        print()

    if total_hits == 0:
        print("No matching content found in the specified pages.")
        print()
        print("Tip: use 'search-index' first, then pass the top doc_indexes to --pages")
    else:
        print(f"({total_hits} hits across {docs_matched}/{len(page_indexes)} pages, showing top {printed_docs})")
    print()
    next_hint("content", "<doc_index>", '"<heading_path>"')


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

    # search-index
    p_search_idx = sub.add_parser(
        "search-index",
        help="Rank pages by keyword (from llms.txt title/description)",
    )
    add_cache_dir_arg(p_search_idx, default=DEFAULT_CACHE_DIR)
    p_search_idx.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_idx.add_argument("--limit", type=int, default=15,
                              help="Max results to show (default: 15)")
    p_search_idx.set_defaults(func=cmd_search_index)

    # search-content (lazy fetch of --pages only, since llms-full.txt does not exist)
    p_search_body = sub.add_parser(
        "search-content",
        help="Keyword search across explicitly specified pages (lazy fetch)",
    )
    add_cache_dir_arg(p_search_body, default=DEFAULT_CACHE_DIR)
    p_search_body.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_body.add_argument("--pages", required=True,
                               help="Comma-separated page indexes to search (e.g., '1,5,12')")
    p_search_body.add_argument("--limit", type=int, default=10,
                               help="Max pages to display (default: 10)")
    p_search_body.add_argument("--context", type=int, default=2,
                               help="Context lines around each hit (default: 2)")
    p_search_body.add_argument("--max-hits", type=int, default=5,
                               help="Max hits to display per page (default: 5)")
    p_search_body.set_defaults(func=cmd_search_content)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
