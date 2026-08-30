from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

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
        groups = _maybe_collect_domain_types(ctx, max_items)
        if not groups:
            return None
        lines = [self.section_title]
        for rel, names in groups:
            lines.append(f"- {rel}: {', '.join(names)}")
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

# Directory segments that hold code this collector should never treat as a
# hand-written domain concept, even when they otherwise match a token above.
_EXCLUDED_DIR_TOKENS = ("/vendor/", "/generated/")

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

# This collector runs on every SessionStart/SubagentStart (hooks.json passes
# --include-domain-types unconditionally, so this is not opt-in in practice).
# Bounding how many candidate files get opened+scanned keeps a monorepo with
# hundreds of domain-path matches from turning every hook invocation into a
# large scan, mirroring the caps collectors/dependencies.py
# (_MAX_DEP_FILES) and detectors/flutter.py (_MAX_PUBSPEC_SCAN) already use
# for the same kind of "read N candidate files" work.
#
# This is a soft cap on scan cost, not a hard truncation of the candidate
# list: candidates past this index are only skipped once the >= 5
# (_MIN_DOMAIN_TYPES) cluster gate has already been satisfied. While the
# gate is still undecided, scanning continues past this index (bounded
# overall by _MAX_SCANNED_FILES below, independent of gate status) so a
# genuine cluster is never hidden purely by where it happens to fall in
# git-ls-files order (monorepos commonly have their real domain types under
# a directory that sorts after 20+ barrel/index files, e.g. packages/ after
# apps/). Internal backlog: an earlier version truncated the candidate list
# itself before opening any file, which made the whole ## Domain Types
# section silently disappear for repos with >= 21 matching candidates.
_MAX_CANDIDATE_FILES = 20

# Hard cap on how many candidate files get opened (read_text'd) in total,
# independent of the _MIN_DOMAIN_TYPES gate above. The soft cap only stops
# opening *new* files once the gate is already satisfied; a repo whose
# domain-path matches never cluster to >= 5 qualifying names never
# satisfies that condition, so without this cap the soft cap alone leaves
# the walk unbounded and every matching candidate gets opened on every
# SessionStart/SubagentStart hook call. 200 is a scan-cost ceiling this
# hook can absorb per invocation -- well above the soft cap's own 20-file
# threshold, so a genuine cluster that only clears the soft cap late is
# still found. Counted in files actually opened, not len(candidate_paths).
_MAX_SCANNED_FILES = 200

# Per-file cap on how many names any single file contributes to the
# *displayed* result (internal backlog: representativeness). Round-robin
# below already interleaves across files each pass, but without this a
# single outsized file with nothing else to compete against would still end
# up filling the whole section on its own -- this is the second, independent
# half of the fix. Deliberately NOT applied to the >= _MIN_DOMAIN_TYPES
# cluster-gate count (see _maybe_collect_domain_types): a single file with a
# genuine cluster of 5+ domain types is still a real cluster, and gating on
# the capped count would silently suppress the whole section for exactly the
# repos this collector exists to help with.
_MAX_ITEMS_PER_FILE = 3


def _is_infra_name(name: str) -> bool:
    return any(name != suffix and name.endswith(suffix) for suffix in _INFRA_SUFFIXES)


def _is_excluded_candidate(rel: str) -> bool:
    """True for generated/vendored code that happens to sit under a domain-ish
    path (internal backlog: a real-world repo had 7 of 10 slots taken by a
    vendored ``*.d.ts`` under src/types/).

    ``.d.ts`` needs its own check because ``Path.suffix`` only ever returns
    the last dot-segment (``Path("foo.d.ts").suffix == ".ts"``), so the
    extension allow-list in the caller cannot tell a TypeScript declaration
    file apart from a regular ``.ts`` module on its own.
    """
    name = Path(rel).name.lower()
    if name.endswith(".d.ts"):
        return True
    if ".generated." in name or ".pb." in name:
        return True
    lowered = f"/{rel.lower()}"
    return any(token in lowered for token in _EXCLUDED_DIR_TOKENS)


