"""metrics.py: テキスト → 数値メトリクスのテスト。"""
from __future__ import annotations

import subprocess
import sys
import unittest
import warnings
from dataclasses import dataclass
from pathlib import Path

import _testutil  # noqa: F401

import metrics


@dataclass
class _FakeLoaded:
    text: str
    lines: list[str]


def _loaded(text: str) -> _FakeLoaded:
    return _FakeLoaded(text=text, lines=text.splitlines())


class TestComputeEmptyFile(unittest.TestCase):
    def test_empty_file_no_zero_division(self):
        result = metrics.compute(_loaded(""), "python", Path("/repo/empty.py"))
        self.assertEqual(result.line_count, 0)
        self.assertEqual(result.def_count, 0)
        self.assertEqual(result.import_category_count, 0)
        self.assertEqual(result.import_categories, ())
        self.assertEqual(result.control_flow_density, 0.0)


class TestCountDefsPython(unittest.TestCase):
    def test_ast_counts_top_level_defs(self):
        text = "def a():\n    pass\n\ndef b():\n    pass\n\nclass C:\n    pass\n"
        self.assertEqual(metrics.count_defs_python(text), 3)

    def test_ast_counts_nested_methods(self):
        text = (
            "class Outer:\n"
            "    def method_a(self):\n"
            "        def inner():\n"
            "            pass\n"
            "        return inner\n"
            "\n"
            "    async def method_b(self):\n"
            "        pass\n"
        )
        # Outer(class) + method_a + inner + method_b = 4
        self.assertEqual(metrics.count_defs_python(text), 4)

    def test_syntax_error_returns_none(self):
        self.assertIsNone(metrics.count_defs_python("def a(:\n  pass"))

    def test_compute_falls_back_to_regex_on_syntax_error(self):
        text = "def a(:\n  pass\ndef b():\n  pass\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/broken.py"))
        # regex フォールバック: 行頭 "def " にマッチする行数
        self.assertEqual(result.def_count, 2)


class TestCountDefsPythonSyntaxWarnings(unittest.TestCase):
    def test_invalid_escape_sequences_do_not_leak_syntax_warning(self):
        # 無効なエスケープシーケンス ("\d" 等) を含む文字列リテラルは、
        # ast.parse() が SyntaxWarning を出す (Python 3.12+)。debounce で
        # 抑制される場合も含め毎回の判定で発生するため、stderr を汚さないよう
        # count_defs_python 内で抑制することを固定する。
        text = "\n".join(f'x{i} = "\\d+"' for i in range(50)) + "\n"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            metrics.count_defs_python(text)
        syntax_warnings = [w for w in caught if issubclass(w.category, SyntaxWarning)]
        self.assertEqual(syntax_warnings, [])

    def test_invalid_escape_sequences_still_parse_correctly(self):
        # 警告の抑制が解析結果自体に影響しないことを確認する。
        text = 'def a():\n    x = "\\d+"\n    return x\n'
        self.assertEqual(metrics.count_defs_python(text), 1)


class TestCountDefsGeneric(unittest.TestCase):
    def test_generic_keywords_counted(self):
        text = (
            "function foo() {}\n"
            "class Bar {}\n"
            "interface Baz {}\n"
            "struct Qux {}\n"
            "enum Quux {}\n"
            "func corge() {}\n"
        )
        result = metrics.compute(_loaded(text), "go", Path("/repo/foo.go"))
        self.assertEqual(result.def_count, 6)


