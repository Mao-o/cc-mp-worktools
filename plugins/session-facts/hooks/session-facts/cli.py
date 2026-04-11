from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

from core.constants import (
    DEFAULT_MAX_ENV_KEYS,
    DEFAULT_MAX_NOTES,
    DEFAULT_MAX_SCRIPT_ENTRIES,
    DEFAULT_MAX_SERVICE_ENTRIES,
    DEFAULT_MAX_TREE_LINES,
    DEFAULT_TREE_DEPTH,
    SKIP_DIRS,
)
from core.context import RepoContext
from core.fs import read_text, walk_files
from core.git import git_ls_files, git_root, is_git_repo
from core.pm import detect_package_manager
from core.util import collapse_space
from registry import discover_custom_plugins, discover_plugins
from renderer import render_header


def _infer_purpose(ctx: RepoContext) -> Optional[str]:
    pkg = ctx.package_json
    description = pkg.get("description")
    if isinstance(description, str) and description.strip():
        return collapse_space(description.strip())

    for readme_name in ("README.md", "README", "readme.md"):
        path = ctx.root / readme_name
        if not path.exists():
            continue
        text = read_text(path, limit=20_000)
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
                if line:
                    continue
            if line.startswith(("```", "---", "***")):
                continue
            if len(line) < 12:
                continue
            return collapse_space(line)
    return ctx.root.name


def summarize_repo(root: Path, args: argparse.Namespace, is_git: bool) -> str:
    ctx = RepoContext(root=root, args=args)
    ctx.tracked_files = git_ls_files(root) if is_git else walk_files(root, SKIP_DIRS)
    ctx.results["is_git_repo"] = is_git

    ctx.results["purpose"] = _infer_purpose(ctx)
    ctx.results["package_manager"] = detect_package_manager(ctx)

    # Phase 1: Run stack detectors
    pkg_dir = Path(__file__).resolve().parent
    detectors = discover_plugins(pkg_dir / "detectors", "detectors")
    detectors.sort(key=lambda d: d.priority)
    for detector in detectors:
        ctx.stack.extend(detector.detect(ctx))

    # Phase 2: Run section collectors
    collectors = discover_plugins(pkg_dir / "collectors", "collectors")
    collectors.extend(discover_custom_plugins(pkg_dir / "custom"))
    collectors.sort(key=lambda c: c.priority)

    # Collect sections (some collectors populate ctx.results for header)
    collected_sections = []
    for collector in collectors:
        if collector.should_run(ctx):
            section = collector.collect(ctx)
            if section:
                collected_sections.append(section)

    # Header rendered after collectors so ctx.results is fully populated
    sections = [render_header(ctx)] + collected_sections
    return "\n\n".join(sections)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a compact session-start facts bundle for coding agents."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Path inside the git repository")
    parser.add_argument("--format", choices=("markdown", "json", "human"), default="markdown")
    parser.add_argument("--tree-depth", type=int, default=DEFAULT_TREE_DEPTH)
    parser.add_argument("--max-tree-lines", type=int, default=DEFAULT_MAX_TREE_LINES)
    parser.add_argument("--max-service-entries", type=int, default=DEFAULT_MAX_SERVICE_ENTRIES)
    parser.add_argument("--max-script-entries", type=int, default=DEFAULT_MAX_SCRIPT_ENTRIES)
    parser.add_argument("--max-env-keys", type=int, default=DEFAULT_MAX_ENV_KEYS)
    parser.add_argument("--max-notes", type=int, default=DEFAULT_MAX_NOTES)
    parser.add_argument("--max-major-deps", type=int, default=8)
    parser.add_argument("--include-domain-types", action="store_true")
    parser.add_argument("--max-domain-types", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    resolved = args.root.resolve()
    is_git = is_git_repo(resolved)
    root = git_root(resolved) if is_git else resolved
    output = summarize_repo(root, args, is_git)
    print(output)
    return 0
