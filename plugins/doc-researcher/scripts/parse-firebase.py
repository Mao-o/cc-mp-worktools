#!/usr/bin/env python3
"""Progressive loader for Firebase documentation (firebase.google.com/docs/llms.txt).

Firebase publishes a lightweight ``llms.txt`` index of per-page ``.md.txt``
documents. Unlike AI SDK or Claude docs, there is **no ``llms-full.txt``**, so
individual pages are fetched on demand and cached alongside the index.

Subcommands:

  fetch-index     — Fetch (if uncached) and print page index (paginated)
  search-index    — Rank pages by keyword (from llms.txt title/description)
  search-content  — Keyword search across one page or all pages (lazy fetch)
  search          — Smart search: rank pages + drill into top N bodies
  sections        — List sections within a specific page (auto-fetches the page)
  content         — Print content of a page or section (auto-fetches the page)

Page references (``<page_ref>``) accept three forms:

  - integer index    (e.g. ``42``)
  - URL slug         (e.g. ``firestore-vector-search``)
  - full URL         (e.g. ``https://firebase.google.com/docs/firestore/vector-search``)
"""

import argparse
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _common import (
    add_cache_dir_arg,
    add_heading_path_arg,
    die,
    die_index_out_of_range,
    extract_content,
    extract_sections,
    fetch_url,
    load_lines,
    next_hint,
    normalize_doc_url,
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
    # Avoid rstrip("/"): cache_dir="/" would become "" and os.path.join("", ...)
    # returns a relative path. os.path.join already handles trailing slashes
    # in cache_dir correctly. (Codex Review P3 feedback on PR #17.)
    return os.path.join(cache_dir, INDEX_CACHE_NAME)


def _pages_cache_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, PAGES_CACHE_SUBDIR)


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
# Page reference resolution (int / slug / full URL)
# ---------------------------------------------------------------------------

def _entry_url_for_match(url: str) -> str:
    """Return a comparable URL: normalised, with trailing ``.md.txt`` stripped.

    Firebase index entries point at ``.md.txt`` siblings of the human-facing
    page (``.../firestore/vector-search.md.txt``). When the user passes a
    full URL we normally get the human URL (``.../firestore/vector-search``),
    so strip the ``.md.txt`` suffix before comparing.
    """
    if not url:
        return ""
    u = url
    if u.endswith(".md.txt"):
        u = u[:-len(".md.txt")]
    return normalize_doc_url(u)


def _resolve_page_ref(entries: list[dict], page_ref: str) -> int:
    """Resolve a page reference to an entry index.

    Tries, in order:
      1. integer index into *entries*
      2. full URL (``http(s)://...``) matched against entry URL
      3. URL slug (last path component) matched against entry URL

    Exits with a helpful error when no candidate is found or when a slug is
    ambiguous (multiple entries end with the same last path component).
    """
    if page_ref is None or page_ref == "":
        die("page_ref required: integer index, URL slug, or full URL")

    try:
        idx = int(page_ref)
    except ValueError:
        pass
    else:
        if 0 <= idx < len(entries):
            return idx
        die_index_out_of_range(idx, len(entries))

    if page_ref.startswith("http://") or page_ref.startswith("https://"):
        # Use _entry_url_for_match on both sides so a URL copied verbatim from
        # search-index output (which retains ``.md.txt``) still matches a
        # human-facing page URL stored in the index. Without this, the two
        # paths are asymmetric: only the entry side strips ``.md.txt``, so
        # ``content --page-ref https://.../vector-search.md.txt`` would fail
        # even though that page exists. (Codex Review P2 feedback on PR #17.)
        target = _entry_url_for_match(page_ref)
        for i, entry in enumerate(entries):
            if _entry_url_for_match(entry.get("url", "")) == target:
                return i
        die(f"No page found for URL: {page_ref}")

    slug_pattern = re.compile(rf"/{re.escape(page_ref)}/?$")
    candidates: list[tuple[int, str]] = []
    for i, entry in enumerate(entries):
        url = entry.get("url", "")
        if slug_pattern.search(_entry_url_for_match(url)):
            candidates.append((i, url))
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        detail = "\n  ".join(f"[{i}] {url}" for i, url in candidates)
        die(f"Ambiguous slug '{page_ref}'. Matches:\n  {detail}")
    die(f"No page found for slug: {page_ref}")


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
    next_hint("sections", "<page_ref>")


