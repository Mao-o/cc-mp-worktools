"""Shared command/render layer for the three ``parse-*.py`` scripts.

The three scripts used to carry their own copy of every subcommand
(``sections`` / ``content`` / ``search-content`` / ...), differing only in how
a page is *loaded* (one ``llms-full.txt`` split by H1, one split by
frontmatter, or an ``llms.txt`` index plus one fetched file per page). The
printed output was otherwise the same template, copied three times.

This module keeps the **output template** in one place. Each script keeps a
small adapter that loads its corpus and describes a page as a ``PageView``;
the ``render_*`` functions here print it. Behaviour is byte-for-byte what the
per-script copies printed (verified by an offline output snapshot of every
subcommand across all three scripts when this was introduced).

Wiring rule for ``Next:`` hints (see ``tests/test_hint_wiring.py``): the
renderers never build corpus flags themselves. The calling script composes
``hint_args`` (always including ``corpus_hint_args(args)``, plus
``_source_hint_args(args)`` where the script has sources) and passes it in;
every ``next_hint`` call here forwards ``*hint_args`` unchanged.
"""

import shlex
from dataclasses import dataclass, field

from _common import (
    content_command,
    dropped_query_terms,
    duplicate_heading_note,
    extract_content,
    extract_sections,
    format_heading_path_for_display,
    next_hint,
    print_metadata_header,
    print_subsection_hints,
    query_terms,
    retry_option_args,
    search_in_page_command,
    search_in_page_keyword,
    subsection_children,
    truncate_content,
)


@dataclass
class PageView:
    """One resolved page, described in the terms the renderers need.

    ``header_lines`` are printed verbatim under the ``Sections in [...]``
    title line (``  URL: ...`` / ``  (file: ...)`` / ``  Cache: ...``).
    ``min_level`` is ``None`` for the default heading floor (H2) and ``1`` for
    AI SDK, whose pages use H1 inside the body. ``protect_tables`` is passed to
    ``extract_content`` only when set (``None`` keeps its default).
    ``meta`` holds the keyword arguments for ``print_metadata_header``
    other than the title and heading path (``source=`` / ``tags=``).
    """

    idx: int
    title: str
    body_lines: list
    header_lines: list = field(default_factory=list)
    min_level: int | None = None
    protect_tables: bool | None = None
    meta: dict = field(default_factory=dict)

    def _level_kwargs(self) -> dict:
        return {} if self.min_level is None else {"min_level": self.min_level}

    def _indent_floor(self) -> int:
        return 2 if self.min_level is None else self.min_level


def render_sections(page: PageView, *, hint_args: tuple) -> None:
    """Print the ``sections`` listing for *page*."""
    sections = extract_sections(page.body_lines, **page._level_kwargs())

    print(f'Sections in [{page.idx}] "{page.title}"')
    for line in page.header_lines:
        print(line)
    print("=" * 60)

    floor = page._indent_floor()
    for s in sections:
        indent = "  " * (s["level"] - floor)
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
    next_hint("content", str(page.idx), '"<heading_path>"', *hint_args)


