from __future__ import annotations

import re
from typing import List, Optional, Set

from core.constants import IMPORTANT_DEPENDENCIES
from core.context import RepoContext
from core.fs import read_text
from core.util import normalize_version


class DependenciesCollector:
    name = "dependencies"
    section_title = ""
    priority = 5

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_major_deps
        deps = _collect_major_dependencies(ctx, max_items)
        ctx.results["major_dependencies"] = deps
        # This collector contributes to the header, not its own section
        return None


def _collect_major_dependencies(ctx: RepoContext, max_items: int) -> List[str]:
    results: List[str] = []
    seen: Set[str] = set()
    root = ctx.root

    pkg = ctx.package_json
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if name in IMPORTANT_DEPENDENCIES and name not in seen:
                results.append(f"{name}@{normalize_version(str(version))}")
                seen.add(name)

    pyproject = read_text(root / "pyproject.toml") if (root / "pyproject.toml").exists() else ""
    for name in sorted(IMPORTANT_DEPENDENCIES):
        if len(results) >= max_items:
            break
        if name in seen or not pyproject:
            continue
        match = re.search(rf'(?mi)^\s*{re.escape(name)}\s*=\s*["\']([^"\']+)["\']', pyproject)
        if match:
            results.append(f"{name}@{normalize_version(match.group(1))}")
            seen.add(name)

    go_mod = read_text(root / "go.mod") if (root / "go.mod").exists() else ""
    for line in go_mod.splitlines():
        if len(results) >= max_items:
            break
        parts = line.strip().split()
        if len(parts) == 2:
            mod, version = parts
            leaf = mod.rsplit("/", 1)[-1]
            if leaf in IMPORTANT_DEPENDENCIES and leaf not in seen:
                results.append(f"{leaf}@{normalize_version(version)}")
                seen.add(leaf)

    return results[:max_items]


def register():
    return DependenciesCollector()
