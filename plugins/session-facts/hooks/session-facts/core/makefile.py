from __future__ import annotations

import re
from typing import List

# A target line starts at column 0 with a name followed by a single/double
# colon that is NOT ``:=`` (variable assignment). Recipe lines (tab-indented),
# comments, and special targets (``.PHONY`` etc., which start with a dot) are
# excluded.
_TARGET_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-/]*)\s*::?(?!=)")


def extract_targets(text: str) -> List[str]:
    """Return Makefile target names in declaration order (deduped).

    Variable assignments (``CC = gcc`` / ``CC := gcc``), recipe lines, comments
    and dot-prefixed special targets are skipped.
    """
    targets: List[str] = []
    seen = set()
    for line in text.splitlines():
        if not line or line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        match = _TARGET_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            targets.append(name)
    return targets
