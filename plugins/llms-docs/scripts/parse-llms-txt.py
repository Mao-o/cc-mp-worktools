#!/usr/bin/env python3
"""Progressive loader for any site's ``llms-full.txt``, described by a profile.

The three dedicated loaders (``parse-claude-docs.py`` / ``parse-ai-sdk.py`` /
``parse-firebase.py``) hard-code one site each. This one reads the site from a
profile: the presets bundled in ``presets.json`` next to this script, overlaid
by the user's ``sources.json`` (a user profile replaces a preset of the same
name). It supports the page shapes seen in the wild (measured 2026-09-26 and
2026-10-03, see ``docs/generic-llms-txt-source.md``):

  split "h1"           one page per H1 outside code fences      (Zod / Hono / Agent Skills)
  split "frontmatter"  one page per YAML frontmatter block      (Next.js / Vite / Vitest /
                                                                 Cloudflare per-product files)
  split "line"         one page per line starting with a prefix (Drizzle: ``Source: <url>``)

Subcommands mirror the other loaders (``fetch-index`` / ``search-index`` /
``search-content`` / ``search`` / ``sections`` / ``content``) plus ``sources``
to list the configured profiles. Every subcommand except ``sources`` requires
``--source <name>``.

Out of scope (use a dedicated loader or WebFetch): two-level indexes whose
``llms.txt`` only links to more ``llms.txt`` files (Cloudflare's root; its
per-product ``/<product>/llms-full.txt`` files are supported), sites that
publish one file per page, and joining an ``llms.txt`` index against the full
text other than by exact title. Only the profile's ``url`` (the
``llms-full.txt``) and, when set, its ``index_url`` (the ``llms.txt``, used
to give URL-less pages a URL by exact title) are fetched. A two-level index
is not a usable ``index_url`` either: its entries that link another
``llms.txt`` file are skipped, but a page that shares its title with an entry
for a page of another docs set still gets that entry's URL.
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
    FetchError,
    add_cache_dir_arg,
    add_heading_path_arg,
    add_include_changelog_priority_arg,
    add_max_age_arg,
    add_max_chars_arg,
    add_max_snippet_chars_arg,
    assert_parsed,
    corpus_hint_args,
    die,
    die_ambiguous_page,
    die_index_out_of_range,
    fetch_url,
    full_corpus_extra_hits,
    load_lines,
    next_hint,
    parse_llms_index,
    retry_for_page_ref,
    search_content_in_body,
    search_content_rank_key,
    search_index_entries,
    search_rank_key,
)
from _commands import (  # noqa: E402
    PageView,
    hit_candidates,
    print_entry,
    print_page_hits,
    print_search_result,
    render_content,
    render_next_content,
    render_sections,
    render_zero_hits,
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
_PROFILE_KEYS = {
    "url", "split", "frontmatter_key", "line_prefix", "page_url", "url_base", "description",
    "drop_lines", "skip_empty", "frontmatter_delimiter", "h1_needs_url", "index_url",
}
PRESETS_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "presets.json")
_PAGE_URL_FORMS = '"none", "frontmatter:<key>", "line:<prefix>" or "link:<link text>"'
_FM_KEY_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_FM_DELIM_RE = re.compile(r"^(-{3,}|\+{3,})$")


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
    """``"none"`` / ``"frontmatter:<key>"`` / ``"line:<prefix>"`` /
    ``"link:<link text>"`` -> (kind, arg)."""
    if value is None or value == "none":
        return None
    if not isinstance(value, str) or ":" not in value:
        _bad(path, f"sources.{name}.page_url must be {_PAGE_URL_FORMS}")
    kind, arg = value.split(":", 1)
    if kind == "frontmatter" and _FM_KEY_NAME_RE.match(arg):
        return ("frontmatter", arg)
    if kind in ("line", "link") and arg.strip():
        return (kind, arg)
    _bad(path, f"sources.{name}.page_url must be {_PAGE_URL_FORMS}")
    return None


def _parse_drop_lines(path: str, name: str, value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        _bad(path, f"sources.{name}.drop_lines must be a list of regular expressions")
    out = []
    for v in value:
        try:
            out.append(re.compile(v))
        except re.error as e:
            _bad(path, f"sources.{name}.drop_lines has an invalid regular expression {v!r}: {e}")
    return out


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
    if not isinstance(raw.get("description", ""), str):
        _bad(path, f"sources.{name}.description must be a string")
    split = raw.get("split")
    if split not in _SPLITS:
        _bad(path, f"sources.{name}.split must be one of: {', '.join(_SPLITS)}")
    profile = {
        "name": name,
        "url": url,
        "split": split,
        "description": raw.get("description", ""),
        "frontmatter_key": "title",
        "frontmatter_delimiter": "---",
        "line_prefix": None,
        "page_url": _parse_page_url(path, name, raw.get("page_url")),
        "url_base": None,
        "drop_lines": _parse_drop_lines(path, name, raw.get("drop_lines")),
        "skip_empty": raw.get("skip_empty", False),
        "origin": path,
    }
    if not isinstance(profile["skip_empty"], bool):
        _bad(path, f"sources.{name}.skip_empty must be true or false")
    if split == "frontmatter":
        key = raw.get("frontmatter_key", "title")
        if not isinstance(key, str) or not _FM_KEY_NAME_RE.match(key):
            _bad(path, f"sources.{name}.frontmatter_key must be a YAML key name")
        profile["frontmatter_key"] = key
        delim = raw.get("frontmatter_delimiter", "---")
        if not isinstance(delim, str) or not _FM_DELIM_RE.match(delim):
            _bad(path, f"sources.{name}.frontmatter_delimiter must be a line of 3 or more '-' or '+'")
        profile["frontmatter_delimiter"] = delim
    else:
        for key in ("frontmatter_key", "frontmatter_delimiter"):
            if key in raw:
                _bad(path, f'sources.{name}.{key} is only valid with split "frontmatter"')
    needs_url = raw.get("h1_needs_url", False)
    if not isinstance(needs_url, bool):
        _bad(path, f"sources.{name}.h1_needs_url must be true or false")
    if needs_url and split != "h1":
        _bad(path, f'sources.{name}.h1_needs_url is only valid with split "h1"')
    if needs_url and not (profile["page_url"] and profile["page_url"][0] in ("line", "link")):
        _bad(path, f'sources.{name}.h1_needs_url needs page_url "line:<prefix>" or "link:<link text>"')
    profile["h1_needs_url"] = needs_url
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
    index_url = raw.get("index_url")
    if index_url is not None and (not isinstance(index_url, str) or not re.match(r"^https?://\S+$", index_url)):
        _bad(path, f"sources.{name}.index_url must be an http(s) URL of the llms.txt for the same docs as url (not a 2-level index)")
    profile["index_url"] = index_url
    return profile


def resolve_sources_file(args) -> None:
    """Fill ``args.sources_file`` (``None`` when ``--sources-file`` was not
    given) and set ``args.sources_file_explicit``: true when the file came
    from ``--sources-file`` or ``$LLMS_DOCS_SOURCES_FILE``, even if the value
    equals the config-dir default."""
    args.sources_file_explicit = args.sources_file is not None or bool(os.environ.get(SOURCES_ENV))
    if args.sources_file is None:
        args.sources_file = default_sources_file()


def load_sources(path: str, *, explicit: bool) -> dict:
    """Bundled presets overlaid by the user's *path* (a user profile replaces
    the preset of the same name). A missing *path* is fine when it is the
    config-dir default — the presets alone are then available — but an
    explicitly given file must exist. Bad JSON / bad profile -> die."""
    sources = _read_sources_file(PRESETS_FILE)
    if os.path.exists(path):
        sources.update(_read_sources_file(path))
    elif explicit:
        die(f"no sources file at {path} (given by --sources-file or ${SOURCES_ENV}; {README_HINT})")
    return sources


def _read_sources_file(path: str) -> dict:
    """Read and validate one sources file. Bad JSON / bad profile -> die."""
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
    sources = load_sources(args.sources_file, explicit=args.sources_file_explicit)
    if args.source not in sources:
        known = ", ".join(sorted(sources)) or "(none)"
        die(f"unknown --source {args.source!r}. Configured: {known}")
    return sources[args.source]


# ---------------------------------------------------------------------------
# Splitters. Each returns [{"title", "url", "body_lines", "min_level"}].
# ---------------------------------------------------------------------------

_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _first_heading(body_lines: list[str], fence=None) -> str:
    """First heading outside a code fence. *fence* is a ``_Fence`` for the
    ``line`` shape; otherwise the shared ``FenceTracker`` is used."""
    if fence is not None:
        for line in body_lines:
            text = line.rstrip("\n\r")
            was_open = fence.open is not None
            fence.update(text)
            if was_open or fence.open is not None:
                continue
            m = _HEADING_RE.match(text)
            if m:
                return m.group(2).strip()
        return ""
    tracker = FenceTracker()
    for line in body_lines:
        was = tracker.in_fence
        tracker.update(line)
        if was or tracker.in_fence:
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


def _link_url(body_lines: list[str], text: str, limit: int = 20) -> str:
    """Target of the first Markdown link ``[<text>](<url>)`` among the first
    *limit* lines (Cloudflare: ``[View as Markdown](https://…/index.md)``)."""
    needle = f"[{text}]("
    for line in body_lines[:limit]:
        pos = line.find(needle)
        if pos < 0:
            continue
        rest = line[pos + len(needle):]
        end = rest.find(")")
        if end > 0 and not any(c.isspace() for c in rest[:end]):
            return rest[:end]
    return ""


def _body_url(body_lines: list[str], rule) -> str:
    """Page URL from the body for a ``line:`` / ``link:`` rule, else ``""``."""
    if rule and rule[0] == "line":
        return _line_url(body_lines, rule[1])
    if rule and rule[0] == "link":
        return _link_url(body_lines, rule[1])
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
        d["url"] = _body_url(d["body_lines"], rule)
        d["min_level"] = None  # the H1 is the delimiter, so sections start at H2
    if profile["h1_needs_url"]:
        docs = _merge_urlless_h1(docs)
    return docs


def _merge_urlless_h1(docs: list[dict]) -> list[dict]:
    """Fold an H1 that has no page URL under it back into the page before it.

    Sites that put ``Source: <url>`` under every page title (Mintlify: Bun,
    MCP) also use H1 inside a page body; without a URL such an H1 is a
    heading, not a page. A URL-less H1 before the first page is kept as is.
    """
    out: list[dict] = []
    for d in docs:
        if not d["url"] and out and out[-1]["url"]:
            out[-1]["body_lines"] = out[-1]["body_lines"] + [f"# {d['title']}\n"] + d["body_lines"]
            # the page now holds an H1, so sections start at H1: the folded
            # heading is listed and its subsections nest under it
            out[-1]["min_level"] = 1
        else:
            out.append(d)
    return out


_FM_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")
_FM_LIST_RE = re.compile(r"^\s*- ")
_FM_CONTINUATION_RE = re.compile(r"^\s+\S")
# a line that starts like Markdown, never the rest of a frontmatter value
_FM_PROSE_START_RE = re.compile(r"^(#|[-*+>|`<\[!]|\d+\. )")
_FM_LOOKAHEAD = 30


def _frontmatter_at(lines: list[str], pos: int, required_key: str,
                    delimiter: str = "---") -> tuple[dict, int] | None:
    """Parse a frontmatter block opening at *pos*; return (fields, end) or None.

    A ``---`` (or the profile's ``frontmatter_delimiter``) opens a block
    only when a closing one follows within ``_FM_LOOKAHEAD`` lines, every
    line in between is YAML-shaped (``key: value`` / ``- item`` / indented
    continuation / blank / the rest of a quoted value), and *required_key*
    is among the keys. A Markdown horizontal rule followed by prose (even
    prose that starts with ``Note:``) is therefore not a page boundary.

    A double-quoted value may span lines, as YAML allows (Vercel writes long
    ``description:`` values that way); the lines up to its closing quote
    belong to the value, whatever they look like, except the closing
    delimiter itself, which always ends the block. An unquoted value may
    also run on to the next lines without indentation (invalid YAML, but
    Cloudflare writes some descriptions so); such a line is taken as part
    of the value unless it looks like Markdown (``_FM_PROSE_START_RE``).
    """
    if lines[pos].rstrip("\n\r") != delimiter:
        return None
    fields: dict = {}
    open_key = None  # key whose double-quoted value is still open
    plain_key = None  # key whose unquoted value the next line may continue
    for j in range(pos + 1, min(pos + _FM_LOOKAHEAD, len(lines))):
        line = lines[j].rstrip("\n\r")
        if open_key is not None and line == delimiter:
            # a quote left open by mistake: the block still ends here and the
            # value is kept as written (losing the page would be worse)
            open_key = None
        if open_key is not None:
            fields[open_key] += " " + line.strip()
            if _closes_double_quote(line):
                fields[open_key] = fields[open_key].strip().strip('"')
                open_key = None
            continue
        if line == delimiter:
            return (fields, j + 1) if required_key in fields else None
        if not line.strip():
            plain_key = None
            continue
        if _FM_LIST_RE.match(line) or _FM_CONTINUATION_RE.match(line):
            continue
        m = _FM_KEY_RE.match(line)
        if not m:
            if plain_key is not None and not _FM_PROSE_START_RE.match(line):
                # an unquoted value carried on to an unindented line
                # (Cloudflare writes long descriptions this way)
                fields[plain_key] += " " + line.strip()
                continue
            return None
        value = m.group(2).strip()
        if value.startswith('"') and not _closes_double_quote(value[1:]):
            open_key = m.group(1)
            fields[open_key] = value
            plain_key = None
            continue
        fields[m.group(1)] = value.strip("'\"")
        plain_key = m.group(1) if value and value[0] not in "'\"|>[{" else None
    return None


def _closes_double_quote(text: str) -> bool:
    """True when *text* ends with a ``"`` that is not backslash-escaped."""
    text = text.rstrip()
    if not text.endswith('"'):
        return False
    backslashes = len(text[:-1]) - len(text[:-1].rstrip("\\"))
    return backslashes % 2 == 0


def split_frontmatter(lines: list[str], profile: dict) -> list[dict]:
    key = profile["frontmatter_key"]
    blocks: list[tuple[dict, int, int]] = []  # (fields, block_start, body_start)
    fence = FenceTracker()
    i = 0
    while i < len(lines):
        if not fence.in_fence:
            hit = _frontmatter_at(lines, i, key, profile["frontmatter_delimiter"])
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
        if rule and rule[0] == "frontmatter":
            url = fields.get(rule[1], "")
        else:
            url = _body_url(body, rule)
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


class _Fence:
    """Code-fence state with the rules the ``line`` shape needs.

    Shares the closer rules with ``_common.FenceTracker``: a line with an
    info string (```` ```ts ````) never *closes* a fence (CommonMark), and a
    closer may be indented any amount (blocks nested in JSX close with an
    indented ```` ``` ````). Written when the shared tracker still took
    info-string lines for closers and lost 299 of 496 page boundaries on
    Drizzle; both now find all 496.

    Differs from ``FenceTracker`` in the opener, which must be indented 0-3
    spaces and, for backticks, carry no backtick in its info string (inline
    code), and in having no MDX comment rule (``FenceTracker`` closes on a
    run followed by ``*/}`` when the ``{/*`` comment opened outside any
    block, and never opens on one).
    """

    def __init__(self):
        self.open: tuple[str, int] | None = None

    def closes(self, text: str) -> bool:
        m = _FENCE_CLOSE_RE.match(text)
        return bool(m and self.open and m.group(1)[0] == self.open[0] and len(m.group(1)) >= self.open[1])

    def update(self, text: str) -> None:
        if self.open is None:
            m = _FENCE_OPEN_RE.match(text)
            # a backtick fence's info string may not contain backticks (inline code)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                self.open = (m.group(1)[0], len(m.group(1)))
        elif self.closes(text):
            self.open = None


def split_line(lines: list[str], profile: dict) -> list[dict]:
    """One page per ``<prefix><url>`` line, skipping fenced examples of it.

    A delimiter must be the prefix followed by a single URL (absolute, or a
    ``/path``). One seen inside a code fence is an example when that fence
    closes before the next delimiter-looking line; when it does not, the
    fence is malformed (never closed), so the line is taken as a real
    boundary and the fence state is reset — one broken block must not
    swallow every later page.
    """
    prefix = profile["line_prefix"]
    texts = [line.rstrip("\n\r") for line in lines]

    def is_delimiter(text: str) -> bool:
        return text.startswith(prefix) and bool(_URLISH_RE.match(text[len(prefix):].strip()))

    def fence_closes_before_next_delimiter(fence: _Fence, pos: int) -> bool:
        for text in texts[pos + 1:]:
            if is_delimiter(text):
                return False
            if fence.closes(text):
                return True
        return False

    starts: list[int] = []
    fence = _Fence()
    for i, text in enumerate(texts):
        if is_delimiter(text) and (fence.open is None or not fence_closes_before_next_delimiter(fence, i)):
            starts.append(i)
            fence = _Fence()
            continue
        fence.update(text)
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
        elif rule == ("line", prefix):
            url = delimiter_url
        else:
            url = _body_url(body, rule)
        title = _first_heading(body, fence=_Fence()) or delimiter_url.rstrip("/").rsplit("/", 1)[-1] or "(untitled)"
        docs.append({"title": title, "url": url, "body_lines": body, "min_level": 1})
    return docs


_SPLITTERS = {"h1": split_h1, "frontmatter": split_frontmatter, "line": split_line}


_RULE_RE = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")


def _drop_boilerplate(body_lines: list[str], patterns: list) -> list[str]:
    """Remove lines outside code fences that match one of *patterns*. Runs
    after the page URL is read, so a link on a dropped line still counts."""
    fence = FenceTracker()
    out = []
    for line in body_lines:
        was = fence.in_fence
        fence.update(line)
        text = line.rstrip("\n\r")
        if not was and not fence.in_fence and any(p.search(text) for p in patterns):
            continue
        out.append(line)
    return out


def split_documents(lines: list[str], profile: dict) -> list[dict]:
    docs = _SPLITTERS[profile["split"]](lines, profile)
    base = profile["url_base"]
    for d in docs:
        d.setdefault("description", "")
        if d["url"] and base and not re.match(r"^https?://", d["url"]):
            d["url"] = urljoin(base, d["url"])
        if profile["drop_lines"]:
            d["body_lines"] = _drop_boilerplate(d["body_lines"], profile["drop_lines"])
    if profile["skip_empty"]:
        # A heading with no body (Hono's "# Start of Hono documentation"
        # banner, Codex's category titles followed only by a "---" rule)
        # is not a page.
        docs = [d for d in docs if any(line.strip() and not _RULE_RE.match(line) for line in d["body_lines"])]
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
    if profile["index_url"]:
        index_lines = _load_index_lines(args, profile)
        if index_lines is not None:
            join_index_urls(docs, index_lines)
    return profile, path, docs


def _load_index_lines(args, profile: dict) -> list[str] | None:
    """The profile's ``llms.txt`` for URL joining: ``--index-file`` if
    given, else fetched to the cache — but not when ``--file`` reads a local
    corpus (that mode never touches the network). A failed fetch only costs
    the URLs, so it warns instead of exiting."""
    if args.index_file is not None:
        if not os.path.exists(args.index_file):
            die(f"--index-file '{args.index_file}' does not exist")
        return load_lines(args.index_file)
    if args.file is not None:
        return None
    digest = hashlib.sha256(profile["index_url"].encode("utf-8")).hexdigest()[:12]
    cache = os.path.join(args.cache_dir, f"generic-{profile['name']}-{digest}-llms.txt")
    try:
        return load_lines(fetch_url(profile["index_url"], cache, user_agent=USER_AGENT,
                                    timeout=60, max_age=args.max_age, raise_on_error=True))
    except FetchError as e:
        print(f"WARNING: could not fetch {profile['index_url']} ({e}); page URLs from the index are omitted",
              file=sys.stderr)
        return None


def _title_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_]", "", title)).strip().lower()


# The last path segment of an llms.txt-family file: llms.txt, llms-full.txt,
# llms-small.txt, llms-ctx-full.txt. Measured on real indexes: OpenAI's and
# Cloudflare's root llms.txt link one ``<product>/llms.txt`` per product,
# Hono's links llms-small.txt, Codex's links llms-full.txt and use-cases/llms.txt.
_INDEX_FILE_NAME_RE = re.compile(r"^llms(?:-[a-z0-9]+)*\.txt$", re.IGNORECASE)


def _is_index_file_url(url: str) -> bool:
    """True when *url* names an ``llms.txt``-family file (another index, or a
    full-text export) rather than a page. Query, fragment and a trailing slash
    are ignored; only the last path segment counts, so ``/docs/llms.txt.md``
    and a host that happens to be called ``llms.txt`` are not index files."""
    path = re.sub(r"^https?://[^/]*", "", url.split("#", 1)[0].split("?", 1)[0]).rstrip("/")
    return bool(_INDEX_FILE_NAME_RE.match(path.rsplit("/", 1)[-1]))


def join_index_urls(docs: list[dict], index_lines: list[str]) -> int:
    """Give a URL to each page that has none, from the ``llms.txt`` entry
    with the same title. Only an exact title (ignoring case, spacing and
    ``*_``` marks) that is unique on both sides counts: a near match (index
    "Basic Auth" for page "Basic Auth Middleware"), a title shared by two
    entries, or one shared by two pages (Zod's site banner and its
    ``packages/zod`` page are both "Zod") is left without a URL, because a
    wrong URL misleads more than a missing one (measured on Hono:
    word-prefix matching gave 4 wrong URLs in 40). Entries are read with
    the shared ``parse_llms_index`` (absolute URLs only). An entry that
    links another ``llms.txt``-family file is not a page and is skipped
    before the titles are compared (a 2-level index such as OpenAI's root
    ``llms.txt`` would otherwise give "Sign in with ChatGPT" the URL of its
    ``siwc/llms.txt``); it does not make a page's title ambiguous either.
    Returns the number of pages that got a URL."""
    by_title: dict[str, set] = {}
    for entry in parse_llms_index(index_lines):
        if _is_index_file_url(entry["url"]):
            continue
        by_title.setdefault(_title_key(entry["title"]), set()).add(entry["url"])
    pages_per_title: dict[str, int] = {}
    for d in docs:
        key = _title_key(d["title"])
        pages_per_title[key] = pages_per_title.get(key, 0) + 1
    joined = 0
    for d in docs:
        if d["url"]:
            continue
        key = _title_key(d["title"])
        urls = by_title.get(key, set())
        if len(urls) == 1 and pages_per_title[key] == 1:
            d["url"] = next(iter(urls))
            joined += 1
    return joined


def _source_hint_args(args) -> tuple:
    """``--source`` is always required here, so it is always echoed. An
    explicit sources file (``--sources-file`` or ``$LLMS_DOCS_SOURCES_FILE``)
    is echoed as ``--sources-file`` too: the hint may run in a shell without
    that env var, where the same name could resolve to a bundled preset —
    another corpus — instead of failing."""
    out = ["--source", shlex.quote(args.source)]
    if args.sources_file_explicit:
        out += ["--sources-file", shlex.quote(args.sources_file)]
    if getattr(args, "index_file", None) is not None:
        out += ["--index-file", shlex.quote(args.index_file)]
    return tuple(out)


def _resolve_page_ref(docs: list[dict], page_ref: str, retry=None) -> int:
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
            rows = [(i, d["title"] + (f" ({d['url']})" if d["url"] else "")) for i, d in found[:20]]
            die_ambiguous_page(f"{field_name} substring", page_ref, rows, retry)
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
    sources = load_sources(args.sources_file, explicit=args.sources_file_explicit)
    user_state = "" if os.path.exists(args.sources_file) else " — not present, presets only"
    print(f"Configured sources (presets: {PRESETS_FILE}; user file: {args.sources_file}{user_state})")
    print("=" * 60)
    for name, p in sorted(sources.items()):
        origin = "preset" if p["origin"] == PRESETS_FILE else "user"
        print(f"{name}  [{origin}, {p['split']}]  {p['url']}")
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


def _page_ref_retry(args):
    """``idx -> command`` re-running this invocation on page *idx*."""
    return retry_for_page_ref(args, _source_hint_args(args) + corpus_hint_args(args))


def _page_view(args) -> PageView:
    _profile, path, docs = _load_docs(args)
    idx = _resolve_page_ref(docs, args.page_ref, _page_ref_retry(args))
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
    targets = ([_resolve_page_ref(docs, args.page_ref, _page_ref_retry(args))]
               if args.page_ref is not None else range(len(docs)))
    print(f'Search-content results for "{args.query}" (source: {profile["name"]}, file: {path})')
    print("=" * 60)
    print()
    total = 0
    collected = []
    for idx in targets:
        hits = _body_hits(args, docs[idx])
        if hits["total_matches"] == 0:
            continue
        total += hits["total_matches"]
        collected.append((idx, hits))
    # Changelog pages last, then strict-AND before "[partial match]", then
    # most hits, then doc order; --limit cuts only after ordering (same key in
    # every script).
    collected.sort(key=lambda t: search_content_rank_key(
        t[0], docs[t[0]]["title"], t[1],
        include_changelog_priority=args.include_changelog_priority))
    matched = len(collected)
    printed = 0
    shown = []
    for idx, hits in collected[: max(args.limit, 0)]:
        d = docs[idx]
        printed += 1
        shown.append((idx, hits, ()))
        print_page_hits(f"[{idx}] {d['title'] or '(untitled)'}", hits, noun="document", extra_lines=_url_line(d))
    if total == 0:
        print("No matching content found.")
        print()
        print("Tip: try broader keywords or 'search-index' to find relevant documents first")
        print()
        render_zero_hits(args.query, (d["body_lines"] for d in docs),
                         subcommand="search-content",
                         hint_args=_source_hint_args(args) + corpus_hint_args(args),
                         index_hint_args=_source_hint_args(args) + corpus_hint_args(args),
                         scope="documents", restricted_to=args.page_ref)
        return
    print(f"({total} hits across {matched} documents, showing top {printed})")
    print()
    render_next_content(hit_candidates(shown),
                        hint_args=_source_hint_args(args) + corpus_hint_args(args))


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
        {"doc_idx": idx, "index_score": score, "body_hits": _body_hits(args, docs[idx]), "body_only": False,
         "title": docs[idx]["title"] or "(untitled)"}
        for score, idx, _entry in scored
    ]
    # The full-corpus search also runs when the candidates have body hits but
    # none has every keyword in one section (full_corpus_extra_hits).
    # It takes one min_level for the whole corpus; every page of a profile
    # shares its split, so the first page's is used.
    level = (docs[0]["min_level"] or 2) if docs else 2
    for idx, hits in full_corpus_extra_hits(
        results, [d["body_lines"] for d in docs], args.query, context_lines=args.context,
        max_matches_per_doc=args.max_hits, max_snippet_chars=args.max_snippet_chars,
        min_level=level, limit=args.top_n,
        include_changelog_priority=args.include_changelog_priority,
    ):
        results.append({"doc_idx": idx, "index_score": None, "body_hits": hits, "body_only": True,
                        "title": docs[idx]["title"] or "(untitled)"})
    if not results:
        print("No matching documents found.")
        print()
        print("Tip: try broader keywords or 'search-content' for a full-body scan")
        print()
        render_zero_hits(args.query, (d["body_lines"] for d in docs),
                         subcommand="search",
                         hint_args=_source_hint_args(args) + corpus_hint_args(args),
                         index_hint_args=_source_hint_args(args) + corpus_hint_args(args),
                         scope="documents")
        return
    if all(r["body_only"] for r in results):
        print("  (no title/description match — showing full-body search results instead)")
        print()
    results.sort(key=lambda r: search_rank_key(r, include_changelog_priority=args.include_changelog_priority))
    for r in results:
        tag = " [body-only]" if r["body_only"] else f" (index_score: {r['index_score']})"
        print_search_result(f"[{r['doc_idx']}] {r['title']}{tag}", r["body_hits"],
                            extra_lines=_url_line(docs[r["doc_idx"]]))
    print(f"({len(results)} documents, ranked via index → body)")
    print()
    # The top index candidate keeps a Next: line even when appended pages
    # rank above it.
    keep = next(((r["doc_idx"], ()) for r in results if not r["body_only"]), None)
    render_next_content(hit_candidates([(r["doc_idx"], r["body_hits"], ()) for r in results],
                                       keep=keep),
                        hint_args=_source_hint_args(args) + corpus_hint_args(args))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common(parser, *, source: bool = True) -> None:
    parser.add_argument(
        "--sources-file", default=None,
        help=f"User profiles file, overlaid on the bundled presets "
             f"(default: ${SOURCES_ENV} or ~/.config/llms-docs/sources.json; may be absent)",
    )
    if not source:
        return
    parser.add_argument("--source", required=True,
                        help="Profile name (a bundled preset or one in the sources file; see `sources`)")
    parser.add_argument("--file", default=None,
                        help="Read a local llms-full.txt instead of fetching the profile's url")
    parser.add_argument("--index-file", default=None,
                        help="Read a local llms.txt for the profile's index_url (page URLs by title)")
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
    add_include_changelog_priority_arg(p)
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
    resolve_sources_file(args)
    args.func(args)


if __name__ == "__main__":
    main()
