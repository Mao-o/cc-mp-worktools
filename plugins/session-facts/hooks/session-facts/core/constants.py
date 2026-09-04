from __future__ import annotations

SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".next",
    "coverage",
    ".turbo",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".DS_Store",
    ".yarn",
    ".pnpm-store",
    "vendor",
    "target",
    ".svelte-kit",
    "storybook-static",
    "out",
}

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rs", ".rb", ".php", ".java", ".kt",
    ".swift", ".cs", ".scala", ".lua", ".sh", ".bash",
    ".dart",
    # detectors/elixir_stack.py reports "stack: elixir" off mix.exs alone;
    # without these, a conventional Mix project's own lib/*.ex sources and
    # test/*_test.exs tests are silently dropped from Test Snapshot and
    # Service Entry Points (merge-review finding).
    ".ex", ".exs",
    # detectors/cmake_stack.py reports "stack: cmake" off CMakeLists.txt
    # alone -- CMakeLists.txt itself is a build script, not a source file,
    # so the actual sources a CMake project's Test Snapshot/Service Entry
    # Points need to see are the C/C++ files it builds. Same gap as
    # Elixir's, one level removed. .cxx/.hxx/.hh are also conventional
    # C++ suffixes (merge-review finding: the initial set only covered
    # .cc/.cpp/.h/.hpp and silently dropped these).
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".hh",
}

TEST_PATH_MARKERS = {
    "__tests__", "test", "tests", "spec", "specs", "cypress", "playwright", "e2e"
}

# collectors/scripts.py: a Dockerfile alone only grounds `docker build .`;
# these additionally ground `docker compose up`. Kept separate from
# detectors/docker.py's own (Dockerfile + these) tuple, which only needs to
# know "is docker present at all", not which specific command it grounds.
COMPOSE_FILE_CANDIDATES = (
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
)

SERVICE_DIR_MARKERS = [
    "services",
    "service",
    "usecases",
    "usecase",
    "repositories",
    "repository",
    "clients",
    "client",
    "adapters",
    "adapter",
    "gateways",
    "gateway",
    "api",
]

SCRIPT_PRIORITY_PATTERNS = [
    r"^dev$",
    r"^build$",
    r"^test$",
    r"^lint$",
    r"^typecheck$",
    r"^check$",
    r"^start$",
    r"^test:",
    r"^emulators?",
    r"^seed",
    r"^sync",
    r"^migrate",
]

# Priority order for surfacing Makefile targets in Likely Commands. Targets
# matching no pattern are dropped (a Makefile can have dozens of internal
# targets; only the conventional entry points are useful to an agent).
MAKE_TARGET_PRIORITY_PATTERNS = [
    r"^dev$",
    r"^run$",
    r"^start$",
    r"^serve$",
    r"^up$",
    r"^build$",
    r"^install$",
    r"^setup$",
    r"^bootstrap$",
    r"^test$",
    r"^test[-_]",
    r"^check$",
    r"^lint$",
    r"^fmt$",
    r"^format$",
    r"^typecheck$",
    r"^migrate",
    r"^seed",
]

IMPORTANT_DEPENDENCIES = {
    # JS/TS
    "next", "react", "react-dom", "typescript", "firebase", "@firebase/app", "@firebase/auth",
    "@prisma/client", "prisma", "zod", "vitest", "jest", "playwright", "cypress",
    "@playwright/test", "@tanstack/react-query", "zustand", "redux", "@reduxjs/toolkit",
    "express", "hono", "fastify", "nestjs", "@nestjs/core", "trpc", "@trpc/server",
    "tailwindcss",
    # Python
    "fastapi", "django", "flask", "pydantic", "sqlalchemy", "pytest", "uvicorn",
    "celery", "alembic", "redis", "gunicorn", "httpx", "langchain", "openai",
    # Go/Rust/Ruby/PHP
    "gin", "echo", "rails", "rspec", "laravel/framework",
}

