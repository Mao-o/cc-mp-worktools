#!/usr/bin/env python3
"""Progressive loader for any site's ``llms-full.txt``, described by a profile.

The three dedicated loaders (``parse-claude-docs.py`` / ``parse-ai-sdk.py`` /
``parse-firebase.py``) hard-code one site each. This one reads the site from a
user-maintained ``sources.json`` and supports the page shapes seen in the wild
(measured 2026-09-26, see ``docs/generic-llms-txt-source.md``):

  split "h1"           one page per H1 outside code fences      (Zod)
  split "frontmatter"  one page per YAML frontmatter block      (Next.js / Vite / Vitest)
  split "line"         one page per line starting with a prefix (Drizzle: ``Source: <url>``)

Subcommands mirror the other loaders (``fetch-index`` / ``search-index`` /
``search-content`` / ``search`` / ``sections`` / ``content``) plus ``sources``
to list the configured profiles. Every subcommand except ``sources`` requires
``--source <name>``.

Out of scope (use a dedicated loader or WebFetch): two-level indexes whose
``llms.txt`` only links to more ``llms.txt`` files (Cloudflare), sites that
publish one file per page, and joining an ``llms.txt`` index against the full
text. Only the single ``llms-full.txt`` named in the profile is fetched.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _common import (  # noqa: E402
    FenceTracker,
    add_cache_dir_arg,
    add_heading_path_arg,
    add_include_changelog_priority_arg,
    add_max_age_arg,
    add_max_chars_arg,
    add_max_snippet_chars_arg,
    assert_parsed,
    corpus_hint_args,
    die,
    die_index_out_of_range,
    fetch_url,
    full_corpus_body_search,
    load_lines,
    next_hint,
    search_content_in_body,
    search_index_entries,
    search_rank_key,
)
from _commands import (  # noqa: E402
    PageView,
    print_entry,
    print_page_hits,
    print_search_result,
    render_content,
    render_sections,
)

SCRIPT = "parse-llms-txt.py"
USER_AGENT = "claude-code-llms-docs-generic/1.0"
SOURCES_ENV = "LLMS_DOCS_SOURCES_FILE"
README_HINT = "see plugins/llms-docs/README.md「任意の llms-full.txt を読む」"

# ---------------------------------------------------------------------------
# sources.json
# ---------------------------------------------------------------------------

# The name becomes part of the cache file name, so it is restricted to a
# plain slug (no path separators, no leading dot).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SPLITS = ("h1", "frontmatter", "line")
_PROFILE_KEYS = {"url", "split", "frontmatter_key", "line_prefix", "page_url", "url_base", "description"}
_FM_KEY_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def default_sources_file() -> str:
    """``$LLMS_DOCS_SOURCES_FILE`` > ``$XDG_CONFIG_HOME/llms-docs/sources.json``
    > ``~/.config/llms-docs/sources.json``.

    A config file, not a cache: it lives under the config dir so that
    clearing the download cache does not delete the user's profiles. A
    relative ``$XDG_CONFIG_HOME`` is ignored per the XDG spec.
    """
    override = os.environ.get(SOURCES_ENV)
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        expanded = os.path.expanduser(xdg)
        if os.path.isabs(expanded):
            return os.path.join(expanded, "llms-docs", "sources.json")
    return os.path.join(os.path.expanduser("~/.config"), "llms-docs", "sources.json")


def _bad(path: str, msg: str) -> None:
    die(f"{path}: {msg} ({README_HINT})")


def _parse_page_url(path: str, name: str, value) -> tuple[str, str] | None:
    """``"none"`` / ``"frontmatter:<key>"`` / ``"line:<prefix>"`` -> (kind, arg)."""
    if value is None or value == "none":
        return None
    if not isinstance(value, str) or ":" not in value:
        _bad(path, f'sources.{name}.page_url must be "none", "frontmatter:<key>" or "line:<prefix>"')
    kind, arg = value.split(":", 1)
    if kind == "frontmatter" and _FM_KEY_NAME_RE.match(arg):
        return ("frontmatter", arg)
    if kind == "line" and arg.strip():
        return ("line", arg)
    _bad(path, f'sources.{name}.page_url must be "none", "frontmatter:<key>" or "line:<prefix>"')
    return None


def _validate_profile(path: str, name: str, raw) -> dict:
    if not _NAME_RE.match(name):
        _bad(path, f"source name {name!r} must match {_NAME_RE.pattern}")
    if not isinstance(raw, dict):
        _bad(path, f"sources.{name} must be an object")
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        _bad(path, f"sources.{name} has unknown keys: {', '.join(sorted(unknown))}")
    url = raw.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
        _bad(path, f"sources.{name}.url must be an http(s) URL of the site's llms-full.txt")
    split = raw.get("split")
    if split not in _SPLITS:
        _bad(path, f"sources.{name}.split must be one of: {', '.join(_SPLITS)}")
    profile = {
        "name": name,
        "url": url,
        "split": split,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
        "frontmatter_key": "title",
        "line_prefix": None,
        "page_url": _parse_page_url(path, name, raw.get("page_url")),
        "url_base": None,
    }
    if split == "frontmatter":
        key = raw.get("frontmatter_key", "title")
        if not isinstance(key, str) or not _FM_KEY_NAME_RE.match(key):
            _bad(path, f"sources.{name}.frontmatter_key must be a YAML key name")
        profile["frontmatter_key"] = key
    elif "frontmatter_key" in raw:
        _bad(path, f'sources.{name}.frontmatter_key is only valid with split "frontmatter"')
    if split == "line":
        prefix = raw.get("line_prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            _bad(path, f'sources.{name}.line_prefix is required with split "line" (e.g. "Source:")')
        profile["line_prefix"] = prefix
        if "page_url" not in raw:
            # The delimiter line carries the page URL (Drizzle's ``Source: <url>``).
            profile["page_url"] = ("line", prefix)
    elif "line_prefix" in raw:
        _bad(path, f'sources.{name}.line_prefix is only valid with split "line"')
    if profile["page_url"] and profile["page_url"][0] == "frontmatter" and split != "frontmatter":
        _bad(path, f'sources.{name}.page_url "frontmatter:<key>" is only valid with split "frontmatter"')
    base = raw.get("url_base")
    if base is not None:
        if not isinstance(base, str) or not re.match(r"^https?://\S+$", base):
            _bad(path, f"sources.{name}.url_base must be an http(s) URL")
        profile["url_base"] = base
    return profile


def load_sources(path: str) -> dict:
    """Read and validate *path*. Missing file / bad JSON / bad profile -> die."""
    if not os.path.exists(path):
        die(
            f"no sources file at {path}. Create it with at least one profile, "
            f"or point --sources-file / ${SOURCES_ENV} at one ({README_HINT})"
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        _bad(path, f"cannot read as JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        _bad(path, 'top level must be {"sources": {"<name>": {...}}}')
    unknown = set(data) - {"sources"}
    if unknown:
        _bad(path, f"unknown top-level keys: {', '.join(sorted(unknown))}")
    return {name: _validate_profile(path, name, raw) for name, raw in data["sources"].items()}


def _get_profile(args) -> dict:
    sources = load_sources(args.sources_file)
    if args.source not in sources:
        known = ", ".join(sorted(sources)) or "(none)"
        die(f"unknown --source {args.source!r}. Configured: {known}")
    return sources[args.source]


# ---------------------------------------------------------------------------
# Splitters. Each returns [{"title", "url", "body_lines", "min_level"}].
# ---------------------------------------------------------------------------

_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _first_heading(body_lines: list[str]) -> str:
    fence = FenceTracker()
    for line in body_lines:
        was = fence.in_fence
        fence.update(line)
        if was or fence.in_fence:
            continue
        m = _HEADING_RE.match(line.rstrip("\n\r"))
        if m:
            return m.group(2).strip()
    return ""


def _line_url(body_lines: list[str], prefix: str, limit: int = 10) -> str:
    """URL on the first ``<prefix> <url>`` line among the first *limit* lines."""
    for line in body_lines[:limit]:
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value:
                return value.split()[0]
    return ""


def split_h1(lines: list[str], profile: dict) -> list[dict]:
    docs: list[dict] = []
    fence = FenceTracker()
    title = None
    start = 0
    for i, line in enumerate(lines):
        was = fence.in_fence
        fence.update(line)
        if was or fence.in_fence:
            continue
        m = _H1_RE.match(line.rstrip("\n\r"))
        if not m:
            continue
        if title is not None:
            docs.append({"title": title, "body_lines": lines[start:i]})
        title = m.group(1).strip()
        start = i + 1
    if title is not None:
        docs.append({"title": title, "body_lines": lines[start:]})
    rule = profile["page_url"]
    for d in docs:
        d["url"] = _line_url(d["body_lines"], rule[1]) if rule and rule[0] == "line" else ""
        d["min_level"] = None  # the H1 is the delimiter, so sections start at H2
    return docs


_FM_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_FM_LIST_RE = re.compile(r"^\s*- ")
_FM_CONTINUATION_RE = re.compile(r"^\s+\S")
_FM_LOOKAHEAD = 30


def _frontmatter_at(lines: list[str], pos: int, required_key: str) -> tuple[dict, int] | None:
    """Parse a frontmatter block opening at *pos*; return (fields, end) or None.

    A ``---`` opens a block only when a closing ``---`` follows within
    ``_FM_LOOKAHEAD`` lines, every line in between is YAML-shaped (``key:
    value`` / ``- item`` / indented continuation / blank), and *required_key*
    is among the keys. A Markdown horizontal rule followed by prose (even
    prose that starts with ``Note:``) is therefore not a page boundary.
    """
    if lines[pos].rstrip("\n\r") != "---":
        return None
    fields: dict = {}
    for j in range(pos + 1, min(pos + _FM_LOOKAHEAD, len(lines))):
        line = lines[j].rstrip("\n\r")
        if line == "---":
            return (fields, j + 1) if required_key in fields else None
        if not line.strip() or _FM_LIST_RE.match(line) or _FM_CONTINUATION_RE.match(line):
            continue
        m = _FM_KEY_RE.match(line)
        if not m:
            return None
        fields[m.group(1)] = m.group(2).strip().strip("'\"")
    return None


def split_frontmatter(lines: list[str], profile: dict) -> list[dict]:
    key = profile["frontmatter_key"]
    blocks: list[tuple[dict, int, int]] = []  # (fields, block_start, body_start)
    fence = FenceTracker()
    i = 0
    while i < len(lines):
        if not fence.in_fence:
            hit = _frontmatter_at(lines, i, key)
            if hit is not None:
                blocks.append((hit[0], i, hit[1]))
                i = hit[1]
                continue
        fence.update(lines[i])
        i += 1
    rule = profile["page_url"]
    docs = []
    for k, (fields, _start, body_start) in enumerate(blocks):
        end = blocks[k + 1][1] if k + 1 < len(blocks) else len(lines)
        body = lines[body_start:end]
        url = ""
        if rule and rule[0] == "frontmatter":
            url = fields.get(rule[1], "")
        elif rule and rule[0] == "line":
            url = _line_url(body, rule[1])
        docs.append({
            "title": fields.get("title") or _first_heading(body),
            "description": fields.get("description", ""),
            "url": url,
            "body_lines": body,
            "min_level": 1,
        })
    return docs


_URLISH_RE = re.compile(r"^(https?://|/)\S*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")


def split_line(lines: list[str], profile: dict) -> list[dict]:
    """One page per ``<prefix><url>`` line outside a code fence.

    Fences are tracked here with their own rules instead of ``_common``'s
    ``FenceTracker``, because MDX-heavy corpora break that tracker (measured
    on Drizzle: it loses 299 of 496 page boundaries). Two deviations from
    it, both matching how these corpora are actually written:

    - a line with an info string (```` ```ts ````) never *closes* a fence
      (CommonMark rule; ``FenceTracker`` treats it as a closer, which is
      where most of the losses came from)
    - a closer may be indented any amount (code blocks nested in JSX close
      with an indented ```` ``` ````)

    Fence state is also reset at every accepted delimiter: a page boundary
    never sits inside a fence, so one malformed block cannot hide the rest
    of the corpus. A delimiter must be the prefix followed by a single URL
    (absolute, or a ``/path``), which prose practically never produces.
    """
    prefix = profile["line_prefix"]
    starts: list[int] = []
    fence: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        text = line.rstrip("\n\r")
        if fence is None:
            if text.startswith(prefix) and _URLISH_RE.match(text[len(prefix):].strip()):
                starts.append(i)
                continue
            m = _FENCE_OPEN_RE.match(text)
            # a backtick fence's info string may not contain backticks (inline code)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = (m.group(1)[0], len(m.group(1)))
        else:
            m = _FENCE_CLOSE_RE.match(text)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
    docs = []
    for k, s in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        delimiter_url = lines[s][len(prefix):].strip()
        body = lines[s + 1:end]
        # The delimiter always splits; the published URL follows page_url
        # (the default for split "line" is the delimiter's own URL).
        rule = profile["page_url"]
        if rule is None:
            url = ""
        elif rule[1] == prefix:
            url = delimiter_url
        else:
            url = _line_url(body, rule[1])
        title = _first_heading(body) or delimiter_url.rstrip("/").rsplit("/", 1)[-1] or "(untitled)"
        docs.append({"title": title, "url": url, "body_lines": body, "min_level": 1})
    return docs


_SPLITTERS = {"h1": split_h1, "frontmatter": split_frontmatter, "line": split_line}


def split_documents(lines: list[str], profile: dict) -> list[dict]:
    docs = _SPLITTERS[profile["split"]](lines, profile)
    base = profile["url_base"]
    for d in docs:
        d.setdefault("description", "")
        if d["url"] and base and not re.match(r"^https?://", d["url"]):
            d["url"] = urljoin(base, d["url"])
    return docs


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: str, profile: dict) -> str:
    """Cache file for *profile*. The URL is part of the name: reusing a
    source name for another URL (an edited profile, or a second sources
    file) must not serve the previous site's cached corpus under the new
    name until ``--max-age`` expires."""
    digest = hashlib.sha256(profile["url"].encode("utf-8")).hexdigest()[:12]
    return os.path.join(cache_dir, f"generic-{profile['name']}-{digest}-llms-full.txt")


def _load_docs(args) -> tuple[dict, str, list[dict]]:
    """Resolve the profile, fetch (or read ``--file``), and split."""
    profile = _get_profile(args)
    if args.file is None:
        path = fetch_url(
            profile["url"], _cache_path(args.cache_dir, profile),
            user_agent=USER_AGENT, timeout=120, max_age=args.max_age,
        )
    else:
        if not os.path.exists(args.file):
            die(f"--file '{args.file}' does not exist. Drop --file to auto-fetch to the cache.")
        path = args.file
    docs = split_documents(load_lines(path), profile)
    assert_parsed(profile["name"], len(docs), path)
    return profile, path, docs


def _source_hint_args(args) -> tuple:
    """``--source`` is always required here, so it is always echoed; a
    non-default ``--sources-file`` is echoed too (else the hint would look
    the source up in a different file)."""
    out = ["--source", shlex.quote(args.source)]
    if args.sources_file != default_sources_file():
        out += ["--sources-file", shlex.quote(args.sources_file)]
    return tuple(out)


def _resolve_page_ref(docs: list[dict], page_ref: str) -> int:
    """Integer index, else a unique title substring, else a unique URL substring."""
    if not page_ref:
        die("page_ref required: integer index, title substring or URL substring")
    try:
        idx = int(page_ref)
    except ValueError:
        pass
    else:
        if 0 <= idx < len(docs):
            return idx
        die_index_out_of_range(idx, len(docs))
    needle = page_ref.lower()
    for field_name in ("title", "url"):
        found = [(i, d) for i, d in enumerate(docs) if d[field_name] and needle in d[field_name].lower()]
        if len(found) == 1:
            return found[0][0]
        if len(found) > 1:
            detail = "\n  ".join(f"[{i}] {d['title']}" + (f" ({d['url']})" if d["url"] else "") for i, d in found[:20])
            die(f"Ambiguous {field_name} substring '{page_ref}'. Matches:\n  {detail}")
    die(f"No document found for: {page_ref}")
    return -1


def _headings(body_lines: list[str]) -> list[str]:
    fence = FenceTracker()
    out = []
    for line in body_lines:
        was = fence.in_fence
        fence.update(line)
        if was or fence.in_fence:
            continue
        m = re.match(r"^(#{1,2})\s+(.+)", line)
        if m:
            out.append(m.group(2).strip())
    return out


def _url_line(doc: dict) -> list[str]:
    return [f"    url: {doc['url']}"] if doc["url"] else []


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_sources(args):
    sources = load_sources(args.sources_file)
    print(f"Configured sources (file: {args.sources_file})")
    print("=" * 60)
    for name, p in sorted(sources.items()):
        print(f"{name}  [{p['split']}]  {p['url']}")
        if p["description"]:
            print(f"    {p['description']}")
    print()
    print(f"({len(sources)} sources)")


def cmd_fetch_index(args):
    profile, path, docs = _load_docs(args)
    print(f"{profile['name']} llms-full.txt Document Index (file: {path})")
    print("=" * 60)
    print()
    for i, d in enumerate(docs):
        if args.compact:
            print(f"[{i}] {d['title'] or '(untitled)'}")
        else:
            print_entry(f"[{i}] {d['title'] or '(untitled)'}", description=d["description"],
                        extra_lines=_url_line(d))
    print()
    print(f"({len(docs)} documents total)")
    print()
    next_hint("sections", "<page_ref>", *(_source_hint_args(args) + corpus_hint_args(args)))


def _page_view(args) -> PageView:
    _profile, path, docs = _load_docs(args)
    idx = _resolve_page_ref(docs, args.page_ref)
    d = docs[idx]
    header = [f"  URL: {d['url']}"] if d["url"] else []
    header.append(f"  (file: {path})")
    return PageView(
        idx=idx,
        title=d["title"] or "(untitled)",
        body_lines=d["body_lines"],
        header_lines=header,
        min_level=d["min_level"],
        meta={"source": d["url"] or None},
    )


def cmd_sections(args):
    render_sections(_page_view(args), hint_args=_source_hint_args(args) + corpus_hint_args(args))


def cmd_content(args):
    render_content(_page_view(args), args, script=SCRIPT,
                   hint_args=_source_hint_args(args) + corpus_hint_args(args))


def _ranked(args, docs: list[dict], limit: int):
    entries = [{"title": d["title"] or "", "description": d["description"] or ""} for d in docs]
    headings = [_headings(d["body_lines"]) for d in docs]
    return search_index_entries(
        entries, args.query, limit=limit,
        get_extras=lambda _e, idx: {"tags": [], "headings": headings[idx]},
    ), headings


def cmd_search_index(args):
    profile, path, docs = _load_docs(args)
    if not args.query.strip():
        die("query must not be empty")
    scored, headings = _ranked(args, docs, args.limit)
    print(f'Search-index results for "{args.query}" (source: {profile["name"]}, file: {path})')
    print("=" * 60)
    print()
    if not scored:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords, 'search' for an automatic full-text fallback, "
              "or 'search-content' to search bodies directly")
    for score, idx, _entry in scored:
        d = docs[idx]
        extra = _url_line(d)
        if args.show_sections:
            extra += [f"      - {h}" for h in headings[idx]]
        print_entry(f"[{idx}] {d['title'] or '(untitled)'} (score: {score})",
                    description=d["description"], extra_lines=extra)
    print(f"({len(scored)} results, {len(docs)} documents searched)")
    print()
    next_hint("search", '"<query>"', *(_source_hint_args(args) + corpus_hint_args(args)))


def _body_hits(args, d: dict):
    return search_content_in_body(
        d["body_lines"], args.query, context_lines=args.context,
        max_matches_per_doc=args.max_hits, min_level=d["min_level"] or 2,
        max_snippet_chars=args.max_snippet_chars,
    )


def cmd_search_content(args):
    profile, path, docs = _load_docs(args)
    if not args.query.strip():
        die("query must not be empty")
    targets = [_resolve_page_ref(docs, args.page_ref)] if args.page_ref is not None else range(len(docs))
    print(f'Search-content results for "{args.query}" (source: {profile["name"]}, file: {path})')
    print("=" * 60)
    print()
    total = matched = printed = 0
    for idx in targets:
        d = docs[idx]
        hits = _body_hits(args, d)
        if hits["total_matches"] == 0:
            continue
        total += hits["total_matches"]
        matched += 1
        if printed >= args.limit:
            continue
        printed += 1
        print_page_hits(f"[{idx}] {d['title'] or '(untitled)'}", hits, noun="document", extra_lines=_url_line(d))
    if total == 0:
        print("No matching content found.")
        print()
        print("Tip: try broader keywords or 'search-index' to find relevant documents first")
    else:
        print(f"({total} hits across {matched} documents, showing top {printed})")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"', *(_source_hint_args(args) + corpus_hint_args(args)))


def cmd_search(args):
    profile, path, docs = _load_docs(args)
    if not args.query.strip():
        die("query must not be empty")
    scored, _headings_unused = _ranked(args, docs, args.top_n)
    print(f'Search results for "{args.query}" (source: {profile["name"]}, file: {path})')
    print(f"  (top-{args.top_n} candidate docs drilled into bodies)")
    print("=" * 60)
    print()
    results = [
        {"doc_idx": idx, "index_score": score, "body_hits": _body_hits(args, docs[idx]), "body_only": False}
        for score, idx, _entry in scored
    ]
    if not any(r["body_hits"]["total_matches"] for r in results):
        shown = {r["doc_idx"] for r in results}
        # full_corpus_body_search takes one min_level for the whole corpus;
        # every page of a profile shares its split, so the first page's is used.
        level = (docs[0]["min_level"] or 2) if docs else 2
        for idx, hits in full_corpus_body_search(
            [d["body_lines"] for d in docs], args.query, context_lines=args.context,
            max_matches_per_doc=args.max_hits, max_snippet_chars=args.max_snippet_chars,
            min_level=level, limit=args.top_n,
        ):
            if idx not in shown:
                results.append({"doc_idx": idx, "index_score": None, "body_hits": hits, "body_only": True})
    if not results:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords or 'search-content' for a full-body scan")
        return
    if all(r["body_only"] for r in results):
        print("  (no title/description match — showing full-body search results instead)")
        print()
    for r in results:
        r["title"] = docs[r["doc_idx"]]["title"] or "(untitled)"
    results.sort(key=lambda r: search_rank_key(r, include_changelog_priority=args.include_changelog_priority))
    for r in results:
        tag = " [body-only]" if r["body_only"] else f" (index_score: {r['index_score']})"
        print_search_result(f"[{r['doc_idx']}] {r['title']}{tag}", r["body_hits"],
                            extra_lines=_url_line(docs[r["doc_idx"]]))
    print(f"({len(results)} documents, ranked via index → body)")
    print()
    next_hint("content", "<page_ref>", '"<heading_path>"', *(_source_hint_args(args) + corpus_hint_args(args)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common(parser, *, source: bool = True) -> None:
    parser.add_argument(
        "--sources-file", default=default_sources_file(),
        help=f"Profiles file (default: ${SOURCES_ENV} or ~/.config/llms-docs/sources.json)",
    )
    if not source:
        return
    parser.add_argument("--source", required=True, help="Profile name in the sources file")
    parser.add_argument("--file", default=None,
                        help="Read a local llms-full.txt instead of fetching the profile's url")
    add_cache_dir_arg(parser)
    add_max_age_arg(parser)


def main():
    parser = argparse.ArgumentParser(description="Progressive loader for any llms-full.txt described in sources.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sources", help="List configured source profiles")
    _add_common(p, source=False)
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("fetch-index", help="Fetch and print the document index")
    _add_common(p)
    p.add_argument("--compact", action="store_true", help="One title per line")
    p.set_defaults(func=cmd_fetch_index)

    p = sub.add_parser("search-index", help="Rank documents by keyword (title/description/headings)")
    p.add_argument("query", help="Space-separated keywords (AND search)")
    _add_common(p)
    p.add_argument("--limit", type=int, default=15, help="Max results (default: 15)")
    p.add_argument("--show-sections", action="store_true", help="Show H1/H2 headings for each result")
    p.set_defaults(func=cmd_search_index)

    p = sub.add_parser("search-content", help="Keyword search across document bodies")
    p.add_argument("query", help="Space-separated keywords (AND search)")
    p.add_argument("--page-ref", default=None, help="Restrict to one document")
    _add_common(p)
    p.add_argument("--limit", type=int, default=10, help="Max documents to display (default: 10)")
    p.add_argument("--context", type=int, default=2, help="Context lines around each hit (default: 2)")
    p.add_argument("--max-hits", type=int, default=5, help="Max hits per document (default: 5)")
    add_max_snippet_chars_arg(p)
    p.set_defaults(func=cmd_search_content)

    p = sub.add_parser("sections", help="List sections in a document")
    p.add_argument("page_ref", help="Integer index, title substring or URL substring")
    _add_common(p)
    p.set_defaults(func=cmd_sections)

    p = sub.add_parser("content", help="Print document/section content")
    p.add_argument("page_ref", help="Integer index, title substring or URL substring")
    add_heading_path_arg(p)
    _add_common(p)
    add_max_chars_arg(p)
    p.add_argument("--no-subsection-hints", action="store_true",
                   help="Suppress the subsection hint block printed before/after content")
    p.set_defaults(func=cmd_content)

    p = sub.add_parser("search", help="Smart search: rank documents and drill into the top N bodies")
    p.add_argument("query", help="Space-separated keywords (AND search)")
    _add_common(p)
    p.add_argument("--top-n", type=int, default=5, help="Candidate documents to drill into (default: 5)")
    p.add_argument("--max-hits", type=int, default=3, help="Max body hits per document (default: 3)")
    p.add_argument("--context", type=int, default=2, help="Context lines around each hit (default: 2)")
    add_max_snippet_chars_arg(p)
    add_include_changelog_priority_arg(p)
    p.set_defaults(func=cmd_search)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
