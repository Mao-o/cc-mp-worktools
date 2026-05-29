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
}

TEST_PATH_MARKERS = {
    "__tests__", "test", "tests", "spec", "specs", "cypress", "playwright", "e2e"
}

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
