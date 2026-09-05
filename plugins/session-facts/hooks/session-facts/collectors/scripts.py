from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from core.constants import (
    COMPOSE_FILE_CANDIDATES,
    MAKE_TARGET_PRIORITY_PATTERNS,
    MAX_SCRIPT_COMMAND_CHARS,
    SCRIPT_PRIORITY_PATTERNS,
    TEST_PATH_MARKERS,
)
from core.context import RepoContext
from core.fs import read_text
from core.makefile import extract_targets
from core.pytest_config import matches_python_files, python_files_patterns
from core.runtime import mise_config_path, runner_prefix
from core.util import collapse_space, truncate_text

# The package-manager prefix for ``<pm> <script>``; scripts of a repo whose
# pm is not listed here are not expanded into commands at all.
_PM_RUN_PREFIX = {
    "pnpm": "pnpm",
    "npm": "npm run",
    "yarn": "yarn",
    "bun": "bun run",
}

# Only these script names are promoted from ## Scripts into ## Likely
# Commands (internal backlog joa.6): the conventional day-one entry
# points. Everything else is already listed once under ## Scripts, and
# repeating the whole table as ``npm run <name>`` lines was pure
# duplication (16 + 16 lines on real repos).
_PROMOTED_SCRIPT_NAMES = ("dev", "test", "build", "lint", "typecheck", "start", "check")
_MAX_PROMOTED_SCRIPTS = 4


class ScriptsCollector:
    name = "scripts"
    section_title = "## Scripts"
    priority = 30

    def should_run(self, ctx: RepoContext) -> bool:
        scripts = ctx.package_json.get("scripts")
        return isinstance(scripts, dict) and len(scripts) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_script_entries
        scripts = _collect_scripts(ctx, max_items)
        if not scripts:
            return None
        prefix = _PM_RUN_PREFIX.get(ctx.results.get("package_manager") or "")
        title = f"{self.section_title} (run: {prefix} <name>)" if prefix else self.section_title
        lines = [title]
        for item in scripts:
            lines.append(f"- {item['name']}: {item['command']}")
        return "\n".join(lines)


class LikelyCommandsCollector:
    name = "likely_commands"
    section_title = "## Likely Commands"
    priority = 90

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_script_entries
        commands = _likely_commands(ctx, max_items)
        if not commands:
            return None
        lines = [self.section_title]
        for cmd in commands:
            lines.append(f"- {cmd}")
        return "\n".join(lines)


def _collect_scripts(ctx: RepoContext, max_items: int) -> List[Dict[str, str]]:
    scripts = ctx.package_json.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return []
    scored: List[Tuple[int, str, str]] = []
    for name, command in scripts.items():
        score = 100
        for idx, pattern in enumerate(SCRIPT_PRIORITY_PATTERNS):
            if re.search(pattern, name):
                score = idx
                break
        command_text = truncate_text(
            collapse_space(str(command)), MAX_SCRIPT_COMMAND_CHARS
        )
        scored.append((score, str(name), command_text))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [{"name": name, "command": command} for _score, name, command in scored[:max_items]]


def _runner_prefix(ctx: RepoContext) -> Optional[str]:
    """Local-tool command prefix from the detected runtime, or None.

    venv wins (``.venv/bin/``); otherwise a mise-managed Python yields
    ``mise exec -- ``. uv/poetry manage their own environments and are left
    untouched by callers.
    """
    return runner_prefix(ctx.results.get("runtime") or {})


def _make_commands(ctx: RepoContext, max_items: int) -> List[str]:
    """Surface conventional Makefile targets as ``make <target>`` commands.

    Targets are scored by MAKE_TARGET_PRIORITY_PATTERNS; targets matching no
    pattern are dropped so internal helper targets do not crowd the output.
    Falls back to a bare ``make`` when no Makefile is readable or none of its
    targets are conventional entry points.
    """
    text = ""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = ctx.root / name
        if path.exists():
            text = read_text(path)
            break
    if not text:
        return ["make"]

    scored: List[Tuple[int, str]] = []
    for target in extract_targets(text):
        for idx, pattern in enumerate(MAKE_TARGET_PRIORITY_PATTERNS):
            if re.search(pattern, target):
                scored.append((idx, target))
                break
    scored.sort(key=lambda item: (item[0], item[1]))
    commands = [f"make {target}" for _idx, target in scored[:max_items]]
    return commands or ["make"]


# --- Python test-runner evidence (internal backlog joa.14 / joa.28 / joa.30)
#
# A test command is only suggested when the repo gives two pieces of
# evidence: test files exist (by pytest's own ``python_files`` rule, read
# from the project's config when it overrides the default), and the runner
# is declared somewhere the project controls (pytest in a manifest /
# requirements file / a pytest config file). Files that match the naming
# rule but no declared runner get the standard-library ``unittest``
# suggestion instead, anchored at the shallowest literal test directory.


def _pytest_declared(ctx: RepoContext) -> bool:
    root = ctx.root
    for _rel, text in ctx.pyproject_manifests():
        if "pytest" in text.lower():
            return True
    if (root / "pytest.ini").exists():
        return True
    for name in ("tox.ini", "setup.cfg"):
        path = root / name
        if path.exists() and "pytest" in read_text(path).lower():
            return True
    for path in ctx.tracked_files:
        base = path.rsplit("/", 1)[-1]
        if base.startswith("requirements") and base.endswith(".txt"):
            if "pytest" in read_text(root / path).lower():
                return True
    return False


def _python_test_files(ctx: RepoContext) -> List[str]:
    patterns = python_files_patterns(ctx.root, ctx.pyproject_toml)
    return [
        p for p in ctx.tracked_files
        if p.endswith(".py") and matches_python_files(p, patterns)
    ]