def render_content(page: PageView, args, *, script: str, hint_args: tuple,
                   transform=None) -> None:
    """Print the ``content`` output for *page*.

    *transform*, when given, rewrites the extracted content before
    truncation (Claude docs annotates in-corpus links with page indices).
    *script* is the file name quoted in the truncation hint.
    """
    content_kwargs = page._level_kwargs()
    if page.protect_tables is not None:
        content_kwargs = {"protect_tables": page.protect_tables, **content_kwargs}
    content, resolved_heading_path = extract_content(
        page.body_lines, args.heading_path,
        retry=lambda heading: content_command(
            script, page.idx, heading, retry_option_args(args, "content") + tuple(hint_args)),
        **content_kwargs,
    )

    if transform is not None:
        content = transform(content)

    hint_suffix = (" " + " ".join(hint_args)) if hint_args else ""
    narrow_hint = f'{script} content {page.idx} "<heading_path>"{hint_suffix}'
    # A page or section with no subsections cannot be narrowed by heading: the
    # truncation notice then points at a search inside the page instead.
    # A page with no heading at all is childless too; a page whose headings
    # start below H1 is not (``subsection_children`` takes the shallowest).
    truncate_kwargs = {}
    sections = extract_sections(page.body_lines, **page._level_kwargs())
    found = subsection_children(sections, page.body_lines, resolved_heading_path)
    if found is None or not found[1]:
        target = found[2] if found else None
        title = target["title"] if target else page.title
        next_command = search_in_page_command(script, page.idx, title, hint_args)
        if next_command:
            stand_in = "the section heading" if target else "the page title"
            truncate_kwargs = {
                "next_command": next_command,
                "no_narrow_reason": ("this section has no subsections to narrow to" if target
                                     else "this page has no headings to narrow to"),
                "next_note": (f"To find a line inside it, run the command below with "
                              f"'{search_in_page_keyword(title)}' ({stand_in}, a stand-in) "
                              f"replaced by the term you are looking for:"),
            }
    content = truncate_content(content, args.max_chars, narrow_hint=narrow_hint,
                               **truncate_kwargs)

    print_metadata_header(page.title, heading_path=resolved_heading_path, **page.meta)

    # Printed BEFORE the body too (duplicating the same hint printed after
    # it, below): a long page can exceed the Bash tool's ~30KB inline-
    # output threshold even after --max-chars truncation (e.g. --max-chars
    # 0, or a large metadata header), and the diverted output shows only
    # the first ~2KB — hiding the after-body hint entirely. The subsection
    # list + Next hint are the tool's own documented drill-down path (see
    # SKILL.md Quick Start), so they must survive truncation regardless of
    # where the cut lands.
    if not args.no_subsection_hints:
        print_subsection_hints(page.body_lines, page.idx, resolved_heading_path,
                               extra_hint_args=hint_args, **page._level_kwargs())

    print(content, end="")

    if not args.no_subsection_hints:
        print_subsection_hints(page.body_lines, page.idx, resolved_heading_path,
                               extra_hint_args=hint_args, **page._level_kwargs())


def partial_note(hits: dict) -> str:
    """`` [partial match]`` when the body search fell back to partial matching."""
    return " [partial match]" if hits.get("match_mode") == "partial" else ""


def print_hit_sections(hits: dict, *, anchor_for=None) -> None:
    """Print each matched section of one page: ``Section:`` line + snippet.

    *anchor_for*, when given, maps a section's leaf title to the suffix
    appended to its ``Section:`` line (a `` [https://...#anchor]`` link).
    Callers pass the leaf title, not ``heading_path`` — a heading whose own
    title contains "/" (e.g. "## CI/CD") would otherwise be misread as a
    nested breadcrumb (マージ前レビューの指摘).
    """
    partial = hits.get("match_mode") == "partial"
    for r in hits["results"]:
        kw_info = f"  keywords: {', '.join(r['matched_keywords'])}" if partial else ""
        anchor = anchor_for(r["title"]) if anchor_for is not None else ""
        print(f"    Section: {format_heading_path_for_display(r['heading_path'])}  (x{r['hit_count']}){kw_info}{anchor}")
        for snippet_line in r["snippet"].splitlines():
            print(f"      {snippet_line}")
        print()


def print_overflow_sections(hits: dict) -> None:
    """List the sections with hits that were not shown (per-page cap)."""
    overflow = hits.get("overflow_sections", [])
    if overflow:
        print("    Other sections with hits (not shown):")
        for s in overflow:
            print(f"      - {format_heading_path_for_display(s['heading_path'])}  (x{s['hit_count']})")
        print()


def print_entry(head: str, *, description: str = "", extra_lines=()) -> None:
    """Print one ``fetch-index`` / ``search-index`` row.

    *head* (``[<ref>] <title>``, plus `` (score: N)`` in search results), then
    the description cut to 120 chars, then *extra_lines* verbatim
    (``    URL: ...`` / ``    tags: ...`` / ``    Variants: ...`` / heading
    bullets), then a blank line.
    """
    print(head)
    if description:
        if len(description) > 120:
            description = description[:117] + "..."
        print(f"    {description}")
    for line in extra_lines:
        print(line)
    print()