# Flutter/Dart packages worth surfacing from pubspec.yaml. Kept separate from
# IMPORTANT_DEPENDENCIES because pubspec is parsed on its own path.
FLUTTER_IMPORTANT_DEPENDENCIES = {
    "firebase_core", "firebase_auth", "cloud_firestore", "firebase_storage",
    "firebase_messaging", "cloud_functions",
    "riverpod", "flutter_riverpod", "hooks_riverpod", "provider",
    "bloc", "flutter_bloc", "get", "get_it",
    "go_router", "auto_route",
    "dio", "http", "retrofit",
    "drift", "sqflite", "hive", "isar", "shared_preferences",
    "freezed", "json_serializable", "build_runner",
    "flutter_hooks", "intl",
}

ENV_FILE_CANDIDATES = [
    ".env.example",
    ".env.sample",
    ".env.local.example",
    ".env.local.sample",
    ".env.test.example",
    ".env.test.sample",
    ".env.development.example",
    ".env.development.sample",
]

NEXT_CONFIG_CANDIDATES = ["next.config.js", "next.config.mjs", "next.config.ts"]

# Dynamic-depth search bounds (collectors/structure.py, cwd_subtree.py).
# build_dir_tree runs once at MAX, render_tree is retried from MIN upward and
# the deepest rendering that fits DEFAULT_MAX_TREE_LINES wins.
MIN_TREE_DEPTH = 1
MAX_TREE_DEPTH = 5
DEFAULT_MAX_TREE_LINES = 100
MAX_PURPOSE_CHARS = 140
DEFAULT_MAX_SERVICE_ENTRIES = 12
DEFAULT_MAX_SCRIPT_ENTRIES = 16
DEFAULT_MAX_ENV_KEYS = 40
DEFAULT_MAX_NOTES = 8
DEFAULT_MAX_MAJOR_DEPS = 8
DEFAULT_MAX_DOMAIN_TYPES = 10
DEFAULT_MAX_CONFIG_HINTS = 8
DEFAULT_MAX_HUB_FILES = 8

# cli.py's _enforce_output_budget(): a hard ceiling on the whole rendered
# output. Claude Code's SessionStart plain-stdout / additionalContext
# injection has a real ceiling of its own (~10,000 chars) beyond which the
# harness saves the content to a file and substitutes a preview instead of
# injecting it directly; 8,000 leaves headroom under that so the full facts
# bundle is normally injected in place rather than falling back to a file.
DEFAULT_MAX_OUTPUT_CHARS = 8000

# collectors/scripts.py: a single overly-long script command (some
# generators produce one-liners several hundred chars wide) can dominate
# the ## Scripts section on its own; cap each command's displayed length.
MAX_SCRIPT_COMMAND_CHARS = 120

# cli.py: root-level files/dirs that mark a directory as "a project" worth
# the (potentially expensive, filesystem-walking) non-git analysis at all.
# A false negative here (a real project mistaken for a bare directory)
# silently drops facts for a legitimate repo, which is worse than
# occasionally still walking a marker-light edge case -- so both tiers
# below deliberately err toward inclusion, not toward a tight, provable
# scope.
#
# Tier 1: every static file/dir that one of this plugin's own detectors/
# or collectors/ already keys off of directly (mechanically grepped for
# ``.exists()`` checks -- or, for requirements*.txt, the equivalent
# startswith()/endswith() basename check -- against a plugin-recognised
# name), so a stack this plugin already recognises is never skipped by the
# gate. A handful of entries predate that discipline and match no current
# detector at all (Pipfile/setup.cfg/setup.py -- detectors/python_stack.py
# only reads pyproject.toml or a whole-tree .py-file-ratio heuristic that a
# marker *filename* fundamentally cannot express); they are kept for the
# same false-negative-avoidance reason, not removed for consistency.
#
# Tier 2: common project roots. Most of these now have a matching detector
# (CMakeLists.txt/cmake_stack.py, Package.swift/swift_stack.py,
# mix.exs/elixir_stack.py, build.sbt/scala_stack.py, *.csproj+*.sln/
# dotnet_stack.py) and so also satisfy the Tier 1 rationale above; they stay
# listed here rather than being moved, since this tuple is a flat list with
# no enforced Tier 1/Tier 2 split. Cargo.lock/Gemfile.lock are lockfile-only
# fallbacks for rust_stack.py/ruby_stack.py (which key off Cargo.toml/
# Gemfile), not markers for a still-undetected stack. Terraform (*.tf) is
# the one entry left with no detector at all in this plugin. Still worth a
# walk either way: the generic collectors (Structure, Test Snapshot,
# Scripts, ...) produce useful output even without a "stack:" line naming
# the language.
# Four of these are glob patterns (matched via has_project_markers()'s
# Path.glob() branch, not a literal exists() check): *.csproj/*.sln/*.tf
# since the manifest filename is project-specific, not fixed, and
# requirements*.txt to mirror collectors/dependencies.py's
# _tracked_requirements(), which already recognises any
# requirements-prefixed/.txt-suffixed basename (e.g. requirements-dev.txt)
# -- not just the exact "requirements.txt" name.
# `$HOME` 直下ではユーザー全体の既定を意味し、そのディレクトリが
# プロジェクトであることを示さないマーカー (mise config と同じ扱い)。
GLOBAL_ONLY_AT_HOME_MARKERS = (
    ".tool-versions",
    ".python-version",
    # core/runtime.py::detect_venv() が見るローカル venv。
    ".venv/pyvenv.cfg",
    "venv/pyvenv.cfg",
    # `$HOME/.venv` は「ホーム直下に作った作業用 venv」であって、
    # ホームがプロジェクトであることを示さない。
    ".venv/pyvenv.cfg",
    "venv/pyvenv.cfg",
)

