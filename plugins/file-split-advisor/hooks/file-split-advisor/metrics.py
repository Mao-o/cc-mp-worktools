#!/usr/bin/env python3
"""純粋関数: テキスト → 数値メトリクス。ファイルシステムアクセスなし。

``compute()`` の第一引数 ``loaded`` は ``source.LoadedFile`` 相当
(``.text: str`` / ``.lines: list[str]``) を期待するダックタイピングで、
source.py への型依存は意図的に持たない (I/O 境界と純粋関数の分離を保つため)。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from language import is_vague_filename

_DEF_KEYWORDS_RE = re.compile(r"^\s*(def|class|function|func|interface|struct|enum)\b")

_CONTROL_FLOW_RE = re.compile(r"\b(if|for|while|switch|case|catch|except)\b")

_IMPORT_HINT_RE = re.compile(
    r"^\s*(import\b|from\b.*\bimport\b|require\(|use\s|#include\b)",
    re.IGNORECASE,
)

# import 文を分類する 7 カテゴリのキーワード辞書。「うっかり露出予防」と同種の
# ヒューリスティックであり、完全な import resolver ではない (既知の限界)。
IMPORT_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "network": (
        "http",
        "https",
        "fetch",
        "axios",
        "requests",
        "socket",
        "grpc",
        "urllib",
        "okhttp",
        "retrofit",
        "websocket",
        "net/http",
        "reqwest",
    ),
    "db": (
        "sql",
        "mongo",
        "mongodb",
        "redis",
        "postgres",
        "postgresql",
        "mysql",
        "sqlite",
        "prisma",
        "sequelize",
        "typeorm",
        "gorm",
        "dynamodb",
        "firestore",
        "database/sql",
    ),
    "ui": (
        "react",
        "vue",
        "angular",
        "svelte",
        "widget",
        "component",
        "swiftui",
        "uikit",
        "compose",
        "flutter/material",
        "flutter/widgets",
        "tkinter",
        "pyqt",
        "pyside",
    ),
    "logging": (
        "logging",
        "logger",
        "sentry",
        "winston",
        "slf4j",
        "zap",
        "log4j",
        "loguru",
    ),
    "testing": (
        "pytest",
        "unittest",
        "jest",
        "junit",
        "mocha",
        "chai",
        "testing",
        "mock",
        "rspec",
        "xctest",
    ),
    "auth": (
        "auth",
        "jwt",
        "oauth",
        "passport",
        "session",
        "credential",
        "keycloak",
    ),
    "filesystem": (
        "pathlib",
        "os.path",
        "filesystem",
        "shutil",
        "ioutil",
        "glob",
        "'fs'",
        '"fs"',
    ),
}


def _compile_category_patterns() -> dict[str, re.Pattern]:
    patterns = {}
    for category, keywords in IMPORT_CATEGORY_KEYWORDS.items():
        escaped = [re.escape(k) for k in keywords]
        pattern = r"(?<![A-Za-z0-9_])(?:" + "|".join(escaped) + r")(?![A-Za-z0-9_])"
        patterns[category] = re.compile(pattern, re.IGNORECASE)
    return patterns


_CATEGORY_PATTERNS = _compile_category_patterns()


@dataclass(frozen=True)
class Metrics:
    line_count: int
    def_count: int
    import_category_count: int
    import_categories: tuple[str, ...]
    control_flow_density: float
    vague_filename: bool


def count_defs_python(text: str) -> int | None:
    """AST で FunctionDef/AsyncFunctionDef/ClassDef を再帰的にカウント。

    構文解析できない場合は None (呼び出し側が generic regex にフォールバックする)。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, RecursionError, ValueError):
        return None
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count += 1
    return count


def _count_defs_generic(lines: list[str]) -> int:
    """行頭キーワード正規表現による近似カウント (Java/C#/Kotlin のメソッド宣言は拾えない)。"""
    return sum(1 for line in lines if _DEF_KEYWORDS_RE.match(line))


def _count_import_categories(lines: list[str]) -> tuple[int, tuple[str, ...]]:
    import_lines = [line for line in lines if _IMPORT_HINT_RE.match(line)]
    if not import_lines:
        return 0, ()
    matched: set[str] = set()
    for line in import_lines:
        for category, pattern in _CATEGORY_PATTERNS.items():
            if category in matched:
                continue
            if pattern.search(line):
                matched.add(category)
    ordered = tuple(category for category in IMPORT_CATEGORY_KEYWORDS if category in matched)
    return len(ordered), ordered


def _control_flow_density(lines: list[str]) -> float:
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return 0.0
    hits = sum(1 for line in non_empty if _CONTROL_FLOW_RE.search(line))
    return hits / len(non_empty)


def compute(loaded, language: str, path: Path) -> Metrics:
    lines = loaded.lines
    line_count = len(lines)
    if line_count == 0:
        return Metrics(
            line_count=0,
            def_count=0,
            import_category_count=0,
            import_categories=(),
            control_flow_density=0.0,
            vague_filename=is_vague_filename(path),
        )

    if language == "python":
        exact = count_defs_python(loaded.text)
        def_count = exact if exact is not None else _count_defs_generic(lines)
    else:
        def_count = _count_defs_generic(lines)

    category_count, category_names = _count_import_categories(lines)

    return Metrics(
        line_count=line_count,
        def_count=def_count,
        import_category_count=category_count,
        import_categories=category_names,
        control_flow_density=_control_flow_density(lines),
        vague_filename=is_vague_filename(path),
    )
