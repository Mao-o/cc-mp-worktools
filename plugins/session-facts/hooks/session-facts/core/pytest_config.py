"""Read the pytest ``python_files`` setting so test-file detection follows a
project's own naming rule (internal backlog joa.30).

pytest reads, in this precedence, ``pytest.ini`` > ``pyproject.toml``
``[tool.pytest.ini_options]`` > ``tox.ini`` ``[pytest]`` > ``setup.cfg``
``[tool:pytest]``. Only the ``python_files`` key is read here; everything
else about pytest configuration is out of scope. A file that cannot be
read, or that has no ``python_files``, falls back to pytest's own default
(``test_*.py`` and ``*_test.py``).
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple

from core.fs import read_text

DEFAULT_PYTHON_FILES: Tuple[str, ...] = ("test_*.py", "*_test.py")

_KEY_RE = re.compile(r"^\s*python_files\s*=\s*(.+?)\s*$")


def _split_values(raw: str) -> List[str]:
    """``["a", "b"]`` (TOML array) or ``a b`` / ``a, b`` (ini) -> ``[a, b]``."""
    raw = raw.strip()
    if raw.startswith("["):
        raw = raw.strip("[]")
        items = [v.strip().strip("\"'") for v in raw.split(",")]
    else:
        raw = raw.strip("\"'")
        items = [v for v in re.split(r"[\s,]+", raw)]
    return [v for v in items if v]


def _from_ini_section(text: str, section_names: Tuple[str, ...]) -> Optional[List[str]]:
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_section = line in section_names
            continue
        if not in_section:
            continue
        m = _KEY_RE.match(line)
        if m:
            values = _split_values(m.group(1))
            return values or None
    return None


def python_files_patterns(root: Path, pyproject_text: str = "") -> Tuple[str, ...]:
    """``python_files`` globs in effect at ``root``, or the pytest default."""
    ini = root / "pytest.ini"
    if ini.exists():
        found = _from_ini_section(read_text(ini), ("[pytest]",))
        if found:
            return tuple(found)
    if pyproject_text:
        found = _from_ini_section(pyproject_text, ("[tool.pytest.ini_options]",))
        if found:
            return tuple(found)
    tox = root / "tox.ini"
    if tox.exists():
        found = _from_ini_section(read_text(tox), ("[pytest]",))
        if found:
            return tuple(found)
    cfg = root / "setup.cfg"
    if cfg.exists():
        found = _from_ini_section(read_text(cfg), ("[tool:pytest]",))
        if found:
            return tuple(found)
    return DEFAULT_PYTHON_FILES


def matches_python_files(path: str, patterns: Tuple[str, ...]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(name, pattern) for pattern in patterns)
