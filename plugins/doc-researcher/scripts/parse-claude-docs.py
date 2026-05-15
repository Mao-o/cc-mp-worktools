#!/usr/bin/env python3
"""Progressive loader for Claude documentation (llms-full.txt).

Supports two documentation sources:
  - code     : code.claude.com/docs      (Claude Code)
  - platform : platform.claude.com/docs  (Claude Developer Platform)

Parses the concatenated H1-delimited Markdown pages and provides
subcommands for progressive (layered) access:

  fetch-index     — Fetch (if uncached) and print page index (from llms.txt)
  search-index    — Rank pages by keyword against llms.txt title/description
  search-content  — Keyword search across llms-full.txt bodies with snippets
  sections        — List sections (headings) within a specific page
  content         — Print content of a specific page or section
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _common import (
    FenceTracker,
    add_cache_dir_arg,
    add_heading_path_arg,
    build_url_to_full_index,
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

# Pages whose body text reliably overwhelms keyword searches (release notes,
# changelogs) get pushed below higher-signal pages in search results.
DEPRIORITIZE_PAGE_PATTERNS = ("changelog", "release-notes", "release notes")


def _is_low_priority(title: str) -> bool:
    t = (title or "").lower()
    return any(p in t for p in DEPRIORITIZE_PAGE_PATTERNS)


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
    path = fetch_url(src["index_url"], index_cache, user_agent=USER_AGENT,
                     max_age=args.max_age)
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


def _load_full_txt(file_arg: str | None, source_key: str, cache_dir: str,
                   *, max_age: int | None = None) -> tuple[str, list[str]]:
    """Load llms-full.txt, auto-fetching when missing or stale.

    *file_arg* is the explicit ``--file`` path; when ``None`` it is derived
    from *source_key* under *cache_dir*.
    """
    src = SOURCES[source_key]
    if file_arg is None:
        file_arg = _cache_path(cache_dir, src["full_cache"])
    file_arg = fetch_url(src["full_url"], file_arg, user_agent=USER_AGENT,
                         max_age=max_age)
    return file_arg, load_lines(file_arg)


def _resolve_page_ref(docs: list[dict], page_ref: str) -> int:
    """Resolve a page reference to a doc index.

    Tries, in order:
      1. integer index into *docs*
      2. full URL (``http(s)://...``) matched against ``source_url``
      3. URL slug (last path component) matched against ``source_url``

    Exits with a helpful error when no candidate is found or when a slug is
    ambiguous (multiple docs end with the same last path component).
    """
    if page_ref is None or page_ref == "":
        die("page_ref required: integer index, URL slug, or full URL")

    try:
        idx = int(page_ref)
    except ValueError:
        pass
    else:
        if 0 <= idx < len(docs):
            return idx
        die_index_out_of_range(idx, len(docs))

    if page_ref.startswith("http://") or page_ref.startswith("https://"):
        target = normalize_doc_url(page_ref)
        for i, doc in enumerate(docs):
            if normalize_doc_url(doc.get("source_url", "")) == target:
                return i
        die(f"No page found for URL: {page_ref}")

    slug_pattern = re.compile(rf"/{re.escape(page_ref)}/?$")
    candidates: list[tuple[int, str]] = []
    for i, doc in enumerate(docs):
        url = doc.get("source_url", "")
        if slug_pattern.search(normalize_doc_url(url)):
            candidates.append((i, url))
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        detail = "\n  ".join(f"[{i}] {url}" for i, url in candidates)
        die(f"Ambiguous slug '{page_ref}'. Matches:\n  {detail}")
    die(f"No page found for slug: {page_ref}")


def cmd_sections(args):
    """List sections within a specific page."""
    file_path, lines = _load_full_txt(args.file, args.source, args.cache_dir,
                                      max_age=args.max_age)
    docs = split_documents(lines)
    idx = _resolve_page_ref(docs, args.page_ref)

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
    next_hint("content", str(idx), '"<heading_path>"')


def cmd_content(args):
    """Print content of a specific page or section."""
    file_path, lines = _load_full_txt(args.file, args.source, args.cache_dir,
                                      max_age=args.max_age)
    docs = split_documents(lines)
    idx = _resolve_page_ref(docs, args.page_ref)

    doc = docs[idx]
    content = extract_content(doc["body_lines"], args.heading_path)

    print_metadata_header(
        doc["title"],
        source=doc["source_url"] or None,
        heading_path=args.heading_path,
    )
    print(content, end="")


def cmd_search_index(args):
    """Rank pages by keyword against llms.txt title/description."""
    src = _get_source(args)
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    path = fetch_url(src["index_url"], index_cache, user_agent=USER_AGENT,
                     max_age=args.max_age)
    entries = parse_llms_index(load_lines(path))

    if not args.query.strip():
        die("query must not be empty")

    scored = search_index_entries(entries, args.query, limit=args.limit)

    print(f'Search-index results for "{args.query}" ({src["label"]})')
    print(f"  (index: {path})")
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
    print("Note: llms.txt and llms-full.txt may use different doc_index numbering.")
    print("      Prefer 'search' (URL-joined) for a stable doc_idx into content/sections.")
    next_hint("search", '"<query>"')


def cmd_search_content(args):
    """Keyword search across llms-full.txt bodies with snippets."""
    file_path, lines = _load_full_txt(args.file, args.source, args.cache_dir,
                                      max_age=args.max_age)
    docs = split_documents(lines)

    if not args.query.strip():
        die("query must not be empty")

    if args.page_ref is not None:
        target_docs = [_resolve_page_ref(docs, args.page_ref)]
    else:
        target_docs = list(range(len(docs)))

    print(f'Search-content results for "{args.query}" (file: {file_path})')
    print("=" * 60)
    print()

    collected: list[tuple[int, dict, dict]] = []
    total_hits = 0

    for idx in target_docs:
        doc = docs[idx]
        hits = search_content_in_body(
            doc["body_lines"], args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=2,
            max_snippet_chars=args.max_snippet_chars,
        )
        if hits["total_matches"] == 0:
            continue
        total_hits += hits["total_matches"]
        collected.append((idx, doc, hits))

    if not args.include_changelog_priority:
        collected.sort(key=lambda t: (
            1 if _is_low_priority(t[1]["title"]) else 0,
            -t[2]["total_matches"],
            t[0],
        ))

    docs_matched = len(collected)
    printed = collected[: args.limit]

    for idx, doc, hits in printed:
        shown = len(hits["results"])
        print(f"[{idx}] {doc['title']}")
        if doc["source_url"]:
            print(f"    URL: {doc['source_url']}")
        print(f"    ({hits['total_matches']} hits in this page, showing {shown})")
        for r in hits["results"]:
            print(f"    Section: {r['heading_path']}  (x{r['hit_count']})")
            for snippet_line in r["snippet"].splitlines():
                print(f"      {snippet_line}")
            print()
        print()

    if total_hits == 0:
        print("No matching content found.")
        print()
        print("Tip: try broader keywords or 'search-index' to find relevant pages first")
    else:
        print(f"({total_hits} hits across {docs_matched} pages, showing top {len(printed)})")
    print()
    next_hint("content", "<doc_index>", '"<heading_path>"')


def cmd_search(args):
    """Smart search: rank pages via llms.txt and drill into bodies via llms-full.txt.

    Pages are joined across the two indexes by their normalised ``source_url``
    so the doc_idx returned here is always valid for ``content`` / ``sections``.
    """
    src = _get_source(args)

    if not args.query.strip():
        die("query must not be empty")

    # Phase 1: rank pages from llms.txt
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    index_path = fetch_url(src["index_url"], index_cache,
                           user_agent=USER_AGENT, max_age=args.max_age)
    entries = parse_llms_index(load_lines(index_path))
    scored_entries = search_index_entries(entries, args.query,
                                          limit=args.index_limit)

    # Phase 2: load llms-full.txt + build URL → doc_idx mapping
    full_cache = _cache_path(args.cache_dir, src["full_cache"])
    full_path = fetch_url(src["full_url"], full_cache,
                          user_agent=USER_AGENT, max_age=args.max_age)
    lines = load_lines(full_path)
    docs = split_documents(lines)
    url_to_idx = build_url_to_full_index(docs)

    # Phase 2.5: pre-flight URL join sanity (warn if join rate is too low)
    if entries:
        joinable = sum(1 for e in entries
                       if normalize_doc_url(e["url"]) in url_to_idx)
        join_rate = joinable / len(entries)
        if join_rate < 0.8:
            print(
                f"WARNING: URL join rate is {join_rate:.0%} "
                f"({joinable}/{len(entries)}). Some index entries cannot be "
                f"joined to full text.",
                file=sys.stderr,
            )

    print(f'Search results for "{args.query}" ({src["label"]})')
    print(f"  (index: {index_path})")
    print(f"  (full:  {full_path})")
    print("=" * 60)
    print()

    if not scored_entries:
        print("No matching pages found.")
        print()
        print("Tip: try broader keywords")
        return

    # Phase 3: drill into bodies of candidate pages
    results = []
    for score, _, entry in scored_entries:
        normalized = normalize_doc_url(entry["url"])
        full_idx = url_to_idx.get(normalized)
        if full_idx is None:
            print(f"  (skip: no full-text for {entry['url']})", file=sys.stderr)
            continue
        body_hits = search_content_in_body(
            docs[full_idx]["body_lines"], args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=2,
            max_snippet_chars=args.max_snippet_chars,
        )
        results.append({
            "doc_idx": full_idx,
            "title": entry["title"],
            "url": entry["url"],
            "index_score": score,
            "body_hits": body_hits,
        })

    # Phase 4: rank, applying changelog deprioritise unless opted out
    def _sort_key(r):
        return (
            (0 if args.include_changelog_priority else
             1 if _is_low_priority(r["title"]) else 0),
            -r["index_score"],
            -r["body_hits"]["total_matches"],
            r["doc_idx"],
        )
    results.sort(key=_sort_key)

    for r in results:
        hits = r["body_hits"]
        shown = len(hits["results"])
        print(f"[{r['doc_idx']}] {r['title']} (index_score: {r['index_score']})")
        print(f"    URL: {r['url']}")
        if hits["total_matches"]:
            print(f"    ({hits['total_matches']} body hits, showing {shown})")
            for s in hits["results"]:
                print(f"    Section: {s['heading_path']}  (x{s['hit_count']})")
                for snippet_line in s["snippet"].splitlines():
                    print(f"      {snippet_line}")
                print()
        else:
            print(f"    (no body hits — index match only)")
        print()

    print(f"({len(results)} pages, ranked via URL-joined index)")
    print()
    next_hint("content", "<doc_index>", '"<heading_path>"')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_source_arg(parser) -> None:
    parser.add_argument(
        "--source", choices=list(SOURCES.keys()), default=DEFAULT_SOURCE,
        help=f"Documentation source (default: {DEFAULT_SOURCE})",
    )


def _add_max_age_arg(parser) -> None:
    parser.add_argument(
        "--max-age", type=int, default=None,
        help="Re-fetch cache if older than N seconds (default: never expire)",
    )


def _add_file_arg(parser) -> None:
    parser.add_argument(
        "--file", default=None,
        help="Path to llms-full.txt (default: derived from --source)",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Progressive loader for Claude documentation (llms-full.txt)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-index
    p_index = sub.add_parser("fetch-index", help="Fetch and print page index")
    _add_source_arg(p_index)
    add_cache_dir_arg(p_index)
    _add_max_age_arg(p_index)
    p_index.set_defaults(func=cmd_fetch_index)

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a page")
    p_sections.add_argument(
        "page_ref",
        help="Page reference: integer index (e.g. '5'), URL slug (e.g. 'hooks'), "
             "or full URL (e.g. 'https://code.claude.com/docs/en/hooks')",
    )
    _add_file_arg(p_sections)
    _add_source_arg(p_sections)
    add_cache_dir_arg(p_sections)
    _add_max_age_arg(p_sections)
    p_sections.set_defaults(func=cmd_sections)

    # content
    p_content = sub.add_parser("content", help="Print page/section content")
    p_content.add_argument(
        "page_ref",
        help="Page reference: integer index, URL slug, or full URL",
    )
    add_heading_path_arg(p_content, help="Heading path (omit for full page)")
    _add_file_arg(p_content)
    _add_source_arg(p_content)
    add_cache_dir_arg(p_content)
    _add_max_age_arg(p_content)
    p_content.set_defaults(func=cmd_content)

    # search-index
    p_search_idx = sub.add_parser(
        "search-index",
        help="Rank pages by keyword (from llms.txt title/description)",
    )
    _add_source_arg(p_search_idx)
    add_cache_dir_arg(p_search_idx)
    _add_max_age_arg(p_search_idx)
    p_search_idx.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_idx.add_argument("--limit", type=int, default=15,
                              help="Max results to show (default: 15)")
    p_search_idx.set_defaults(func=cmd_search_index)

    # search-content
    p_search_body = sub.add_parser(
        "search-content",
        help="Keyword search across llms-full.txt bodies with snippets",
    )
    p_search_body.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_body.add_argument(
        "--page-ref", default=None,
        help="Restrict search to a single page (int / URL slug / URL)",
    )
    _add_file_arg(p_search_body)
    _add_source_arg(p_search_body)
    add_cache_dir_arg(p_search_body)
    _add_max_age_arg(p_search_body)
    p_search_body.add_argument("--limit", type=int, default=10,
                               help="Max pages to display (default: 10)")
    p_search_body.add_argument("--context", type=int, default=2,
                               help="Context lines around each hit (default: 2)")
    p_search_body.add_argument("--max-hits", type=int, default=5,
                               help="Max hits to display per page (default: 5)")
    p_search_body.add_argument(
        "--max-snippet-chars", type=int, default=500,
        help="Truncate each snippet to N chars (0 = no limit, default: 500)",
    )
    p_search_body.add_argument(
        "--include-changelog-priority", action="store_true",
        help="Do not deprioritize Changelog / release-notes pages",
    )
    p_search_body.set_defaults(func=cmd_search_content)

    # search (smart: llms.txt ranking + URL-joined body drill-in)
    p_search = sub.add_parser(
        "search",
        help="Smart search: ranks pages via index and drills into bodies "
             "(URL-joined so doc_idx is reliable for content/sections)",
    )
    p_search.add_argument("query", help="Space-separated keywords (AND search)")
    _add_source_arg(p_search)
    add_cache_dir_arg(p_search)
    _add_max_age_arg(p_search)
    p_search.add_argument("--index-limit", type=int, default=5,
                          help="Max candidate pages from index (default: 5)")
    p_search.add_argument("--max-hits", type=int, default=3,
                          help="Max body hits per page (default: 3)")
    p_search.add_argument("--context", type=int, default=2,
                          help="Context lines around each hit (default: 2)")
    p_search.add_argument(
        "--max-snippet-chars", type=int, default=500,
        help="Truncate each snippet to N chars (0 = no limit, default: 500)",
    )
    p_search.add_argument(
        "--include-changelog-priority", action="store_true",
        help="Do not deprioritize Changelog / release-notes pages",
    )
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
