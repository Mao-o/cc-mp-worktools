from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

from .constants import (
    MAX_WORKSPACE_MANIFESTS,
    MAX_WORKSPACE_MANIFEST_DEPTH,
    SKIP_DIRS,
    WORKSPACE_MANIFEST_NAMES,
    DEFAULT_MAX_CONFIG_HINTS,
    DEFAULT_MAX_DOMAIN_TYPES,
    DEFAULT_MAX_ENV_KEYS,
    DEFAULT_MAX_HUB_FILES,
    DEFAULT_MAX_MAJOR_DEPS,
    DEFAULT_MAX_NOTES,
    DEFAULT_MAX_OUTPUT_CHARS,
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


class RuntimeInfo(TypedDict, total=False):
    manager: str  # "mise" | "asdf"
    tools: Dict[str, str]  # {tool: version}
    python_version: str  # from .python-version
    venv: str  # virtualenv directory name (".venv" / "venv")
    venv_python: str  # interpreter version from pyvenv.cfg


class ResultsDict(TypedDict, total=False):
    is_git_repo: bool
    purpose: str
    package_manager: str
    major_dependencies: List[str]
    runtime: RuntimeInfo
    test_snapshot: TestSnapshot
    # Memoization slot for core.firebase.has_firebase(), set by whichever of
    # detectors/firebase.py / collectors/repo_notes.py runs first.
    has_firebase: bool


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
    include_hub_files: bool = False
    max_hub_files: int = DEFAULT_MAX_HUB_FILES
    # SessionStart passes False: the harness already injects recent commits
    # there (gitStatus), while subagents receive no git context at all.
    include_recent_commits: bool = True
    # Hard ceiling on the whole rendered output (see DEFAULT_MAX_OUTPUT_CHARS
    # for the rationale). Enforced by cli.py's _enforce_output_budget().
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS


@dataclass
class RepoContext:
    root: Path
    config: AnalysisConfig
    cwd: Optional[Path] = None
    # The literal path this run was invoked with (sys.argv[0]), so the
    # injected output can name a copy-pasteable follow-up command (e.g.
    # `python3 <invoked_as> --help`) without the reader needing to resolve
    # ${CLAUDE_PLUGIN_ROOT} itself. invoked_as is a directory for the real
    # `python3 <dir>` hook invocation, not an executable, so the `python3 `
    # prefix is required for the command to actually run. None when
    # summarize_repo() is called as a library (e.g. from tests).
    invoked_as: Optional[str] = None
    tracked_files: List[str] = field(default_factory=list)
    stack: List[str] = field(default_factory=list)
    results: ResultsDict = field(default_factory=dict)

    _pkg_json: Optional[dict] = field(default=None, init=False, repr=False)
    _all_deps: Optional[Dict[str, str]] = field(default=None, init=False, repr=False)
    _pyproject_toml: Optional[str] = field(default=None, init=False, repr=False)
    _workspace_dirs: Optional[List[str]] = field(default=None, init=False, repr=False)
    _pkg_manifests: Optional[List[Tuple[str, dict]]] = field(default=None, init=False, repr=False)
    _pyproject_manifests: Optional[List[Tuple[str, str]]] = field(default=None, init=False, repr=False)

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
    def rerun_root(self) -> Path:
        """The path a rerun hint's ``--root`` should embed: cwd when it
        names root itself or a subdirectory of root, else root (cwd
        unset, or -- defensively, since the real hook invocation never
        constructs this case -- outside root).

        root is the git top-level, but a run started against a
        subdirectory (cwd) must keep that same scope when its printed
        hint (see renderer.build_rerun_hint()) is copied and rerun.
        Embedding root unconditionally would silently re-analyze the
        whole repository on rerun and drop whatever cwd-scoped context
        (``## Subtree``, test filtering, ...) the original run had.
        """
        if self.cwd is None:
            return self.root
        try:
            self.cwd.resolve().relative_to(self.root.resolve())
        except ValueError:
            return self.root
        return self.cwd

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

    # --- workspace (sub-project) manifests -------------------------------
    #
    # Monorepos and "api/ + web/" layouts keep their manifests below the
    # root (internal backlog joa.2). The tracked-file list already names
    # every manifest, so discovery is a filter over it, not a walk: no
    # node_modules, no vendored copies, bounded depth and count.

    @property
    def workspace_dirs(self) -> List[str]:
        """Directories (relative, POSIX) holding a package.json or
        pyproject.toml below the root, shallowest first. Excludes the root
        itself and anything under SKIP_DIRS."""
        if self._workspace_dirs is None:
            found: List[str] = []
            seen = set()
            for path in self.tracked_files:
                parts = path.split("/")
                if len(parts) < 2 or parts[-1] not in WORKSPACE_MANIFEST_NAMES:
                    continue
                if len(parts) - 1 > MAX_WORKSPACE_MANIFEST_DEPTH:
                    continue
                if any(part in SKIP_DIRS for part in parts[:-1]):
                    continue
                rel_dir = "/".join(parts[:-1])
                if rel_dir not in seen:
                    seen.add(rel_dir)
                    found.append(rel_dir)
            found.sort(key=lambda d: (d.count("/"), d))
            self._workspace_dirs = found[:MAX_WORKSPACE_MANIFESTS]
        return self._workspace_dirs

    def package_json_manifests(self) -> List[Tuple[str, dict]]:
        """``[(rel_dir, parsed package.json)]`` for the root ("" when
        present) followed by each workspace dir that has one."""
        if self._pkg_manifests is None:
            from .fs import load_json
            out: List[Tuple[str, dict]] = []
            if self.package_json:
                out.append(("", self.package_json))
            for rel_dir in self.workspace_dirs:
                data = load_json(self.root / rel_dir / "package.json")
                if isinstance(data, dict):
                    out.append((rel_dir, data))
            self._pkg_manifests = out
        return self._pkg_manifests

    def pyproject_manifests(self) -> List[Tuple[str, str]]:
        """``[(rel_dir, pyproject text)]`` for the root ("" when present)
        followed by each workspace dir that has one."""
        if self._pyproject_manifests is None:
            from .fs import read_text
            out: List[Tuple[str, str]] = []
            if self.pyproject_toml:
                out.append(("", self.pyproject_toml))
            for rel_dir in self.workspace_dirs:
                path = self.root / rel_dir / "pyproject.toml"
                if path.exists():
                    text = read_text(path)
                    if text:
                        out.append((rel_dir, text))
            self._pyproject_manifests = out
        return self._pyproject_manifests