def cmd_sections(args):
    entries = _load_index(args.cache_dir)
    idx = _resolve_page_ref(entries, args.page_ref)

    entry = entries[idx]
    page_path = _fetch_page(entry["url"], args.cache_dir)
    lines = load_lines(page_path)
    sections = extract_sections(lines)

    print(f'Sections in [{idx}] "{entry["title"]}"')
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
    next_hint("content", str(idx), '"<heading_path>"')


def cmd_content(args):
    entries = _load_index(args.cache_dir)
    idx = _resolve_page_ref(entries, args.page_ref)

    entry = entries[idx]
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
        print("Tip: try broader keywords or 'search' to drill into bodies")
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
    next_hint("search", '"<query>"')


def cmd_search_content(args):
    """Keyword search across a single page or all pages (lazy fetch).

    Firebase has no ``llms-full.txt``, so each page is fetched on demand.
    Pass ``--page-ref`` to restrict to a single page (recommended — fetching
    every page is expensive). When omitted, searches across the entire index
    (warns if N is large).
    """
    entries = _load_index(args.cache_dir)

    if not args.query.strip():
        die("query must not be empty")

    if args.page_ref is not None:
        target_indexes = [_resolve_page_ref(entries, args.page_ref)]
    else:
        if len(entries) > 30:
            print(
                f"WARNING: --page-ref not given. About to fetch and search all "
                f"{len(entries)} pages on demand (may be slow on first run; "
                f"subsequent runs use cache). Use --page-ref to restrict.",
                file=sys.stderr,
            )
        target_indexes = list(range(len(entries)))

    print(f'Search-content results for "{args.query}" (Firebase, {len(target_indexes)} pages)')
    print("=" * 60)
    print()

    total_hits = 0
    docs_matched = 0
    printed_docs = 0

    for idx in target_indexes:
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
        mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
        print(f"    ({hits['total_matches']} hits in this page, showing {shown}){mode_note}")
        for r in hits["results"]:
            kw_info = f"  keywords: {', '.join(r['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
            print(f"    Section: {r['heading_path']}  (x{r['hit_count']}){kw_info}")
            for snippet_line in r["snippet"].splitlines():
                print(f"      {snippet_line}")
            print()
        print()

    if total_hits == 0:
        print("No matching content found in the targeted pages.")
        print()
        print("Tip: use 'search-index' first, then pass the top doc_index to --page-ref")
    else:
        print(f"({total_hits} hits across {docs_matched}/{len(target_indexes)} pages, showing top {printed_docs})")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"')


