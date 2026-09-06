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
    HUB_FILES_MAX_SCAN,
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
    # core/workspaces.py summaries for the header (joa.2).
    workspaces: List[Dict[str, object]]
    # True when the tracked-file list hit MAX_TRACKED_FILES (joa.25).
    tracked_files_truncated: bool


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
    max_hub_scan: int = HUB_FILES_MAX_SCAN
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

    # --- manifest scoping -------------------------------------------------
    #
    # ``package_json`` / ``pyproject_toml`` / ``detect_package_manager`` read
    # the manifest directory this run is scoped to: in subtree mode (cwd
    # inside a workspace such as ``api/`` or ``apps/web/``) that is the
    # workspace's own manifest, otherwise the repo root (joa.2 part 2).
    # ``root_package_json`` is the root manifest unconditionally, for the
    # few readers (purpose) that describe the repository as a whole.

    @property
    def manifest_rel(self) -> str:
        """Relative dir of the manifest this run is scoped to ("" = root)."""
        cwd_rel = self.cwd_relative
        if not cwd_rel:
            return ""
        best = ""
        for rel_dir in self.workspace_dirs:
            if cwd_rel == rel_dir or cwd_rel.startswith(rel_dir + "/"):
                if len(rel_dir) > len(best):
                    best = rel_dir
        return best

    @property
    def manifest_root(self) -> Path:
        rel = self.manifest_rel
        return self.root / rel if rel else self.root

    @property
    def manifest_dirs(self) -> List[str]:
        """Root ("") followed by every workspace dir."""
        return [""] + list(self.workspace_dirs)

    def find_in_manifest_dirs(self, *names: str) -> Optional[str]:
        """The first manifest dir ("" = root) where any of ``names`` exists,
        or None. Lets a detector keyed off a config file (``next.config.js``,
        ``Dockerfile``, ``go.mod``) fire for a sub-project too."""
        for rel_dir in self.manifest_dirs:
            base = self.root / rel_dir if rel_dir else self.root
            for name in names:
                if (base / name).exists():
                    return rel_dir
        return None

    @property
    def root_package_json(self) -> dict:
        from .fs import load_json
        return load_json(self.root / "package.json") or {}

    @property
    def package_json(self) -> dict:
        if self._pkg_json is None:
            from .fs import load_json
            self._pkg_json = load_json(self.manifest_root / "package.json") or {}
        return self._pkg_json

    @property
    def all_deps(self) -> Dict[str, str]:
        """npm dependencies across the root and every workspace manifest
        (root first, so its version wins on a name clash)."""
        if self._all_deps is None:
            self._all_deps = {}
            for _rel, pkg in self.package_json_manifests():
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    d = pkg.get(section)
                    if isinstance(d, dict):
                        for name, version in d.items():
                            self._all_deps.setdefault(name, version)
        return self._all_deps

    @property
    def pyproject_toml(self) -> str:
        if self._pyproject_toml is None:
            from .fs import read_text
            path = self.manifest_root / "pyproject.toml"
            self._pyproject_toml = read_text(path) if path.exists() else ""
        return self._pyproject_toml

    @property
    def all_pyproject_text(self) -> str:
        """Every tracked pyproject.toml joined, for keyword-style detection."""
        return "\n".join(text for _rel, text in self.pyproject_manifests())

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
                if any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1]):
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
            root_pkg = self.root_package_json
            if root_pkg:
                out.append(("", root_pkg))
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
            root_path = self.root / "pyproject.toml"
            if root_path.exists():
                text = read_text(root_path)
                if text:
                    out.append(("", text))
            for rel_dir in self.workspace_dirs:
                path = self.root / rel_dir / "pyproject.toml"
                if path.exists():
                    text = read_text(path)
                    if text:
                        out.append((rel_dir, text))
            self._pyproject_manifests = out
        return self._pyproject_manifests
