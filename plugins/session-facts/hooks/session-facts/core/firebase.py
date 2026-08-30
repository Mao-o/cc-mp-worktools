from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Iterable, List, Set, TYPE_CHECKING

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
# distinct from the "@firebase/*" scoped-package check below. ctx.all_deps
# is a dict keyed by the exact package.json dependency name (core/context.py
# builds it from the parsed JSON, not from raw manifest text), so
# `name in deps` and `.startswith("@firebase/")` below are already an exact
# dependency-name match and a scoped-prefix match, not a substring search
# over serialized text -- an unrelated package like "firebase-admin-helper"
# cannot pass either check.
_FIREBASE_JS_DEPENDENCY_NAMES = ("firebase", "firebase-admin", "firebase-functions")

# Python dependency names that mean "this is a Firebase project" on their
# own: firebase-admin (server SDK) and firebase-functions (the Python Cloud
# Functions runtime package), matched by exact normalized name -- see
# _normalize_dep_name() below. A package merely containing one of these as
# a substring (e.g. "firebase-admin-helper", "not-firebase-admin") must not
# match; see _pyproject_dependency_names()/_dep_names_from_requirements()
# and their caller, _has_firebase_python_dependency().
_FIREBASE_PY_DEPENDENCY_NAMES = frozenset({"firebase-admin", "firebase-functions"})

# Anchored on YAML key syntax (like detectors/flutter.py's own regexes) so a
# match requires an actual pubspec dependency entry, not an incidental
# "firebase_core" substring in a comment or string elsewhere in the file.
_FIREBASE_CORE_PUBSPEC_RE = re.compile(r"(?m)^\s*firebase_core\s*:")

# The leading package-name token of a PEP 508 requirement string (e.g. from
# a requirements.txt line, or a pyproject.toml dependency array element).
# Stops at whitespace, "[extras]", a version specifier ("=="/">="/...), an
# environment marker (";"), or a direct URL reference ("@ url") -- whatever
# follows is never part of the name. Does NOT match a legacy VCS requirement
# line (e.g. "git+https://.../pkg.git#egg=pkg"): that starts with the URL
# scheme, not a name token, so this regex reads only "git" off it -- see
# _EGG_NAME_RE/_egg_name_from_line() below, which _dep_names_from_requirements()
# checks first for exactly this shape.
_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")

# The "#egg=name" fragment of a legacy VCS requirement line (pip's older,
# still-seen-in-the-wild convention for naming a direct URL/VCS requirement,
# e.g. "git+https://example.com/x/pkg.git#egg=pkg" or, with an editable
# install, "-e git+https://.../pkg.git#egg=pkg"). The captured value stops
# at the first "[" (an extras marker, e.g. "#egg=pkg[extra]"), "&" (a
# following URL-query-style fragment, e.g. "#egg=pkg&subdirectory=sub"), or
# whitespace -- whichever comes first -- so none of those trail into the
# name. The "(?<!\s)" guard mirrors pip's own COMMENT_RE
# (`re.compile(r"(^|\s+)#.*$")`): a "#" preceded by whitespace starts a real
# trailing comment there, not a URL fragment, so e.g.
# "pkg==1.0  # see #egg=other" must still resolve to "pkg", not "other". A
# "#" at the very start of a line is the other half of pip's rule (handled
# by the whole-line "#"-prefix check in _dep_names_from_requirements(), not
# by this regex).
_EGG_NAME_RE = re.compile(r"(?<!\s)#egg=([^&\[\s]+)")

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


