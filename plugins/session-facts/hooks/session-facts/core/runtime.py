from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from core.fs import read_text

# A TOML scalar (``"3.12"``), array (``["3.12", "3.11"]``), or inline table
# (``{version = "3.12", ...}``) value, used to pull a version out of mise/poetry
# entries without a TOML parser (tomllib is intentionally avoided — see CLAUDE.md).
_QUOTED_RE = re.compile(r"""["']([^"']*)["']""")
_INLINE_VERSION_RE = re.compile(r"""\bversion\s*=\s*["']([^"']*)["']""")
# A bare ``key = value`` assignment line inside a TOML table.
_ASSIGN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.+)$")

# mise config locations, in resolution order.
_MISE_CONFIG_NAMES = (".mise.toml", "mise.toml", ".config/mise/config.toml")


def first_version(value: str) -> str:
    """Extract a version string from a TOML scalar / array / inline-table value.

    Handles ``"3.12"`` (scalar), ``["3.12", "3.11"]`` (array -> first), and
    ``{version = "3.12", virtualenv = ".venv"}`` (inline table -> version field).
    Returns ``""`` when no version can be found (e.g. a git/path dependency).
    """
    value = value.strip()
    if value.startswith("{"):
        match = _INLINE_VERSION_RE.search(value)
        return match.group(1) if match else ""
    match = _QUOTED_RE.search(value)
    if match:
        return match.group(1)
    # Bare unquoted scalar (rare): strip a trailing comment / bracket punctuation.
    return value.split("#", 1)[0].strip().strip("[]{},")


def mise_config_path(root: Path) -> Optional[Path]:
    """Return the first existing mise config file under ``root``, or None.

    Checks ``.mise.toml`` / ``mise.toml`` (dotless) / ``.config/mise/config.toml``.
    """
    for name in _MISE_CONFIG_NAMES:
        path = root / name
        if path.exists():
            return path
    return None


def has_mise(root: Path) -> bool:
    """True when a mise config or an asdf ``.tool-versions`` file is present.

    mise honours ``.tool-versions`` too, so its presence still implies a
    mise-compatible runtime pin for the stack detector.
    """
    return mise_config_path(root) is not None or (root / ".tool-versions").exists()


def parse_mise_tools(text: str) -> Dict[str, str]:
    """Parse the ``[tools]`` table of a mise config into ``{tool: version}``.

    Only assignments under ``[tools]`` are considered (``[env]`` / ``[settings]``
    / ``[tasks]`` are ignored). Array and inline-table values collapse to their
    first / ``version`` entry via :func:`first_version`.
    """
    out: Dict[str, str] = {}
    in_tools = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_tools = stripped == "[tools]"
            continue
        if not in_tools:
            continue
        match = _ASSIGN_RE.match(stripped)
        if not match:
            continue
        out[match.group(1)] = first_version(match.group(2))
    return out


def parse_tool_versions(text: str) -> Dict[str, str]:
    """Parse an asdf-style ``.tool-versions`` blob into ``{tool: version}``.

    Each line is ``<tool> <version> [<version>...]``; only the first version is
    kept. Blank and comment lines are skipped.
    """
    out: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def read_python_version(root: Path) -> Optional[str]:
    """Return the first non-comment line of ``.python-version``, or None."""
    path = root / ".python-version"
    if not path.exists():
        return None
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def detect_venv(root: Path) -> Optional[str]:
    """Return ``.venv`` / ``venv`` when it is a real virtualenv, else None.

    A directory is only treated as a venv when it contains ``pyvenv.cfg``; a
    bare ``.exists()`` check would misfire on a source directory literally named
    ``venv/``.
    """
    for name in (".venv", "venv"):
        if (root / name / "pyvenv.cfg").exists():
            return name
    return None


def read_venv_python(root: Path, venv: str) -> Optional[str]:
    """Read the interpreter version from ``<venv>/pyvenv.cfg``, or None.

    Prefers the ``version`` key and falls back to ``version_info`` (trimming the
    ``.final.0`` release-level suffix to a plain dotted version).
    """
    text = read_text(root / venv / "pyvenv.cfg")
    if not text:
        return None
    for key in ("version", "version_info"):
        match = re.search(rf"(?mi)^\s*{key}\s*=\s*([0-9][\w.]*)", text)
        if match:
            numeric = re.match(r"[0-9]+(?:\.[0-9]+)*", match.group(1))
            return numeric.group(0) if numeric else match.group(1)
    return None


def build_runtime_info(root: Path) -> Dict[str, object]:
    """Synthesize a runtime-context dict from mise/asdf/python-version/venv.

    Keys (all optional): ``manager`` (``mise`` | ``asdf``), ``tools``
    (``{tool: version}``), ``python_version`` (``.python-version``), ``venv``
    (dir name), ``venv_python`` (its interpreter). Returns ``{}`` when nothing
    runtime-related is detected.
    """
    info: Dict[str, object] = {}

    config = mise_config_path(root)
    if config is not None:
        tools = parse_mise_tools(read_text(config))
        info["manager"] = "mise"
        if tools:
            info["tools"] = tools
    elif (root / ".tool-versions").exists():
        tools = parse_tool_versions(read_text(root / ".tool-versions"))
        info["manager"] = "asdf"
        if tools:
            info["tools"] = tools

    python_version = read_python_version(root)
    if python_version:
        info["python_version"] = python_version

    venv = detect_venv(root)
    if venv:
        info["venv"] = venv
        venv_python = read_venv_python(root, venv)
        if venv_python:
            info["venv_python"] = venv_python

    return info


def runner_prefix(info: Dict[str, object]) -> Optional[str]:
    """Command prefix for invoking local tools, given a runtime-info dict.

    A concrete local interpreter wins: an existing venv yields ``<venv>/bin/``
    (path prefix, no trailing space). Otherwise a mise-managed Python yields
    ``mise exec -- `` (command prefix, trailing space). Returns None when there
    is no actionable wrapper (e.g. asdf shims, or nothing detected).
    """
    venv = info.get("venv")
    if venv:
        return f"{venv}/bin/"
    tools = info.get("tools") or {}
    if info.get("manager") == "mise" and isinstance(tools, dict) and "python" in tools:
        return "mise exec -- "
    return None
