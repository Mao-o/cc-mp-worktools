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
#
# 末尾の否定先読みは「宣言ではない位置に現れた同じ綴り」を 2 系統除外する。
#
# 1. ``:`` / ``?:`` — オブジェクトリテラル/インタフェースのプロパティ名。
#    `type` / `enum` / `class` は TypeScript の
#    ``{ type: string; enum?: string[] }`` のようなプロパティ名として頻出し、
#    これを数えると宣言の少ないデータ定義ファイルで def_count が水増しされる
#    (実コーパスで 1 → 37 に膨らむ fixture を観測した)
# 2. ``.`` / ``(`` / ``[`` — 識別子として使われた行頭のキーワード。
#    ``object.keys(x)`` (JS) / ``impl.run()`` / ``type(x)`` (Python の組み込み)
#    / ``fun(x)`` はいずれも宣言ではないのに旧版は定義として数えていた
#    (マージ前レビューの指摘)
#
# 宣言側は必ず ``type Foo = ...`` のように識別子が続くため、キーワード直後の
# これらの記号だけを弾けば分離できる。Go の ``interface{}`` / ``struct{}``
# (型リテラル) は ``{`` が続くのでこの否定先読みには掛からず、従来どおり
# 数える。
_DEF_KEYWORDS_RE = re.compile(
    r"^\s*"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:declare\s+)?"
    r"(?:abstract\s+)?"
    r"(?:pub(?:\([^)]*\))?\s+)?"  # Rust: pub / pub(crate)
    r"(?:async\s+)?"
    r"(?:def|class|function|func|fn|fun|interface|struct|enum|trait|impl|type|object)"
    r"\b(?!\s*\??\s*:)(?!\s*[.(\[])"
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

# 制御フロー密度に数える語。どの言語でも同じ綴りが同じ意味で使われる基本集合。
_BASE_CONTROL_FLOW_KEYWORDS = (
    "if",
    "for",
    "while",
    "switch",
    "case",
    "catch",
    "except",
)

# 言語固有の分岐・繰り返し・例外構文。汎用集合に入れると別言語で誤検出する
# (JavaScript の ``str.match(...)``、Go 以外での ``select``) ため、
# ``detect_language`` の結果で絞る。未登録の言語は基本集合のみ。
_LANGUAGE_CONTROL_FLOW_KEYWORDS: dict[str, tuple[str, ...]] = {
    "python": ("elif", "try"),
    # ``elsif`` は ``elif`` と同じ位置づけの語で、落とすと Ruby の if/elsif
    # 連鎖だけが数えられない歪みが残る。``when`` は Ruby の ``case`` 式の
    # 分岐節で、``case`` だけ数えて ``when`` を落とすと分岐の本数が
    # 数えられない (マージ前レビューの指摘)。
    "ruby": ("elsif", "unless", "until", "rescue", "when"),
    "rust": ("match", "loop"),
    "kotlin": ("when",),
    "go": ("select",),
}

# 文の先頭でだけ数える語。Python の ``match`` は soft keyword で、
# ``re.match(...)`` / ``m.match(...)`` という呼び出し形が同じ綴りで頻出する
# ため、行内一致にすると正規表現を使うだけのファイルが高密度に見える。
# Rust の ``match`` は ``let x = match y {`` のように行中に来るのが普通なので
# そちらは行内一致のまま残す。
_LANGUAGE_CONTROL_FLOW_STATEMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "python": ("match",),
}

# 文頭限定の語に付ける否定先読み。文頭でも ``match = re.match(...)`` (代入)
# ・``match(x)`` (呼び出し) ・``match.group(0)`` ・``match[0]`` は制御フロー
# ではなく、soft keyword を普通の変数名として使っているだけなので弾く
# (マージ前レビューの指摘)。
#
# ただし ``(`` を無条件に弾くと ``match (value):`` (subject を括弧で囲む形) と
# ``match (a, b):`` (tuple subject) という正当な match 文まで落ちる。文なら
# 行が ``:`` で終わるので、**行末が ``:`` のときだけ ``(`` を許す**
# (マージ前レビューの指摘)。``match(x)`` のような呼び出しは行末が ``:`` に
# ならないため引き続き除外される。行末コメントは呼び出し前にマスクされて
# 空白になっているため ``[ \t]*$`` で足りる。
_STATEMENT_KEYWORD_SUFFIX = (
    r"(?!\s*[=.\[])"  # match = ... / match.group(0) / match[0]
    r"(?:(?!\s*\()|(?=[^\n]*:[ \t]*$))"  # match(x) は呼び出し / match (x): は文
)

