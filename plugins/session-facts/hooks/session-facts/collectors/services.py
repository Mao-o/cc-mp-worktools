from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.constants import (
    CODE_EXTENSIONS,
    SERVICE_AMBIGUOUS_DIR_MARKERS,
    SERVICE_DIR_MARKERS,
    SERVICE_NOISE_DIR_MARKERS,
    SERVICE_ROUTE_DIR_MARKERS,
)
from core.context import RepoContext
from core.util import filter_to_cwd, is_test_path

DEPRIORITY_NAMES = {
    "__init__.py",
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
    "types.ts",
    "types.py",
    "type.ts",
    "mod.rs",
}

# Tier 1: conventional process entry points. A file with one of these
# names is what you open first to see how the program starts, regardless
# of which directory it sits in.
PRIORITY_NAMES = {
    "__main__.py",
    "main.py",
    "app.py",
    "server.py",
    "wsgi.py",
    "asgi.py",
    "manage.py",
    "main.go",
    "main.rs",
    "main.ts",
    "main.js",
    "app.ts",
    "app.js",
    "server.ts",
    "server.js",
    # Vite / CRA style browser entry.
    "main.tsx",
    "main.jsx",
    # Next.js App Router / Remix-style route handlers: one per API path.
    "route.ts",
    "route.js",
}

# Next.js App Router page/layout files live under URL-segment directories
# that can be called anything (``app/services/d-payment/page.tsx``); they
# are views, not service entry points.
_VIEW_NAMES = {"page.tsx", "page.jsx", "page.ts", "page.js", "layout.tsx", "layout.jsx", "layout.ts", "layout.js"}

# Files named for the plumbing they hold, not for a role in request flow.
_NOISE_NAME_TOKENS = ("types", "utils", "util", "helpers", "helper", "constants", "config")

# Names that are unambiguous at any depth (``__main__.py`` three levels
# down is still a program entry); the rest of PRIORITY_NAMES decay with
# depth because ``app.py`` / ``server.ts`` are also common module names.
_STRONG_ENTRY_NAMES = {
    "__main__.py", "main.py", "manage.py", "wsgi.py", "asgi.py",
    "main.go", "main.rs", "main.tsx", "main.jsx", "route.ts", "route.js",
}
_NEVER_ENTRY_NAMES = {"__init__.py"}

# ``index.*`` is an entry point only when it sits near the top of a
# package (``src/index.ts``, ``functions/src/index.ts``); deeper down it is
# the barrel-file convention and carries no entry-point signal.
_INDEX_NAMES = {"index.ts", "index.tsx", "index.js", "index.mjs", "index.cjs"}
_INDEX_ENTRY_MAX_PARTS = 3

# Name tokens that mark a file as part of the service layer (tier 2) on
# their own, and also unlock the ambiguous directory markers (``api/``,
# ``client/``) that are otherwise too common to score.
_NAME_TOKENS = (
    "service", "client", "repository", "gateway", "adapter", "usecase",
    "handler", "controller", "router", "routes",
)

# At most this many entries from the same parent directory, so a
# ``routes/`` folder with 15 route files cannot fill every slot on its own
# (the remaining slots go to the next-best directories; if there are no
# other candidates the overflow fills back in).
_MAX_PER_DIR = 4

SCORE_MANIFEST_ENTRY = 12
SCORE_PRIORITY_NAME = 10
SCORE_ROUTE_DIR = 6
SCORE_SERVICE_DIR = 4
SCORE_AMBIGUOUS_DIR = 3
SCORE_NAME_TOKEN = 2
SCORE_DEPRIORITY_NAME = -3
SCORE_NOISE_NAME = -4
# Slots the service layer keeps even when tier 1 alone could fill the list,
# so both "where requests enter" and "where the logic lives" stay visible.
TIER2_RESERVED = 4


