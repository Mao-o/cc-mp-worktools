from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

from .constants import (
    DEFAULT_MAX_CONFIG_HINTS,
    DEFAULT_MAX_DOMAIN_TYPES,
    DEFAULT_MAX_ENV_KEYS,
    DEFAULT_MAX_MAJOR_DEPS,
    DEFAULT_MAX_NOTES,
    DEFAULT_MAX_SCRIPT_ENTRIES,
    DEFAULT_MAX_SERVICE_ENTRIES,
    DEFAULT_MAX_TREE_LINES,
    MAX_TREE_DEPTH,
    MIN_TREE_DEPTH,
)


class TestSnapshot(TypedDict, total=False):
    code_files: int
    test_files: int
    test_to_code_ratio: float
    unit_tests: int
    integration_tests: int
    e2e_tests: int
    test_dirs: List[str]


class ResultsDict(TypedDict, total=False):
    is_git_repo: bool
    purpose: str
    package_manager: str
    major_dependencies: List[str]
    test_snapshot: TestSnapshot


@dataclass
class AnalysisConfig:
    # tree_depth is an optional fixed-depth override; None enables the
    # dynamic-depth search bounded by [min_tree_depth, max_tree_depth].
    tree_depth: Optional[int] = None
    min_tree_depth: int = MIN_TREE_DEPTH
    max_tree_depth: int = MAX_TREE_DEPTH
    max_tree_lines: int = DEFAULT_MAX_TREE_LINES
    max_service_entries: int = DEFAULT_MAX_SERVICE_ENTRIES
    max_script_entries: int = DEFAULT_MAX_SCRIPT_ENTRIES
    max_env_keys: int = DEFAULT_MAX_ENV_KEYS
    max_notes: int = DEFAULT_MAX_NOTES
    max_major_deps: int = DEFAULT_MAX_MAJOR_DEPS
    include_domain_types: bool = False
    max_domain_types: int = DEFAULT_MAX_DOMAIN_TYPES
    max_config_hints: int = DEFAULT_MAX_CONFIG_HINTS


@dataclass
class RepoContext:
    root: Path
    config: AnalysisConfig
    cwd: Optional[Path] = None
    tracked_files: List[str] = field(default_factory=list)
    stack: List[str] = field(default_factory=list)
    results: ResultsDict = field(default_factory=dict)

    _pkg_json: Optional[dict] = field(default=None, init=False, repr=False)
    _all_deps: Optional[Dict[str, str]] = field(default=None, init=False, repr=False)
    _pyproject_toml: Optional[str] = field(default=None, init=False, repr=False)

    @property
    def cwd_relative(self) -> Optional[str]:
        """POSIX-style path of cwd relative to root, or None when cwd == root / unset / outside root."""
        if self.cwd is None:
            return None
        try:
            rel = self.cwd.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        rel_str = rel.as_posix()
        if rel_str in ("", "."):
            return None
        return rel_str

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

    @property
    def pyproject_toml(self) -> str:
        if self._pyproject_toml is None:
            from .fs import read_text
            path = self.root / "pyproject.toml"
            self._pyproject_toml = read_text(path) if path.exists() else ""
        return self._pyproject_toml
