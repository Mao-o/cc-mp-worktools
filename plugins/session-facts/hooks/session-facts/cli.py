from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from core.constants import (
    DEFAULT_MAX_DOMAIN_TYPES,
    DEFAULT_MAX_ENV_KEYS,
    DEFAULT_MAX_HUB_FILES,
    DEFAULT_MAX_MAJOR_DEPS,
    DEFAULT_MAX_NOTES,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_MAX_SCRIPT_ENTRIES,
    DEFAULT_MAX_SERVICE_ENTRIES,
    DEFAULT_MAX_TREE_LINES,
    MAX_TREE_DEPTH,
    MIN_TREE_DEPTH,
    PROJECT_MARKERS,
    SKIP_DIRS,
)
from core.context import AnalysisConfig, RepoContext
from core.fs import has_project_markers, read_text, walk_files
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


def _enforce_output_budget(header: str, sections: Sequence[str], max_chars: int) -> str:
    """Trim total output to max_chars, shrinking the lowest-value content
    first, in this order: the ``## Structure`` section's tail (one line at
    a time), then ``## Scripts``, ``## Env Keys``, and
    ``## Repo-Specific Notes`` dropped wholesale. ``## Test Snapshot``,
    ``## Service Entry Points``, and ``## Likely Commands`` are never
    touched by this cascade -- they carry facts an agent is least able to
    reconstruct on its own. Appends a single "... (truncated)" marker if
    anything had to give, and hard-cuts as a last resort so the result never
    exceeds max_chars even if what remains is itself oversized.
    """
    sections = list(sections)

    def joined() -> str:
        return "\n\n".join([header] + sections)

    if len(joined()) <= max_chars:
        return joined()

    marker = "\n\n... (truncated)"

    def finish(content: str) -> str:
        """Append the marker if it fits under max_chars; otherwise hard-cut
        with no marker (there is no length left to spare on one). Either
        way the result never exceeds max_chars, even for a pathologically
        small budget shorter than the marker itself.
        """
        if len(content) + len(marker) <= max_chars:
            return content + marker
        return content[:max_chars]

    budget = max(max_chars - len(marker), 0)

    # Step 1: shrink the Structure section's tail, one tree line at a time.
    for i, sec in enumerate(sections):
        if not sec.startswith("## Structure"):
            continue
        struct_lines = sec.splitlines()
        struct_header, body = struct_lines[0], struct_lines[1:]
        while body:
            candidate = "\n".join([struct_header] + body)
            if len("\n\n".join([header] + sections[:i] + [candidate] + sections[i + 1:])) <= budget:
                sections[i] = candidate
                break
            body.pop()
        else:
            sections[i] = struct_header
        break

    if len(joined()) <= budget:
        return finish(joined())

    # Steps 2-4: drop lower-priority sections wholesale, one at a time.
    for title in ("## Scripts", "## Env Keys", "## Repo-Specific Notes"):
        sections = [s for s in sections if not s.startswith(title)]
        if len(joined()) <= budget:
            return finish(joined())

    # Last resort: guarantees the result never exceeds max_chars even if
    # what remains (header, Test Snapshot, Service Entry Points, Likely
    # Commands, ...) is itself larger than the budget.
    return finish(joined())