def _candidate_type_names(ctx: RepoContext, rel: str) -> Iterator[str]:
    """Yield structural type/interface/class/enum names declared in ``rel``,
    in file order, already excluding stop-words and infra-suffixed names.

    Cross-file de-duplication (the same name declared in two files) is the
    caller's job: it is the only place tracking what has already been
    consumed across the whole round-robin.
    """
    # Scan only the first 200 lines: type declarations live near the top,
    # and this bounds work on large files.
    text = "\n".join(read_text(ctx.root / rel, limit=40_000).splitlines()[:200])
    for match in _TYPE_PATTERN.finditer(text):
        name = match.group(1)
        if name in _STOP_NAMES or _is_infra_name(name):
            continue
        yield name


def _maybe_collect_domain_types(
    ctx: RepoContext, max_items: int
) -> List[Tuple[str, List[str]]]:
    candidate_paths = [
        p
        for p in ctx.tracked_files
        if Path(p).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs"}
        and any(token in f"/{p.lower()}" for token in _DOMAIN_PATH_TOKENS)
        and not is_test_path(p)
        and not _is_excluded_candidate(p)
    ]
    if not candidate_paths:
        return []

    # Collect enough to both fill the requested cap and evaluate the cluster
    # gate: when --max-domain-types is below the gate we still need
    # _MIN_DOMAIN_TYPES candidates to decide whether a real cluster exists.
    collect_target = max(max_items, _MIN_DOMAIN_TYPES)

    n = len(candidate_paths)
    iterators = [_candidate_type_names(ctx, rel) for rel in candidate_paths]
    finished = [False] * n
    displayed_counts = [0] * n
    seen: Set[str] = set()
    by_path: Dict[str, List[str]] = {}
    order: List[str] = []  # first-contribution file order, for stable output
    gate_total = 0  # uncapped per file; only this decides whether to show anything

    # Round-robin: one qualifying name per active file per pass, instead of
    # exhausting one file before moving to the next (internal backlog: a
    # real-world repo had every one of the 10 shown come from a single
    # entities file). A file keeps taking gate-counted turns even after it hits
    # _MAX_ITEMS_PER_FILE for *display* -- only its own supply running out
    # removes it from the rotation.
    while gate_total < collect_target and not all(finished):
        progressed = False
        for i in range(n):
            if finished[i]:
                continue
            if i >= _MAX_SCANNED_FILES:
                # Hard cap: stop opening new candidate files entirely, even
                # though the gate below may still be unsatisfied -- see
                # _MAX_SCANNED_FILES for why the soft cap alone cannot
                # bound this case.
                break
            if i >= _MAX_CANDIDATE_FILES and gate_total >= _MIN_DOMAIN_TYPES:
                # Gate already satisfied -- stop opening new candidate files
                # past the soft cap; files below the cap keep taking turns.
                break
            name = None
            for candidate in iterators[i]:
                if candidate not in seen:
                    name = candidate
                    break
            if name is None:
                finished[i] = True
                continue
            seen.add(name)
            gate_total += 1
            progressed = True
            if displayed_counts[i] < _MAX_ITEMS_PER_FILE:
                rel = candidate_paths[i]
                if rel not in by_path:
                    by_path[rel] = []
                    order.append(rel)
                by_path[rel].append(name)
                displayed_counts[i] += 1
            if gate_total >= collect_target:
                break
        if not progressed:
            break

    if gate_total < _MIN_DOMAIN_TYPES:
        return []

    # Cap the rendered total to max_items, trimming from the tail. This can
    # end up showing fewer than max_items entries even though gate_total
    # cleared the gate (e.g. one rich file capped to 3 plus a couple of
    # smaller ones) -- a smaller, cross-file-representative sample is the
    # intended trade-off, not a bug.
    groups: List[Tuple[str, List[str]]] = []
    remaining = max_items
    for rel in order:
        if remaining <= 0:
            break
        names = by_path[rel][:remaining]
        groups.append((rel, names))
        remaining -= len(names)
    return groups


def register():
    return DomainTypesCollector()