_CONTROL_FLOW_RE_CACHE: dict[str, re.Pattern] = {}


def _control_flow_re(language: str) -> re.Pattern:
    cached = _CONTROL_FLOW_RE_CACHE.get(language)
    if cached is not None:
        return cached
    words = _BASE_CONTROL_FLOW_KEYWORDS + _LANGUAGE_CONTROL_FLOW_KEYWORDS.get(language, ())
    alternatives = [r"\b(?:" + "|".join(words) + r")\b"]
    statement_words = _LANGUAGE_CONTROL_FLOW_STATEMENT_KEYWORDS.get(language, ())
    if statement_words:
        alternatives.append(
            r"^\s*(?:" + "|".join(statement_words) + r")\b" + _STATEMENT_KEYWORD_SUFFIX
        )
    compiled = re.compile("|".join(alternatives), re.MULTILINE)
    _CONTROL_FLOW_RE_CACHE[language] = compiled
    return compiled


# 行コメント記号。``#`` を C 系に適用すると ``#if`` / ``#include`` のような
# プリプロセッサ指令まで消えるため、言語ごとに分ける。
_SLASH_COMMENT_LANGUAGES = frozenset(
    {
        "javascript",
        "typescript",
        "javascriptreact",
        "typescriptreact",
        "java",
        "csharp",
        "kotlin",
        "dart",
        "go",
        "rust",
        "php",
        "swift",
        "c",
        "cpp",
        "objectivec",
        "scala",
        "groovy",
        "zig",
        "vue",
        "svelte",
    }
)

_HASH_COMMENT_LANGUAGES = frozenset(
    {
        "python",
        "ruby",
        "shell",
        "perl",
        "r",
        "elixir",
        "julia",
        "nim",
        "powershell",
        "php",  # `//` と `#` の両方が行コメント
    }
)

_OTHER_LINE_COMMENTS: dict[str, tuple[str, ...]] = {
    "lua": ("--",),
    "haskell": ("--",),
    "clojure": (";",),
    "erlang": ("%",),
}

# ``/* ... */`` を持つ言語 (= `//` 行コメントを持つ言語と同じ集合)。
_BLOCK_COMMENT_LANGUAGES = _SLASH_COMMENT_LANGUAGES

# 三重引用符の複数行文字列を持つ言語。
_TRIPLE_QUOTE_LANGUAGES = frozenset({"python", "elixir"})
_TRIPLE_QUOTE_DELIMITERS = ('"""', "'''")

# **改行を跨ぐ**文字列リテラルの引用符。JS/TS のテンプレートリテラルと Go の
# raw string はバッククォートで囲まれ、複数行に跨るのが普通の書き方
# (SQL・HTML・GraphQL の埋め込み等)。行内で閉じる前提のパターンで扱うと
# マスクが最初の改行で止まり、残りの本文にある ``if`` / ``for`` / ``while``
# で始まる散文行が制御フローとして数えられていた (マージ前レビューの指摘)。
# 三重引用符と同じ扱いにする。
_MULTILINE_STRING_DELIMITERS: dict[str, tuple[str, ...]] = {
    "javascript": ("`",),
    "typescript": ("`",),
    "javascriptreact": ("`",),
    "typescriptreact": ("`",),
    "vue": ("`",),
    "svelte": ("`",),
    "go": ("`",),
}

# 行内で閉じる文字列リテラルの引用符。既定は ``'`` と ``"``。
#   - rust: ``'`` はライフタイム注釈 (``&'a str``) で使われ、文字列として
#     扱うと閉じ引用符を探して行末まで飲み込む。``"`` のみに絞る
_DEFAULT_STRING_DELIMITERS = ("'", '"')
_STRING_DELIMITERS: dict[str, tuple[str, ...]] = {
    "rust": ('"',),
}

_NOISE_RE_CACHE: dict[str, re.Pattern | None] = {}


def _line_comment_prefixes(language: str) -> tuple[str, ...]:
    prefixes: list[str] = []
    if language in _SLASH_COMMENT_LANGUAGES:
        prefixes.append("//")
    if language in _HASH_COMMENT_LANGUAGES:
        prefixes.append("#")
    prefixes.extend(_OTHER_LINE_COMMENTS.get(language, ()))
    return tuple(prefixes)


