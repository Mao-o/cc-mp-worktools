from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from core.constants import FLUTTER_IMPORTANT_DEPENDENCIES, IMPORTANT_DEPENDENCIES
from core.context import RepoContext
from core.fs import read_text
from core.runtime import first_version
from core.util import normalize_version

# A requirement spec line: name, optional [extras], optional operator+version.
_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?:[=<>~!]=?\s*([0-9][^\s;,#]*))?"
)
# A pubspec / Pipfile dep entry: name then value after ':' or '='.
_PUBSPEC_DEP_RE = re.compile(r"^( +)([A-Za-z0-9_]+)\s*:\s*(.*)$")
_PIPFILE_DEP_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.+)$")
# A bare ``key = value`` assignment inside a TOML table (poetry deps).
_TOML_ASSIGN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.+)$")
# Quoted string elements inside a TOML array. Two alternatives (not a single
# ["']...["']) so a double-quoted element keeps inner single quotes, e.g. the
# marker "httpx; python_version >= '3.9'", and vice versa.
_ARRAY_STR_RE = re.compile(r"""\"([^"]*)\"|'([^']*)'""")
# A ``[tool.poetry.group.<name>.dependencies]`` table header.
_POETRY_GROUP_RE = re.compile(
    r"(?m)^\s*\[(tool\.poetry\.group\.[^\].]+\.dependencies)\]\s*$"
)
_MAX_DEP_FILES = 6


def _format_dep(name: str, version: str) -> str:
    norm = normalize_version(version) if version else ""
    return f"{name}@{norm}" if norm else name


def _tracked_with_basename(ctx: RepoContext, *names: str) -> List[str]:
    targets = set(names)
    return [p for p in ctx.tracked_files if Path(p).name in targets]


def _tracked_requirements(ctx: RepoContext) -> List[str]:
    out = []
    for p in ctx.tracked_files:
        base = Path(p).name
        if base == "requirements.txt" or (base.startswith("requirements") and base.endswith(".txt")):
            out.append(p)
    return out


