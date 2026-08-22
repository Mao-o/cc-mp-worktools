"""flock 付き read-modify-write。"""
import fcntl
import os
import tempfile
import threading
import unittest

import _testutil  # noqa: F401  (sys.path 設定)

from _common import flock


class TestLockedFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "nested", "dir", "marker")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_parent_and_round_trips(self):
        with flock.locked_file(self.path) as f:
            self.assertEqual(flock.read_all(f), "")
            flock.rewrite(f, "hash\n1")
        with flock.locked_file(self.path) as f:
            self.assertEqual(flock.read_all(f), "hash\n1")

    def test_rewrite_truncates_longer_previous_content(self):
        with flock.locked_file(self.path) as f:
            flock.rewrite(f, "a much longer content")
        with flock.locked_file(self.path) as f:
            flock.rewrite(f, "x")
        with open(self.path) as f:
            self.assertEqual(f.read(), "x")

    def test_lock_is_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with flock.locked_file(self.path):
                raise RuntimeError("boom")
        with open(self.path, "a+") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # 取れなければ OSError
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)

    def test_concurrent_increments_are_serialized(self):
        threads_n, per_thread = 8, 25

        def worker():
            for _ in range(per_thread):
                with flock.locked_file(self.path) as f:
                    current = int(flock.read_all(f).strip() or 0)
                    flock.rewrite(f, str(current + 1))

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.path) as f:
            self.assertEqual(int(f.read()), threads_n * per_thread)


if __name__ == "__main__":
    unittest.main()
