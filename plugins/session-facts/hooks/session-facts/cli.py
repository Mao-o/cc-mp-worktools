from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from core.constants import (
    DEFAULT_MAX_DOMAIN_TYPES,
    DEFAULT_MAX_ENV_KEYS,
    DEFAULT_MAX_MAJOR_DEPS,
    DEFAULT_MAX_NOTES,
    DEFAULT_MAX_SCRIPT_ENTRIES,
    DEFAULT_MAX_SERVICE_ENTRIES,
    DEFAULT_MAX_TREE_LINES,
    MAX_TREE_DEPTH,
    MIN_TREE_DEPTH,
    SKIP_DIRS,
)
from core.context import AnalysisConfig, RepoContext
from core.fs import read_text, walk_files
from core.git import git_ls_files, git_root_or_none
from core.pm import detect_package_manager
from core.util import truncate_purpose
from registry import discover_custom_plugins, discover_plugins
from renderer import render_header


def _iter_readme_body_lines(text: str):
    """Yield README body lines, skipping YAML frontmatter at the top."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                start = idx + 1
                break
    for raw in lines[start:]:
        yield raw


def _infer_purpose(ctx: RepoContext) -> Optional[str]:
    pkg = ctx.package_json
    description = pkg.get("description")
    if isinstance(description, str) and description.strip():
        return truncate_purpose(description)

    for readme_name in ("README.md", "README", "readme.md"):
        path = ctx.root / readme_name
        if not path.exists():
            continue
        text = read_text(path, limit=20_000)
        for raw in _iter_readme_body_lines(text):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
                if line:
                    continue
            if line.startswith(("```", "---", "***", "![", "[!", ">", "|", "<")):
                continue
            if len(line) < 12:
                continue
            return truncate_purpose(line)
    # A bare directory name restates repo_root and trains readers to skip the
    # field, so omit purpose entirely rather than fall back to it.
    return None


def summarize_repo(
    root: Path,
    config: AnalysisConfig,
    is_git: bool,
    cwd: Optional[Path] = None,
) -> str:
    ctx = RepoContext(root=root, config=config, cwd=cwd)
    ctx.tracked_files = git_ls_files(root) if is_git else walk_files(root, SKIP_DIRS)
    ctx.results["is_git_repo"] = is_git

    purpose = _infer_purpose(ctx)
    if purpose:
        ctx.results["purpose"] = purpose
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
    parser.add_argument(
        "--tree-depth",
        type=int,
        default=None,
        help="Force a fixed tree depth. Omit to auto-select depth dynamically.",
    )
    parser.add_argument("--min-tree-depth", type=int, default=MIN_TREE_DEPTH)
    parser.add_argument("--max-tree-depth", type=int, default=MAX_TREE_DEPTH)
    parser.add_argument("--max-tree-lines", type=int, default=DEFAULT_MAX_TREE_LINES)
    parser.add_argument("--max-service-entries", type=int, default=DEFAULT_MAX_SERVICE_ENTRIES)
    parser.add_argument("--max-script-entries", type=int, default=DEFAULT_MAX_SCRIPT_ENTRIES)
    parser.add_argument("--max-env-keys", type=int, default=DEFAULT_MAX_ENV_KEYS)
    parser.add_argument("--max-notes", type=int, default=DEFAULT_MAX_NOTES)
    parser.add_argument("--max-major-deps", type=int, default=DEFAULT_MAX_MAJOR_DEPS)
    parser.add_argument("--include-domain-types", action="store_true")
    parser.add_argument("--max-domain-types", type=int, default=DEFAULT_MAX_DOMAIN_TYPES)
    parser.add_argument(
        "--no-recent-commits",
        action="store_true",
        help=(
            "Skip the recent_commits header lines. Use on SessionStart, where "
            "the harness already injects the same commits via gitStatus."
        ),
    )
    parser.add_argument(
        "--emit",
        choices=("stdout", "subagent-json"),
        default="stdout",
        help=(
            "Output envelope. Plain stdout only reaches the model on "
            "SessionStart; SubagentStart requires the hookSpecificOutput "
            "JSON envelope ('subagent-json')."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = AnalysisConfig(
        tree_depth=args.tree_depth,
        min_tree_depth=args.min_tree_depth,
        max_tree_depth=args.max_tree_depth,
        max_tree_lines=args.max_tree_lines,
        max_service_entries=args.max_service_entries,
        max_script_entries=args.max_script_entries,
        max_env_keys=args.max_env_keys,
        max_notes=args.max_notes,
        max_major_deps=args.max_major_deps,
        include_domain_types=args.include_domain_types,
        max_domain_types=args.max_domain_types,
        include_recent_commits=not args.no_recent_commits,
    )
    resolved = args.root.resolve()
    root = git_root_or_none(resolved)
    is_git = root is not None
    if root is None:
        root = resolved
    output = summarize_repo(root, config, is_git, cwd=resolved)
    if args.emit == "subagent-json":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": output,
            }
        }, ensure_ascii=False))
    else:
        print(output)
    return 0
