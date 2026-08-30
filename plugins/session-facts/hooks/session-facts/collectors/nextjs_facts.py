from __future__ import annotations

import re
from typing import List, Optional

from core.constants import NEXT_CONFIG_CANDIDATES
from core.context import RepoContext
from core.fs import read_text
from core.util import has_app_router, has_pages_router, normalize_version


class NextjsFactsCollector:
    name = "nextjs_facts"
    section_title = "## Next.js Facts"
    priority = 50

    def should_run(self, ctx: RepoContext) -> bool:
        return "nextjs" in ctx.stack

    def collect(self, ctx: RepoContext) -> Optional[str]:
        lines: List[str] = [self.section_title]
        root = ctx.root

        version = ctx.all_deps.get("next")
        if version:
            lines.append(f"- next_version: {normalize_version(str(version))}")

        if has_app_router(root):
            lines.append("- app_router: yes")
        if has_pages_router(root):
            lines.append("- pages_router: yes")

        config_text = ""
        for candidate in NEXT_CONFIG_CANDIDATES:
            path = root / candidate
            if path.exists():
                config_text = read_text(path, limit=50_000)
                lines.append(f"- config_file: {candidate}")
                break

        hints: List[str] = []
        if config_text:
            if re.search(r'\btypedRoutes\s*:\s*true', config_text):
                hints.append("typedRoutes enabled")
            if re.search(r'\boutput\s*:\s*["\']standalone["\']', config_text):
                hints.append("output standalone")
            if re.search(r'\btrailingSlash\s*:\s*true', config_text):
                hints.append("trailingSlash enabled")
            if re.search(r'\bimages\s*:', config_text):
                hints.append("custom images config")
            if re.search(r'\bexperimental\s*:', config_text):
                hints.append("experimental config present")
        for hint in hints[:ctx.config.max_config_hints]:
            lines.append(f"- config_hint: {hint}")

        return "\n".join(lines) if len(lines) > 1 else None


def register():
    return NextjsFactsCollector()