def _noise_re(language: str) -> re.Pattern | None:
    """コメント・文字列リテラルにマッチする正規表現 (言語別、なければ None)。

    交替の順序が意味を持つ: 複数行のもの → 単一行の文字列 → 行コメント。
    正規表現は左から順に位置を進めるため、文字列の中の ``//`` は文字列側の
    交替に先に飲まれ、コメントの中の引用符はコメント側に飲まれる。
    """
    if language in _NOISE_RE_CACHE:
        return _NOISE_RE_CACHE[language]

    parts: list[str] = []
    multiline_delimiters: tuple[str, ...] = ()
    if language in _TRIPLE_QUOTE_LANGUAGES:
        multiline_delimiters += _TRIPLE_QUOTE_DELIMITERS
    multiline_delimiters += _MULTILINE_STRING_DELIMITERS.get(language, ())
    for delim in multiline_delimiters:
        escaped = re.escape(delim)
        # 閉じられていない複数行文字列は「そこから先すべて」を文字列とみなす。
        parts.append(rf"{escaped}[\s\S]*?{escaped}|{escaped}[\s\S]*")
    if language in _BLOCK_COMMENT_LANGUAGES:
        parts.append(r"/\*[\s\S]*?\*/|/\*[\s\S]*")
    for delim in _STRING_DELIMITERS.get(language, _DEFAULT_STRING_DELIMITERS):
        escaped = re.escape(delim)
        # 改行を含まない = 閉じ忘れの引用符が次行以降を巻き込まない。
        parts.append(rf"{escaped}(?:\\.|[^{escaped}\\\n])*{escaped}?")
    for prefix in _line_comment_prefixes(language):
        parts.append(re.escape(prefix) + r"[^\n]*")

    compiled = re.compile("|".join(parts)) if parts else None
    _NOISE_RE_CACHE[language] = compiled
    return compiled


def _blank_noise(match: re.Match) -> str:
    """マッチ部分を同じ長さの空白に置き換える (改行だけ残す)。

    長さと改行位置を保つことで、置換後のテキストを ``splitlines()`` しても
    元のテキストと行数・行の対応が変わらない。
    """
    return "".join("\n" if ch == "\n" else " " for ch in match.group())


def mask_comments_and_strings(text: str, language: str) -> str:
    """コメント・文字列リテラルを空白に潰したテキストを返す。

    既知の限界: ``<!-- -->`` (vue/svelte のテンプレート)、Ruby の
    ``=begin/=end``、JavaScript の正規表現リテラル中の引用符は扱わない。
    三重引用符とバッククォート (テンプレートリテラル / Go の raw string) は
    改行を跨いで潰すため、対になる閉じ記号を持たない 1 個 (正規表現リテラルの
    中に現れたバッククォート等) があるとそこから先すべてを文字列とみなす。
    いずれも「本来コードである部分まで潰す」方向の誤りに倒れるため、
    制御フロー密度は過小評価側に寄る (= 通知が減る側 = advisory hook の
    fail-open 方向)。
    """
    pattern = _noise_re(language)
    if pattern is None:
        return text
    return pattern.sub(_blank_noise, text)

# 行頭に来る import 文の形。**大文字小文字を区別する** — IGNORECASE だと
# docstring や行頭の英文 ("Use the following helper ..." / "Import the module
# ...") が import 行として数えられる。代わりに、大文字で始まるのが正規の
# 綴りである PowerShell の `Import-Module` だけ明示的に列挙する。
_IMPORT_HINT_RE = re.compile(
    r"^\s*("
    r"import\b"
    r"|from\b.*\bimport\b"
    # C# / PowerShell (using namespace ...)。``using (var conn = ...)`` /
    # ``using (Stream s = ...)`` は同じ綴りの using **文** (リソース解放
    # ブロック) で import ではないため、開き括弧が続く形を除外する
    # (マージ前レビューの指摘)。宣言側は必ず名前空間名が続く。
    r"|using\b(?!\s*\()"
    r"|use\s"  # Rust / PHP
    r"|require_relative\b|require\b"  # Ruby
    r"|Import-Module\b"  # PowerShell
    r"|#include\b"
    r")"
)

# 行頭とは限らない CommonJS の require 呼び出し
# (``const fs = require('fs')`` / ``import x = require('y')``)。
#
# メンバ呼び出し (``loader.require('x')``) を除外する — CommonJS の require は
# 常に素の識別子で、``.`` に続く同じ綴りは別物のメソッドである
# (マージ前レビューの指摘)。
_REQUIRE_CALL_RE = re.compile(r"(?<![\w$.])require\s*\(")

