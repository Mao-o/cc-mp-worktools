from __future__ import annotations

import re
from pathlib import Path
from typing import List, TYPE_CHECKING

from core.fs import read_text

if TYPE_CHECKING:
    from core.context import RepoContext

# firebase.json / .firebaserc existing at the repo root is a Firebase signal
# on its own, independent of any dependency manifest: a Cloud Functions-only
# repo (whose firebase-functions dependency lives in a nested
# functions/package.json, not the tracked root one) or a static-hosting-only
# repo (no package.json at all) has neither an npm nor a Python dependency
# hit below, but `firebase init` always drops at least one of these two
# files at the project root.
_FIREBASE_CONFIG_FILES = ("firebase.json", ".firebaserc")

# npm dependency names that mean "this is a Firebase project" on their own,
# distinct from the "@firebase/*" scoped-package check below.
_FIREBASE_JS_DEPENDENCY_NAMES = ("firebase", "firebase-admin", "firebase-functions")

# Anchored on YAML key syntax (like detectors/flutter.py's own regexes) so a
# match requires an actual pubspec dependency entry, not an incidental
# "firebase_core" substring in a comment or string elsewhere in the file.
_FIREBASE_CORE_PUBSPEC_RE = re.compile(r"(?m)^\s*firebase_core\s*:")

_MAX_REQUIREMENTS_SCAN = 6
_MAX_PUBSPEC_SCAN = 12


def _has_firebase_js_dependency(ctx: "RepoContext") -> bool:
    deps = ctx.all_deps
    if any(name in deps for name in _FIREBASE_JS_DEPENDENCY_NAMES):
        return True
    return any(name.startswith("@firebase/") for name in deps)


def _tracked_requirements_files(ctx: "RepoContext") -> List[str]:
    out = []
    for p in ctx.tracked_files:
        base = Path(p).name
        if base == "requirements.txt" or (base.startswith("requirements") and base.endswith(".txt")):
            out.append(p)
    return out


def _has_firebase_python_dependency(ctx: "RepoContext") -> bool:
    pyproject_lower = ctx.pyproject_toml.lower()
    if "firebase-admin" in pyproject_lower or "firebase_admin" in pyproject_lower:
        return True
    for rel in _tracked_requirements_files(ctx)[:_MAX_REQUIREMENTS_SCAN]:
        text = read_text(ctx.root / rel).lower()
        if "firebase-admin" in text or "firebase_admin" in text:
            return True
    return False


def _has_firebase_flutter_dependency(ctx: "RepoContext") -> bool:
    pubspec_paths = [p for p in ctx.tracked_files if Path(p).name == "pubspec.yaml"]
    for rel in pubspec_paths[:_MAX_PUBSPEC_SCAN]:
        if _FIREBASE_CORE_PUBSPEC_RE.search(read_text(ctx.root / rel)):
            return True
    return False


def has_firebase(ctx: "RepoContext") -> bool:
    """True when this repo shows any signal of Firebase integration.

    Single source of truth for "is this a Firebase project", called from
    both detectors/firebase.py (the ``firebase`` stack tag) and
    collectors/repo_notes.py (the firebase-integration note). Before this,
    the two had separately hand-rolled overlapping-but-different versions of
    this check: the detector only looked at root ``firebase``/``@firebase/*``
    deps, while repo_notes.py additionally covered firebase.json/.firebaserc,
    firebase-admin/firebase-functions, and pyproject firebase-admin -- so the
    stack tag silently missed Cloud Functions-only and Python-backend repos
    that the note already recognized as real Firebase usage.

    Checks, in order: firebase.json/.firebaserc at the repo root; the npm
    dependency names above (root package.json only -- a nested
    functions/package.json is not scanned, so a Cloud Functions dependency
    that lives only there is caught via the config-file check instead, not
    this one); firebase-admin in pyproject.toml or any tracked
    requirements*.txt; firebase_core in any tracked pubspec.yaml.

    Memoized on ctx.results: both call sites (the detector, which always
    runs, and repo_notes.py, which runs on every repo with any tracked
    files) evaluate this in the same run, and the Python-dependency check
    alone can read up to 6 requirements*.txt files -- worth avoiding twice
    per hook invocation. Mirrors how ctx.results["is_git_repo"] is already
    used as a cross-phase cache slot.
    """
    cached = ctx.results.get("has_firebase")
    if cached is not None:
        return cached
    root = ctx.root
    result = (
        any((root / name).exists() for name in _FIREBASE_CONFIG_FILES)
        or _has_firebase_js_dependency(ctx)
        or _has_firebase_python_dependency(ctx)
        or _has_firebase_flutter_dependency(ctx)
    )
    ctx.results["has_firebase"] = result
    return result


def has_firebase_functions(ctx: "RepoContext") -> bool:
    """True when ``firebase-functions`` is a direct root npm dependency.

    Narrower than has_firebase(): used to add a distinct
    ``firebase-functions`` stack tag alongside ``firebase`` so a Cloud
    Functions repo is distinguishable from one that only uses client-side
    Firebase SDKs. Root package.json only, same limitation as
    _has_firebase_js_dependency() above.
    """
    return "firebase-functions" in ctx.all_deps