def print_page_hits(head: str, hits: dict, *, extra_lines=(), noun: str = "page",
                    anchor_for=None, show_overflow: bool = False) -> None:
    """Print one page's block in ``search-content`` output.

    *noun* is the word the script uses for a unit (``"page"`` / ``"document"``).
    *show_overflow* lists the sections with hits beyond the per-page cap
    (Claude docs only; the other scripts never printed that list).
    """
    print(head)
    for line in extra_lines:
        print(line)
    print(f"    ({hits['total_matches']} hits in this {noun}, showing {len(hits['results'])}){partial_note(hits)}")
    print_hit_sections(hits, anchor_for=anchor_for)
    if show_overflow:
        print_overflow_sections(hits)
    print()


def print_search_result(head: str, hits: dict, *, extra_lines=(), anchor_for=None,
                        show_overflow: bool = False) -> None:
    """Print one ranked page's block in ``search`` output (index → body).

    A page that ranked on the index but has no body hits keeps its row,
    marked ``(no body hits — index match only)``.
    """
    print(head)
    for line in extra_lines:
        print(line)
    if hits["total_matches"]:
        print(f"    ({hits['total_matches']} body hits, showing {len(hits['results'])}){partial_note(hits)}")
        print_hit_sections(hits, anchor_for=anchor_for)
        if show_overflow:
            print_overflow_sections(hits)
    else:
        print("    (no body hits — index match only)")
    print()


NEXT_CONTENT_LIMIT = 3


def hit_candidates(ranked, *, limit: int = NEXT_CONTENT_LIMIT, keep=None) -> list:
    """Pick the ``content`` targets to suggest from ranked search results.

    *ranked* is ``[(ref, hits, extra_args), ...]`` in display order: *ref* is
    the page reference the ``content`` command takes, *hits* the page's
    ``search_content_in_body`` result, *extra_args* any per-page option (the
    ``--source`` of that page). Returns ``[(ref, heading_path | None,
    extra_args, heading_count), ...]``, at most *limit*: the best section of
    each top page first, then the next sections of the top page if there is
    room. A page ranked on the index only (no body hits) is offered, without
    a heading, only when no page has a body hit. *heading_count* is how many
    sections of the page share that heading_path (1 when unique or unknown).

    *keep* is ``(ref, extra_args)`` of a page whose best section must stay
    among the picks when it has one (``search`` passes its top index
    candidate, so pages appended from the full-corpus search above it cannot
    take every line). It replaces the last pick when there is no room.
    """
    firsts = []
    spare = []
    index_only = []
    for ref, hits, extra in ranked:
        sections = hits.get("results") or []
        if not sections:
            index_only.append((ref, None, extra, 1))
            continue
        firsts.append((ref, sections[0]["heading_path"], extra,
                       sections[0].get("heading_count", 1)))
        spare += [(ref, r["heading_path"], extra, r.get("heading_count", 1))
                  for r in sections[1:]]
    # A page that only ranked on the index is worth suggesting only when no
    # page has a body hit to point at.
    picked = (firsts or index_only)[:limit]
    if keep is not None and limit > 0 and not any((p[0], p[2]) == keep for p in picked):
        kept = next((c for c in firsts if (c[0], c[2]) == keep), None)
        if kept is not None:
            picked = picked[:limit - 1] + [kept]
    if len(picked) < limit and picked:
        top_ref = picked[0][0]
        # A repeated heading_path hits more than once, but every copy prints
        # the same command; offer it once.
        for c in spare:
            if len(picked) >= limit:
                break
            if c[0] == top_ref and all(c[1] != p[1] for p in picked if p[0] == top_ref):
                picked.append(c)
    return picked


def dropped_terms_note(query: str) -> None:
    """Print one line naming the function words *query* was searched
    without (``dropped_query_terms``); nothing when none was dropped."""
    dropped = dropped_query_terms(query)
    if dropped:
        print(f"(not searched, too common: {', '.join(dropped)})")
        print()


def render_next_content(candidates: list, *, hint_args: tuple,
                        query: str | None = None) -> None:
    """Print up to three ``Next: ... content <page> "<heading>"`` lines, filled
    in from the search results just shown (no placeholders to copy by hand).
    With *query*, the function words it was searched without are named first
    (``dropped_terms_note``).

    Falls back to the generic placeholder hint when *candidates* is empty.
    Each candidate is ``(ref, heading_path | None, extra_args,
    heading_count)``; *extra_args* goes before *hint_args* (a per-page
    ``--source``). A heading_path the page has more than once gets a note
    after the command: the command reads the first, which may not be the
    section that matched.
    """
    if query is not None:
        dropped_terms_note(query)
    if not candidates:
        next_hint("content", "<page_ref>", '"<heading_path>"', *hint_args)
        return
    for ref, heading, extra, count in candidates[:NEXT_CONTENT_LIMIT]:
        heading_args = (shlex.quote(heading),) if heading is not None else ()
        note = duplicate_heading_note(count)
        next_hint("content", str(ref), *heading_args, *extra, *hint_args,
                  *((note.strip(),) if note else ()))