def summarize_repo(
    root: Path,
    config: AnalysisConfig,
    is_git: bool,
    cwd: Optional[Path] = None,
    invoked_as: Optional[str] = None,
    force_walk: bool = False,
) -> str:
    # Non-git directories with no recognizable project marker (package.json,
    # pyproject.toml, Makefile, ...) get a filesystem walk (core/fs.py's
    # walk_files(), capped at 5000 files but otherwise unconditional) that
    # produces a Structure/Test Snapshot/etc. bundle built from whatever
    # happens to be lying around -- e.g. running from $HOME or Desktop,
    # unrelated to any coding task. Skip straight to a minimal header
    # instead; --force-walk restores the old unconditional behaviour.
    if not is_git and not force_walk and not has_project_markers(root, PROJECT_MARKERS):
        return "\n".join([
            "## Project Facts",
            "- git_repo: false",
            "- no project markers found; facts skipped",
        ])
    ctx = RepoContext(root=root, config=config, cwd=cwd, invoked_as=invoked_as)
    ctx.tracked_files = git_ls_files(root) if is_git else walk_files(root, SKIP_DIRS)
    ctx.results["is_git_repo"] = is_git

    purpose = _infer_purpose(ctx)
    if purpose:
        ctx.results["purpose"] = purpose
    ctx.results["package_manager"] = detect_package_manager(ctx)

    # Phase 1: Run stack detectors. Each detector is isolated: one raising
    # (e.g. on a malformed config file it tries to parse) must not blank out
    # the stack line and every section that follows it — the caller only
    # sees a stderr warning and the run continues with the rest.
    pkg_dir = Path(__file__).resolve().parent
    detectors = discover_plugins(pkg_dir / "detectors", "detectors")
    detectors.sort(key=lambda d: d.priority)
    for detector in detectors:
        detector_name = getattr(detector, "name", detector.__class__.__name__)
        try:
            ctx.stack.extend(detector.detect(ctx))
        except Exception as e:
            print(f"[session-facts] WARNING: detector {detector_name} failed: {e}", file=sys.stderr)

    # Phase 2: Run section collectors
    collectors = discover_plugins(pkg_dir / "collectors", "collectors")
    collectors.extend(discover_custom_plugins(pkg_dir / "custom"))
    collectors.sort(key=lambda c: c.priority)

    # Collect sections (some collectors populate ctx.results for header).
    # Same isolation as detectors above: a collector that raises loses only
    # its own section, not the rest of the output.
    collected_sections = []
    for collector in collectors:
        collector_name = getattr(collector, "name", collector.__class__.__name__)
        try:
            if collector.should_run(ctx):
                section = collector.collect(ctx)
                if section:
                    collected_sections.append(section)
        except Exception as e:
            print(f"[session-facts] WARNING: collector {collector_name} failed: {e}", file=sys.stderr)

    # Header rendered after collectors so ctx.results is fully populated
    header = render_header(ctx)
    return _enforce_output_budget(header, collected_sections, config.max_output_chars)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a compact session-start facts bundle for coding agents."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Path inside the git repository")
    parser.add_argument(
        "--format",
        choices=("markdown", "json", "human"),
        default="markdown",
        help="Output format. 'markdown' is what hooks inject into agent context.",
    )
    parser.add_argument(
        "--tree-depth",
        type=int,
        default=None,
        help="Force a fixed tree depth. Omit to auto-select depth dynamically.",
    )
    parser.add_argument(
        "--min-tree-depth", type=int, default=MIN_TREE_DEPTH,
        help="Lower bound for dynamic tree depth selection.",
    )
    parser.add_argument(
        "--max-tree-depth", type=int, default=MAX_TREE_DEPTH,
        help="Upper bound for dynamic tree depth selection.",
    )
    parser.add_argument(
        "--max-tree-lines", type=int, default=DEFAULT_MAX_TREE_LINES,
        help="Max lines for the directory tree; the deepest depth that fits wins.",
    )
    parser.add_argument(
        "--max-service-entries", type=int, default=DEFAULT_MAX_SERVICE_ENTRIES,
        help="Max entries in the Service Entry Points section.",
    )
    parser.add_argument(
        "--max-script-entries", type=int, default=DEFAULT_MAX_SCRIPT_ENTRIES,
        help="Max entries in the Likely Commands / scripts section.",
    )
    parser.add_argument(
        "--max-env-keys", type=int, default=DEFAULT_MAX_ENV_KEYS,
        help="Max env var keys listed from .env.example-style files.",
    )
    parser.add_argument(
        "--max-notes", type=int, default=DEFAULT_MAX_NOTES,
        help="Max entries in the Repo-Specific Notes section.",
    )
    parser.add_argument(
        "--max-major-deps", type=int, default=DEFAULT_MAX_MAJOR_DEPS,
        help="Max entries in the major_dependencies header line.",
    )
    parser.add_argument(
        "--max-output-chars", type=int, default=DEFAULT_MAX_OUTPUT_CHARS,
        help=(
            "Hard ceiling on the whole rendered output. Beyond this, the "
            "Structure section's tail is trimmed first, then Scripts / Env "
            "Keys / Repo-Specific Notes are dropped wholesale, in that order."
        ),
    )
    parser.add_argument(
        "--include-domain-types",
        action="store_true",
        help=(
            "Enable the Domain Types section (interface/type/class/enum names "
            "under domain-ish paths, e.g. Case/Order/User — not Props/Config "
            "plumbing). Opt-in: scans file bodies, unlike the rest of "
            "session-facts. Requires a genuine cluster (>=5 types) to show."
        ),
    )
    parser.add_argument(
        "--max-domain-types", type=int, default=DEFAULT_MAX_DOMAIN_TYPES,
        help="Max entries in the Domain Types section.",
    )
    parser.add_argument(
        "--include-hub-files",
        action="store_true",
        help=(
            "Enable the Hub Files section (files most referenced by other "
            "tracked files via a lightweight import scan). Opt-in: scans "
            "every candidate file's body, unlike the rest of session-facts."
        ),
    )
    parser.add_argument(
        "--max-hub-files", type=int, default=DEFAULT_MAX_HUB_FILES,
        help="Max entries in the Hub Files section.",
    )
    parser.add_argument(
        "--no-recent-commits",
        action="store_true",
        help=(
            "Skip the recent_commits header lines. Use on SessionStart, where "
            "the harness already injects the same commits via gitStatus."
        ),
    )
    parser.add_argument(
        "--force-walk",
        action="store_true",
        help=(
            "Run the full filesystem-walk analysis on a non-git directory "
            "even when no project marker (package.json, pyproject.toml, "
            "Makefile, ...) is found at --root. Without this, such "
            "directories get a minimal 2-line header instead of a facts "
            "bundle built from whatever files happen to be there."
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
    # Force UTF-8 + a non-fatal error handler on stdout before printing
    # anything. Without this, a lone surrogate from a non-UTF-8 filename, or
    # an inherited non-UTF-8 stdout encoding (e.g. PYTHONIOENCODING=ascii
    # with Japanese README text or the tree-drawing box characters), raises
    # UnicodeEncodeError and the whole facts bundle is lost. Guarded because
    # not every stdout-like object supports reconfigure() (e.g. io.StringIO
    # used by tests to capture output in-process).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
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
        max_output_chars=args.max_output_chars,
        include_domain_types=args.include_domain_types,
        max_domain_types=args.max_domain_types,
        include_hub_files=args.include_hub_files,
        max_hub_files=args.max_hub_files,
        include_recent_commits=not args.no_recent_commits,
    )
    resolved = args.root.resolve()
    root = git_root_or_none(resolved)
    is_git = root is not None
    if root is None:
        root = resolved
    # sys.argv[0] is the literal path the hook invoked (e.g. the
    # ${CLAUDE_PLUGIN_ROOT}-resolved directory), which the injected output
    # would otherwise have no way to name for a follow-up --help call.
    try:
        output = summarize_repo(
            root, config, is_git, cwd=resolved, invoked_as=sys.argv[0], force_walk=args.force_walk,
        )
    except Exception as e:
        # Per-detector/collector isolation above covers the expected failure
        # points; this guard covers the rest of summarize_repo() itself, so a
        # bug there degrades to a minimal header instead of exit 1 + traceback
        # (which silently drops the entire facts bundle from the agent's
        # context). Scope note: this does NOT cover the steps outside the try
        # block -- stdout reconfiguration, path resolution, the git-root probe,
        # and the final print/json.dumps. A failure in those still exits
        # non-zero with no output.
        print(f"[session-facts] WARNING: summarize_repo failed, emitting minimal header: {e}", file=sys.stderr)
        output = f"## Project Facts\n- repo_root: {root}"
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
