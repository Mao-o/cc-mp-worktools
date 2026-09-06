#!/usr/bin/env python3
"""純粋関数: テキスト → 数値メトリクス。ファイルシステムアクセスなし。

``compute()`` の第一引数 ``loaded`` は ``source.LoadedFile`` 相当
(``.text: str`` / ``.lines: list[str]``) を期待するダックタイピングで、
source.py への型依存は意図的に持たない (I/O 境界と純粋関数の分離を保つため)。
"""
from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from language import is_vague_filename

# 定義宣言の行頭キーワード。ES modules の主流形 (`export function` /
# `export default function` / `async function`)、Rust (`pub fn` / `impl` /
# `trait`)、Kotlin (`fun` / `object`)、TypeScript/Go (`type`) を含む。
_DEF_KEYWORDS_RE = re.compile(
    r"^\s*"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:declare\s+)?"
    r"(?:abstract\s+)?"
    r"(?:pub(?:\([^)]*\))?\s+)?"  # Rust: pub / pub(crate)
    r"(?:async\s+)?"
    r"(?:def|class|function|func|fn|fun|interface|struct|enum|trait|impl|type|object)\b"
)

# `const foo = (a, b) => {` 形のアロー関数。**矢印が右辺の最上位**であること
# を要求する — `=` と `=>` の間に許すのは仮引数リスト (括弧で囲まれた 1 組、
# 入れ子なし) か識別子 1 個だけ。これを緩めると
# `const total = arr.reduce((acc, x) => acc + x, 0)` のような「アロー関数を
# 引数に取る呼び出し」まで定義として数え、def_count がコーパス全体で膨らむ。
_ARROW_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]*)?=\s*"
    r"(?:async\s+)?(?:\([^()]*\)|\w+)\s*=>"
)

_CONTROL_FLOW_RE = re.compile(r"\b(if|for|while|switch|case|catch|except)\b")

# 行頭に来る import 文の形。**大文字小文字を区別する** — IGNORECASE だと
# docstring や行頭の英文 ("Use the following helper ..." / "Import the module
# ...") が import 行として数えられる。代わりに、大文字で始まるのが正規の
# 綴りである PowerShell の `Import-Module` だけ明示的に列挙する。
_IMPORT_HINT_RE = re.compile(
    r"^\s*("
    r"import\b"
    r"|from\b.*\bimport\b"
    r"|using\b"  # C# / PowerShell (using namespace ...)
    r"|use\s"  # Rust / PHP
    r"|require_relative\b|require\b"  # Ruby
    r"|Import-Module\b"  # PowerShell
    r"|#include\b"
    r")"
)

# 行頭とは限らない CommonJS の require 呼び出し
# (``const fs = require('fs')`` / ``import x = require('y')``)。
_REQUIRE_CALL_RE = re.compile(r"\brequire\s*\(")

# ``import (`` / ``from x import (`` のような括弧付き import ブロックの継続行を
# 何行まで追うか。閉じ括弧を見失ったときにファイル全体を import 扱いしない
# ための安全弁。
_IMPORT_BLOCK_MAX_LINES = 100

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
        "websockets",
        "httpx",
        "aiohttp",
        "urllib3",
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
        "node:fs",
        "fs/promises",
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

    ``ast.parse`` は無効なエスケープシーケンス (``"\\d"`` 等) を含む文字列
    リテラルに対して Python 3.12+ で ``SyntaxWarning`` を stderr に出す。この
    hook は毎回の Write/Edit で呼ばれる (debounce で emit が抑制される場合も
    判定自体は毎回走る) ため、抑制しないと編集のたびに stderr が汚れる。判定
    結果には影響しないため ``catch_warnings`` で無条件に抑制する。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, RecursionError, ValueError):
        return None
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count += 1
    return count


def _count_defs_generic(lines: list[str]) -> int:
    """行頭キーワード + アロー関数代入による近似カウント。

    Java/C# のメソッド宣言はアクセス修飾子と戻り値型から始まりキーワードを
    伴わないため、依然として拾えない (既知の限界)。
    """
    return sum(
        1
        for line in lines
        if _DEF_KEYWORDS_RE.match(line) or _ARROW_DEF_RE.match(line)
    )


def _iter_import_lines(lines: list[str]):
    """import 行を列挙する (括弧付き import ブロックの継続行を含む)。

    Go の ``import ( ... )`` や Python の ``from x import ( ... )`` は、実際の
    モジュール名が継続行に書かれる。開き括弧で終わる import 行を見たら、
    対応する閉じ括弧までを import 行として扱う簡易ステートを持つ。
    閉じ括弧を見失ったときのために ``_IMPORT_BLOCK_MAX_LINES`` で打ち切る。
    """
    block_remaining = 0
    for line in lines:
        stripped = line.strip()
        if block_remaining > 0:
            block_remaining -= 1
            if stripped.startswith(")"):
                block_remaining = 0
                continue
            if stripped:
                yield line
            continue
        if _IMPORT_HINT_RE.match(line) or _REQUIRE_CALL_RE.search(line):
            yield line
            if stripped.endswith("("):
                block_remaining = _IMPORT_BLOCK_MAX_LINES


def _count_import_categories(lines: list[str]) -> tuple[int, tuple[str, ...]]:
    import_lines = list(_iter_import_lines(lines))
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
