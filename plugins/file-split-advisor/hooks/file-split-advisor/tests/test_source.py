"""source.py: パス解決・早期 skip・安全な読み込みのテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import source


class TestResolvePath(unittest.TestCase):
    def test_absolute_path_returned_as_is(self):
        result = source.resolve_path("/abs/path/foo.py", "/some/cwd")
        self.assertEqual(result, Path("/abs/path/foo.py"))

    def test_relative_path_joined_with_cwd(self):
        result = source.resolve_path("foo.py", "/some/cwd")
        self.assertEqual(result, Path("/some/cwd/foo.py"))

    def test_relative_path_without_cwd(self):
        result = source.resolve_path("foo.py", "")
        self.assertEqual(result, Path("foo.py"))


class TestIsUnderTempDir(unittest.TestCase):
    def test_slash_tmp_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/tmp/foo.py")))

    def test_private_tmp_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/private/tmp/scratchpad/foo.py")))

    def test_var_folders_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/var/folders/xx/yyyy/T/foo.py")))

    def test_tmpdir_env_root_is_temp(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "/custom/tmp-root"}):
            self.assertTrue(source.is_under_temp_dir(Path("/custom/tmp-root/sub/foo.py")))

    def test_project_path_is_not_temp(self):
        self.assertFalse(source.is_under_temp_dir(Path("/repo/src/foo.py")))

    def test_similar_prefix_is_not_falsely_matched(self):
        # "/tmpfoo" は "/tmp" 配下ではない (前方一致ではなくパスセグメント単位で
        # 判定するため誤検知しない)。
        self.assertFalse(source.is_under_temp_dir(Path("/tmpfoo/bar.py")))


class TestShouldSkipTempDir(unittest.TestCase):
    def test_temp_path_skipped_when_cwd_is_real_project(self):
        self.assertTrue(
            source.should_skip_temp_dir(Path("/private/tmp/scratch/foo.py"), "/repo")
        )

    def test_temp_path_not_skipped_when_path_is_inside_temp_cwd(self):
        # session 全体が一時領域内 (ephemeral project) で、path がその cwd の
        # 内側にあるときは skip しない。
        self.assertFalse(
            source.should_skip_temp_dir(
                Path("/private/tmp/proj/foo.py"), "/private/tmp/proj"
            )
        )

    def test_sibling_temp_dir_outside_cwd_is_still_skipped(self):
        # cwd 自体は一時領域配下でも、path が cwd の外にある別の一時ディレクトリ
        # (兄弟プロジェクト) なら skip する。「cwd が一時領域かどうか」ではなく
        # 「path が cwd の内側かどうか」で判定する。
        self.assertTrue(
            source.should_skip_temp_dir(
                Path("/private/tmp/projB/foo.py"), "/private/tmp/projA"
            )
        )

    def test_non_temp_path_never_skipped(self):
        self.assertFalse(source.should_skip_temp_dir(Path("/repo/src/foo.py"), "/repo"))

    def test_empty_cwd_does_not_prevent_skip(self):
        self.assertTrue(source.should_skip_temp_dir(Path("/tmp/foo.py"), ""))


class TestIsOutsideCwd(unittest.TestCase):
    def test_path_inside_cwd_is_not_outside(self):
        self.assertFalse(source.is_outside_cwd(Path("/repo/src/foo.py"), "/repo"))

    def test_path_outside_cwd_is_outside(self):
        self.assertTrue(source.is_outside_cwd(Path("/other/foo.py"), "/repo"))

    def test_empty_cwd_never_outside(self):
        self.assertFalse(source.is_outside_cwd(Path("/other/foo.py"), ""))


class TestParseIgnoreGlobs(unittest.TestCase):
    def test_skips_blank_and_comment_lines(self):
        text = "\n".join(["# comment", "", "*.generated.py", "  ", "migrations/*"])
        self.assertEqual(
            source._parse_ignore_globs(text), ("*.generated.py", "migrations/*")
        )

    def test_empty_text_yields_no_patterns(self):
        self.assertEqual(source._parse_ignore_globs(""), ())

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(source._parse_ignore_globs("  test_*.py  \n"), ("test_*.py",))


class TestLoadIgnoreGlobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ignore_file(self, content: str) -> Path:
        path = Path(self.tmp) / "ignore.local.txt"
        path.write_text(content)
        return path

    def test_merges_env_and_file(self):
        ignore_file = self._write_ignore_file("legacy/*\n# comment\n")
        patterns = source.load_ignore_globs("*.min.py, foo_*.py", ignore_file)
        self.assertEqual(patterns, ("*.min.py", "foo_*.py", "legacy/*"))

    def test_missing_file_is_ignored(self):
        patterns = source.load_ignore_globs("*.foo", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ("*.foo",))

    def test_empty_env_value_and_no_file(self):
        patterns = source.load_ignore_globs("", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ())

    def test_env_value_with_only_whitespace_entries_ignored(self):
        patterns = source.load_ignore_globs(" , ,", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ())

    def test_default_ignore_file_path(self):
        expected = Path.home() / ".claude" / "file-split-advisor" / "ignore.local.txt"
        self.assertEqual(source._default_ignore_file(), expected)


class TestMatchesIgnoreGlob(unittest.TestCase):
    def test_matches_by_filename(self):
        self.assertTrue(source.matches_ignore_glob(Path("/repo/test_foo.py"), ("test_*.py",)))

    def test_matches_by_full_path(self):
        self.assertTrue(
            source.matches_ignore_glob(
                Path("/repo/migrations/0001_init.py"), ("*/migrations/*",)
            )
        )

    def test_no_match(self):
        self.assertFalse(
            source.matches_ignore_glob(Path("/repo/handler.py"), ("test_*.py",))
        )

    def test_empty_patterns_never_match(self):
        self.assertFalse(source.matches_ignore_glob(Path("/repo/handler.py"), ()))


class TestShouldSkipByName(unittest.TestCase):
    def test_lockfiles_skipped(self):
        for name in (
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Cargo.lock",
            "Pipfile.lock",
            "poetry.lock",
            "go.sum",
            "composer.lock",
        ):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_minified_skipped(self):
        for name in ("app.min.js", "app.min.css", "app.js.map"):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_generated_patterns_skipped(self):
        for name in (
            "foo.pb.go",
            "foo_pb2.py",
            "foo_pb2_grpc.py",
            "foo.g.dart",
            "foo.freezed.dart",
            "foo_generated.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_normal_file_not_skipped(self):
        self.assertFalse(source.should_skip_by_name(Path("/repo/handler.py")))
        self.assertFalse(source.should_skip_by_name(Path("/repo/lockpicking.py")))


class TestLoadText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> Path:
        path = Path(self.tmp) / name
        path.write_text(content)
        return path

    def test_normal_file_loaded(self):
        path = self._write("foo.py", "a = 1\nb = 2\n")
        loaded = source.load_text(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.lines, ["a = 1", "b = 2"])
        self.assertEqual(loaded.text, "a = 1\nb = 2\n")

    def test_missing_file_returns_none(self):
        path = Path(self.tmp) / "does-not-exist.py"
        self.assertIsNone(source.load_text(path))

    def test_oversized_bytes_not_read(self):
        path = self._write("big.py", "x = 1\n")
        with mock.patch.object(Path, "read_text") as mock_read:
            loaded = source.load_text(path, max_bytes=1)
            self.assertIsNone(loaded)
            mock_read.assert_not_called()

    def test_oversized_line_count_returns_none(self):
        path = self._write("many-lines.py", "\n".join(f"x{i} = {i}" for i in range(50)))
        loaded = source.load_text(path, max_lines=10)
        self.assertIsNone(loaded)

    def test_within_limits_loaded(self):
        path = self._write("ok.py", "\n".join(f"x{i} = {i}" for i in range(10)))
        loaded = source.load_text(path, max_bytes=10_000, max_lines=10)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.lines), 10)

    def test_symlink_skipped(self):
        target = self._write("target.py", "a = 1\n")
        link = Path(self.tmp) / "link.py"
        os.symlink(target, link)
        self.assertIsNone(source.load_text(link))

    def test_directory_skipped(self):
        directory = Path(self.tmp) / "adir"
        directory.mkdir()
        self.assertIsNone(source.load_text(directory))

    def test_oserror_on_read_returns_none(self):
        path = self._write("foo.py", "a = 1\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            self.assertIsNone(source.load_text(path))


if __name__ == "__main__":
    unittest.main()
