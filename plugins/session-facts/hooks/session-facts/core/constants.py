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

IMPORTANT_DEPENDENCIES = {
    # JS/TS
    "next", "react", "react-dom", "typescript", "firebase", "@firebase/app", "@firebase/auth",
    "@prisma/client", "prisma", "zod", "vitest", "jest", "playwright", "cypress",
    "@playwright/test", "@tanstack/react-query", "zustand", "redux", "@reduxjs/toolkit",
    "express", "hono", "fastify", "nestjs", "@nestjs/core", "trpc", "@trpc/server",
    "tailwindcss",
    # Python
    "fastapi", "django", "flask", "pydantic", "sqlalchemy", "pytest", "uvicorn",
    # Go/Rust/Ruby/PHP
    "gin", "echo", "rails", "rspec", "laravel/framework",
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

DEFAULT_TREE_DEPTH = 3
DEFAULT_MAX_TREE_LINES = 100
DEFAULT_MAX_SERVICE_ENTRIES = 12
DEFAULT_MAX_SCRIPT_ENTRIES = 16
DEFAULT_MAX_ENV_KEYS = 40
DEFAULT_MAX_NOTES = 8
DEFAULT_MAX_CONFIG_HINTS = 8
