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
        return ctx.config.include_domain_types

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_domain_types
        types = _maybe_collect_domain_types(ctx, max_items)
        if not types:
            return None
        lines = [self.section_title]
        for item in types:
            lines.append(f"- {item['name']} \u2014 {item['path']}")
        return "\n".join(lines)


# Path segments that tend to hold domain concepts. Broadened beyond the
# original domain/model/entity/types set so repos that keep their types under
# repositories/ services/ schemas/ dto/ are no longer a dead spot (#10).
_DOMAIN_PATH_TOKENS = (
    "/domain/", "/domains/", "/model/", "/models/",
    "/entity/", "/entities/", "/types/",
    "/repositories/", "/repository/", "/services/", "/service/",
    "/schemas/", "/schema/", "/dto/", "/dtos/",
)

# Exact type names that are structural plumbing, not domain concepts.
_STOP_NAMES = {
    "Props", "State", "Config", "Options", "Params", "Request", "Response",
    "Input", "Output", "Schema", "Dto", "Meta", "Result", "Context",
    "Builder", "Factory", "Handler", "Manager", "Validator",
}

# Names ending in these suffixes are infrastructure (UserService, CaseRepository)
# rather than the domain object itself; surfaced from the broadened service/
# repository dirs they would be noise.
_INFRA_SUFFIXES = (
    "Repository", "Service", "Controller", "Handler", "Manager", "Factory",
    "Builder", "Validator", "Middleware", "Serializer", "Mapper", "Module",
    "Provider", "Resolver", "Interceptor",
)

_TYPE_PATTERN = re.compile(
    r'\b(?:export\s+)?(?:interface|type|class|enum)\s+([A-Z][A-Za-z0-9_]*)\b'
)

# A domain type section only earns its place when several concepts show up.
_MIN_DOMAIN_TYPES = 5


def _is_infra_name(name: str) -> bool:
    return any(name != suffix and name.endswith(suffix) for suffix in _INFRA_SUFFIXES)


def _maybe_collect_domain_types(ctx: RepoContext, max_items: int) -> List[Dict[str, str]]:
    candidate_paths = [
        p
        for p in ctx.tracked_files
        if Path(p).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs"}
        and any(token in f"/{p.lower()}" for token in _DOMAIN_PATH_TOKENS)
        and not is_test_path(p)
    ]
    if not candidate_paths:
        return []

    items: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for rel in candidate_paths:
        # Scan only the first 200 lines: type declarations live near the top,
        # and this bounds work on large files.
        text = "\n".join(read_text(ctx.root / rel, limit=40_000).splitlines()[:200])
        for match in _TYPE_PATTERN.finditer(text):
            name = match.group(1)
            if name in _STOP_NAMES or name in seen or _is_infra_name(name):
                continue
            items.append({"name": name, "path": rel})
            seen.add(name)
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break

    # Require a meaningful cluster of concepts to avoid false positives.
    if len(items) < _MIN_DOMAIN_TYPES:
        return []
    return items


def register():
    return DomainTypesCollector()
