from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RepoContext:
    root: Path
    args: argparse.Namespace
    tracked_files: List[str] = field(default_factory=list)
    stack: List[str] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)

    _pkg_json: Optional[dict] = field(default=None, repr=False)
    _all_deps: Optional[Dict[str, str]] = field(default=None, repr=False)

    @property
    def package_json(self) -> dict:
        if self._pkg_json is None:
            from .fs import load_json
            self._pkg_json = load_json(self.root / "package.json") or {}
        return self._pkg_json

    @property
    def all_deps(self) -> Dict[str, str]:
        if self._all_deps is None:
            self._all_deps = {}
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                d = self.package_json.get(section)
                if isinstance(d, dict):
                    self._all_deps.update(d)
        return self._all_deps