# ``require(`` を import として見る言語。CommonJS を持つ JS/TS 系に限る
# — 全言語に適用すると Python の ``schema.require(requests)`` のような
# 同じ綴りのメソッド呼び出しまで import 行として分類していた
# (マージ前レビューの指摘)。Ruby の ``require 'foo'`` は行頭形なので
# ``_IMPORT_HINT_RE`` 側が拾う。
_REQUIRE_CALL_LANGUAGES = frozenset(
    {
        "javascript",
        "typescript",
        "javascriptreact",
        "typescriptreact",
        "vue",
        "svelte",
    }
)

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
        # AWS SDK。S3 (ストレージ) / DynamoDB (DB) にも使われるため排他的な
        # 分類ではないが、実体はどれも HTTP API クライアントなので network に
        # 寄せる。
        "boto3",
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


def _strip_trailing_comment(text: str, language: str) -> str:
    """行コメント記号以降を落とす (import ブロックの閉じ括弧判定用の近似)。

    引用符の中の ``#`` / ``//`` も落としうるが、用途が閉じ括弧の検出に限られる
    ため、誤る方向は「ブロックを早く閉じる」= import 行を増やさない側に倒れる。
    """
    for prefix in _line_comment_prefixes(language):
        index = text.find(prefix)
        if index >= 0:
            text = text[:index]
    return text.rstrip()


def _iter_import_lines(lines: list[str], language: str):
    """import 行を列挙する (括弧付き import ブロックの継続行を含む)。

    Go の ``import ( ... )`` や Python の ``from x import ( ... )`` は、実際の
    モジュール名が継続行に書かれる。開き括弧で終わる import 行を見たら、
    対応する閉じ括弧までを import 行として扱う簡易ステートを持つ。
    閉じ括弧を見失ったときのために ``_IMPORT_BLOCK_MAX_LINES`` で打ち切る。

    閉じ括弧は**独立行 (``)``) と内容行の末尾 (``    b)``) の両方**で認識する。
    後者を見ていないと、``from x import (a, b)`` を折り返した実在の書き方で
    ブロックが閉じず、後続の最大 100 行が import 行として分類されていた
    (マージ前レビューの指摘)。内容行末尾の判定では**行末コメントを無視する**
    — ``    last_name)  # noqa`` のように閉じ括弧の後にコメントが続く形で
    ブロックが閉じないままだった (マージ前レビューの指摘)。
    """
    block_remaining = 0
    require_call_is_import = language in _REQUIRE_CALL_LANGUAGES
    for line in lines:
        stripped = line.strip()
        if block_remaining > 0:
            block_remaining -= 1
            if stripped.startswith(")"):
                block_remaining = 0
                continue
            if stripped:
                yield line
            if _strip_trailing_comment(stripped, language).endswith(")"):
                block_remaining = 0
            continue
        if _IMPORT_HINT_RE.match(line) or (
            require_call_is_import and _REQUIRE_CALL_RE.search(line)
        ):
            yield line
            if stripped.endswith("("):
                block_remaining = _IMPORT_BLOCK_MAX_LINES


def _count_import_categories(
    lines: list[str], language: str
) -> tuple[int, tuple[str, ...]]:
    import_lines = list(_iter_import_lines(lines, language))
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


def _control_flow_density(lines: list[str], language: str, text: str = "") -> float:
    """制御フローを含む行の割合。

    **分母は元テキストの非空行**のまま (コメント行も 1 行として数える)。
    **分子だけ**をコメント・文字列リテラルを潰したテキストで数える。分母から
    コメントを除くと全ファイルの密度が一斉に動くうえ、「1 行あたりどれだけ
    分岐が詰まっているか」という指標の意味が変わるため、誤検出の除去
    (分子側) に限定している。

    ``text`` を渡さない呼び出しでは行単位のマスクにフォールバックする
    (複数行文字列・ブロックコメントは潰せない)。
    """
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return 0.0

    masked_lines = lines
    if text:
        candidate = mask_comments_and_strings(text, language).splitlines()
        # マスクは長さと改行位置を保つので通常は行数が一致する。万一ずれたら
        # (改ページ文字が文字列内にある等) マスクせず元の行で数える。
        if len(candidate) == len(lines):
            masked_lines = candidate
    else:
        masked_lines = [
            mask_comments_and_strings(line, language) for line in lines
        ]

    pattern = _control_flow_re(language)
    hits = sum(
        1
        for original, masked in zip(lines, masked_lines)
        if original.strip() and pattern.search(masked)
    )
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

    category_count, category_names = _count_import_categories(lines, language)

    return Metrics(
        line_count=line_count,
        def_count=def_count,
        import_category_count=category_count,
        import_categories=category_names,
        control_flow_density=_control_flow_density(lines, language, loaded.text),
        vague_filename=is_vague_filename(path),
    )
