from __future__ import annotations

import posixpath
from collections import Counter
from pathlib import Path
from typing import Optional

from core.constants import HUB_FILES_MAX_SCAN, HUB_FILES_MIN_REFS
from core.context import RepoContext
from core.fs import read_text
from core.imports import (
    JS_EXTENSIONS,
    PY_EXTENSIONS,
    extract_js_specifiers,
    extract_python_specifiers,
    resolve_js_import,
    resolve_python_import,
)
from core.util import is_test_path

_SCANNABLE_EXTENSIONS = set(JS_EXTENSIONS) | set(PY_EXTENSIONS)


class HubFilesCollector:
    """Ranks tracked files by how many distinct other tracked files import them.

    A lightweight, regex-based approximation of the "high-degree node" idea
    from full AST-based code-graph tools: no tree-sitter, no persistent
    artifact, just an import-specifier scan bounded to each file's first 200
    lines. Opt-in (``--include-hub-files``) because scanning file bodies is
    real work, unlike the rest of session-facts which only reads metadata.
    """

    name = "hub_files"
    section_title = "## Hub Files"
    priority = 75

    def should_run(self, ctx: RepoContext) -> bool:
        return ctx.config.include_hub_files

    def collect(self, ctx: RepoContext) -> Optional[str]:
        candidates = [
            p
            for p in ctx.tracked_files
            if Path(p).suffix.lower() in _SCANNABLE_EXTENSIONS and not is_test_path(p)
        ]
        # Perf guard: a full-body scan of every code file risks blowing the
        # hook's timeout on very large repos. Skip rather than degrade silently.
        if not candidates or len(candidates) > HUB_FILES_MAX_SCAN:
            return None

        tracked = set(ctx.tracked_files)
        counts: Counter = Counter()
        for rel in candidates:
            suffix = Path(rel).suffix.lower()
            # Imports live near the top of the file; 200 lines bounds work on
            # large files (mirrors domain_types.py's scan limit).
            text = "\n".join(read_text(ctx.root / rel, limit=40_000).splitlines()[:200])
            targets_in_file = set()
            if suffix in JS_EXTENSIONS:
                importer_dir = posixpath.dirname(rel)
                for spec in extract_js_specifiers(text):
                    target = resolve_js_import(spec, importer_dir, tracked)
                    if target and target != rel:
                        targets_in_file.add(target)
            else:
                for spec in extract_python_specifiers(text):
                    target = resolve_python_import(spec, rel, tracked)
                    if target and target != rel:
                        targets_in_file.add(target)
            counts.update(targets_in_file)

        ranked = [
            (path, n) for path, n in counts.most_common() if n >= HUB_FILES_MIN_REFS
        ][: ctx.config.max_hub_files]
        if not ranked:
            return None

        lines = [self.section_title]
        for path, n in ranked:
            lines.append(f"- {path} — referenced by {n} files")
        return "\n".join(lines)


def register():
    return HubFilesCollector()