PROJECT_MARKERS = (
    "package.json",
    "pyproject.toml",
    "requirements*.txt",
    "Pipfile",
    "setup.cfg",
    "setup.py",
    "go.mod",
    "Cargo.toml",
    "pubspec.yaml",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "Justfile",
    "justfile",
    "Taskfile.yml",
    "Taskfile.yaml",
    "deno.json",
    "deno.jsonc",
    "marketplace.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    # detectors/docker.py
    "Dockerfile",
    *COMPOSE_FILE_CANDIDATES,
    # detectors/mise.py, via core/runtime.py's has_mise()/_MISE_CONFIG_NAMES
    ".mise.toml",
    "mise.toml",
    ".config/mise/config.toml",
    ".tool-versions",
    # core/runtime.py::build_runtime_info() はこれも読む。ランタイム
    # 固定ファイルだけを持つ Python プロジェクトを gate で落とすと、
    # ファイル比率による言語検出もランタイム情報の収集も走らないまま
    # facts が丸ごと消える。
    ".python-version",
    # collectors/env_keys.py の ENV_FILE_CANDIDATES。テンプレートだけを
    # 置くプロジェクト (実 .env は gitignore) でも env keys とソース由来の
    # facts は出せるので、gate で落とさない。実体の `.env` は機密なので
    # マーカーに含めない。
    *ENV_FILE_CANDIDATES,
    # detectors/nextjs.py
    *NEXT_CONFIG_CANDIDATES,
    # detectors/node_typescript.py
    "tsconfig.json",
    "tsconfig.base.json",
    # detectors/prisma.py (directory, not a file -- exists() doesn't care)
    "prisma",
    # detectors/python_stack.py
    # core/pm.py::detect_package_manager() が認識する lockfile 一式。
    # lockfile だけを持つディレクトリ (マニフェストが消えている / 生成物だけ
    # 配布されている構成) でも package manager・構造・ソース・テストの収集は
    # 動くので、gate で落とすと facts が無意味に消える。
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "package-lock.json",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "uv.toml",
    "uv.lock",
    "uv.toml",
    "poetry.lock",
    # detectors/react_vite.py
    "vite.config.ts",
    "vite.config.js",
    # detectors/taskrunner.py
    "nx.json",
    # detectors/testing.py
    "pnpm-workspace.yaml",
    "turbo.json",
    "playwright.config.ts",
    "cypress.config.ts",
    # collectors/repo_notes.py's firebase-integration note wording
    "firebase.json",
    ".firebaserc",
    # Tier 2 (see module comment above)
    "CMakeLists.txt",
    "Package.swift",
    "mix.exs",
    "build.sbt",
    "Cargo.lock",
    "Gemfile.lock",
    "*.csproj",
    "*.sln",
    "*.tf",
)

# hub_files collector (core/imports.py + collectors/hub_files.py): scanning
# every candidate file's body is real work, so cap the candidate count to
# bound worst-case cost on very large repos, and require multiple distinct
# referrers before a file is surfaced as noise-free signal.
HUB_FILES_MAX_SCAN = 3000
HUB_FILES_MIN_REFS = 2
