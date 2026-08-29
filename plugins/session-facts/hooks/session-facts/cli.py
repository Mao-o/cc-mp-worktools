from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import List, Optional, Sequence

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
from core.runtime import MISE_CONFIG_NAMES, mise_config_path
from core.util import truncate_purpose
from registry import discover_custom_plugins, discover_plugins
from renderer import render_header

# PROJECT_MARKERS minus the mise config names: those three are checked via
# mise_config_path() in _has_relevant_project_markers() below instead of a
# plain has_project_markers() exists() check, so that function's $HOME/XDG
# -global exception (see its docstring) applies to the marker gate too,
# rather than re-deriving an "is root $HOME" judgment here separately.
_NON_MISE_PROJECT_MARKERS = tuple(m for m in PROJECT_MARKERS if m not in MISE_CONFIG_NAMES)


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


def _trim_tree_sections(header: str, sections: List[str], target: int) -> List[str]:
    """Shrink ``## Subtree`` then ``## Structure`` tails, one tree line at a
    time, until the joined result fits ``target`` or both are down to their
    bare header line (whichever comes first). Returns a new list; does not
    mutate ``sections``.

    Subtree is tried first: in cwd-scoped mode (SubagentStart Explore/Plan),
    ``## Structure`` self-shrinks to a small top-level, cross-module
    overview ("just enough to know which other modules exist") while
    ``## Subtree`` carries the cwd-scoped detail and is usually the larger
    of the two -- so it is the one to sacrifice first.
    """
    secs = list(sections)

    def joined(candidate_secs: List[str]) -> str:
        return "\n\n".join([header] + candidate_secs)

    for prefix in ("## Subtree", "## Structure"):
        for i, sec in enumerate(secs):
            if not sec.startswith(prefix):
                continue
            lines = sec.splitlines()
            sec_header, body = lines[0], lines[1:]
            while body:
                candidate = "\n".join([sec_header] + body)
                candidate_secs = secs[:i] + [candidate] + secs[i + 1:]
                if len(joined(candidate_secs)) <= target:
                    secs[i] = candidate
                    break
                body.pop()
            else:
                secs[i] = sec_header
            break
        if len(joined(secs)) <= target:
            break
    return secs


def _enforce_output_budget(header: str, sections: Sequence[str], max_chars: int) -> str:
    """Trim total output to max_chars, shrinking the lowest-value content
    first, in this order: the dynamic-depth tree sections' tails
    (``## Subtree`` then ``## Structure``, one line at a time -- see
    _trim_tree_sections()), then ``## Scripts``, ``## Env Keys``, and
    ``## Repo-Specific Notes`` dropped wholesale. ``## Test Snapshot``,
    ``## Service Entry Points``, and ``## Likely Commands`` are never
    touched by this cascade -- they carry facts an agent is least able to
    reconstruct on its own.

    Every step targets max_chars itself, not some pre-shrunk "leave room
    for the marker" budget: a 1-character overage should cost 1 character
    of tree tail, not an extra whole section dropped just to reserve
    marker headroom. Once the cascade is as small as it can get, a marker
    is attached only if something was actually dropped/shrunk (tracked
    directly, not inferred from where the result length happens to land)
    and the marker itself still fits under max_chars; if it doesn't fit,
    the fine-grained tree-tail lever gets one more targeted trim to make
    exactly enough room for it, without sacrificing an additional whole
    section purely for that cosmetic purpose. Hard-cuts as a last resort
    so the result never exceeds max_chars even if what remains is itself
    oversized or nothing could be trimmed/dropped at all.
    """
    sections = list(sections)
    original_sections = list(sections)

    def joined(candidate_secs: Sequence[str]) -> str:
        return "\n\n".join([header] + list(candidate_secs))

    if len(joined(sections)) <= max_chars:
        return joined(sections)

    marker = "\n\n... (truncated)"

    # Step 1: shrink the tree sections' tails against max_chars directly.
    sections = _trim_tree_sections(header, sections, max_chars)

    # Steps 2-4: drop lower-priority sections wholesale, one at a time,
    # stopping as soon as the result fits max_chars.
    if len(joined(sections)) > max_chars:
        for title in ("## Scripts", "## Env Keys", "## Repo-Specific Notes"):
            sections = [s for s in sections if not s.startswith(title)]
            if len(joined(sections)) <= max_chars:
                break

    result = joined(sections)
    dropped = sections != original_sections
    if not dropped:
        # Nothing this cascade knows how to shrink/drop was present, yet
        # the original was over max_chars: hard-cut with no marker (there
        # is no drop to announce).
        return result[:max_chars]

    if len(result) + len(marker) <= max_chars:
        return result + marker

    # Marker-headroom retry: give the fine-grained tree-tail lever one more
    # chance to carve out exactly len(marker) more characters so the
    # marker can be shown, without dropping any additional whole section
    # purely for that headroom.
    retried = _trim_tree_sections(header, sections, max_chars - len(marker))
    retried_result = joined(retried)
    if len(retried_result) + len(marker) <= max_chars:
        return retried_result + marker

    return result[:max_chars]


