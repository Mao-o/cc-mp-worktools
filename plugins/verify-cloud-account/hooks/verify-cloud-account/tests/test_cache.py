"""cache.get_success / set_success のラウンドトリップと無効化テスト。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import cache  # noqa: E402


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._p = mock.patch.dict(os.environ, {"TMPDIR": self.tmp})
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_miss_returns_false(self):
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    def test_roundtrip_hit(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))

    def test_different_expected_miss(self):
        cache.set_success("svc", "/p", "expA", 1.0)
        self.assertFalse(cache.get_success("svc", "/p", "expB", 1.0))

    def test_different_project_miss(self):
        cache.set_success("svc", "/p1", "exp", 1.0)
        self.assertFalse(cache.get_success("svc", "/p2", "exp", 1.0))

    def test_different_service_miss(self):
        cache.set_success("svcA", "/p", "exp", 1.0)
        self.assertFalse(cache.get_success("svcB", "/p", "exp", 1.0))

    def test_mtime_change_miss(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertFalse(cache.get_success("svc", "/p", "exp", 2.0))

    def test_ttl_expiry(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))
        with mock.patch.object(cache, "_CACHE_TTL_SEC", 0):
            time.sleep(0.05)
            self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    def test_dict_expected_roundtrip(self):
        exp = {"project": "p", "account": "a"}
        cache.set_success("svc", "/p", exp, 1.0)
        self.assertTrue(cache.get_success("svc", "/p", exp, 1.0))

    def test_corrupt_cache_file_miss(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        base = Path(self.tmp) / "cc-mp-verify-cloud-account"
        files = list(base.glob("*.json"))
        self.assertTrue(files)
        files[0].write_text("not json", encoding="utf-8")
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))


if __name__ == "__main__":
    unittest.main()
