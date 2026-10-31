"""`cache._write_atomic` は書き込み・置き換えに失敗しても tmp ファイルを残さない。"""
from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import cache  # noqa: E402


class TestWriteAtomicCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "state.json"

    def _leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir())

    def test_write_failure_leaves_no_tmp(self):
        """tmp への書き込みが途中で失敗 (ENOSPC) しても、書きかけの tmp を残さない。"""
        real = Path.write_text

        def partial(path, text, *args, **kwargs):
            real(path, text[:1], *args, **kwargs)  # 書きかけが残る
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(Path, "write_text", partial):
            with self.assertRaises(OSError):
                cache._write_atomic(self.target, "abcdef")
        self.assertEqual(self._leftovers(), [])

    def test_encoding_failure_leaves_no_tmp(self):
        """UTF-8 にできない文字 (孤立サロゲート) で失敗しても残さない。"""
        with self.assertRaises(UnicodeEncodeError):
            cache._write_atomic(self.target, "\ud800")
        self.assertEqual(self._leftovers(), [])

    def test_replace_failure_leaves_no_tmp(self):
        with mock.patch.object(os, "replace", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(OSError):
                cache._write_atomic(self.target, "abc")
        self.assertEqual(self._leftovers(), [])

    def test_cleanup_failure_does_not_hide_the_original_error(self):
        with mock.patch.object(os, "replace", side_effect=OSError(errno.EACCES, "denied")):
            with mock.patch.object(Path, "unlink", side_effect=PermissionError):
                with self.assertRaises(OSError) as ctx:
                    cache._write_atomic(self.target, "abc")
        self.assertEqual(ctx.exception.errno, errno.EACCES)

    def test_success_leaves_only_the_target(self):
        cache._write_atomic(self.target, "abc")
        self.assertEqual(self._leftovers(), ["state.json"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "abc")


if __name__ == "__main__":
    unittest.main()
