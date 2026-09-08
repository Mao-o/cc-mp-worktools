"""metrics.py: テキスト → 数値メトリクスのテスト。"""
from __future__ import annotations

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
            list(metrics._iter_import_lines(lines)), ["using System.Net.Http;"]
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
            list(metrics._iter_import_lines(lines)),
            ["from mypkg import (", "    alpha,", "    beta)"],
        )
        # ブロックが閉じていれば、後続の `redis.Redis()` (import ではない)
        # から db カテゴリが立つこともない。
        text = "\n".join(lines) + "\n"
        self.assertEqual(self._cats(text, "python", "foo.py"), set())

    def test_ruby_require_forms(self):
        text = "require 'net/http'\nrequire 'redis'\nrequire_relative 'auth/session'\n"
        self.assertEqual(
            self._cats(text, "ruby", "svc.rb"), {"network", "db", "auth"}
        )

    def test_powershell_import_module_is_recognized(self):
        # 大文字始まりが正規の綴りである唯一の import 形。IGNORECASE を外した
        # 分をここで明示的に補っている (カテゴリ辞書に載る語かどうかとは別)。
        lines = ["Import-Module Az.Accounts", "Import-Module Pester"]
        self.assertEqual(list(metrics._iter_import_lines(lines)), lines)

    def test_uppercase_prose_is_not_an_import_line(self):
        lines = ["Use the following helper", "Import the module first", "IF YOU NEED IT"]
        self.assertEqual(list(metrics._iter_import_lines(lines)), [])

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
        self.assertEqual(list(metrics._iter_import_lines(lines)), lines)


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