def _minimal_header(root: Path, invoked_as: Optional[str]) -> str:
    """The gate-skipped header for a non-project, non-git directory.

    Names the analyzed directory (root) and, since neither the target
    directory nor the existence of a flag to force a full scan is
    otherwise discoverable from this output alone, points at --force-walk
    so an agent that actually needed the full analysis here isn't stuck
    with no way out. Mirrors renderer._render_more_hint()'s
    invoked_as/shlex.quote handling: invoked_as is sys.argv[0], the
    *directory* the interpreter was pointed at for a real hook run, so the
    printed command keeps the ``python3 `` prefix and quotes the path in
    case the plugin is installed under a directory containing spaces.
    """
    lines = [
        "## Project Facts",
        f"- repo_root: {root}",
        "- git_repo: false",
        "- no project markers found; facts skipped",
    ]
    # ヒントには解析対象 root を必ず含める。`--root` を明示して別ディレクトリ
    # から起動された場合、root を落としたヒントをそのまま実行すると
    # `Path.cwd()` が解析され、ヘッダーに出している repo_root とは別の
    # ディレクトリの結果が返る (復帰経路として機能しない)。
    root_arg = f"--root {shlex.quote(str(root))}"
    if invoked_as:
        lines.append(
            f"- more: run `python3 {shlex.quote(invoked_as)} {root_arg} "
            "--force-walk` to force the full analysis anyway"
        )
    else:
        lines.append(
            f"- more: pass `{root_arg} --force-walk` to force the full "
            "analysis anyway"
        )
    return "\n".join(lines)


def _has_relevant_project_markers(root: Path) -> bool:
    """PROJECT_MARKERS gate check, with the mise config names routed through
    core.runtime.mise_config_path() instead of has_project_markers()'s plain
    exists() check.

    Without this, a user's global mise config at ``~/.config/mise/config.toml``
    (XDG default) makes the gate treat $HOME as "a project" -- exactly the
    environment this gate exists to protect -- because a literal exists()
    check cannot tell that path apart from a deliberate project-level config.
    mise_config_path() already carries that distinction (see its docstring);
    sharing it here keeps the exception defined in one place instead of
    reimplementing an "is root $HOME" check against this gate too.
    """
    if has_project_markers(root, _NON_MISE_PROJECT_MARKERS):
        return True
    return mise_config_path(root) is not None


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
    if not is_git and not force_walk and not _has_relevant_project_markers(root):
        # Route through the same budget enforcement as the full-analysis
        # path below: with no sections to shrink/drop, this is a no-op when
        # the header already fits max_chars, and a marker-free hard cut
        # when it doesn't (a long --root/invoked_as can push this well past
        # a small custom --max-output-chars otherwise).
        return _enforce_output_budget(_minimal_header(root, invoked_as), [], config.max_output_chars)
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


def _non_negative_int(raw: str) -> int:
    """``--max-output-chars`` 用の型。負値を拒否する。

    ハードカットは ``result[:max_chars]`` で行うため、負値を通すと Python の
    負インデックス slice になり「ほぼ全文」が返る。上限として機能しないまま
    ハーネスの注入上限を超えうるので、引数解析の時点で弾く
    (0 は「すべて削る」の意味で意図的に許容している)。
    """
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or greater, got {value}")
    return value


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
        "--max-output-chars", type=_non_negative_int,
        default=DEFAULT_MAX_OUTPUT_CHARS,
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
        # このフォールバックも上限適用を通す。通さないと
        # `--max-output-chars` が「出力全体の上限」として成立せず、
        # 長い root を持つ環境ではハーネスの注入上限を超えうる。
        output = _enforce_output_budget(
            f"## Project Facts\n- repo_root: {root}",
            [],
            config.max_output_chars,
        )
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