class TestCountDefsGenericExtended(unittest.TestCase):
    """0.4.0 で追加した定義形。旧版はいずれも 0 と数えていた。"""

    def _defs(self, text: str, language: str, name: str) -> int:
        return metrics.compute(_loaded(text), language, Path(f"/repo/{name}")).def_count

    def test_es_module_forms(self):
        text = (
            "export function a() {}\n"
            "export default function b() {}\n"
            "async function c() {}\n"
            "export async function d() {}\n"
            "export class E {}\n"
            "export interface F {}\n"
            "type G = { x: number };\n"
            "export type H = G;\n"
            "declare function i(): void;\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 9)

    def test_arrow_function_assignment(self):
        text = (
            "const a = (x) => x;\n"
            "export const b = async (x) => x;\n"
            "let c = x => x;\n"
            "var d = () => {};\n"
            "const e: Handler = (req, res) => {};\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 5)

    def test_arrow_as_callback_argument_not_counted(self):
        # `=` の右辺最上位が矢印であることを要求しないと、アロー関数を引数に
        # 取る呼び出しまで定義として数えてしまう。
        text = (
            "const total = arr.reduce((acc, x) => acc + x, 0);\n"
            "const found = list.find((item) => item.id === id);\n"
            "const wrapped = wrap(() => run());\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 0)

    def test_arrow_function_with_a_return_type_annotation(self):
        # `=>` の直前に戻り型注釈があるアロー関数。TypeScript では主流の書き方
        # なのに旧版はパラメータ括弧の直後に `=>` を要求していたため 1 件も
        # 数えていなかった (マージ前レビューの指摘)。
        text = (
            "const parse = (x: Input): Output => x;\n"
            "export const load = async (id: string): Promise<Row | null> => null;\n"
            "const pick = (rows: Row[]): Record<string, number> => ({});\n"
            "const identity = <T,>(x: T): T => x;\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 4)

    def test_return_type_annotation_does_not_widen_the_arrow_rule(self):
        # 戻り型注釈を許しても、アロー関数を引数に取る呼び出し・三項演算子・
        # オブジェクトリテラルのプロパティは数えない (床テスト)。
        text = (
            "const total = arr.reduce((acc: number, x: number): number => acc + x, 0);\n"
            "const label = cond ? (a) : (b);\n"
            "const m = new Map<string, number>();\n"
            "const cb = { onDone: (x: T): void => run(x) };\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 0)

    def test_rust_and_kotlin_forms(self):
        rust = (
            "pub fn a() {}\n"
            "fn b() {}\n"
            "pub(crate) fn c() {}\n"
            "impl Trait for S {}\n"
            "trait T {}\n"
            "pub struct S {}\n"
        )
        self.assertEqual(self._defs(rust, "rust", "foo.rs"), 6)
        kotlin = "fun a() {}\nobject B {}\nabstract class C {}\n"
        self.assertEqual(self._defs(kotlin, "kotlin", "Foo.kt"), 3)

    def test_object_literal_property_names_not_counted(self):
        # `type` / `enum` / `class` はオブジェクトリテラル・インタフェースの
        # プロパティ名として頻出する。数えると宣言の少ないデータ定義ファイルで
        # def_count が水増しされる (実コーパスで観測)。
        text = (
            "export const actions = [\n"
            "  { type: 'a', enum: 1, class: 'x' },\n"
            "  { type: 'b', enum: 2, class: 'y' },\n"
            "];\n"
            "interface Shape {\n"
            "  type: string;\n"
            "  enum?: string[];\n"
            "}\n"
        )
        # `export const actions = [` はアロー関数ではないので 0、
        # `interface Shape {` のみが定義。
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 1)

    def test_keyword_used_as_identifier_not_counted(self):
        # 行頭のキーワードでも、直後が `.` / `(` / `[` なら宣言ではなく
        # 「同じ綴りの識別子」を使っているだけ (マージ前レビューの指摘)。
        text = (
            "object.keys(x);\n"
            "impl.run();\n"
            "type(x);\n"
            "fun(x);\n"
            "class[0].render();\n"
        )
        self.assertEqual(self._defs(text, "typescript", "foo.ts"), 0)

    def test_go_type_literals_still_counted(self):
        # `interface{}` / `struct{}` は `{` が続くので上の否定先読みに掛からず、
        # 従来どおり数える (床テスト)。
        text = "interface{}\nstruct{}\n"
        self.assertEqual(self._defs(text, "go", "foo.go"), 2)

    def test_original_keywords_still_counted(self):
        # 旧版が数えていた形を落としていないこと (床テスト)。
        text = (
            "function foo() {}\n"
            "class Bar {}\n"
            "interface Baz {}\n"
            "struct Qux {}\n"
            "enum Quux {}\n"
            "func corge() {}\n"
            "def grault():\n"
        )
        self.assertEqual(self._defs(text, "go", "foo.go"), 7)


class TestImportCategories(unittest.TestCase):
    def test_multiple_categories_detected(self):
        text = "\n".join(
            [
                "import requests",
                "import logging",
                "from django.contrib.auth import authenticate",
                "import react",
                "x = 1",
            ]
        )
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertEqual(result.import_category_count, 4)
        self.assertEqual(
            set(result.import_categories), {"network", "ui", "logging", "auth"}
        )

    def test_no_import_lines_zero_categories(self):
        text = "x = 1\ny = 2\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertEqual(result.import_category_count, 0)

    def test_login_does_not_false_positive_logging(self):
        text = "from django.contrib.auth import login\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertNotIn("logging", result.import_categories)
        self.assertIn("auth", result.import_categories)


class TestImportExtractionExtended(unittest.TestCase):
    """0.4.0 で追加した import 形。旧版はいずれも 0 カテゴリだった。"""

    def _cats(self, text: str, language: str, name: str) -> set[str]:
        return set(
            metrics.compute(_loaded(text), language, Path(f"/repo/{name}")).import_categories
        )

    def test_commonjs_require(self):
        text = (
            "const fs = require('fs');\n"
            "const http = require('http');\n"
            "const winston = require('winston');\n"
            "const jwt = require('jsonwebtoken');\n"
        )
        self.assertEqual(
            self._cats(text, "javascript", "svc.js"),
            {"network", "logging", "auth", "filesystem"},
        )

    def test_require_call_is_an_import_only_in_the_js_family(self):
        # 他言語の `require(` は同じ綴りの普通の関数・メソッド呼び出し。
        # 言語で絞らないと import でない行が import として分類される
        # (マージ前レビューの指摘)。素の識別子としての `require(` を含めて
        # いるのは、メンバ呼び出しの除外だけでは塞げない形 (言語で絞らないと
        # 通ってしまう形) を固定するため。
        text = "cfg = require(requests)\nschema.require(redis)\nx = 1\n"
        self.assertEqual(self._cats(text, "python", "schema.py"), set())

    def test_member_require_call_is_not_a_commonjs_import(self):
        # `loader.require('redis')` は CommonJS の require ではない
        # (マージ前レビューの指摘)。
        text = "loader.require('redis');\nconst x = 1;\n"
        self.assertEqual(self._cats(text, "javascript", "svc.js"), set())
        # 素の識別子としての require は従来どおり import 行 (床テスト)。
        self.assertEqual(
            self._cats("import fs = require('fs');\n", "typescript", "svc.ts"),
            {"filesystem"},
        )

    def test_go_import_block_continuation_lines(self):
        text = (
            "package main\n"
            "\n"
            "import (\n"
            '\t"net/http"\n'
            '\t"database/sql"\n'
            '\t"log"\n'
            ")\n"
            "\n"
            "func main() {}\n"
        )
        self.assertEqual(self._cats(text, "go", "main.go"), {"network", "db"})

    def test_python_parenthesized_import_block(self):
        text = "from mypkg import (\n    requests,\n    logging,\n)\n"
        self.assertEqual(
            self._cats(text, "python", "foo.py"), {"network", "logging"}
        )

    def test_csharp_using(self):
        text = "using System.Net.Http;\nusing Serilog.Core;\n"
        self.assertIn("network", self._cats(text, "csharp", "Svc.cs"))

    def test_csharp_using_statement_is_not_an_import(self):
        # `using (var conn = ...)` はリソース解放ブロックの using **文** で
        # import ではない (マージ前レビューの指摘)。宣言側だけを拾う。
        lines = [
            "using (var conn = new SqlConnection(cs))",
            "using (Stream s = File.OpenRead(p))",
            "using System.Net.Http;",
        ]
        self.assertEqual(
            list(metrics._iter_import_lines(lines, "csharp")),
            ["using System.Net.Http;"],
        )

    def test_cpp_using_alias_is_not_an_import(self):
        # C++ の型エイリアス `using Client = http::Client;` は import ではない
        # (マージ前レビューの指摘)。`using` の import 判定は C# に限る。
        lines = [
            "using Client = http::Client;",
            "using Store = sql::Store;",
            "using namespace std;",
            "#include <vector>",
        ]
        self.assertEqual(
            list(metrics._iter_import_lines(lines, "cpp")), ["#include <vector>"]
        )
        self.assertEqual(
            self._cats("\n".join(lines) + "\n", "cpp", "svc.cpp"), set()
        )

    def test_using_declaration_is_not_an_import(self):
        # C# 8 の `using var conn = ...` と TypeScript 5.2 の
        # `using resource = ...` は**宣言**であって import ではない
        # (マージ前レビューの指摘)。括弧付きの using 文と違い `(` が直後に
        # 来ないため、旧版はどちらも import 行として分類していた。
        lines = [
            "using var conn = Redis.Connect(cs);",
            "using resource = auth.acquire();",
            "using System.Net.Http;",
            "using static System.Math;",
            "using HttpAlias = System.Net.Http;",
        ]
        # import 形 (名前空間 / static / エイリアス) は引き続き import 行。
        self.assertEqual(
            list(metrics._iter_import_lines(lines, "csharp")), lines[2:]
        )
        # 宣言側の行から db / auth のカテゴリが立たない。
        self.assertEqual(
            self._cats("\n".join(lines) + "\n", "csharp", "Svc.cs"), {"network"}
        )
        # TypeScript のリソース宣言も同じ (言語をまたいで同じ綴りが使われる)。
        self.assertEqual(
            self._cats("using session = auth.open();\n", "typescript", "a.ts"),
            set(),
        )

    def test_import_block_closed_by_paren_on_a_content_line(self):
        # 閉じ括弧が独立行ではなく内容行の末尾にある形 (`    beta)`)。旧版は
        # ここでブロックが閉じず、後続の最大 100 行を import 行として分類して
        # いた (マージ前レビューの指摘)。
        lines = [
            "from mypkg import (",
            "    alpha,",
            "    beta)",
            "",
            "engine = sqlalchemy.create_engine(DSN)",
            "session = redis.Redis()",
        ]
        self.assertEqual(
            list(metrics._iter_import_lines(lines, "python")),
            ["from mypkg import (", "    alpha,", "    beta)"],
        )
        # ブロックが閉じていれば、後続の `redis.Redis()` (import ではない)
        # から db カテゴリが立つこともない。
        text = "\n".join(lines) + "\n"
        self.assertEqual(self._cats(text, "python", "foo.py"), set())

    def test_import_block_closed_by_paren_with_a_trailing_comment(self):
        # 閉じ括弧の後に行末コメントがある形 (`    beta)  # noqa`)。旧版は
        # `endswith(")")` を満たさずブロックが閉じないまま、後続の最大 100 行を
        # import 行として分類していた (マージ前レビューの指摘)。
        lines = [
            "from mypkg import (",
            "    alpha,",
            "    beta)  # noqa: F401",
            "",
            "session = redis.Redis()",
        ]
        self.assertEqual(
            self._cats("\n".join(lines) + "\n", "python", "foo.py"), set()
        )
        # 行コメント記号は言語ごとに違う (Go は `//`)。
        go_lines = [
            "import (",
            '\t"log")  // 1 パッケージだけ残した',
            "",
            "var conn = redis.NewClient()",
        ]
        self.assertEqual(
            self._cats("\n".join(go_lines) + "\n", "go", "main.go"), set()
        )

    def test_import_block_opened_by_paren_with_a_trailing_comment(self):
        # 開き括弧の後に行末コメントがある形 (`from deps import (  # grouped`)。
        # 旧版は `endswith("(")` を満たさず継続行をまったく走査せず、ブロック
        # 形式の import からモジュール名が 1 件も分類されなかった
        # (マージ前レビューの指摘)。
        lines = [
            "from deps import (  # grouped by layer",
            "    requests,",
            "    redis,",
            ")",
            "x = 1",
        ]
        self.assertEqual(
            list(metrics._iter_import_lines(lines, "python")), lines[:3]
        )
        self.assertEqual(
            self._cats("\n".join(lines) + "\n", "python", "foo.py"),
            {"network", "db"},
        )
        # 行コメント記号は言語ごとに違う (Go は `//`)。
        go_lines = [
            "import (  // グループ分け",
            '\t"net/http"',
            '\t"database/sql"',
            ")",
            "func main() {}",
        ]
        self.assertEqual(
            self._cats("\n".join(go_lines) + "\n", "go", "main.go"),
            {"network", "db"},
        )

    def test_ruby_require_forms(self):
        text = "require 'net/http'\nrequire 'redis'\nrequire_relative 'auth/session'\n"
        self.assertEqual(
            self._cats(text, "ruby", "svc.rb"), {"network", "db", "auth"}
        )

    def test_powershell_import_module_is_recognized(self):
        # 大文字始まりが正規の綴りである唯一の import 形。IGNORECASE を外した
        # 分をここで明示的に補っている (カテゴリ辞書に載る語かどうかとは別)。
        lines = ["Import-Module Az.Accounts", "Import-Module Pester"]
        self.assertEqual(list(metrics._iter_import_lines(lines, "powershell")), lines)

    def test_uppercase_prose_is_not_an_import_line(self):
        lines = ["Use the following helper", "Import the module first", "IF YOU NEED IT"]
        self.assertEqual(list(metrics._iter_import_lines(lines, "python")), [])

    def test_modern_python_http_clients(self):
        # 1 語 1 ケース。まとめて書くと 1 語でも network に載っていれば通って
        # しまい、残りの語が辞書から落ちても検出できない。
        for module in ("httpx", "aiohttp", "websockets", "urllib3", "boto3"):
            with self.subTest(module=module):
                text = f"import {module}\n"
                self.assertIn("network", self._cats(text, "python", "svc.py"))

    def test_node_fs_specifiers(self):
        for specifier in ("node:fs", "fs/promises"):
            with self.subTest(specifier=specifier):
                text = f"import fs from '{specifier}';\n"
                self.assertIn(
                    "filesystem", self._cats(text, "typescript", "svc.ts")
                )

    def test_prose_lines_not_treated_as_imports(self):
        # 旧版は IGNORECASE だったため、docstring の英文が import 行として
        # 数えられていた。
        text = (
            '"""Module docs.\n'
            "\n"
            "Use the following helper when you need it.\n"
            "Import the module before calling it.\n"
            '"""\n'
            "x = 1\n"
        )
        self.assertEqual(self._cats(text, "python", "foo.py"), set())

    def test_unterminated_import_block_is_bounded(self):
        # 閉じ括弧を見失っても、ファイル全体を import 行として扱わない。
        # 打ち切り幅より後ろにある `requests` は import 行として拾われない。
        # 行位置は定数から導出せず固定する (定数を緩める mutation を検出できる
        # ようにするため)。
        self.assertLess(metrics._IMPORT_BLOCK_MAX_LINES, 400)
        body = [f"\tline{i}" for i in range(500)]
        body[400] = "\trequests,"
        text = "import (\n" + "\n".join(body) + "\n"
        result = metrics.compute(_loaded(text), "go", Path("/repo/main.go"))
        self.assertEqual(result.import_categories, ())

    def test_import_block_within_bound_is_scanned(self):
        # 打ち切り幅の内側は継続行として拾う (床テスト。上の境界テストが
        # 「そもそも継続行を読んでいない」ことで通ってしまうのを防ぐ)。
        text = "import (\n\trequests,\n)\n"
        result = metrics.compute(_loaded(text), "go", Path("/repo/main.go"))
        self.assertEqual(result.import_categories, ("network",))

    def test_original_import_forms_still_recognized(self):
        # 旧版が import 行として拾えていた形を落としていないこと (床テスト)。
        # カテゴリ辞書に載る語かどうかとは独立に、行の認識だけを固定する。
        lines = [
            "import requests",
            "from django.contrib.auth import authenticate",
            "use std::fs;",
            "#include <stdio.h>",
            "require('fs')",
        ]
        self.assertEqual(list(metrics._iter_import_lines(lines, "javascript")), lines)


class TestMaskCommentsAndStrings(unittest.TestCase):
    def test_python_line_comment_and_string(self):
        masked = metrics.mask_comments_and_strings(
            "x = 'for example'  # if you need this\n", "python"
        )
        self.assertNotIn("for", masked)
        self.assertNotIn("if", masked)
        self.assertIn("x =", masked)

    def test_python_triple_quoted_block(self):
        text = '"""\nif for while\n"""\nx = 1\n'
        masked = metrics.mask_comments_and_strings(text, "python")
        self.assertNotIn("while", masked)
        self.assertIn("x = 1", masked)

    def test_c_family_block_comment(self):
        text = "/*\n * if for while\n */\nconst a = 1;\n"
        masked = metrics.mask_comments_and_strings(text, "javascript")
        self.assertNotIn("while", masked)
        self.assertIn("const a = 1;", masked)

    def test_js_template_literal_spans_lines(self):
        # テンプレートリテラルは改行を跨ぐ。行内で閉じる前提のパターンだと
        # マスクが最初の改行で止まり、本文の散文が制御フローとして残る
        # (マージ前レビューの指摘)。
        text = (
            "const doc = `\n"
            "if the value is missing, ask again\n"
            "for each row, switch to the next page\n"
            "`;\n"
            "export const a = 1;\n"
        )
        masked = metrics.mask_comments_and_strings(text, "javascript")
        self.assertNotIn("switch", masked)
        self.assertNotIn("for each", masked)
        self.assertIn("export const a = 1;", masked)
        # 行数は保つ (`_control_flow_density` の安全弁を無駄撃ちさせない)。
        self.assertEqual(len(masked.splitlines()), len(text.splitlines()))

    def test_escaped_backtick_does_not_close_a_template_literal(self):
        # ``\` `` はテンプレートリテラルの中のエスケープされたバッククォート。
        # 閉じ区切りと誤認すると、本当の閉じ記号が「新しい未終端文字列の開始」
        # になり、以降のコードが**全部**マスクされる (マージ前レビューの指摘)。
        text = (
            "const s = `use \\` here`;\n"
            "if (rows.length) {\n"
            "  for (const row of rows) switch (row.kind) {}\n"
            "}\n"
        )
        masked = metrics.mask_comments_and_strings(text, "javascript")
        self.assertNotIn("here", masked)  # 文字列本体は潰れている
        self.assertIn("if (rows.length)", masked)  # 閉じた後の実コードは残る
        self.assertIn("switch", masked)
        self.assertEqual(len(masked.splitlines()), len(text.splitlines()))

    def test_unterminated_multiline_string_with_backslashes_is_linear(self):
        # エスケープ交替 (`\\.`) と通常文字の交替が同じ位置 (`\`) で開始できると、
        # 閉じ記号を持たない長い文字列で組み合わせが指数爆発し、`re.sub` が
        # 事実上停止する (minify 済み bundle で実測)。hook は毎回の Write/Edit
        # で走るため、この停止はそのまま編集のハングになる。
        #
        # 別プロセスに時間予算を与えて固定する — 線形なら 1 ミリ秒未満、指数なら
        # 返らないので、タイムアウトが「壊れた」を意味する。
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import metrics\n"
            "text = 'const r = `' + 'a\\\\b' * 400\n"
            "metrics.mask_comments_and_strings(text, 'javascript')\n"
            "print('ok')\n" % str(Path(metrics.__file__).parent)
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_url_inside_string_is_not_a_comment(self):
        masked = metrics.mask_comments_and_strings(
            'const u = "http://x"; if (u) {}\n', "javascript"
        )
        self.assertIn("if (u)", masked)

    def test_line_count_is_preserved(self):
        text = '"""\na\nb\n"""\nx = 1\n'
        masked = metrics.mask_comments_and_strings(text, "python")
        self.assertEqual(len(masked.splitlines()), len(text.splitlines()))

    def test_c_slash_line_comment_is_masked(self):
        # C は `#` を行コメントにしない代わりに `//` を持つ。片方だけ設定して
        # 他方を落とすと、C のコメント中の英単語が制御フローとして残る。
        masked = metrics.mask_comments_and_strings("// if\nint a;\n", "c")
        self.assertNotIn("if", masked)
        self.assertIn("int a;", masked)

    def test_hash_is_not_a_comment_in_c(self):
        # C 系で `#` を行コメント扱いすると `#if` / `#include` が消える。
        masked = metrics.mask_comments_and_strings("#if defined(X)\n", "c")
        self.assertIn("#if", masked)

    def test_rust_lifetime_is_not_a_string(self):
        # rust で `'` を文字列開始として扱うと、閉じ引用符を持たない
        # ライフタイム注釈 (`&'static`) が行末までを飲み込んで制御フローが消える。
        masked = metrics.mask_comments_and_strings(
            'let x: &\'static str = "y"; if x.is_empty() {}\n', "rust"
        )
        self.assertIn("if", masked)
        self.assertNotIn('"y"', masked)


class TestControlFlowDensity(unittest.TestCase):
    def test_known_ratio(self):
        text = "\n".join(
            [
                "if x:",
                "    pass",
                "for y in z:",
                "    pass",
                "a = 1",
                "b = 2",
            ]
        )
        # 6 non-empty lines, 2 with control-flow keywords (if/for)
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        self.assertAlmostEqual(result.control_flow_density, 2 / 6)

    def test_blank_lines_excluded_from_denominator(self):
        text = "if x:\n\n\n    pass\n"
        result = metrics.compute(_loaded(text), "python", Path("/repo/foo.py"))
        # non-empty lines: "if x:" と "    pass" の 2 行、うち 1 行が control-flow
        self.assertAlmostEqual(result.control_flow_density, 1 / 2)


class TestControlFlowLanguageKeywords(unittest.TestCase):
    """0.4.0: 言語固有の分岐・繰り返し・例外構文を数える (旧版はいずれも 0)。"""

    def _density(self, text: str, language: str, name: str) -> float:
        return metrics.compute(
            _loaded(text), language, Path(f"/repo/{name}")
        ).control_flow_density

    def test_python_elif_try_match(self):
        text = "if a:\n    pass\nelif b:\n    pass\ntry:\n    pass\nmatch c:\n    pass\n"
        # 8 行中 4 行 (if / elif / try / match)
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 4 / 8)

    def test_python_re_match_call_is_not_control_flow(self):
        # `match` は soft keyword。行内一致にすると re.match を使うだけの
        # ファイルが高密度に見える。
        text = "import re\nm = re.match(P, s)\nn = p.match(s)\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_python_match_as_a_variable_name_is_not_control_flow(self):
        # 文頭でも `match = ...` (代入) / `match(...)` (呼び出し) /
        # `match.group(0)` / `match[0]` は soft keyword を変数名として使って
        # いるだけで制御フローではない (マージ前レビューの指摘)。
        text = "match = re.match(P, s)\nmatch(x)\nmatch.group(0)\nmatch[0]\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_python_match_statement_with_parenthesized_subject(self):
        # `match (value):` (subject を括弧で囲む) / `match (a, b):`
        # (tuple subject) は正当な match 文。`(` を無条件に弾くと分子から
        # 漏れる (マージ前レビューの指摘)。行末コメントはマスク後に空白に
        # なるため `:` 終端の判定に影響しない。
        for subject in (
            "match (value):",
            "match (a, b):",
            "match(value):",
            "match (value):  # 分岐",
        ):
            with self.subTest(subject=subject):
                text = subject + "\n    pass\n"
                self.assertAlmostEqual(self._density(text, "python", "foo.py"), 1 / 2)
        # 引き続き数えない形 (床テスト): 代入・呼び出し・属性/添字アクセス。
        text = "match = re.match(P, s)\nmatch(x)\nmatch.group(0)\nmatch[0]\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_ruby_keywords(self):
        text = "x = 1 unless y\nuntil done\nend\nbegin\nrescue => e\nend\nelsif z\n"
        # unless / until / rescue / elsif の 4 行
        self.assertAlmostEqual(self._density(text, "ruby", "foo.rb"), 4 / 7)

    def test_ruby_case_when(self):
        # `case` だけ数えて `when` を落とすと、Ruby の case 式の分岐本数が
        # 密度に現れない (マージ前レビューの指摘)。
        text = "case x\nwhen 1\nwhen 2\nend\n"
        self.assertAlmostEqual(self._density(text, "ruby", "foo.rb"), 3 / 4)

    def test_rust_match_and_loop(self):
        text = "let v = match x {\n};\nloop {\n}\n"
        self.assertAlmostEqual(self._density(text, "rust", "foo.rs"), 2 / 4)

    def test_kotlin_when(self):
        text = "val r = when (x) {\n}\n"
        self.assertAlmostEqual(self._density(text, "kotlin", "Foo.kt"), 1 / 2)

    def test_go_select(self):
        text = "select {\ncase <-ch:\n}\n"
        self.assertAlmostEqual(self._density(text, "go", "foo.go"), 2 / 3)

    def test_language_keywords_do_not_leak_across_languages(self):
        # JavaScript の `str.match(...)` / `select` を制御フローとして
        # 数えないこと (言語別集合にしている理由)。
        text = "const m = s.match(re);\nconst q = select(state);\nconst w = when(x);\n"
        self.assertAlmostEqual(self._density(text, "javascript", "foo.js"), 0.0)

    def test_base_keywords_still_counted_for_unknown_language(self):
        # 未登録言語でも基本集合は従来どおり数える (床テスト)。
        text = "if (x) {\nfor (;;) {\nswitch (y) {\nz = 1\n"
        self.assertAlmostEqual(self._density(text, "generic", "foo.xyz"), 3 / 4)


class TestControlFlowExcludesCommentsAndStrings(unittest.TestCase):
    def _density(self, text: str, language: str, name: str) -> float:
        return metrics.compute(
            _loaded(text), language, Path(f"/repo/{name}")
        ).control_flow_density

    def test_python_comment_keywords_not_counted(self):
        text = "# if you need this, for each item\nx = 1\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_python_string_keywords_not_counted(self):
        text = "MSG = 'for example, if this then switch'\ny = 2\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_python_docstring_prose_not_counted(self):
        text = '"""\nif you need this, while working, for each item\n"""\nx = 1\n'
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 0.0)

    def test_js_block_comment_not_counted(self):
        text = "/*\n * if for while switch\n */\nconst a = 1;\n"
        self.assertAlmostEqual(self._density(text, "javascript", "foo.js"), 0.0)

    def test_go_raw_string_spans_lines(self):
        # Go の raw string (バッククォート) も改行を跨ぐ (マージ前レビューの指摘)。
        text = (
            "const q = `\n"
            "if the row is missing\n"
            "for each column\n"
            "`\n"
            "x := 1\n"
        )
        self.assertAlmostEqual(self._density(text, "go", "foo.go"), 0.0)

    def test_code_after_an_escaped_backtick_is_still_counted(self):
        # エスケープされたバッククォートを閉じ区切りと誤認すると、以降の行が
        # まるごと文字列扱いになり制御フローが 0 になる (マージ前レビューの
        # 指摘)。密度で固定する。
        text = (
            "const s = `use \\` here`;\n"
            "if (a) {\n"
            "  for (const x of a) {}\n"
            "}\n"
        )
        self.assertAlmostEqual(self._density(text, "javascript", "foo.js"), 0.5)

    def test_comment_lines_stay_in_denominator(self):
        # 分子だけをマスクし、分母は元テキストの非空行のまま。
        text = "# if you need this\nif x:\n    pass\n"
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 1 / 3)

    def test_code_after_inline_comment_marker_in_string_is_kept(self):
        text = 'URL = "http://x/#frag"\nif URL:\n    pass\n'
        self.assertAlmostEqual(self._density(text, "python", "foo.py"), 1 / 3)

    def test_c_preprocessor_conditional_still_counted(self):
        # C 系で `#` を行コメント扱いすると `#if` が消える。
        text = "#if defined(X)\nint a;\n#endif\n"
        self.assertAlmostEqual(self._density(text, "c", "foo.c"), 1 / 3)

    def test_without_text_falls_back_to_per_line_masking(self):
        # 全文を渡さない呼び出しでは行単位のマスクになる。行コメントは消えるが、
        # 複数行文字列は行をまたぐため消えない (既知の限界)。
        lines = ["# if you need this", "if x:", "    pass"]
        self.assertAlmostEqual(
            metrics._control_flow_density(lines, "python", ""), 1 / 3
        )
        docstring_lines = ['"""', "if you need this", '"""', "x = 1"]
        self.assertAlmostEqual(
            metrics._control_flow_density(docstring_lines, "python", ""), 1 / 4
        )
        # 全文を渡せば複数行文字列も潰れる。
        self.assertAlmostEqual(
            metrics._control_flow_density(
                docstring_lines, "python", "\n".join(docstring_lines) + "\n"
            ),
            0.0,
        )

    def test_masking_is_skipped_when_the_line_count_shifts(self):
        # マスクは改行以外を同じ長さの空白に置き換えるため、`\x0c` (改ページ)
        # のように `splitlines()` が行区切りとして扱う文字が文字列リテラルの
        # 中にあると、マスク後だけ行数が減る。この安全弁 (行数がずれたら
        # マスクせず元の行で数える) を固定する。
        text = 'MSG = "if you need\x0c for each"\nif x:\n    pass\n'
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            len(metrics.mask_comments_and_strings(text, "python").splitlines()), 3
        )
        # マスクが効いていれば `if x:` の 1 行だけ (1/4)。安全弁が働いて
        # 元の行で数えるため、文字列の中の if / for も数えられて 3/4 になる。
        self.assertAlmostEqual(
            metrics._control_flow_density(lines, "python", text), 3 / 4
        )


if __name__ == "__main__":
    unittest.main()
