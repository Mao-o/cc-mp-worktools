from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from core.constants import FLUTTER_IMPORTANT_DEPENDENCIES, IMPORTANT_DEPENDENCIES
from core.context import RepoContext
from core.fs import read_text
from core.util import normalize_version

# A requirement spec line: name, optional [extras], optional operator+version.
_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?:[=<>~!]=?\s*([0-9][^\s;,#]*))?"
)
# A pubspec / Pipfile dep entry: name then value after ':' or '='.
_PUBSPEC_DEP_RE = re.compile(r"^( +)([A-Za-z0-9_]+)\s*:\s*(.*)$")
_PIPFILE_DEP_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*(.+)$")
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


def parse_pipfile(text: str) -> List[Tuple[str, str]]:
    """Parse Pipfile [packages]/[dev-packages] entries into (name, version)."""
    out: List[Tuple[str, str]] = []
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
        out.append((match.group(1), version_match.group(1) if version_match else ""))
    return out


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


def _collect_major_dependencies(ctx: RepoContext, max_items: int) -> List[str]:
    results: List[str] = []
    seen: Set[str] = set()
    root = ctx.root

    def add(name: str, version: str) -> bool:
        if name in seen or len(results) >= max_items:
            return False
        results.append(_format_dep(name, version))
        seen.add(name)
        return True

    # --- JS/TS (package.json) ---
    pkg = ctx.package_json
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if name in IMPORTANT_DEPENDENCIES:
                add(name, str(version))

    # --- Python, in priority order: pyproject > Pipfile > requirements > setup.cfg ---
    pyproject = ctx.pyproject_toml
    for name in sorted(IMPORTANT_DEPENDENCIES):
        if name in seen or not pyproject:
            continue
        match = re.search(rf'(?mi)^\s*{re.escape(name)}\s*=\s*["\']([^"\']+)["\']', pyproject)
        if match:
            add(name, match.group(1))

    python_sources: List[Tuple[str, ...]] = [
        ("parse_pipfile", *_tracked_with_basename(ctx, "Pipfile")),
        ("parse_requirements", *_tracked_requirements(ctx)),
        ("parse_setup_cfg", *_tracked_with_basename(ctx, "setup.cfg")),
    ]
    parsers = {
        "parse_pipfile": parse_pipfile,
        "parse_requirements": parse_requirements,
        "parse_setup_cfg": parse_setup_cfg_requires,
    }
    for parser_name, *paths in python_sources:
        parser = parsers[parser_name]
        for rel in paths[:_MAX_DEP_FILES]:
            if len(results) >= max_items:
                break
            for name, version in parser(read_text(root / rel)):
                if name.lower() in IMPORTANT_DEPENDENCIES:
                    add(name.lower(), version)

    # --- Flutter/Dart (pubspec.yaml) ---
    for rel in _tracked_with_basename(ctx, "pubspec.yaml")[:_MAX_DEP_FILES]:
        if len(results) >= max_items:
            break
        for name, version in parse_pubspec_deps(read_text(root / rel)):
            if name in FLUTTER_IMPORTANT_DEPENDENCIES:
                add(name, version)

    # --- Go (go.mod) ---
    go_mod = read_text(root / "go.mod") if (root / "go.mod").exists() else ""
    for line in go_mod.splitlines():
        if len(results) >= max_items:
            break
        parts = line.strip().split()
        if len(parts) == 2:
            mod, version = parts
            leaf = mod.rsplit("/", 1)[-1]
            if leaf in IMPORTANT_DEPENDENCIES:
                add(leaf, version)

    return results[:max_items]


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
