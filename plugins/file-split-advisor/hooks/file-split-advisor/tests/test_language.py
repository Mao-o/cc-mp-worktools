"""language.py: 言語判定・test判定・generated判定・vague filename のテスト。"""
from __future__ import annotations

import unittest
from pathlib import Path

import _testutil  # noqa: F401

import language


class TestDetectLanguage(unittest.TestCase):
    def test_known_extensions(self):
        cases = {
            "foo.py": "python",
            "foo.js": "javascript",
            "foo.mjs": "javascript",
            "foo.cjs": "javascript",
            "foo.jsx": "javascriptreact",
            "foo.ts": "typescript",
            "foo.mts": "typescript",
            "foo.cts": "typescript",
            "foo.tsx": "typescriptreact",
            "Foo.java": "java",
            "Foo.cs": "csharp",
            "Foo.kt": "kotlin",
            "Foo.kts": "kotlin",
            "foo.dart": "dart",
            "foo.go": "go",
            "foo.rs": "rust",
            "foo.rb": "ruby",
            "foo.php": "php",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(language.detect_language(Path(name)), expected)

    def test_unknown_extension_is_generic(self):
        self.assertEqual(language.detect_language(Path("foo.xyz")), "generic")
        self.assertEqual(language.detect_language(Path("Makefile")), "generic")

    def test_case_insensitive_extension(self):
        self.assertEqual(language.detect_language(Path("Foo.PY")), "python")


class TestIsTestPath(unittest.TestCase):
    def test_test_dir_markers(self):
        for dir_name in ("test", "tests", "__tests__", "spec", "specs", "e2e"):
            with self.subTest(dir_name=dir_name):
                self.assertTrue(language.is_test_path(Path(f"/repo/{dir_name}/foo.py")))

    def test_test_filename_markers(self):
        cases = [
            "test_foo.py",
            "foo_test.py",
            "foo.test.ts",
            "foo.test.tsx",
            "foo.spec.ts",
            "foo.spec.tsx",
            "FooTest.java",
            "foo_test.go",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertTrue(language.is_test_path(Path(f"/repo/src/{name}")))

    def test_extended_filename_markers(self):
        # 0.4.0 で追加したパターン。旧版はこれらをすべて normal 扱いにしており、
        # テストファイルに通常ファイルの閾値 (1/1.6 倍の厳しさ) を当てていた。
        cases = [
            "app.test.js",
            "app.spec.js",
            "app.test.jsx",
            "app.spec.mjs",
            "app.test.cjs",
            "app.spec.cts",
            "app.test.mts",
            "user_spec.rb",
            "user_test.rb",
            "FooTests.cs",
            "FooTest.cs",
            "FooTest.kt",
            "FooTest.kts",
            "FooTest.php",
            "FooTest.swift",
            "FooTest.scala",
            "FooTest.groovy",
            "foo_test.dart",
            "foo_test.rs",
            "foo_test.php",
            "foo_test.ex",
            "foo_test.exs",
            "conftest.py",
        ]
        for name in cases:
            with self.subTest(name=name):
                self.assertTrue(language.is_test_path(Path(f"/repo/src/{name}")))

    def test_lookalike_filenames_not_test(self):
        # 大文字小文字を区別しないと通常ファイル名まで test 扱いになる。
        # conftest は完全一致でないと conftest_helpers.py を巻き込む。
        for name in (
            "Latest.cs",
            "Manifest.kt",
            "greatest.ts",
            "contest.rb",
            "attest.go",
            "conftest_helpers.py",
        ):
            with self.subTest(name=name):
                self.assertFalse(language.is_test_path(Path(f"/repo/src/{name}")))

    def test_normal_path_not_test(self):
        self.assertFalse(language.is_test_path(Path("/repo/src/handler.py")))
        self.assertFalse(language.is_test_path(Path("/repo/src/service.ts")))

    def test_dir_marker_case_insensitive(self):
        self.assertTrue(language.is_test_path(Path("/repo/TESTS/foo.py")))

    def test_ancestor_dir_outside_cwd_is_ignored(self):
        # cwd の外にあるだけの祖先ディレクトリ名で test 扱いにならないこと。
        # 旧版は全祖先を見ていたため、cwd が /home/alice/test/project のとき
        # その配下の src/main.py まで test 判定になっていた。
        path = Path("/home/alice/test/project/src/main.py")
        self.assertTrue(language.is_test_path(path))  # cwd 未指定なら従来どおり
        self.assertFalse(language.is_test_path(path, "/home/alice/test/project"))

    def test_test_dir_inside_cwd_still_detected(self):
        self.assertTrue(
            language.is_test_path(
                Path("/home/alice/test/project/tests/test_x.py"),
                "/home/alice/test/project",
            )
        )

    def test_path_outside_cwd_falls_back_to_all_parts(self):
        # 相対化できないときは従来どおり全祖先を見る (フォールバック方向を
        # 「従来と同じ」に固定する)。
        self.assertTrue(
            language.is_test_path(Path("/other/tests/foo.py"), "/home/alice/project")
        )


class TestRelevantDirParts(unittest.TestCase):
    def test_relative_to_cwd(self):
        self.assertEqual(
            language.relevant_dir_parts(Path("/repo/src/api/handler.py"), "/repo"),
            ("src", "api"),
        )

    def test_empty_cwd_returns_all_parts(self):
        self.assertEqual(
            language.relevant_dir_parts(Path("/repo/src/handler.py")),
            ("/", "repo", "src"),
        )

    def test_outside_cwd_returns_all_parts(self):
        self.assertEqual(
            language.relevant_dir_parts(Path("/other/src/handler.py"), "/repo"),
            ("/", "other", "src"),
        )


class TestIsGeneratedByContent(unittest.TestCase):
    def test_markers_within_scan_window(self):
        markers = [
            "// Code generated by protoc-gen-go. DO NOT EDIT.",
            "# @generated",
            "// This file was generated by a tool",
            "// auto-generated, do not modify",
            # 0.4.0 で追加した実在の言い回し
            "// This file is automatically generated.",
            "# auto generated, do not modify",
            "// generated file - edits will be lost",
            "Revision ID: 1a2b3c4d5e6f",
        ]
        for marker in markers:
            with self.subTest(marker=marker):
                lines = ["", marker, "", "", ""]
                self.assertTrue(language.is_generated_by_content(lines))

    def test_marker_case_insensitive(self):
        self.assertTrue(language.is_generated_by_content(["DO NOT EDIT"]))

    def test_marker_after_license_header_is_detected(self):
        # 12 行のライセンスヘッダの後に生成物注記を置く生成器 (OpenAPI
        # Generator 等) は、走査幅が 5 行だと取りこぼしていた。
        lines = [f"// Copyright line {i}" for i in range(1, 13)]
        lines.append("// This file was automatically generated. Do not modify.")
        self.assertTrue(language.is_generated_by_content(lines))

    def test_marker_outside_scan_window_not_detected(self):
        # 呼び出し側が lines[:GENERATED_MARKER_SCAN_LINES] を渡す想定だが、
        # 関数自身も内部で同じ幅に絞る (二重防御)。走査幅より後ろのマーカーは、
        # 素の (未スライス) リストを渡しても検出されない。
        window = language.GENERATED_MARKER_SCAN_LINES
        lines = [str(i) for i in range(window)] + ["@generated"]
        self.assertFalse(language.is_generated_by_content(lines))

    def test_scan_window_boundary_is_inclusive(self):
        # 走査幅ちょうどの行 (1-indexed で window 行目) は検出される。
        window = language.GENERATED_MARKER_SCAN_LINES
        lines = [str(i) for i in range(window - 1)] + ["@generated"]
        self.assertTrue(language.is_generated_by_content(lines))

    def test_no_marker_returns_false(self):
        self.assertFalse(language.is_generated_by_content(["import os", "x = 1"]))


class TestIsVagueFilename(unittest.TestCase):
    def test_utils_py_is_vague(self):
        self.assertTrue(language.is_vague_filename(Path("/repo/utils.py")))

    def test_user_service_py_is_not_vague(self):
        self.assertFalse(language.is_vague_filename(Path("/repo/user_service.py")))

    def test_common_service_ts_is_vague(self):
        self.assertTrue(language.is_vague_filename(Path("/repo/CommonService.ts")))

    def test_index_ts_is_not_vague(self):
        self.assertFalse(language.is_vague_filename(Path("/repo/index.ts")))


if __name__ == "__main__":
    unittest.main()
