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
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _common import (
    FenceTracker,
    add_cache_dir_arg,
    add_heading_path_arg,
    add_max_age_arg,
    add_max_chars_arg,
    add_max_snippet_chars_arg,
    assert_parsed,
    build_url_to_full_index,
    check_join_rate,
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
    normalize_doc_url,
    parse_llms_index,
    print_metadata_header,
    print_subsection_hints,
    search_content_in_body,
    search_index_entries,
    truncate_content,
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


def _source_hint_args(args) -> tuple:
    """Return ``('--source', '<src>')`` when not the default; else ``()``.

    Used to propagate the active ``--source`` into ``next_hint`` so a follow-up
    command stays on the same documentation set instead of silently falling
    back to ``DEFAULT_SOURCE``.
    """
    if getattr(args, "source", DEFAULT_SOURCE) == DEFAULT_SOURCE:
        return ()
    return ("--source", args.source)

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


def _split_documents_checked(lines: list[str], path: str) -> list[dict]:
    """``split_documents`` wrapper that treats 0 docs as a format-changed
    failure (exit 2) instead of silently behaving like a valid empty page
    set — see ``_common.assert_parsed``."""
    docs = split_documents(lines)
    assert_parsed("claude-docs full-text", len(docs), path)
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


def _load_url_to_idx_if_cached(full_cache: str, max_age: int | None = None) -> dict:
    """Join llms.txt URLs to llms-full.txt doc_idx — but ONLY if the
    full-text file is already cached AND still fresh under *max_age*;
    never fetch it just for this.

    ``fetch-index``/``search-index`` only need the lightweight llms.txt
    index; eagerly fetching llms-full.txt to compute a display label would
    turn ``fetch-index`` — documented as the lightweight fallback command
    (see SKILL.md) — into the single heaviest command in the tool (23.8MB
    for ``platform``). Uses plain ``split_documents`` (not
    ``_split_documents_checked``): an already-cached-but-stale/corrupt
    full-text file should degrade this join to "unavailable" (falls back
    to slug display below), not crash the lightweight index command that
    doesn't otherwise depend on llms-full.txt at all.

    A cached file older than *max_age* is *also* treated as "unavailable"
    here, even though it exists and could be read right now: a later
    ``sections``/``content`` call passes this same *max_age* to
    ``fetch_url`` (see its ``age < max_age`` check) and will actually
    refetch it. If upstream reordered/inserted pages since this stale copy
    was written, joining against its about-to-be-replaced doc_idx would
    silently reproduce the exact numbering drift this feature exists to
    prevent — falling back to the slug form (always valid, ageless) until
    a fresh copy exists is the same trade-off ``fetch_url`` itself makes.
    """
    if not os.path.exists(full_cache):
        return {}
    if max_age is not None:
        age = time.time() - os.path.getmtime(full_cache)
        if age >= max_age:
            return {}
    return build_url_to_full_index(split_documents(load_lines(full_cache)))


def _build_unique_slugs(raw_urls: list[str]) -> dict[str, str]:
    """Map each *normalized* URL (keys are ``normalize_doc_url(u)`` for
    each ``u`` in *raw_urls*) to a display slug unique across all of them.

    Starts from the single last path segment (e.g. ``"hooks"``) and, only
    for URLs whose slug collides with another URL's, grows the suffix one
    segment at a time (``"agent-sdk/hooks"``, ...) until it no longer
    collides — both single- and multi-segment slugs are valid
    ``sections``/``content`` input (``_resolve_page_ref`` matches on the
    URL's trailing path, see its docstring). Falls back to the full path
    if even that doesn't disambiguate (only possible for literal URL
    duplicates). Without this, ``.../hooks`` and ``.../agent-sdk/hooks``
    would both display as ``[hooks]``, and passing either back in would
    hit ``_resolve_page_ref``'s own ambiguous-slug error — making the
    advertised copy-and-paste fallback reference unusable for exactly the
    pages that need it most.
    """
    normalized = [normalize_doc_url(u) for u in raw_urls]
    all_segments = [u.rstrip("/").lstrip("/").split("/") for u in normalized]
    result = {}
    for i, url in enumerate(normalized):
        segments = all_segments[i]
        for n in range(1, len(segments) + 1):
            candidate = "/".join(segments[-n:])
            if not any(
                j != i and "/".join(all_segments[j][-n:]) == candidate
                for j in range(len(normalized))
            ):
                result[url] = candidate
                break
        else:
            result[url] = "/".join(segments) or "?"
    return result


def _joined_or_slug_ref(entry: dict, url_to_idx: dict, unique_slugs: dict) -> str:
    """Return a doc reference for *entry*: the llms-full.txt-joined
    doc_idx when available in *url_to_idx*, else a slug unique across the
    whole index (see ``_build_unique_slugs``).

    Both forms are valid ``sections``/``content`` input (see
    ``_resolve_page_ref``) — the raw llms.txt list position is NOT, and
    passing it through used to silently resolve to the wrong page whenever
    the two indexes weren't in the same order (e.g. after either source
    reorders/inserts a page).
    """
    url = normalize_doc_url(entry.get("url", ""))
    joined = url_to_idx.get(url)
    if joined is not None:
        return str(joined)
    return unique_slugs.get(url, "?")


def cmd_fetch_index(args):
    """Fetch lightweight llms.txt index and print page list with descriptions."""
    src = _get_source(args)
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    path = fetch_url(src["index_url"], index_cache, user_agent=USER_AGENT,
                     max_age=args.max_age)
    lines = load_lines(path)
    entries = parse_llms_index(lines)
    assert_parsed(f"{src['label']} index", len(entries), path)

    full_cache = _cache_path(args.cache_dir, src["full_cache"])
    url_to_idx = _load_url_to_idx_if_cached(full_cache, max_age=args.max_age)
    unique_slugs = _build_unique_slugs([e.get("url", "") for e in entries])

    grouped = _group_index_entries(entries)

    print(f"{src['label']} Document Index (from llms.txt)")
    print("=" * 60)
    print()

    displayed = 0
    grouped_count = 0
    for item in grouped:
        if item["type"] == "single":
            entry = item["entry"]
            print(f"[{_joined_or_slug_ref(entry, url_to_idx, unique_slugs)}] {entry['title']}")
            if entry["description"]:
                desc = entry["description"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print()
            displayed += 1
        else:
            variants = item["variants"]
            variant_labels = [
                f"{name} [{_joined_or_slug_ref(entries[i], url_to_idx, unique_slugs)}]"
                for i, name in variants
            ]
            print(item["base"])
            if item["desc"]:
                desc = item["desc"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print(f"    Variants: {', '.join(variant_labels)}")
            print()
            displayed += 1
            grouped_count += len(variants)

    print(f"({len(entries)} pages total, {displayed} entries shown — {grouped_count} pages grouped)")
    print()
    if url_to_idx:
        next_hint("sections", "<page_ref>", *_source_hint_args(args))
        print(f"  (llms-full.txt will be fetched automatically on first use)")
    else:
        next_hint("sections", "<slug>", *_source_hint_args(args))
        print("  (llms-full.txt not cached yet, so the bracketed ref above is a")
        print("   URL slug, not an integer doc_idx; run 'search' once to fetch")
        print("   it and get numeric doc_idx here too)")


def _resolve_source_from_path(file_path: str) -> tuple[str, dict] | None:
    """Guess ``(source_key, profile)`` from the cache file path."""
    basename = os.path.basename(file_path)
    for key, src in SOURCES.items():
        if src["full_cache"] in basename:
            return key, src
    return None


def _load_full_txt(file_arg: str | None, source_key: str, cache_dir: str,
                   *, max_age: int | None = None) -> tuple[str, list[str]]:
    """Load llms-full.txt.

    Two distinct modes:

    * **No ``--file``** (``file_arg is None``): derive the cache path from
      *source_key* under *cache_dir* and auto-fetch (honouring *max_age*).
      This is the fetch-and-cache lifecycle.

    * **Explicit ``--file``**: read-only. The file must already exist and
      the caller is responsible for keeping it up to date — we never
      overwrite a user-supplied path. *max_age* is intentionally ignored
      in this mode to avoid clobbering local snapshots. If the path looks
      like a known cache for a different source we fail early.
    """
    src = SOURCES[source_key]
    if file_arg is None:
        cache_path = _cache_path(cache_dir, src["full_cache"])
        cache_path = fetch_url(src["full_url"], cache_path,
                               user_agent=USER_AGENT, max_age=max_age)
        return cache_path, load_lines(cache_path)

    inferred = _resolve_source_from_path(file_arg)
    if inferred is not None and inferred[0] != source_key:
        inferred_key = inferred[0]
        die(
            f"--file '{file_arg}' looks like a '{inferred_key}' cache but "
            f"--source is '{source_key}'. Pass --source {inferred_key} "
            f"or drop --file to let --source pick the path."
        )
    if not os.path.exists(file_arg):
        die(
            f"--file '{file_arg}' does not exist. Drop --file to auto-fetch "
            f"to the source cache, or download the snapshot manually first."
        )
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
    docs = _split_documents_checked(lines, file_path)
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
        # Print the canonical heading_path, not the bare title: this line
        # is documented (SKILL.md) as copy-pasteable straight into
        # content's heading_path argument, and two sibling subsections
        # with the same title (e.g. "Examples" under two different
        # parents) are only distinguishable via the full path.
        print(f"{indent}[L{s['level']}] {format_heading_path_for_display(s['heading_path'])}{code_marker}")

    print()
    print(f"({len(sections)} sections)")
    print()
    next_hint("content", str(idx), '"<heading_path>"',
              *(_source_hint_args(args) + corpus_hint_args(args)))


_DOC_LINK_RE = re.compile(r'\((https?://[^\s)]+|/[^\s)]+)\)')


def _build_path_to_idx(docs: list[dict]) -> dict:
    """Map URL path (no host) → doc idx, with ``/docs/en/...`` ↔ ``/en/...`` aliasing.

    docs.* often link to siblings with relative paths like ``/en/skills`` or
    ``/docs/en/skills`` — both forms point at the same page. We index every
    page under both forms so either style of in-body link can be resolved.
    """
    out: dict = {}
    for i, d in enumerate(docs):
        url = normalize_doc_url(d.get("source_url", ""))
        if not url:
            continue
        m = re.match(r"https?://[^/]+(/.*)$", url)
        if not m:
            continue
        path = m.group(1)
        out[path] = i
        if path.startswith("/docs/"):
            out[path[len("/docs"):]] = i
        elif path.startswith("/en/"):
            out["/docs" + path] = i
    return out


def _resolve_link_to_idx(raw_url: str, url_to_idx: dict, path_to_idx: dict) -> int | None:
    """Return the doc idx a markdown link points to, or None if not a known doc."""
    url = raw_url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if url.endswith(".md"):
        url = url[:-3]
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url_to_idx.get(url)
    if url.startswith("/"):
        return path_to_idx.get(url)
    return None


def _annotate_doc_links(content: str, url_to_idx: dict, path_to_idx: dict,
                         self_idx: int) -> str:
    """Insert ``→ [doc_idx N]`` after Markdown links that point to a known doc.

    Skips code fences and Markdown table rows so we don't break formatting.
    Links to the current page (``self_idx``) and to URLs not in our maps
    are left untouched.
    """
    if not url_to_idx and not path_to_idx:
        return content
    lines = content.split("\n")
    fence = FenceTracker()
    out: list[str] = []
    for line in lines:
        was_in_fence = fence.in_fence
        fence.update(line)
        if was_in_fence or fence.in_fence:
            out.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            out.append(line)
            continue

        def _repl(m: re.Match) -> str:
            idx = _resolve_link_to_idx(m.group(1), url_to_idx, path_to_idx)
            if idx is None or idx == self_idx:
                return m.group(0)
            return f"{m.group(0)} → [doc_idx {idx}]"

        out.append(_DOC_LINK_RE.sub(_repl, line))
    return "\n".join(out)


def cmd_content(args):
    """Print content of a specific page or section."""
    file_path, lines = _load_full_txt(args.file, args.source, args.cache_dir,
                                      max_age=args.max_age)
    docs = _split_documents_checked(lines, file_path)
    idx = _resolve_page_ref(docs, args.page_ref)

    doc = docs[idx]
    content, resolved_heading_path = extract_content(doc["body_lines"], args.heading_path)

    if not args.no_link_annotations:
        url_to_idx = build_url_to_full_index(docs)
        path_to_idx = _build_path_to_idx(docs)
        content = _annotate_doc_links(content, url_to_idx, path_to_idx, self_idx=idx)

    hint_args = _source_hint_args(args) + corpus_hint_args(args)
    hint_suffix = (" " + " ".join(hint_args)) if hint_args else ""
    narrow_hint = f'parse-claude-docs.py content {idx} "<heading_path>"{hint_suffix}'
    content = truncate_content(content, args.max_chars, narrow_hint=narrow_hint)

    print_metadata_header(
        doc["title"],
        source=doc["source_url"] or None,
        heading_path=resolved_heading_path,
    )

    # Printed BEFORE the body too (duplicating the same hint printed after
    # it, below): a long page can exceed the Bash tool's ~30KB inline-
    # output threshold even after --max-chars truncation (e.g. --max-chars
    # 0, or a large metadata header), and the diverted output shows only
    # the first ~2KB — hiding the after-body hint entirely. The subsection
    # list + Next hint are the tool's own documented drill-down path (see
    # SKILL.md Quick Start), so they must survive truncation regardless of
    # where the cut lands.
    if not args.no_subsection_hints:
        print_subsection_hints(
            doc["body_lines"], idx, resolved_heading_path, extra_hint_args=hint_args
        )

    print(content, end="")

    if not args.no_subsection_hints:
        print_subsection_hints(
            doc["body_lines"], idx, resolved_heading_path, extra_hint_args=hint_args
        )


def cmd_search_index(args):
    """Rank pages by keyword against llms.txt title/description."""
    src = _get_source(args)
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    path = fetch_url(src["index_url"], index_cache, user_agent=USER_AGENT,
                     max_age=args.max_age)
    entries = parse_llms_index(load_lines(path))
    assert_parsed(f"{src['label']} index", len(entries), path)

    if not args.query.strip():
        die("query must not be empty")

    scored = search_index_entries(entries, args.query, limit=args.limit)

    full_cache = _cache_path(args.cache_dir, src["full_cache"])
    url_to_idx = _load_url_to_idx_if_cached(full_cache, max_age=args.max_age)
    # Built from the FULL entries list, not just the top-N `scored` results:
    # a slug only "looks" unique within the displayed subset would still
    # collide with some other (unshown) page in _resolve_page_ref's full
    # search, defeating the point of disambiguating it here.
    unique_slugs = _build_unique_slugs([e.get("url", "") for e in entries])

    print(f'Search-index results for "{args.query}" ({src["label"]})')
    print(f"  (index: {path})")
    print("=" * 60)
    print()

    if not scored:
        print("No matching pages found.")
        print()
        print("Tip: try broader keywords, 'search' for an automatic full-text "
              "fallback, or 'search-content \"<query>\"' to search page bodies directly")
    else:
        for score, _idx, entry in scored:
            ref = _joined_or_slug_ref(entry, url_to_idx, unique_slugs)
            print(f"[{ref}] {entry['title']} (score: {score})")
            if entry["description"]:
                desc = entry["description"]
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                print(f"    {desc}")
            print(f"    URL: {entry['url']}")
            print()

    print(f"({len(scored)} results, {len(entries)} pages searched)")
    print()
    if not url_to_idx:
        print("Note: llms-full.txt is not cached yet, so [ref] above is a URL "
              "slug, not an integer doc_idx. Run 'search' once (or 'sections "
              "<slug>' directly) to populate doc_idx numbering here too.")
        print()
    next_hint("search", '"<query>"', *_source_hint_args(args))


def cmd_search_content(args):
    """Keyword search across llms-full.txt bodies with snippets."""
    file_path, lines = _load_full_txt(args.file, args.source, args.cache_dir,
                                      max_age=args.max_age)
    docs = _split_documents_checked(lines, file_path)

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

    collected.sort(key=lambda t: (
        (0 if args.include_changelog_priority else
         1 if _is_low_priority(t[1]["title"]) else 0),
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
        mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
        print(f"    ({hits['total_matches']} hits in this page, showing {shown}){mode_note}")
        for r in hits["results"]:
            kw_info = f"  keywords: {', '.join(r['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
            print(f"    Section: {format_heading_path_for_display(r['heading_path'])}  (x{r['hit_count']}){kw_info}")
            for snippet_line in r["snippet"].splitlines():
                print(f"      {snippet_line}")
            print()
        overflow = hits.get("overflow_sections", [])
        if overflow:
            print(f"    Other sections with hits (not shown):")
            for s in overflow:
                print(f"      - {format_heading_path_for_display(s['heading_path'])}  (x{s['hit_count']})")
            print()
        print()

    if total_hits == 0:
        print("No matching content found.")
        print()
        print("Tip: try broader keywords or 'search-index' to find relevant pages first")
    else:
        print(f"({total_hits} hits across {docs_matched} pages, showing top {len(printed)})")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"',
              *(_source_hint_args(args) + corpus_hint_args(args)))


def _search_one_source(args, source_key: str) -> list[dict]:
    """Run the smart-search pipeline against a single source.

    Returns a list of result dicts ready for display, with the source key
    attached. The output formatting is left to ``cmd_search`` so the multi-
    source mode can group results consistently.
    """
    src = SOURCES[source_key]

    # Phase 1: rank pages from llms.txt
    index_cache = _cache_path(args.cache_dir, src["index_cache"])
    index_path = fetch_url(src["index_url"], index_cache,
                           user_agent=USER_AGENT, max_age=args.max_age)
    entries = parse_llms_index(load_lines(index_path))
    assert_parsed(f"{src['label']} index", len(entries), index_path)
    scored_entries = search_index_entries(entries, args.query,
                                          limit=args.top_n)

    # Phase 2: load llms-full.txt (respecting --file) + build URL → doc_idx mapping
    full_path, lines = _load_full_txt(args.file, source_key, args.cache_dir,
                                      max_age=args.max_age)
    docs = _split_documents_checked(lines, full_path)
    url_to_idx = build_url_to_full_index(docs)

    # Phase 2.5: pre-flight URL join sanity (warn / hard-fail if join rate
    # is too low — a rate this bad usually means the upstream format
    # changed, not that a few pages legitimately lack full text)
    if entries:
        joinable = sum(1 for e in entries
                       if normalize_doc_url(e["url"]) in url_to_idx)
        check_join_rate(f"{src['label']} index↔full-text", joinable, len(entries))

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
            "source_key": source_key,
            "source_label": src["label"],
            "doc_idx": full_idx,
            "title": entry["title"],
            "url": entry["url"],
            "index_score": score,
            "body_hits": body_hits,
        })

    # Phase 3.5: append full-corpus body-only hits when title/description
    # ranking produced no candidates, or none of the drilled candidates had
    # any body hits — e.g. the query only matches an option/field name that
    # lives in a page body (bug: 'search' returned "no matching pages" for
    # terms that 'search-content' could find). Appends rather than replaces:
    # a page that legitimately ranked on title/description keeps its row
    # (shown as "index match only") even when its body has no hits — the
    # fallback only adds pages the index ranking missed entirely.
    if not any(r["body_hits"]["total_matches"] for r in results):
        already_shown = {r["doc_idx"] for r in results}
        fallback = full_corpus_body_search(
            [d["body_lines"] for d in docs], args.query,
            context_lines=args.context, max_matches_per_doc=args.max_hits,
            max_snippet_chars=args.max_snippet_chars, min_level=2,
            limit=args.top_n,
        )
        for idx, hits in fallback:
            if idx in already_shown:
                continue
            results.append({
                "source_key": source_key,
                "source_label": src["label"],
                "doc_idx": idx,
                "title": docs[idx]["title"],
                "url": docs[idx]["source_url"],
                "index_score": None,
                "body_hits": hits,
                "body_only": True,
            })

    # Phase 4: rank, applying changelog deprioritise unless opted out
    def _sort_key(r):
        return (
            (0 if args.include_changelog_priority else
             1 if _is_low_priority(r["title"]) else 0),
            -(r["index_score"] or 0),
            -r["body_hits"]["total_matches"],
            r["doc_idx"],
        )
    results.sort(key=_sort_key)
    return results


def _print_search_results(results: list[dict], *, label_source: bool) -> None:
    """Render search results. When *label_source* is True, prefix each entry
    with ``[<source_key>]`` so multi-source output can be disambiguated."""
    for r in results:
        hits = r["body_hits"]
        shown = len(hits["results"])
        src_tag = f"[{r['source_key']}] " if label_source else ""
        score_tag = " [body-only]" if r.get("body_only") else f" (index_score: {r['index_score']})"
        print(f"{src_tag}[{r['doc_idx']}] {r['title']}{score_tag}")
        print(f"    URL: {r['url']}")
        if hits["total_matches"]:
            mode_note = " [partial match]" if hits.get("match_mode") == "partial" else ""
            print(f"    ({hits['total_matches']} body hits, showing {shown}){mode_note}")
            for s in hits["results"]:
                kw_info = f"  keywords: {', '.join(s['matched_keywords'])}" if hits.get("match_mode") == "partial" else ""
                print(f"    Section: {format_heading_path_for_display(s['heading_path'])}  (x{s['hit_count']}){kw_info}")
                for snippet_line in s["snippet"].splitlines():
                    print(f"      {snippet_line}")
                print()
            overflow = hits.get("overflow_sections", [])
            if overflow:
                print(f"    Other sections with hits (not shown):")
                for s in overflow:
                    print(f"      - {format_heading_path_for_display(s['heading_path'])}  (x{s['hit_count']})")
                print()
        else:
            print(f"    (no body hits — index match only)")
        print()


def cmd_search(args):
    """Smart search: rank pages via llms.txt and drill into bodies via llms-full.txt.

    Pages are joined across the two indexes by their normalised ``source_url``
    so the doc_idx returned here is always valid for ``content`` / ``sections``.
    With ``--source both``, ``code`` and ``platform`` are searched sequentially
    and results are grouped under per-source headers. The ``doc_idx`` is only
    unique within a source, so follow-up ``content``/``sections`` calls need an
    explicit ``--source`` matching the bracket label in the output.

    ``--file`` (a local llms-full.txt snapshot) requires a single ``--source``
    — there's no one local file that could stand in for both at once.
    """
    if not args.query.strip():
        die("query must not be empty")

    if args.file is not None and args.source == "both":
        die("--file requires a single --source (not 'both') — "
            "pick --source code or --source platform")

    if args.source == "both":
        source_keys = ["code", "platform"]
    else:
        source_keys = [args.source]

    sources_label = " + ".join(SOURCES[k]["label"] for k in source_keys)
    print(f'Search results for "{args.query}" ({sources_label})')
    print("=" * 60)
    print()

    any_results = False
    for src_key in source_keys:
        results = _search_one_source(args, src_key)
        if len(source_keys) > 1:
            print(f"--- {SOURCES[src_key]['label']} (--source {src_key}) ---")
            print()
        if not results:
            print(f"  (no matching pages in {SOURCES[src_key]['label']})")
            print()
            continue
        any_results = True
        if all(r.get("body_only") for r in results):
            print("  (no title/description match — showing full-body search "
                  "results instead)")
            print()
        _print_search_results(results, label_source=(len(source_keys) > 1))
        print(f"({len(results)} pages from {SOURCES[src_key]['label']})")
        print()

    if not any_results:
        print("No matching pages found.")
        print()
        print("Tip: try broader keywords, switch --source, or run "
              "'search-content \"<query>\"' to search page bodies directly")
        return

    if len(source_keys) > 1:
        print("Note: doc_idx is unique within a source. For follow-up commands, "
              "pass the matching --source <code|platform> explicitly.")
        print()
        next_hint("content", "<page_ref>", '"<heading_path>"',
                  "--source", "<code|platform>")
    else:
        next_hint("content", "<page_ref>", '"<heading_path>"',
                  *(_source_hint_args(args) + corpus_hint_args(args)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_source_arg(parser) -> None:
    parser.add_argument(
        "--source", choices=list(SOURCES.keys()), default=DEFAULT_SOURCE,
        help=f"Documentation source (default: {DEFAULT_SOURCE})",
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
    add_max_age_arg(p_index)
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
    add_max_age_arg(p_sections)
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
    add_max_age_arg(p_content)
    add_max_chars_arg(p_content)
    p_content.add_argument(
        "--no-subsection-hints", action="store_true",
        help="Suppress the subsection hint block printed before/after content",
    )
    p_content.add_argument(
        "--no-link-annotations", action="store_true",
        help="Suppress '→ [doc_idx N]' annotations on in-body links to known docs",
    )
    p_content.set_defaults(func=cmd_content)

    # search-index
    p_search_idx = sub.add_parser(
        "search-index",
        help="Rank pages by keyword (from llms.txt title/description)",
    )
    _add_source_arg(p_search_idx)
    add_cache_dir_arg(p_search_idx)
    add_max_age_arg(p_search_idx)
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
    add_max_age_arg(p_search_body)
    p_search_body.add_argument("--limit", type=int, default=10,
                               help="Max pages to display (default: 10)")
    p_search_body.add_argument("--context", type=int, default=2,
                               help="Context lines around each hit (default: 2)")
    p_search_body.add_argument("--max-hits", type=int, default=5,
                               help="Max hits to display per page (default: 5)")
    add_max_snippet_chars_arg(p_search_body)
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
    # search supports --source both (other commands operate on a single source)
    p_search.add_argument(
        "--source", choices=list(SOURCES.keys()) + ["both"], default=DEFAULT_SOURCE,
        help=f"Documentation source (default: {DEFAULT_SOURCE}; 'both' "
             f"queries code + platform in sequence)",
    )
    _add_file_arg(p_search)
    add_cache_dir_arg(p_search)
    add_max_age_arg(p_search)
    p_search.add_argument(
        "--top-n", type=int, default=5, dest="top_n",
        help="Max candidate pages from index (default: 5)",
    )
    # --index-limit predates the --top-n rename (unifying the name
    # ai-sdk/firebase already used) — kept as a hidden, functioning alias so
    # an existing invocation doesn't silently break.
    p_search.add_argument("--index-limit", type=int, dest="top_n", help=argparse.SUPPRESS)
    p_search.add_argument("--max-hits", type=int, default=3,
                          help="Max body hits per page (default: 3)")
    p_search.add_argument("--context", type=int, default=2,
                          help="Context lines around each hit (default: 2)")
    add_max_snippet_chars_arg(p_search)
    p_search.add_argument(
        "--include-changelog-priority", action="store_true",
        help="Do not deprioritize Changelog / release-notes pages",
    )
    p_search.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
