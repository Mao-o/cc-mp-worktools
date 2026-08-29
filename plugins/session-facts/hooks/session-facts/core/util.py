from __future__ import annotations

import re
from pathlib import Path
from typing import List, Sequence

from core.constants import CODE_EXTENSIONS, MAX_PURPOSE_CHARS, TEST_PATH_MARKERS


def collapse_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_MD_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_MD_ITALIC = re.compile(r"(?<![\*_])([\*_])([^\*_\n]+?)\1(?![\*_])")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HTML_TAG = re.compile(r"<[^<>\n]+>")
_HTML_ENTITY = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_SENTENCE_END = re.compile(r"[。．\.!?！？]")


def strip_markdown_inline(text: str) -> str:
    text = _HTML_TAG.sub("", text)
    text = _HTML_ENTITY.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\2", text)
    text = _MD_ITALIC.sub(r"\2", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    return text


def truncate_purpose(text: str, max_chars: int = MAX_PURPOSE_CHARS) -> str:
    text = strip_markdown_inline(text)
    text = collapse_space(text)

    first_match = _SENTENCE_END.search(text)
    if first_match:
        first_end = first_match.end()
        rest = text[first_end:].strip()
        if rest and 15 <= first_end <= max_chars:
            return text[:first_end].rstrip()

    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    matches = list(_SENTENCE_END.finditer(window))
    if matches:
        cut = matches[-1].end()
        if cut >= max_chars // 2:
            return text[:cut].rstrip()
    return window.rstrip() + "…"


def normalize_version(version: str) -> str:
    version = version.strip()
    version = re.sub(r'^[\^~<>= ]+', '', version)
    match = re.search(r'(\d+)(?:\.(\d+))?(?:\.(\d+))?', version)
    if not match:
        return version
    groups = [g for g in match.groups() if g is not None]
    return '.'.join(groups[:2]) if len(groups) >= 2 else groups[0]


def is_test_path(path_str: str) -> bool:
    parts = {part.lower() for part in Path(path_str).parts}
    name = Path(path_str).name.lower()
    if any(marker in parts for marker in TEST_PATH_MARKERS):
        return True
    return any(token in name for token in (".test.", ".spec.", "_test.", "_spec."))


def is_code_file(path_str: str) -> bool:
    return Path(path_str).suffix.lower() in CODE_EXTENSIONS and not is_test_path(path_str)


def filter_to_cwd(tracked_files, cwd_relative):
    if not cwd_relative:
        return list(tracked_files)
    prefix = cwd_relative + "/"
    return [p for p in tracked_files if p.startswith(prefix)]


def _build_pattern(group: List[List[str]], length: int) -> List[str]:
    """Position-wise glob pattern for a group of equal-length path segments."""
    pattern = []
    for i in range(length):
        values = {segs[i] for segs in group}
        pattern.append(next(iter(values)) if len(values) == 1 else "*")
    return pattern


def aggregate_paths(paths: Sequence[str], max_listed: int = 4) -> List[str]:
    """Collapse sibling directory paths to a glob-style pattern.

    Paths that share the same number of segments are folded together by
    replacing positions whose segment value differs across the group with
    ``*``. For example::

        plugins/sensitive-files-guardrail/hooks/check-sensitive-files/tests
        plugins/sensitive-files-guardrail/hooks/redact-sensitive-reads/tests
        plugins/verify-cloud-account/hooks/verify-cloud-account/tests

    collapses to a single ``plugins/*/hooks/*/tests`` line. A single path (or a
    length-group with a single member) is returned verbatim — no abstraction is
    applied when there is nothing to aggregate.

    When a length-group's naive pattern would be fully wildcarded (every
    position differs, e.g. ``*/*`` from ``api/tests`` + ``web/__tests__`` +
    ``sdks/spec``), no literal segment survives anywhere and the pattern
    carries zero localization info. In that case the group is re-split by
    its leading directory instead: subsets that DO share a leading directory
    still collapse to a useful pattern (e.g. ``packages/*/tests``), and the
    remainder — paths whose leading directory is unique within the group —
    are listed verbatim, capped at ``max_listed`` entries plus an
    ``... (+N more)`` line so an unbounded pile of unrelated one-off paths
    can't flood the output. A pattern that keeps at least one literal
    segment (e.g. ``*/tests`` or ``*/*/tests``) is left aggregated as-is —
    it still localizes some part of the path.
    """
    unique = sorted(dict.fromkeys(paths))
    if len(unique) <= 1:
        return unique

    groups: dict = {}
    for path in unique:
        segs = path.split("/")
        groups.setdefault(len(segs), []).append(segs)

    out: List[str] = []
    for length in sorted(groups):
        group = groups[length]
        if len(group) == 1:
            out.append("/".join(group[0]))
            continue
        pattern = _build_pattern(group, length)
        if any(seg != "*" for seg in pattern):
            out.append("/".join(pattern))
            continue

        # Fully wildcarded: re-split by leading directory so any genuinely
        # shared prefix still collapses, and cap the rest instead of
        # emitting the useless all-"*" line.
        by_prefix: dict = {}
        for segs in group:
            by_prefix.setdefault(segs[0], []).append(segs)
        leftovers: List[str] = []
        for prefix in sorted(by_prefix):
            sub = by_prefix[prefix]
            if len(sub) == 1:
                leftovers.append("/".join(sub[0]))
            else:
                out.append("/".join(_build_pattern(sub, length)))
        if leftovers:
            if len(leftovers) > max_listed:
                out.extend(leftovers[:max_listed])
                out.append(f"... (+{len(leftovers) - max_listed} more)")
            else:
                out.extend(leftovers)
    return out