def _shallowest_test_dir(test_files: List[str]) -> Optional[str]:
    """The shallowest directory named like a test dir that holds test
    files, when it is unique at its depth (``tests``, ``src/tests``); None
    when tests are spread across several same-depth directories (a
    monorepo's ``plugins/*/tests``), where no single ``-s`` target exists."""
    dirs: Dict[int, Set[str]] = {}
    for path in test_files:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower() in TEST_PATH_MARKERS:
                dirs.setdefault(i, set()).add("/".join(parts[: i + 1]))
                break
    for depth in sorted(dirs):
        if len(dirs[depth]) == 1:
            return next(iter(dirs[depth]))
        return None
    return None


def _python_test_commands(ctx: RepoContext, pm: Optional[str], stack: Set[str]) -> List[str]:
    if "python" not in stack and pm not in ("uv", "poetry", "python"):
        return []
    test_files = _python_test_files(ctx)
    if not test_files:
        return []
    if _pytest_declared(ctx):
        if pm == "uv":
            return ["uv run pytest"]
        if pm == "poetry":
            return ["poetry run pytest"]
        prefix = _runner_prefix(ctx)
        if pm == "python" or prefix:
            return [f"{prefix or ''}python -m pytest"]
        return []
    # No pytest anywhere: the files are runnable with the standard library.
    prefix = _runner_prefix(ctx)
    if pm is None and not prefix:
        # Bare-Python repo with no known interpreter wrapper; the global
        # ``python`` may not be the one the repo uses (pre-existing rule).
        return []
    test_dir = _shallowest_test_dir(test_files)
    cmd = f"{prefix or ''}python -m unittest discover"
    if test_dir:
        cmd += f" -s {test_dir}"
    return [cmd]


def _likely_commands(ctx: RepoContext, max_items: int) -> List[str]:
    scripts = _collect_scripts(ctx, max_items=50)
    pm = ctx.results.get("package_manager")
    stack = set(ctx.stack)
    root = ctx.root
    commands: List[str] = []
    # Reserved for the scala/elixir/swift/dotnet block below: merged in
    # *ahead* of `commands` (see the final concat before dedup) so these
    # stack-derived commands cannot be pushed out of the `deduped[:max_items]`
    # slice by an unrelated source further up the list.
    priority_commands: List[str] = []

    # PM-based commands: only the conventional entry-point scripts are
    # promoted here; the full table is ## Scripts' job (joa.6).
    prefix = _PM_RUN_PREFIX.get(pm or "")
    if prefix:
        by_name = {item["name"]: item for item in scripts}
        promoted = [name for name in _PROMOTED_SCRIPT_NAMES if name in by_name]
        for name in promoted[:_MAX_PROMOTED_SCRIPTS]:
            commands.append(f"{prefix} {name}")
    elif pm == "deno":
        commands.extend(["deno task dev", "deno test"])
    elif pm == "gradle":
        commands.extend(["./gradlew build", "./gradlew test"])
    elif pm == "maven":
        commands.extend(["mvn test", "mvn package"])
    elif pm == "go":
        commands.extend(["go test ./...", "go build ./..."])
    elif pm == "cargo":
        commands.extend(["cargo test", "cargo build"])
    elif pm == "composer":
        commands.append("composer install")

    commands.extend(_python_test_commands(ctx, pm, stack))

    # Stack-based commands for scala/elixir/swift/dotnet: keyed off the
    # detected stack rather than the single primary package_manager, so a
    # root that also has a higher-priority manifest for another stack
    # (package-lock.json next to App.sln) still gets them.
    if "scala" in stack:
        priority_commands.extend(["sbt test", "sbt compile"])
    if "elixir" in stack:
        priority_commands.append("mix test")
    # "swift" in stack alone is NOT enough: detectors/swift_stack.py also
    # reports "swift" for an Xcode-only project (*.xcodeproj, no
    # Package.swift), and xcodebuild needs a -scheme this collector cannot
    # infer, so the SwiftPM commands require Package.swift itself.
    if "swift" in stack and (pm == "swift" or (root / "Package.swift").exists()):
        priority_commands.extend(["swift build", "swift test"])
    if "dotnet" in stack:
        priority_commands.append("dotnet test")

    # Flutter/Dart toolchain
    if "flutter" in stack:
        commands.extend(["flutter pub get", "flutter run", "flutter test"])
    elif "dart" in stack:
        commands.extend(["dart pub get", "dart test"])

    # Stack-based additions (task runners, tools). Each command names the
    # file that grounds it (joa.14): ``docker compose up`` needs a compose
    # file (a Dockerfile alone grounds only ``docker build .``), and
    # ``mise install`` needs a mise config (a bare ``.tool-versions`` is
    # asdf's format, so suggest asdf's command for it).
    if "makefile" in stack:
        commands.extend(_make_commands(ctx, max_items))
    if "justfile" in stack:
        commands.append("just")
    if "taskfile" in stack:
        commands.append("task")
    if "nx" in stack:
        commands.append("nx run-many --target=build")
    if "mise" in stack:
        if mise_config_path(root) is not None:
            commands.append("mise install")
        elif (root / ".tool-versions").exists():
            commands.append("asdf install")
    if "docker" in stack:
        if any((root / name).exists() for name in COMPOSE_FILE_CANDIDATES):
            commands.append("docker compose up")
        elif (root / "Dockerfile").exists():
            commands.append("docker build .")

    ordered = priority_commands + commands

    deduped: List[str] = []
    seen: Set[str] = set()
    for cmd in ordered:
        if cmd not in seen:
            seen.add(cmd)
            deduped.append(cmd)
    return deduped[:max_items]


def register():
    return [ScriptsCollector(), LikelyCommandsCollector()]
