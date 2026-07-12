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
