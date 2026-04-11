from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from core.constants import SCRIPT_PRIORITY_PATTERNS
from core.context import RepoContext
from core.util import collapse_space


class ScriptsCollector:
    name = "scripts"
    section_title = "## Scripts"
    priority = 30

    def should_run(self, ctx: RepoContext) -> bool:
        scripts = ctx.package_json.get("scripts")
        return isinstance(scripts, dict) and len(scripts) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_script_entries
        scripts = _collect_scripts(ctx, max_items)
        if not scripts:
            return None
        lines = [self.section_title]
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
        max_items = ctx.args.max_script_entries
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
        scored.append((score, str(name), collapse_space(str(command))))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [{"name": name, "command": command} for _score, name, command in scored[:max_items]]


def _detect_package_manager(ctx: RepoContext) -> Optional[str]:
    return ctx.results.get("package_manager")


def _likely_commands(ctx: RepoContext, max_items: int) -> List[str]:
    scripts = _collect_scripts(ctx, max_items=50)
    pm = _detect_package_manager(ctx)
    stack = set(ctx.stack)
    commands: List[str] = []

    # PM-based commands
    prefix = {
        "pnpm": "pnpm",
        "npm": "npm run",
        "yarn": "yarn",
        "bun": "bun run",
    }.get(pm or "")
    if prefix:
        for item in scripts:
            commands.append(f"{prefix} {item['name']}")
    elif pm == "deno":
        commands.extend(["deno task dev", "deno test"])
    elif pm == "uv":
        commands.append("uv run pytest")
    elif pm == "poetry":
        commands.append("poetry run pytest")
    elif pm == "python":
        commands.append("python -m pytest")
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

    # Stack-based additions (task runners, tools)
    if "makefile" in stack:
        commands.append("make")
    if "justfile" in stack:
        commands.append("just")
    if "taskfile" in stack:
        commands.append("task")
    if "nx" in stack:
        commands.append("nx run-many --target=build")
    if "mise" in stack:
        commands.append("mise install")
    if "docker" in stack:
        commands.append("docker compose up")

    deduped: List[str] = []
    seen: Set[str] = set()
    for cmd in commands:
        if cmd not in seen:
            seen.add(cmd)
            deduped.append(cmd)
    return deduped[:max_items]


def register():
    return [ScriptsCollector(), LikelyCommandsCollector()]
