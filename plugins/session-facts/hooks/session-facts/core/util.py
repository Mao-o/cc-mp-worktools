from __future__ import annotations

import re
from pathlib import Path

from core.constants import CODE_EXTENSIONS, TEST_PATH_MARKERS


def collapse_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