def _normalize_dep_name(name: str) -> str:
    """PEP 503 normalization: lowercase; runs of "-", "_", "." collapsed to "-".

    So "firebase_admin", "Firebase.Admin", and "firebase-admin" all compare
    equal here -- pip/PyPI already treat these separators as interchangeable
    in a package name. The pre-fix substring check special-cased the
    "firebase_admin" underscore spelling too (``"firebase_admin" in
    pyproject_lower``); normalizing keeps that coverage without reopening
    the substring hole a plain ``==`` comparison on the raw name would
    otherwise leave (e.g. "firebase.admin" would not compare equal to
    "firebase-admin" without this).
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _dep_name_from_spec(spec: str) -> str:
    """Return the normalized package name from a single requirement spec.

    ``spec`` is a bare PEP 508 string (already stripped of any surrounding
    comment/option syntax by the caller) such as
    ``firebase-admin[async]>=6.5.0; python_version >= "3.9"`` or
    ``firebase-admin @ git+https://example.com/x.git``. Returns "" when
    ``spec`` doesn't start with a valid name character (e.g. an empty
    string, or a bare URL with no leading name).
    """
    match = _DEP_NAME_RE.match(spec.strip())
    return _normalize_dep_name(match.group(1)) if match else ""


def _egg_name_from_line(line: str) -> str:
    """Return the normalized ``#egg=`` fragment name from one line, if any.

    ``line`` is a single requirements.txt line, already right-stripped but
    otherwise as written (in particular, NOT yet split on "#" -- doing that
    first would delete the very fragment this reads). Returns "" when no
    ``#egg=`` fragment is present, so the caller can fall back to the
    ordinary name extraction. See ``_EGG_NAME_RE`` above for what the
    captured value stops at and why.
    """
    match = _EGG_NAME_RE.search(line)
    return _normalize_dep_name(match.group(1)) if match else ""


def _dep_names_from_pep508_array(items: object) -> Iterable[str]:
    """Yield normalized names from a TOML array of PEP 508 requirement strings.

    ``items`` is whatever tomllib produced for a dependencies-shaped key; a
    non-list value (missing key, or ``dynamic = ["dependencies"]`` leaving
    no literal array to read) yields nothing rather than raising. Non-string
    array elements are skipped the same way -- PEP 735's
    ``{"include-group": "..."}`` cross-group reference is the one shape
    this matters for in practice; resolving those references is out of
    scope here (a dependency only actually named in an included group,
    with no direct mention in the including group, is not detected).
    """
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, str):
            name = _dep_name_from_spec(item)
            if name:
                yield name


def _dep_names_from_requirements(text: str) -> Iterable[str]:
    """Yield normalized package names declared in a requirements.txt-style file.

    Skips blank lines and lines that are themselves a full comment (first
    non-whitespace character is "#") outright. Otherwise checks each line
    for a legacy VCS ``#egg=name`` fragment first (see
    ``_egg_name_from_line()``) -- a requirement given as a direct VCS URL
    (``git+https://.../pkg.git#egg=pkg``, optionally with a leading ``-e``
    for an editable install) starts with the URL scheme or the ``-e`` flag,
    not a name token, so it has no other way to be read. When no ``#egg=``
    fragment is present, strips an inline trailing ``#`` comment and skips
    option lines (``-r other.txt``, ``-e .``, ``--hash=...``) outright,
    since their first token is a flag, not a name, before falling back to
    the ordinary leading-token extraction.
    """
    for raw_line in text.splitlines():
        stripped_raw = raw_line.strip()
        if not stripped_raw or stripped_raw.startswith("#"):
            continue
        egg_name = _egg_name_from_line(stripped_raw)
        if egg_name:
            yield egg_name
            continue
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _dep_name_from_spec(line)
        if name:
            yield name


def _pyproject_dependency_names(text: str) -> Set[str]:
    """Return every normalized dependency name declared in a pyproject.toml.

    Parses the manifest with ``tomllib`` (stdlib, Python 3.11+ -- this repo
    already assumes 3.11+, see README) and collects declared names from:

    - ``[project].dependencies`` (PEP 621)
    - every group under ``[project.optional-dependencies]``
    - ``[tool.poetry.dependencies]`` (the ``python`` key itself names a
      runtime constraint, not a package, and is excluded)
    - ``[tool.poetry.dev-dependencies]`` (legacy, still emitted by older
      poetry versions/lockfiles)
    - every ``[tool.poetry.group.*.dependencies]`` table
    - every group under ``[dependency-groups]`` (PEP 735)
    - ``[tool.uv].dev-dependencies`` (uv's pre-PEP-735 dev-dependency list;
      deprecated upstream in favor of ``[dependency-groups].dev`` but still
      real in the wild)
    - every group under ``[tool.pdm.dev-dependencies]`` (PDM's own
      pre-PEP-735 table, one group per key, same shape as poetry's groups)
    - ``dependencies``/``extra-dependencies`` under every
      ``[tool.hatch.envs.*]`` table

    A parse failure -- malformed TOML, a manifest truncated mid-table by
    core.fs.read_text's size cap, or pathological nesting that exhausts the
    recursion limit inside tomllib itself (observed to raise
    ``RecursionError``, which is not a ``tomllib.TOMLDecodeError``) --
    returns an empty set. "Can't tell" folds into "not detected" here, same
    as a missing file, rather than raising and losing the whole detector or
    the whole repo_notes.py collector output (both call has_firebase()).
    """
    try:
        data = tomllib.loads(text)
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()

    names: Set[str] = set()

    project = data.get("project")
    if isinstance(project, dict):
        names.update(_dep_names_from_pep508_array(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                names.update(_dep_names_from_pep508_array(group))

    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            for table_name in ("dependencies", "dev-dependencies"):
                table = poetry.get(table_name)
                if isinstance(table, dict):
                    names.update(
                        _normalize_dep_name(name)
                        for name in table
                        if name.lower() != "python"
                    )
            group = poetry.get("group")
            if isinstance(group, dict):
                for group_body in group.values():
                    group_deps = (
                        group_body.get("dependencies")
                        if isinstance(group_body, dict)
                        else None
                    )
                    if isinstance(group_deps, dict):
                        names.update(_normalize_dep_name(name) for name in group_deps)

        uv = tool.get("uv")
        if isinstance(uv, dict):
            names.update(_dep_names_from_pep508_array(uv.get("dev-dependencies")))

        pdm = tool.get("pdm")
        pdm_dev = pdm.get("dev-dependencies") if isinstance(pdm, dict) else None
        if isinstance(pdm_dev, dict):
            for group in pdm_dev.values():
                names.update(_dep_names_from_pep508_array(group))

        hatch = tool.get("hatch")
        envs = hatch.get("envs") if isinstance(hatch, dict) else None
        if isinstance(envs, dict):
            for env in envs.values():
                if not isinstance(env, dict):
                    continue
                names.update(_dep_names_from_pep508_array(env.get("dependencies")))
                names.update(_dep_names_from_pep508_array(env.get("extra-dependencies")))

    dependency_groups = data.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            names.update(_dep_names_from_pep508_array(group))

    return names


def _has_firebase_python_dependency(ctx: "RepoContext") -> bool:
    if _pyproject_dependency_names(ctx.pyproject_toml) & _FIREBASE_PY_DEPENDENCY_NAMES:
        return True
    for rel in _tracked_requirements_files(ctx)[:_MAX_REQUIREMENTS_SCAN]:
        text = read_text(ctx.root / rel)
        if set(_dep_names_from_requirements(text)) & _FIREBASE_PY_DEPENDENCY_NAMES:
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
    this one); firebase-admin or firebase-functions declared as an actual
    Python dependency -- exact normalized name match, not substring, across
    pyproject.toml's common dependency-declaration tables (PEP 621, Poetry,
    PEP 735, and the uv/PDM/Hatch dev-dependency tables -- see
    _pyproject_dependency_names()) or any tracked requirements*.txt;
    firebase_core in any tracked pubspec.yaml.

    The Python-dependency check used to be a raw substring search over the
    whole pyproject.toml/requirements.txt text
    (``"firebase-admin" in text.lower()``), so an unrelated package like
    "firebase-admin-helper" -- in a comment, in a `name = "..."` field, or
    anywhere else in the manifest -- or "not-firebase-admin" falsely tagged
    the repo as Firebase. It now parses the manifest and requirements file
    and compares declared dependency names exactly (0.6.0 had weighed and
    deferred a tomllib-based parser here for a different reason --
    avoiding behavior that would differ across Python versions where
    tomllib may or may not be present -- but that trade-off no longer
    applies now that this project targets Python 3.11+ across the board).

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
    _has_firebase_js_dependency() above -- a Python project whose only
    Firebase signal is a "firebase-functions" pip dependency still gets the
    general "firebase" tag via has_firebase() (see
    _FIREBASE_PY_DEPENDENCY_NAMES), just not this narrower npm-scoped one.
    """
    return "firebase-functions" in ctx.all_deps
