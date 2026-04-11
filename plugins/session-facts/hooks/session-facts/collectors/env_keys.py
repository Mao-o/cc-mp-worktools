from __future__ import annotations

import re
from typing import List, Optional, Set

from core.constants import ENV_FILE_CANDIDATES
from core.context import RepoContext
from core.fs import read_text, safe_iterdir


class EnvKeysCollector:
    name = "env_keys"
    section_title = "## Env Keys"
    priority = 40

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_env_keys
        keys = _collect_env_keys(ctx.root, max_items)
        if not keys:
            return None
        lines = [self.section_title]
        for key in keys:
            lines.append(f"- {key}")
        return "\n".join(lines)


def _collect_env_keys(root, max_items: int) -> List[str]:
    keys: List[str] = []
    seen: Set[str] = set()
    env_files = list(ENV_FILE_CANDIDATES)

    for path in safe_iterdir(root):
        if path.is_file() and path.name.startswith(".env") and (
            "example" in path.name or "sample" in path.name
        ):
            if path.name not in env_files:
                env_files.append(path.name)

    pattern = re.compile(r'^\s*([A-Z][A-Z0-9_]+)\s*=')
    for name in env_files:
        path = root / name
        if not path.exists():
            continue
        for line in read_text(path, limit=50_000).splitlines():
            match = pattern.match(line)
            if not match:
                continue
            key = match.group(1)
            if key not in seen:
                seen.add(key)
                keys.append(key)
                if len(keys) >= max_items:
                    return keys
    return keys


def register():
    return EnvKeysCollector()