def cmd_search(args):
    """Smart search: rank pages via llms.txt and drill into top N bodies.

    Phase 1: search-index for top *--top-n* pages by title/description.
    Phase 2: on-demand fetch each candidate page.
    Phase 3: search each body with section-level AND keyword match.
    Phase 4: rank by (body hit count desc, index score desc, doc_idx asc).

    Default ``--top-n`` is 5 to balance coverage with on-demand fetch cost
    (Firebase has no llms-full.txt; each page is a separate HTTP fetch).
    Cache hits keep repeat runs cheap.
    """
    entries = _load_index(args.cache_dir)

    if not args.query.strip():
        die("query must not be empty")

    scored_entries = search_index_entries(entries, args.query, limit=args.top_n)

    print(f'Search results for "{args.query}" (Firebase)')
    print(f"  (index: {_index_cache_path(args.cache_dir)})")
    print(f"  (top-{args.top_n} candidate pages fetched on demand)")
    print("=" * 60)
    print()

    if not scored_entries:
        print("No matching pages found.")
        print()
        print("Tip: try broader keywords")
        return

    results = []
    for score, idx, entry in scored_entries:
        page_path = _fetch_page(entry["url"], args.cache_dir)
        lines = load_lines(page_path)
        body_hits = search_content_in_body(
            lines, args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=2,
            max_snippet_chars=args.max_snippet_chars,
        )
        results.append({
            "doc_idx": idx,
            "title": entry["title"],
            "url": entry["url"],
            "index_score": score,
            "body_hits": body_hits,
        })

    results.sort(key=lambda r: (
        -r["body_hits"]["total_matches"],
        -r["index_score"],
        r["doc_idx"],
    ))

    for r in results:
        hits = r["body_hits"]
        shown = len(hits["results"])
        print(f"[{r['doc_idx']}] {r['title']} (index_score: {r['index_score']})")
        print(f"    URL: {r['url']}")
        if hits["total_matches"]:
            mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
            print(f"    ({hits['total_matches']} body hits, showing {shown}){mode_note}")
            for s in hits["results"]:
                kw_info = f"  keywords: {', '.join(s['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
                print(f"    Section: {s['heading_path']}  (x{s['hit_count']}){kw_info}")
                for snippet_line in s["snippet"].splitlines():
                    print(f"      {snippet_line}")
                print()
        else:
            print(f"    (no body hits — index match only)")
        print()

    print(f"({len(results)} pages, ranked via index → body fetch)")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_page_ref_arg(parser, *, help: str) -> None:
    parser.add_argument("page_ref", help=help)


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
    _add_page_ref_arg(
        p_sections,
        help="Page reference: integer index, URL slug (e.g. 'vector-search'), or full URL",
    )
    add_cache_dir_arg(p_sections, default=DEFAULT_CACHE_DIR)
    p_sections.set_defaults(func=cmd_sections)

    p_content = sub.add_parser("content", help="Print page/section content")
    _add_page_ref_arg(
        p_content,
        help="Page reference: integer index, URL slug, or full URL",
    )
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

    # search-content (lazy fetch; --page-ref optional)
    p_search_body = sub.add_parser(
        "search-content",
        help="Keyword search across one page or all pages (lazy fetch)",
    )
    add_cache_dir_arg(p_search_body, default=DEFAULT_CACHE_DIR)
    p_search_body.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_body.add_argument(
        "--page-ref", default=None,
        help="Restrict search to a single page (int / URL slug / URL). "
             "Omit to search all pages (slow, fetches every page on first run).",
    )
    p_search_body.add_argument("--limit", type=int, default=10,
                               help="Max pages to display (default: 10)")
    p_search_body.add_argument("--context", type=int, default=2,
                               help="Context lines around each hit (default: 2)")
    p_search_body.add_argument("--max-hits", type=int, default=5,
                               help="Max hits to display per page (default: 5)")
    p_search_body.set_defaults(func=cmd_search_content)

    # search (smart: index rank + on-demand body drill-in)
    p_search = sub.add_parser(
        "search",
        help="Smart search: rank pages via index and drill into top N bodies",
    )
    p_search.add_argument("query", help="Space-separated keywords (AND search)")
    add_cache_dir_arg(p_search, default=DEFAULT_CACHE_DIR)
    p_search.add_argument(
        "--top-n", type=int, default=5,
        help="Number of top candidate pages to fetch and search (default: 5)",
    )
    p_search.add_argument("--max-hits", type=int, default=3,
                          help="Max body hits per page (default: 3)")
    p_search.add_argument("--context", type=int, default=2,
                          help="Context lines around each hit (default: 2)")
    p_search.add_argument(
        "--max-snippet-chars", type=int, default=500,
        help="Truncate each snippet to N chars (0 = no limit, default: 500)",
    )
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
