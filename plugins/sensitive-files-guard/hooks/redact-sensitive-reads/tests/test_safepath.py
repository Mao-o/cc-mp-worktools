"""safepath.classify / open_regular / is_regular_directory のテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from core.safepath import classify, is_regular_directory, normalize, open_regular


class TestNormalize(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(
            normalize("foo.txt", "/tmp"),
            Path("/tmp/foo.txt"),
        )

    def test_absolute(self):
        self.assertEqual(
            normalize("/etc/hosts", "/tmp"),
            Path("/etc/hosts"),
        )

    def test_dotdot(self):
        self.assertEqual(
            normalize("../other/foo", "/tmp/sub"),
            Path("/tmp/other/foo"),
        )


class _BaseTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestClassify(_BaseTmp):
    def test_regular(self):
        p = Path(self.tmp) / "plain.txt"
        p.write_text("hi")
        self.assertEqual(classify(p), "regular")

    def test_missing(self):
        p = Path(self.tmp) / "nope.txt"
        self.assertEqual(classify(p), "missing")

    def test_symlink(self):
        target = Path(self.tmp) / "target.txt"
        target.write_text("hi")
        link = Path(self.tmp) / "link.txt"
        os.symlink(target, link)
        self.assertEqual(classify(link), "symlink")

    def test_broken_symlink(self):
        link = Path(self.tmp) / "broken.txt"
        os.symlink(Path(self.tmp) / "nonexistent", link)
        self.assertEqual(classify(link), "symlink")

    def test_fifo(self):
        fifo = Path(self.tmp) / "pipe.fifo"
        os.mkfifo(fifo)
        self.assertEqual(classify(fifo), "special")


class TestOpenRegular(_BaseTmp):
    """fd ベースの open_regular。fstat 再確認 + O_NOFOLLOW 前提。"""

    def test_small_file_returns_fd(self):
        p = Path(self.tmp) / "small.txt"
        payload = b"hello world"
        p.write_bytes(payload)
        fd, total = open_regular(p)
        try:
            self.assertIsInstance(fd, int)
            self.assertEqual(total, len(payload))
            # fd から読み出せる
            data = os.read(fd, 64)
            self.assertEqual(data, payload)
        finally:
            os.close(fd)

    def test_large_file_size_reported(self):
        p = Path(self.tmp) / "large.txt"
        payload = b"x" * 5000
        p.write_bytes(payload)
        fd, total = open_regular(p)
        try:
            self.assertEqual(total, 5000)
        finally:
            os.close(fd)


class TestOpenRegularTOCTOU(_BaseTmp):
    """symlink を事前に置くと O_NOFOLLOW で ELOOP (UNIX のみ決定的)。

    FIFO/socket/device は ``classify`` が ``special`` で先に止めるため、
    ``open_regular`` は通常ファイル以外では呼ばれない前提。ここでは symlink 経路
    だけ TOCTOU 緩和を確認する (fifo の直接 open はブロックするため省略)。
    """

    def test_symlink_raises(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available on this platform")
        target = Path(self.tmp) / "target.txt"
        target.write_text("hi")
        link = Path(self.tmp) / "link.txt"
        os.symlink(target, link)
        with self.assertRaises(OSError):
            open_regular(link)


class TestOpenRegularFdOwnership(_BaseTmp):
    """fd の close は with os.fdopen(fd) に委譲されるべき。"""

    def test_fd_closed_after_fdopen_with_exits(self):
        p = Path(self.tmp) / "small.txt"
        p.write_bytes(b"hi")
        fd, _size = open_regular(p)
        with os.fdopen(fd, "rb") as f:
            f.read()
        # with 終了後、fd は閉じられているはず → fstat が失敗する
        with self.assertRaises(OSError):
            os.fstat(fd)


class TestIsRegularDirectory(_BaseTmp):
    """Edit/Write 用の親ディレクトリ判定。Step 6 で使う。"""

    def test_regular_directory(self):
        d = Path(self.tmp) / "subdir"
        d.mkdir()
        self.assertTrue(is_regular_directory(d))

    def test_missing_returns_false(self):
        self.assertFalse(is_regular_directory(Path(self.tmp) / "nope"))

    def test_symlink_to_directory_returns_false(self):
        target = Path(self.tmp) / "real"
        target.mkdir()
        link = Path(self.tmp) / "link"
        os.symlink(target, link)
        self.assertFalse(is_regular_directory(link))

    def test_file_returns_false(self):
        p = Path(self.tmp) / "file.txt"
        p.write_text("x")
        self.assertFalse(is_regular_directory(p))


if __name__ == "__main__":
    unittest.main()
