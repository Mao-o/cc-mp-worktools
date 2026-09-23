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

from dataclasses import dataclass, field

from _common import (
    extract_content,
    extract_sections,
    format_heading_path_for_display,
    next_hint,
    print_metadata_header,
    print_subsection_hints,
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
        page.body_lines, args.heading_path, **content_kwargs,
    )

    if transform is not None:
        content = transform(content)

    hint_suffix = (" " + " ".join(hint_args)) if hint_args else ""
    narrow_hint = f'{script} content {page.idx} "<heading_path>"{hint_suffix}'
    content = truncate_content(content, args.max_chars, narrow_hint=narrow_hint)

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
