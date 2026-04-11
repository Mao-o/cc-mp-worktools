from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from core.context import RepoContext
from core.fs import read_text
from core.util import is_test_path


class DomainTypesCollector:
    name = "domain_types"
    section_title = "## Domain Types"
    priority = 80

    def should_run(self, ctx: RepoContext) -> bool:
        return ctx.args.include_domain_types

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.args.max_domain_types
        types = _maybe_collect_domain_types(ctx, max_items)
        if not types:
            return None
        lines = [self.section_title]
        for item in types:
            lines.append(f"- {item['name']} \u2014 {item['path']}")
        return "\n".join(lines)


def _maybe_collect_domain_types(ctx: RepoContext, max_items: int) -> List[Dict[str, str]]:
    candidate_paths = [
        p
        for p in ctx.tracked_files
        if Path(p).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs"}
        and any(
            token in f"/{p.lower()}"
            for token in ("/domain/", "/domains/", "/model/", "/models/", "/entity/", "/entities/", "/types/")
        )
        and not is_test_path(p)
    ]
    if len(candidate_paths) < 3:
        return []

    stop_names = {
        "Props", "State", "Config", "Options", "Params", "Request", "Response",
        "Input", "Output", "Schema", "Dto", "Meta", "Result", "Context",
    }
    pattern = re.compile(r'\b(?:export\s+)?(?:interface|type|class|enum)\s+([A-Z][A-Za-z0-9_]*)\b')
    items: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for rel in candidate_paths:
        text = read_text(ctx.root / rel, limit=20_000)
        for match in pattern.finditer(text):
            name = match.group(1)
            if name in stop_names or name in seen:
                continue
            items.append({"name": name, "path": rel})
            seen.add(name)
            if len(items) >= max_items:
                return items
    return items


def register():
    return DomainTypesCollector()