def parse_requirements(text: str) -> List[Tuple[str, str]]:
    """Parse a requirements.txt-style blob into (name, version) tuples."""
    out: List[Tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        match = _REQ_RE.match(stripped)
        if not match:
            continue
        out.append((match.group(1), match.group(2) or ""))
    return out


def parse_pipfile_grouped(text: str) -> Dict[str, List[Tuple[str, str]]]:
    """Parse Pipfile into ``{"packages": [...], "dev-packages": [...]}``.

    Keeping the two groups separate lets the collector tier dev tooling
    (pytest etc.) after runtime dependencies. :func:`parse_pipfile` flattens
    this back to the historical combined list.
    """
    out: Dict[str, List[Tuple[str, str]]] = {"packages": [], "dev-packages": []}
    section: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section not in ("packages", "dev-packages"):
            continue
        match = _PIPFILE_DEP_RE.match(stripped)
        if not match:
            continue
        version_match = re.search(r"(\d[\w.]*)", match.group(2))
        out[section].append((match.group(1), version_match.group(1) if version_match else ""))
    return out


def parse_pipfile(text: str) -> List[Tuple[str, str]]:
    """Parse Pipfile [packages]/[dev-packages] entries into (name, version)."""
    grouped = parse_pipfile_grouped(text)
    return grouped["packages"] + grouped["dev-packages"]


def parse_setup_cfg_requires(text: str) -> List[Tuple[str, str]]:
    """Parse [options] install_requires from a setup.cfg blob."""
    block: List[str] = []
    in_options = False
    capturing = False
    for line in text.splitlines():
        if line.startswith("["):
            in_options = line.strip() == "[options]"
            capturing = False
            continue
        if not in_options:
            continue
        if not capturing and re.match(r"^\s*install_requires\s*=", line):
            capturing = True
            inline = line.split("=", 1)[1].strip()
            if inline:
                block.append(inline)
            continue
        if capturing:
            if not line.strip():
                continue
            if line[:1].isspace():
                block.append(line.strip())
            else:
                capturing = False
    return parse_requirements("\n".join(block))


def parse_pubspec_deps(text: str) -> List[Tuple[str, str]]:
    """Parse top-level dependencies / dev_dependencies from pubspec.yaml.

    Only the shallowest indent level under each section is treated as a dep
    name; deeper lines (``sdk: flutter``, ``git:`` blocks) are nested config.
    """
    out: List[Tuple[str, str]] = []
    section: Optional[str] = None
    dep_indent: Optional[int] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            key = raw.split(":", 1)[0].strip()
            section = key if key in ("dependencies", "dev_dependencies") else None
            dep_indent = None
            continue
        if section is None:
            continue
        match = _PUBSPEC_DEP_RE.match(raw)
        if not match:
            continue
        this_indent = len(match.group(1))
        if dep_indent is None:
            dep_indent = this_indent
        if this_indent != dep_indent:
            continue  # nested config under a dep (sdk:, git:, path:, ...)
        version = match.group(3).strip().strip("\"'")
        out.append((match.group(2), version))
    return out


# --- pyproject.toml table parsing (no tomllib; regex per CLAUDE.md house style) ---


def _slice_toml_table(text: str, table: str) -> str:
    """Return the body lines of ``[table]`` up to the next table header.

    Any ``[...]`` / ``[[...]]`` line is a table boundary; everything else is
    body. Multi-line array values stay intact (their continuation lines start
    with a quote or ``]``, never a bare ``[header]``).
    """
    target = f"[{table}]"
    out: List[str] = []
    capturing = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            capturing = stripped == target
            continue
        if capturing:
            out.append(line)
    return "\n".join(out)


def _scan_array(text: str, start: int) -> str:
    """Return the contents of a ``[ ... ]`` array starting just after its ``[``.

    Quote-aware so a ``]`` inside a string element (``"uvicorn[standard]"``)
    does not prematurely close the array. ``start`` is the index immediately
    after the opening bracket.
    """
    depth = 1
    quote: Optional[str] = None
    out: List[str] = []
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in ("\"", "'"):
            quote = ch
            out.append(ch)
        elif ch == "[":
            depth += 1
            out.append(ch)
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _extract_array_after(text: str, key: str) -> Optional[str]:
    """Return the inner contents of ``key = [ ... ]``, or None if absent."""
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*\[", text)
    if not match:
        return None
    return _scan_array(text, match.end())


def _iter_array_assignments(body: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(key, array_contents)`` for each ``key = [ ... ]`` in a body."""
    for match in re.finditer(r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*\[", body):
        yield match.group(1), _scan_array(body, match.end())


def _array_elements(body: str) -> List[str]:
    """Extract the quoted string elements of an array body."""
    out: List[str] = []
    for match in _ARRAY_STR_RE.finditer(body):
        out.append(match.group(1) if match.group(1) is not None else match.group(2))
    return out


def parse_pep621_deps(text: str) -> List[Tuple[str, str]]:
    """Parse ``[project] dependencies = [...]`` (PEP 621) into (name, version).

    Returns [] when dependencies are declared ``dynamic`` (no inline array).
    Each array element is run through :func:`parse_requirements`, so extras,
    environment markers, and ``#`` comments are handled by the existing regex.
    """
    body = _slice_toml_table(text, "project")
    if not body:
        return []
    array = _extract_array_after(body, "dependencies")
    if array is None:
        return []
    return parse_requirements("\n".join(_array_elements(array)))


def parse_pep621_optional_deps(text: str) -> List[Tuple[str, str]]:
    """Parse every group under ``[project.optional-dependencies]`` (flattened).

    Optional dependencies are extras (not installed by default), so the
    collector tiers them after core runtime dependencies.
    """
    body = _slice_toml_table(text, "project.optional-dependencies")
    if not body:
        return []
    out: List[Tuple[str, str]] = []
    for _group, array in _iter_array_assignments(body):
        out.extend(parse_requirements("\n".join(_array_elements(array))))
    return out


def _poetry_table_deps(body: str, exclude_python: bool) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        match = _TOML_ASSIGN_RE.match(stripped)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if exclude_python and name.lower() == "python":
            continue
        out.append((name, first_version(value)))
    return out


def parse_poetry_deps(text: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Parse poetry tables into ``(runtime, dev)`` (name, version) lists.

    Runtime comes from ``[tool.poetry.dependencies]`` (``python`` excluded); dev
    from the legacy ``[tool.poetry.dev-dependencies]`` and every
    ``[tool.poetry.group.<name>.dependencies]`` table. Scoping to these tables
    (vs. the old "match anywhere" regex) also stops dev-group deps from being
    mislabelled as runtime.
    """
    runtime = _poetry_table_deps(
        _slice_toml_table(text, "tool.poetry.dependencies"), exclude_python=True
    )
    dev = _poetry_table_deps(
        _slice_toml_table(text, "tool.poetry.dev-dependencies"), exclude_python=True
    )
    for header in _POETRY_GROUP_RE.findall(text):
        dev.extend(_poetry_table_deps(_slice_toml_table(text, header), exclude_python=True))
    return runtime, dev


def _is_dev_requirements(rel: str) -> bool:
    """True when a requirements file's name / parent dir marks it as dev/test."""
    p = Path(rel)
    haystack = f"{p.parent.name}/{p.name}".lower()
    return "dev" in haystack or "test" in haystack


def _collect_major_dependencies(ctx: RepoContext, max_items: int) -> List[str]:
    root = ctx.root
    # key (lowercased name) -> (display_name, version, tier); first-writer-wins,
    # so higher-priority sources are processed first. Tiers: allow-list match=0,
    # non-allow runtime direct dep=1 (Python hybrid fill), dev tooling=2.
    collected: "OrderedDict[str, Tuple[str, str, int]]" = OrderedDict()

    def put(name: str, version: str, tier: int) -> None:
        key = name.lower()
        if key not in collected:
            collected[key] = (name, version, tier)

    def consider_python(name: str, version: str, dev: bool) -> None:
        key = name.lower()
        if key in collected:
            return
        if dev:
            tier = 2
        elif key in IMPORTANT_DEPENDENCIES:
            tier = 0
        else:
            tier = 1
        # Python names are normalised to lowercase (PEP 503) for display.
        collected[key] = (key, version, tier)

    # --- JS/TS (package.json) — allow-list only (unchanged) ---
    pkg = ctx.package_json
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if name in IMPORTANT_DEPENDENCIES:
                put(name, str(version), 0)

    # --- Python (hybrid), sources in priority order: pyproject > Pipfile >
    #     requirements > setup.cfg. allow-list deps lead; direct runtime deps
    #     (e.g. kaggle) fill remaining slots; dev tooling sinks to the bottom. ---
    pyproject = ctx.pyproject_toml
    if pyproject:
        for name, version in parse_pep621_deps(pyproject):
            consider_python(name, version, dev=False)
        for name, version in parse_pep621_optional_deps(pyproject):
            consider_python(name, version, dev=True)
        poetry_runtime, poetry_dev = parse_poetry_deps(pyproject)
        for name, version in poetry_runtime:
            consider_python(name, version, dev=False)
        for name, version in poetry_dev:
            consider_python(name, version, dev=True)

    for rel in _tracked_with_basename(ctx, "Pipfile")[:_MAX_DEP_FILES]:
        grouped = parse_pipfile_grouped(read_text(root / rel))
        for name, version in grouped["packages"]:
            consider_python(name, version, dev=False)
        for name, version in grouped["dev-packages"]:
            consider_python(name, version, dev=True)

    for rel in _tracked_requirements(ctx)[:_MAX_DEP_FILES]:
        dev = _is_dev_requirements(rel)
        for name, version in parse_requirements(read_text(root / rel)):
            consider_python(name, version, dev=dev)

    for rel in _tracked_with_basename(ctx, "setup.cfg")[:_MAX_DEP_FILES]:
        for name, version in parse_setup_cfg_requires(read_text(root / rel)):
            consider_python(name, version, dev=False)

    # --- Flutter/Dart (pubspec.yaml) — allow-list only ---
    for rel in _tracked_with_basename(ctx, "pubspec.yaml")[:_MAX_DEP_FILES]:
        for name, version in parse_pubspec_deps(read_text(root / rel)):
            if name in FLUTTER_IMPORTANT_DEPENDENCIES:
                put(name, version, 0)

    # --- Go (go.mod) — allow-list only ---
    go_mod = read_text(root / "go.mod") if (root / "go.mod").exists() else ""
    for line in go_mod.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            mod, version = parts
            leaf = mod.rsplit("/", 1)[-1]
            if leaf in IMPORTANT_DEPENDENCIES:
                put(leaf, version, 0)

    # Cap is applied AFTER the stable tier sort, so an allow-list (tier-0) match
    # collected late in source order is never crowded out by an earlier tier-1
    # dependency. Within a tier, declaration order is preserved.
    items = sorted(collected.values(), key=lambda entry: entry[2])
    return [_format_dep(name, version) for name, version, _tier in items[:max_items]]


class DependenciesCollector:
    name = "dependencies"
    section_title = ""
    priority = 5

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_major_deps
        deps = _collect_major_dependencies(ctx, max_items)
        ctx.results["major_dependencies"] = deps
        # This collector contributes to the header, not its own section
        return None


def register():
    return DependenciesCollector()