class ServicesCollector:
    name = "services"
    section_title = "## Service Entry Points"
    priority = 20

    def should_run(self, ctx: RepoContext) -> bool:
        return len(ctx.tracked_files) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_service_entries
        cwd_rel = ctx.cwd_relative
        manifest_entries = manifest_entry_points(ctx)

        if not cwd_rel:
            entries = _collect_service_entries(
                ctx.tracked_files, max_items, manifest_entries=manifest_entries
            )
            if not entries:
                return None
            return _format_section(self.section_title, entries)

        cwd_files = filter_to_cwd(ctx.tracked_files, cwd_rel)
        cwd_entries = _collect_service_entries(
            cwd_files, max_items, manifest_entries=manifest_entries
        )
        repo_entries = _collect_service_entries(
            ctx.tracked_files, max(max_items // 2, 4), manifest_entries=manifest_entries
        )
        repo_minus_cwd = [p for p in repo_entries if p not in cwd_entries]

        sections = []
        if cwd_entries:
            sections.append(
                _format_section(f"## Service Entry Points (cwd: {cwd_rel})", cwd_entries)
            )
        if repo_minus_cwd:
            sections.append(
                _format_section("## Service Entry Points (repo-wide)", repo_minus_cwd)
            )
        elif not cwd_entries and repo_entries:
            sections.append(_format_section(self.section_title, repo_entries))
        return "\n\n".join(sections) if sections else None


def _format_section(title: str, entries: List[str]) -> str:
    lines = [title]
    for path in entries:
        lines.append(f"- {path}")
    return "\n".join(lines)


def manifest_entry_points(ctx: RepoContext) -> Dict[str, str]:
    """Files the project's own manifests name as entry points.

    Returns ``{tracked path: reason}``. Sources: package.json ``main`` /
    ``bin`` / ``module`` (root and workspace manifests), and pyproject
    ``[project.scripts]`` targets (``pkg.mod:func`` -> ``pkg/mod.py``).
    Only paths that are actually tracked are returned, so a ``main`` that
    points at a build artifact (``dist/index.js``) is ignored.
    """
    tracked = set(ctx.tracked_files)
    out: Dict[str, str] = {}

    def add(candidate: str, reason: str) -> None:
        cand = candidate.lstrip("./")
        if cand in tracked and cand not in out:
            out[cand] = reason

    for rel_dir, pkg in ctx.package_json_manifests():
        prefix = f"{rel_dir}/" if rel_dir else ""
        for key in ("main", "module"):
            value = pkg.get(key)
            if isinstance(value, str) and value:
                add(prefix + value, f"package.json {key}")
        bins = pkg.get("bin")
        if isinstance(bins, str):
            add(prefix + bins, "package.json bin")
        elif isinstance(bins, dict):
            for value in bins.values():
                if isinstance(value, str):
                    add(prefix + value, "package.json bin")

    for rel_dir, text in ctx.pyproject_manifests():
        prefix = f"{rel_dir}/" if rel_dir else ""
        for target in _pyproject_script_targets(text):
            module = target.split(":", 1)[0].strip()
            if not module:
                continue
            rel = module.replace(".", "/")
            add(prefix + rel + ".py", "pyproject scripts")
            add(prefix + rel + "/__main__.py", "pyproject scripts")
    return out


def _pyproject_script_targets(text: str) -> List[str]:
    """Values of the ``[project.scripts]`` / ``[tool.poetry.scripts]`` tables."""
    targets: List[str] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = line in ("[project.scripts]", "[tool.poetry.scripts]")
            continue
        if not in_table or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip().strip("\"'")
        if value:
            targets.append(value)
    return targets


def _score_path(path_str: str, manifest_entries: Dict[str, str]) -> Tuple[int, bool]:
    """``(score, is_tier1)``; score 0 means "not a candidate". Tier 1 is
    where a request or process *enters* (manifest entry, entry-point file
    name, route/controller directory); tier 2 is the service/repository
    layer behind it."""
    p = Path(path_str)
    if p.suffix.lower() not in CODE_EXTENSIONS:
        return 0, False
    if is_test_path(path_str):
        return 0, False
    lowered_parts = [part.lower() for part in p.parts[:-1]]
    if any(part in SERVICE_NOISE_DIR_MARKERS for part in lowered_parts):
        return 0, False
    # Tool/editor/harness config trees (.opencode/, .storybook/, .cursor/)
    # are never where the program starts.
    if any(part.startswith(".") for part in lowered_parts):
        return 0, False

    name = p.name.lower()
    if name in _VIEW_NAMES or name in _NEVER_ENTRY_NAMES:
        return 0, False
    depth = len(p.parts) - 1
    score = 0
    entry_name = False
    if path_str in manifest_entries:
        score += SCORE_MANIFEST_ENTRY
        entry_name = True
    if name in _STRONG_ENTRY_NAMES:
        score += SCORE_PRIORITY_NAME
        entry_name = True
    elif name in PRIORITY_NAMES:
        # ``api/app.py`` is the process entry; ``api/core/x/y/app.py`` is a
        # module that happens to share the name. Decay with depth.
        score += SCORE_PRIORITY_NAME if depth <= 2 else (
            SCORE_PRIORITY_NAME // 2 if depth <= 3 else 2
        )
        entry_name = True
    elif name in _INDEX_NAMES and len(p.parts) <= _INDEX_ENTRY_MAX_PARTS:
        score += SCORE_PRIORITY_NAME - 2
        entry_name = True

    has_token = any(token in name for token in _NAME_TOKENS)
    route_hit = any(marker in lowered_parts for marker in SERVICE_ROUTE_DIR_MARKERS)
    # ``api/`` as the *top-level* directory is the whole backend (dify) and
    # says nothing about a file; nested (``functions/src/api``, ``app/api``)
    # it is the request-entry layer.
    if not route_hit and "api" in lowered_parts[1:]:
        route_hit = True
    if route_hit:
        score += SCORE_ROUTE_DIR
    for marker in SERVICE_DIR_MARKERS:
        if marker in lowered_parts:
            score += SCORE_SERVICE_DIR
            break
    if (has_token or name in PRIORITY_NAMES) and not route_hit:
        for marker in SERVICE_AMBIGUOUS_DIR_MARKERS:
            if marker in lowered_parts:
                score += SCORE_AMBIGUOUS_DIR
                break
    if has_token:
        score += SCORE_NAME_TOKEN
    if name in DEPRIORITY_NAMES and not entry_name:
        score += SCORE_DEPRIORITY_NAME
    stem = name.rsplit(".", 1)[0]
    if any(stem.endswith(token) or stem.startswith(token) for token in _NOISE_NAME_TOKENS):
        score += SCORE_NOISE_NAME
    if score <= 0:
        return 0, False
    # Shallower files are closer to the process boundary; use the depth as
    # a small bonus so ``api/app.py`` outranks ``api/core/x/y/app.py``.
    score += max(0, 3 - depth)
    return score, entry_name or route_hit


def _collect_service_entries(
    tracked_files: List[str],
    max_items: int,
    manifest_entries: Optional[Dict[str, str]] = None,
) -> List[str]:
    manifest_entries = manifest_entries or {}
    tier1: List[Tuple[int, str]] = []
    tier2: List[Tuple[int, str]] = []
    for path_str in dict.fromkeys(tracked_files):
        score, is_tier1 = _score_path(path_str, manifest_entries)
        if score <= 0:
            continue
        (tier1 if is_tier1 else tier2).append((-score, path_str))
    tier1.sort()
    tier2.sort()

    tier2_slots = min(TIER2_RESERVED, len(tier2), max_items)
    tier1_slots = max_items - tier2_slots
    chosen = _pick_spread(tier1, tier1_slots)
    chosen += _pick_spread(tier2, max_items - len(chosen))
    return chosen[:max_items]


def _pick_spread(candidates: List[Tuple[int, str]], slots: int) -> List[str]:
    """Take up to ``slots`` paths in score order, at most ``_MAX_PER_DIR``
    per parent directory; the overflow fills remaining slots at the end."""
    chosen: List[str] = []
    overflow: List[str] = []
    per_dir: Dict[str, int] = {}
    for _score, path in candidates:
        if len(chosen) >= slots:
            break
        parent = str(Path(path).parent)
        if per_dir.get(parent, 0) >= _MAX_PER_DIR:
            overflow.append(path)
            continue
        per_dir[parent] = per_dir.get(parent, 0) + 1
        chosen.append(path)
    for path in overflow:
        if len(chosen) >= slots:
            break
        chosen.append(path)
    return chosen


def register():
    return ServicesCollector()
