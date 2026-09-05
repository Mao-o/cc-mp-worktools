#!/usr/bin/env python3
"""Progressive loader for ai-sdk.dev/llms-full.txt.

Parses the concatenated frontmatter-delimited Markdown documents in
``llms-full.txt`` (the bundled full-documentation snapshot referenced from
``llms.txt``) and provides subcommands for progressive (layered) access:

  fetch-index     — Fetch (if uncached) and print document index
  search-index    — Rank documents by keyword (title/description/tags/headings)
  search-content  — Keyword search across document bodies with snippets
  search          — Smart search: rank docs by index, drill into top N bodies
  sections        — List sections (headings) within a specific document
  content         — Print content of a specific document or section

Page references (``<page_ref>``) accept two forms:

  - integer index   (e.g. ``42``)
  - title substring (e.g. ``streamtext``)

Upstream layout (as of 2026-06): ``llms.txt`` is a ~2 KB index that points
to ``llms-full.txt`` (~5 MB, ~530 frontmatter-delimited docs). This loader
targets ``llms-full.txt`` directly. Leading non-frontmatter content (a small
contributing-guide block before the first ``---``) is skipped by
``split_documents`` and does not appear as a document.

Passing ``--file`` is optional — if omitted, the cached copy under
``--cache-dir`` is auto-fetched / re-used.
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
    add_max_age_arg,
    add_max_chars_arg,
    add_max_snippet_chars_arg,
    assert_parsed,
    corpus_hint_args,
    die,
    die_index_out_of_range,
    extract_content,
    extract_sections,
    fetch_url,
    format_heading_path_for_display,
    full_corpus_body_search,
    load_lines,
    next_hint,
    print_metadata_header,
    print_subsection_hints,
    search_content_in_body,
    search_index_entries,
    truncate_content,
)

# A document with no frontmatter ``title:`` field almost always means
# split_documents mis-split the file (e.g. a body '---' horizontal rule
# mistaken for a new frontmatter block) rather than the upstream docs
# genuinely lacking titles. Warn once this many documents come back untitled.
UNTITLED_WARN_RATIO = 0.10

LLMS_TXT_URL = "https://ai-sdk.dev/llms-full.txt"
DEFAULT_CACHE_FILENAME = "ai-sdk-llms-full.txt"

USER_AGENT = "claude-code-ai-sdk-researcher/1.0"


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


_FM_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:")
_FM_LIST_RE = re.compile(r"^\s*- ")
_FM_CONTINUATION_RE = re.compile(r"^\s+\S")
_FM_LOOKAHEAD = 30


def _looks_like_frontmatter_start(lines: list[str], pos: int) -> bool:
    """Does the ``---`` at *pos* open a frontmatter block?

    Requires a closing ``---`` within ``_FM_LOOKAHEAD`` lines, a ``title:``
    key in between, and *every* line in between to be YAML-shaped — a
    ``key: value`` line, a ``- item`` list line, an indented continuation,
    or blank. A body horizontal rule followed by prose that merely starts
    with ``Note:`` / ``Example:`` used to pass the old "any key: value in
    the next 30 lines" check and split one page into two (internal
    backlog 2wd.16); prose lines are not YAML-shaped, so they now veto.
    """
    if not _is_frontmatter_delimiter(lines[pos]):
        return False
    n = len(lines)
    limit = min(pos + _FM_LOOKAHEAD, n)
    has_title = False
    for j in range(pos + 1, limit):
        line = lines[j]
        if _is_frontmatter_delimiter(line):
            return has_title
        if not line.strip():
            continue
        if _FM_KEY_RE.match(line):
            if line.startswith("title:"):
                has_title = True
            continue
        if _FM_LIST_RE.match(line) or _FM_CONTINUATION_RE.match(line):
            continue
        return False
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
# Heading extraction for search-index scoring signal
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


# ---------------------------------------------------------------------------
# Fetch + load helpers
# ---------------------------------------------------------------------------

def _default_cache_path(cache_dir: str) -> str:
    # Avoid rstrip("/"): cache_dir="/" would become "" and os.path.join("", ...)
    # returns a relative path. os.path.join already handles trailing slashes
    # in cache_dir correctly. (Codex Review P3 feedback on PR #17.)
    return os.path.join(cache_dir, DEFAULT_CACHE_FILENAME)


def fetch_llms_txt(cache_path: str, *, max_age: int | None = None) -> str:
    """Return path to cached llms-full.txt, fetching if necessary."""
    return fetch_url(
        LLMS_TXT_URL,
        cache_path,
        user_agent=USER_AGENT,
        timeout=120,
        max_age=max_age,
    )


def _load_docs(file_arg: str | None, cache_dir: str,
               *, max_age: int | None = None) -> tuple[str, list[dict]]:
    """Load and split documents from llms-full.txt.

    Two distinct modes:

    * **No ``--file``** (``file_arg is None``): derive the cache path from
      *cache_dir* and auto-fetch. This is the fetch-and-cache lifecycle.

    * **Explicit ``--file``**: read-only. The file must already exist —
      we never overwrite a user-supplied path. This lets users pin to a
      local snapshot for reproducible runs.
    """
    if file_arg is None:
        cache_path = _default_cache_path(cache_dir)
        cache_path = fetch_llms_txt(cache_path, max_age=max_age)
        docs = split_documents(load_lines(cache_path))
        assert_parsed("ai-sdk", len(docs), cache_path)
        return cache_path, docs

    if not os.path.exists(file_arg):
        die(
            f"--file '{file_arg}' does not exist. Drop --file to auto-fetch "
            f"to the cache, or download the snapshot manually first."
        )
    docs = split_documents(load_lines(file_arg))
    assert_parsed("ai-sdk", len(docs), file_arg)
    return file_arg, docs


def _warn_if_untitled_ratio_high(docs: list[dict], path: str) -> None:
    """Warn on stderr when too many parsed docs have an empty frontmatter
    title (see ``UNTITLED_WARN_RATIO``). Called only from the "browse the
    whole corpus" commands (fetch-index / search-index / search) — not from
    single-document hot paths like ``content`` / ``sections`` — since it
    re-parses frontmatter for every document."""
    if not docs:
        return
    untitled = sum(
        1 for d in docs if not parse_frontmatter(d["frontmatter_lines"]).get("title")
    )
    ratio = untitled / len(docs)
    if ratio > UNTITLED_WARN_RATIO:
        print(
            f"WARNING: {untitled}/{len(docs)} documents ({ratio:.0%}) have no "
            f"title in {path}. The upstream format may have changed, or a "
            f"stray '---' line inside a document body is being mistaken for "
            f"a new document boundary.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Page reference resolution (int / title substring)
# ---------------------------------------------------------------------------

def _resolve_page_ref(docs: list[dict], page_ref: str) -> int:
    """Resolve a page reference to a doc index.

    Tries, in order:
      1. integer index into *docs*
      2. title substring (case-insensitive); unique match wins

    AI SDK's llms-full.txt has no Source/URL line in document bodies, so
    URL / slug matching is not supported here (use the integer index from
    ``search-index`` / ``search`` instead).
    """
    if page_ref is None or page_ref == "":
        die("page_ref required: integer index or title substring")

    try:
        idx = int(page_ref)
    except ValueError:
        pass
    else:
        if 0 <= idx < len(docs):
            return idx
        die_index_out_of_range(idx, len(docs))

    needle = page_ref.lower()
    candidates: list[tuple[int, str]] = []
    for i, doc in enumerate(docs):
        fm = parse_frontmatter(doc["frontmatter_lines"])
        title = (fm.get("title") or "").strip()
        if needle in title.lower():
            candidates.append((i, title))
    if len(candidates) == 1:
        return candidates[0][0]
    if len(candidates) > 1:
        detail = "\n  ".join(f"[{i}] {t}" for i, t in candidates)
        die(f"Ambiguous title substring '{page_ref}'. Matches:\n  {detail}")
    die(f"No document found for: {page_ref}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_fetch_index(args):
    """Fetch (if needed) and print document index."""
    cache_path, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)
    _warn_if_untitled_ratio_high(docs, cache_path)

    print(f"AI SDK llms-full.txt Document Index (file: {cache_path})")
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
    print("Tip: use 'search' to rank docs + drill into bodies in one call")
    next_hint("sections", "<page_ref>", *corpus_hint_args(args))


def cmd_sections(args):
    """List sections within a specific document."""
    file_path, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)
    idx = _resolve_page_ref(docs, args.page_ref)

    doc = docs[idx]
    fm = parse_frontmatter(doc["frontmatter_lines"])
    title = fm["title"] or "(untitled)"
    sections = extract_sections(doc["body_lines"], min_level=1)

    print(f'Sections in [{idx}] "{title}"')
    print(f"  (file: {file_path})")
    print("=" * 60)

    for s in sections:
        indent = "  " * (s["level"] - 1)
        code_marker = " [code]" if s["has_code_blocks"] else ""
        # Print the canonical heading_path, not the bare title: this line
        # is documented (SKILL.md) as copy-pasteable straight into
        # content's heading_path argument, and two sibling subsections
        # with the same title (e.g. "Examples" under two different
        # parents) are only distinguishable via the full path.
        print(f"{indent}[L{s['level']}] {format_heading_path_for_display(s['heading_path'])}{code_marker}")

    print()
    print(f"({len(sections)} sections)")
    print()
    next_hint("content", str(idx), '"<heading_path>"', *corpus_hint_args(args))


def cmd_content(args):
    """Print content of a specific document or section."""
    _, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)
    idx = _resolve_page_ref(docs, args.page_ref)

    doc = docs[idx]
    fm = parse_frontmatter(doc["frontmatter_lines"])

    content, resolved_heading_path = extract_content(
        doc["body_lines"], args.heading_path,
        protect_tables=False, min_level=1,
    )

    hint_args = corpus_hint_args(args)
    hint_suffix = (" " + " ".join(hint_args)) if hint_args else ""
    narrow_hint = f'parse-ai-sdk.py content {idx} "<heading_path>"{hint_suffix}'
    content = truncate_content(content, args.max_chars, narrow_hint=narrow_hint)

    print_metadata_header(
        fm["title"] or "(untitled)",
        tags=fm["tags"] or None,
        heading_path=resolved_heading_path,
    )

    # Printed BEFORE the body too — see parse-claude-docs.py's cmd_content
    # for why (survives truncation regardless of where the cut lands).
    if not args.no_subsection_hints:
        print_subsection_hints(doc["body_lines"], idx, resolved_heading_path,
                               min_level=1, extra_hint_args=hint_args)

    print(content, end="")

    if not args.no_subsection_hints:
        print_subsection_hints(doc["body_lines"], idx, resolved_heading_path,
                               min_level=1, extra_hint_args=hint_args)


def cmd_search_index(args):
    """Rank documents by keyword match (title, description, tags, headings)."""
    file_path, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)
    _warn_if_untitled_ratio_high(docs, file_path)

    if not args.query.strip():
        die("query must not be empty")

    # Pre-compute frontmatter + headings for every document so we can use them
    # both for scoring and for display without re-parsing.
    fms: list[dict] = []
    doc_headings: dict[int, list[str]] = {}
    for i, doc in enumerate(docs):
        fm = parse_frontmatter(doc["frontmatter_lines"])
        fms.append(fm)
        doc_headings[i] = _extract_h1_h2_titles(doc["body_lines"])

    # Build synthetic entries matching the shape search_index_entries expects.
    entries = [{"title": fm["title"] or "", "description": fm["description"] or ""} for fm in fms]

    def _extras(_entry, idx):
        return {"tags": fms[idx]["tags"], "headings": doc_headings[idx]}

    scored = search_index_entries(entries, args.query, limit=args.limit, get_extras=_extras)

    print(f'Search-index results for "{args.query}" (file: {file_path})')
    print("=" * 60)
    print()

    if not scored:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords, 'search' for an automatic full-text "
              "fallback, 'search-content' to search bodies directly, or "
              "'fetch-index --compact' to browse")
    else:
        for score, idx, _entry in scored:
            fm = fms[idx]
            title = fm["title"] or "(untitled)"
            desc = fm["description"] or ""
            tags = ", ".join(fm["tags"]) if fm["tags"] else ""

            print(f"[{idx}] {title} (score: {score})")
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
    next_hint("search", '"<query>"', *corpus_hint_args(args))


def cmd_search_content(args):
    """Keyword search across document bodies (returns heading_path + snippets)."""
    file_path, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)

    if not args.query.strip():
        die("query must not be empty")

    if args.page_ref is not None:
        target_docs = [_resolve_page_ref(docs, args.page_ref)]
    else:
        target_docs = list(range(len(docs)))

    print(f'Search-content results for "{args.query}" (file: {file_path})')
    print("=" * 60)
    print()

    total_hits = 0
    docs_matched = 0
    printed_docs = 0

    for idx in target_docs:
        doc = docs[idx]
        fm = parse_frontmatter(doc["frontmatter_lines"])
        title = fm["title"] or "(untitled)"

        hits = search_content_in_body(
            doc["body_lines"], args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=1,
            max_snippet_chars=args.max_snippet_chars,
        )

        if hits["total_matches"] == 0:
            continue

        total_hits += hits["total_matches"]
        docs_matched += 1

        if printed_docs >= args.limit:
            continue
        printed_docs += 1

        shown = len(hits["results"])
        print(f"[{idx}] {title}")
        if fm["tags"]:
            print(f"    tags: {', '.join(fm['tags'])}")
        mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
        print(f"    ({hits['total_matches']} hits in this document, showing {shown}){mode_note}")
        for r in hits["results"]:
            kw_info = f"  keywords: {', '.join(r['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
            print(f"    Section: {format_heading_path_for_display(r['heading_path'])}  (x{r['hit_count']}){kw_info}")
            for snippet_line in r["snippet"].splitlines():
                print(f"      {snippet_line}")
            print()
        print()

    if total_hits == 0:
        print("No matching content found.")
        print()
        print("Tip: try broader keywords or 'search-index' to find relevant documents first")
    else:
        print(f"({total_hits} hits across {docs_matched} documents, showing top {printed_docs})")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"', *corpus_hint_args(args))


def cmd_search(args):
    """Smart search: rank docs via title/desc/tags + drill into top N bodies.

    Phase 1: search-index for top *--top-n* documents.
    Phase 2: search each candidate body with section-level AND keyword match.
    Phase 3: rank by (body hit count desc, index score desc, doc_idx asc).

    AI SDK's ``llms-full.txt`` is one concatenated file (~530 docs),
    so no URL join is needed — index and body refer to the same doc by
    position.
    """
    file_path, docs = _load_docs(args.file, args.cache_dir, max_age=args.max_age)
    _warn_if_untitled_ratio_high(docs, file_path)

    if not args.query.strip():
        die("query must not be empty")

    # Pre-compute frontmatter + headings for ranking
    fms: list[dict] = []
    doc_headings: dict[int, list[str]] = {}
    for i, doc in enumerate(docs):
        fm = parse_frontmatter(doc["frontmatter_lines"])
        fms.append(fm)
        doc_headings[i] = _extract_h1_h2_titles(doc["body_lines"])

    entries = [{"title": fm["title"] or "", "description": fm["description"] or ""} for fm in fms]

    def _extras(_entry, idx):
        return {"tags": fms[idx]["tags"], "headings": doc_headings[idx]}

    scored = search_index_entries(entries, args.query, limit=args.top_n, get_extras=_extras)

    print(f'Search results for "{args.query}" (file: {file_path})')
    print(f"  (top-{args.top_n} candidate docs drilled into bodies)")
    print("=" * 60)
    print()

    results = []
    for score, idx, _entry in scored:
        doc = docs[idx]
        fm = fms[idx]
        body_hits = search_content_in_body(
            doc["body_lines"], args.query,
            context_lines=args.context,
            max_matches_per_doc=args.max_hits,
            min_level=1,
            max_snippet_chars=args.max_snippet_chars,
        )
        results.append({
            "doc_idx": idx,
            "title": fm["title"] or "(untitled)",
            "tags": fm["tags"],
            "index_score": score,
            "body_hits": body_hits,
            "body_only": False,
        })

    # Append full-corpus body-only hits when title/description/tags ranking
    # produced no candidates, or none of the drilled candidates had any body
    # hits — e.g. the query only matches an option/field name that lives in
    # a doc body (bug: 'search' returned "no matching documents" for terms
    # that 'search-content' could find). Appends rather than replaces: a doc
    # that legitimately ranked on title/description/tags keeps its row
    # (shown as "index match only") even when its body has no hits — the
    # fallback only adds docs the index ranking missed entirely.
    if not any(r["body_hits"]["total_matches"] for r in results):
        already_shown = {r["doc_idx"] for r in results}
        fallback = full_corpus_body_search(
            [d["body_lines"] for d in docs], args.query,
            context_lines=args.context, max_matches_per_doc=args.max_hits,
            max_snippet_chars=args.max_snippet_chars, min_level=1,
            limit=args.top_n,
        )
        for idx, hits in fallback:
            if idx in already_shown:
                continue
            results.append({
                "doc_idx": idx,
                "title": fms[idx]["title"] or "(untitled)",
                "tags": fms[idx]["tags"],
                "index_score": None,
                "body_hits": hits,
                "body_only": True,
            })

    if not results:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords, 'search-content' for a full-body "
              "scan, or 'fetch-index --compact' to browse")
        return

    if all(r["body_only"] for r in results):
        print("  (no title/description/tags match — showing full-body "
              "search results instead)")
        print()

    results.sort(key=lambda r: (
        -r["body_hits"]["total_matches"],
        -(r["index_score"] or 0),
        r["doc_idx"],
    ))

    for r in results:
        hits = r["body_hits"]
        shown = len(hits["results"])
        score_tag = " [body-only]" if r["body_only"] else f" (index_score: {r['index_score']})"
        print(f"[{r['doc_idx']}] {r['title']}{score_tag}")
        if r["tags"]:
            print(f"    tags: {', '.join(r['tags'])}")
        if hits["total_matches"]:
            mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
            print(f"    ({hits['total_matches']} body hits, showing {shown}){mode_note}")
            for s in hits["results"]:
                kw_info = f"  keywords: {', '.join(s['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
                print(f"    Section: {format_heading_path_for_display(s['heading_path'])}  (x{s['hit_count']}){kw_info}")
                for snippet_line in s["snippet"].splitlines():
                    print(f"      {snippet_line}")
                print()
        else:
            print(f"    (no body hits — index match only)")
        print()

    print(f"({len(results)} documents, ranked via index → body)")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"', *corpus_hint_args(args))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_file_arg(parser) -> None:
    parser.add_argument(
        "--file", default=None,
        help="Path to a local llms.txt snapshot (default: auto-fetch under --cache-dir)",
    )


def _add_page_ref_arg(parser, *, help: str) -> None:
    parser.add_argument("page_ref", help=help)


def main():
    parser = argparse.ArgumentParser(
        description="Progressive loader for ai-sdk.dev/llms-full.txt",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-index
    p_index = sub.add_parser("fetch-index", help="Fetch and print document index")
    _add_file_arg(p_index)
    add_cache_dir_arg(p_index)
    add_max_age_arg(p_index)
    p_index.add_argument(
        "--compact", action="store_true",
        help="Print titles only in compact format",
    )
    p_index.set_defaults(func=cmd_fetch_index)

    # search-index
    p_search_idx = sub.add_parser(
        "search-index",
        help="Rank documents by keyword (title/description/tags/headings)",
    )
    p_search_idx.add_argument("query", help="Space-separated keywords (AND search)")
    _add_file_arg(p_search_idx)
    add_cache_dir_arg(p_search_idx)
    add_max_age_arg(p_search_idx)
    p_search_idx.add_argument("--limit", type=int, default=15,
                              help="Max results to show (default: 15)")
    p_search_idx.add_argument("--show-sections", action="store_true",
                              help="Show H1/H2 headings for each result")
    p_search_idx.set_defaults(func=cmd_search_index)

    # search-content
    p_search_body = sub.add_parser(
        "search-content",
        help="Keyword search across document bodies with snippets",
    )
    p_search_body.add_argument("query", help="Space-separated keywords (AND search)")
    p_search_body.add_argument(
        "--page-ref", default=None,
        help="Restrict search to a single document (int / title substring)",
    )
    _add_file_arg(p_search_body)
    add_cache_dir_arg(p_search_body)
    add_max_age_arg(p_search_body)
    p_search_body.add_argument("--limit", type=int, default=10,
                               help="Max documents to display (default: 10)")
    p_search_body.add_argument("--context", type=int, default=2,
                               help="Context lines around each hit (default: 2)")
    p_search_body.add_argument("--max-hits", type=int, default=5,
                               help="Max hits to display per document (default: 5)")
    add_max_snippet_chars_arg(p_search_body)
    p_search_body.set_defaults(func=cmd_search_content)

    # sections
    p_sections = sub.add_parser("sections", help="List sections in a document")
    _add_page_ref_arg(
        p_sections,
        help="Page reference: integer index or title substring",
    )
    _add_file_arg(p_sections)
    add_cache_dir_arg(p_sections)
    add_max_age_arg(p_sections)
    p_sections.set_defaults(func=cmd_sections)

    # content
    p_content = sub.add_parser("content", help="Print document/section content")
    _add_page_ref_arg(
        p_content,
        help="Page reference: integer index or title substring",
    )
    add_heading_path_arg(p_content)
    _add_file_arg(p_content)
    add_cache_dir_arg(p_content)
    add_max_age_arg(p_content)
    add_max_chars_arg(p_content)
    p_content.add_argument(
        "--no-subsection-hints", action="store_true",
        help="Suppress the subsection hint block printed before/after content",
    )
    p_content.set_defaults(func=cmd_content)

    # search (smart: index rank + body drill-in)
    p_search = sub.add_parser(
        "search",
        help="Smart search: ranks documents and drills into top N bodies",
    )
    p_search.add_argument("query", help="Space-separated keywords (AND search)")
    _add_file_arg(p_search)
    add_cache_dir_arg(p_search)
    add_max_age_arg(p_search)
    p_search.add_argument(
        "--top-n", type=int, default=5,
        help="Number of top candidate documents to drill into (default: 5)",
    )
    p_search.add_argument("--max-hits", type=int, default=3,
                          help="Max body hits per document (default: 3)")
    p_search.add_argument("--context", type=int, default=2,
                          help="Context lines around each hit (default: 2)")
    add_max_snippet_chars_arg(p_search)
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
