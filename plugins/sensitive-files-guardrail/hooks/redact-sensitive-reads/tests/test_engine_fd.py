"""engine.redact / redact_large_file が fd (file-like) 経由で動くことを確認。

Step 2 の fd ベース移行で path 再 open を排除したことの回帰テスト。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from core.safepath import open_regular
from redaction.engine import MAX_INLINE_BYTES, redact, redact_large_file


class TestRedactWithFd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_small_dotenv_via_fd(self):
        p = Path(self.tmp) / ".env"
        p.write_bytes(b"FOO=bar\nBAZ=qux\n")
        fd, size = open_regular(p)
        with os.fdopen(fd, "rb") as f:
            reason = redact(f, ".env", size)
        self.assertIn("format: dotenv", reason)
        self.assertIn("FOO", reason)
        self.assertIn("BAZ", reason)

    def test_small_dotenv_via_bytesio(self):
        data = b"FOO=bar\n"
        reason = redact(BytesIO(data), ".env", len(data))
        self.assertIn("format: dotenv", reason)
        self.assertIn("FOO", reason)

    def test_large_file_keyonly_via_fd(self):
        p = Path(self.tmp) / ".env"
        lines = [f"KEY_{i}=value_{i}\n".encode() for i in range(3000)]
        p.write_bytes(b"".join(lines))
        self.assertGreater(p.stat().st_size, MAX_INLINE_BYTES)
        fd, _size = open_regular(p)
        with os.fdopen(fd, "rb") as f:
            reason = redact_large_file(f, ".env")
        self.assertIn("keys-only scan", reason)
        # 値は漏れない
        self.assertNotIn("value_0", reason)
        # 鍵名は拾っている
        self.assertIn("KEY_0", reason)

    def test_engine_seek_from_middle(self):
        """engine は seek(0) してから読むので、先頭以外の position で渡しても先頭から読める。"""
        data = b"FOO=bar\nBAZ=qux\n"
        buf = BytesIO(data)
        buf.seek(5)
        reason = redact(buf, ".env", len(data))
        self.assertIn("FOO", reason)
        self.assertIn("BAZ", reason)


if __name__ == "__main__":
    unittest.main()
