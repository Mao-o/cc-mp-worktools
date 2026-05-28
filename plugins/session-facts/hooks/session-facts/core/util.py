from __future__ import annotations

import re
from pathlib import Path

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
