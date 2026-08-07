from __future__ import annotations

import posixpath
import re
from typing import List, Optional, Set, Tuple

JS_EXTENSIONS: Tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTENSIONS: Tuple[str, ...] = (".py",)

# Matches the specifier string after import/from/require/dynamic-import
# keywords. Extraction is intentionally permissive (no AST) — resolution is
# what decides whether a specifier maps to an in-repo file.
_JS_SPEC_RE = re.compile(
    r'''(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|\bimport\s+)(['"])([^'"]+)\1'''
)

_PY_FROM_RE = re.compile(r'^\s*from\s+([.\w]+)\s+import\b', re.M)
_PY_IMPORT_RE = re.compile(r'^\s*import\s+([.\w]+(?:\s*,\s*[.\w]+)*)', re.M)


def extract_js_specifiers(text: str) -> List[str]:
    """Extract raw import/require specifier strings from JS/TS source text."""
    return [m.group(2) for m in _JS_SPEC_RE.finditer(text)]


def extract_python_specifiers(text: str) -> List[str]:
    """Extract raw module specifiers from Python ``from``/``import`` statements."""
    specs: List[str] = []
    for m in _PY_FROM_RE.finditer(text):
        specs.append(m.group(1))
    for m in _PY_IMPORT_RE.finditer(text):
        for name in m.group(1).split(","):
            name = name.strip()
            if name:
                specs.append(name)
    return specs


def resolve_js_import(spec: str, importer_dir: str, tracked: Set[str]) -> Optional[str]:
    """Resolve a relative JS/TS specifier (``./foo``, ``../bar``) to a tracked file.

    Bare package names (``react``) and path aliases (``@/utils``) are left
    unresolved by design — mapping those requires a module resolver
    (node_modules lookup / tsconfig paths) that this lightweight, stdlib-only
    heuristic does not attempt.
    """
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(importer_dir, spec)) if importer_dir else posixpath.normpath(spec)
    candidates = [base]
    for ext in JS_EXTENSIONS:
        candidates.append(base + ext)
        candidates.append(posixpath.join(base, "index" + ext))
    for candidate in candidates:
        if candidate in tracked:
            return candidate
    return None


def resolve_python_import(spec: str, importer_rel: str, tracked: Set[str]) -> Optional[str]:
    """Resolve a Python module specifier to a tracked file.

    Handles both relative imports (leading dots, resolved against the
    importer's package directory) and absolute imports resolved against the
    repo root and a conventional ``src/`` layout. Stdlib and third-party
    modules are left unresolved by design (they aren't tracked files).
    """
    dots = 0
    while dots < len(spec) and spec[dots] == ".":
        dots += 1
    rest = spec[dots:]
    rest_path = rest.replace(".", "/") if rest else ""

    if dots:
        importer_dir = posixpath.dirname(importer_rel)
        parts = importer_dir.split("/") if importer_dir else []
        climb = dots - 1
        if climb:
            parts = parts[:-climb] if climb <= len(parts) else []
        base = "/".join(parts)
        candidate_dir = posixpath.normpath(posixpath.join(base, rest_path)) if rest_path else (base or ".")
        prefixes: Tuple[str, ...] = ("",)
    else:
        candidate_dir = rest_path
        prefixes = ("", "src/")

    for prefix in prefixes:
        rooted = posixpath.normpath(posixpath.join(prefix, candidate_dir)) if prefix else candidate_dir
        for suffix in (".py", "/__init__.py"):
            candidate = rooted + suffix
            if candidate in tracked:
                return candidate
    return None