def term_page_counts(texts, query: str) -> tuple:
    """``([(term, pages_containing_it), ...], pages_checked)`` for *query*.

    Terms are the keywords ``search_content_in_body`` uses (``query_terms``:
    function words dropped), matched the same way (case-insensitive
    substring). *texts* yields one string (or list of lines) per page.
    """
    terms = [t.lower() for t in query_terms(query)]
    counts = {t: 0 for t in terms}
    checked = 0
    for text in texts:
        if not isinstance(text, str):
            text = "".join(text)
        text = text.lower()
        checked += 1
        for t in terms:
            if t in text:
                counts[t] += 1
    return [(t, counts[t]) for t in terms], checked


def render_zero_hits(query: str, texts, *, subcommand: str, hint_args: tuple,
                     scope: str = "pages", restricted_to=None,
                     restricted_only: bool = False, alt_hint_args=None,
                     index_hint_args=None) -> None:
    """Explain an empty search result and print the next commands to try.

    Shows how many pages contain each term, so "the word is not in these
    docs" can be told apart from "the words never appear together in one
    section" (search needs half or more of the terms in the same section) and
    from "``--page-ref`` cut it out" (*restricted_to* is the page reference
    when the search was limited to one page; *restricted_only* says *texts*
    holds just that page too). *scope* names what *texts*
    holds (``"pages"`` or ``"index entries (title/description)"``);
    *subcommand* is the command that came back empty. *alt_hint_args*, when
    given, is the full option tuple of another source to try the same query on.
    *index_hint_args* is the option tuple for the ``search-index`` hint; when
    ``None`` that hint is not printed (claude-docs' ``search-index`` has no
    ``--file``, so with ``--file`` there is no ``search-index`` to point at).
    """
    counts, checked = term_page_counts(texts, query)
    if not counts:
        return
    print(f"Why nothing matched: {checked} {scope} checked, "
          f"how many contain each term:")
    for term, n in counts:
        note = "  <- not in this corpus" if n == 0 else ""
        print(f'  "{term}": {n} of {checked}{note}')
    dropped = dropped_query_terms(query)
    if dropped:
        print(f"  (not searched, too common: {', '.join(dropped)})")
    present = [t for t, n in counts if n]
    absent = [t for t, n in counts if not n]
    reduced = None
    if restricted_only:
        # Only the --page-ref page was loaded (Firebase fetches lazily), so
        # the counts say nothing about the other pages.
        print(f"The counts cover only the --page-ref {restricted_to} page; "
              f"other pages were not fetched. Drop --page-ref to search them all.")
        restricted_to = None
    elif not present:
        index = "'search-index' (titles / descriptions), " if index_hint_args is not None else ""
        print("None of the terms appears anywhere, so rewording the same idea "
              f"is unlikely to help; try a different word, {index}or another source.")
    elif absent:
        print(f"Terms with 0 pages are not in this corpus: {', '.join(absent)}. "
              f"Drop them.")
        reduced = " ".join(present)
    elif len(present) > 1:
        rarest = min(counts, key=lambda c: c[1])[0]
        print("Every term appears, but not together in one section. Use fewer "
              "terms; the rarest one is the most specific.")
        reduced = rarest
    else:
        print("The term appears, but nothing in the targeted pages matched it.")
    if restricted_to is not None:
        print(f"(--page-ref {restricted_to} limited the search to one page; "
              f"drop it to search the whole corpus.)")
    if reduced is not None and reduced != query.strip().lower():
        next_hint(subcommand, shlex.quote(reduced), *hint_args)
    if subcommand != "search-index" and index_hint_args is not None:
        next_hint("search-index", shlex.quote(query), *index_hint_args)
    if alt_hint_args is not None:
        next_hint(subcommand, shlex.quote(query), *alt_hint_args)
