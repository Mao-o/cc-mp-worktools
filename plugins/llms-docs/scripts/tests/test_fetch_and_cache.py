"""Tests for the fetch/cache robustness cluster in scripts/_common.py.

Covers ``default_cache_dir`` (env-override / XDG resolution), atomic cache
writes (no partially-written file ever observable, no leftover temp file),
the stale-cache fallback on transport failure, the widened exception
handling (a plain ``URLError`` catch missed ``TimeoutError`` and
``http.client.IncompleteRead``, which used to reach the user as a raw
Python traceback), and ``load_lines``' ``errors="replace"`` decoding.

``fetch_url``'s failure paths need a mocked ``urllib.request.urlopen``
rather than a cache fixture — a cache fixture works precisely by making
``fetch_url`` short-circuit *before* any network call, which is the one
code path these tests need to get past.
"""

import contextlib
import http.client
import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

import _common


class DefaultCacheDirTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME")
        }
        os.environ.pop("LLMS_DOCS_CACHE_DIR", None)
        os.environ.pop("XDG_CACHE_HOME", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_llms_docs_cache_dir_override_wins(self):
        os.environ["LLMS_DOCS_CACHE_DIR"] = "/tmp/llms-docs-override-test"
        self.assertEqual(
            _common.default_cache_dir(), "/tmp/llms-docs-override-test"
        )

    def test_xdg_cache_home_used_when_no_override(self):
        os.environ["XDG_CACHE_HOME"] = "/tmp/xdg-test-dir"
        self.assertEqual(
            _common.default_cache_dir(), "/tmp/xdg-test-dir/llms-docs"
        )

    def test_falls_back_to_home_cache_when_neither_set(self):
        result = _common.default_cache_dir()
        self.assertTrue(result.endswith(os.path.join(".cache", "llms-docs")))
        self.assertNotIn("~", result)  # expanduser must have run

    def test_does_not_create_the_directory_as_a_side_effect(self):
        target = "/tmp/llms-docs-should-not-be-created-test"
        if os.path.isdir(target):
            os.rmdir(target)
        os.environ["LLMS_DOCS_CACHE_DIR"] = target
        _common.default_cache_dir()
        self.assertFalse(os.path.isdir(target))


class FormatAgeTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(_common._format_age(30), "30s")

    def test_minutes(self):
        self.assertEqual(_common._format_age(180), "3m")

    def test_hours(self):
        self.assertEqual(_common._format_age(3 * 3600), "3h")

    def test_days(self):
        self.assertEqual(_common._format_age(9 * 86400), "9.0d")

    def test_negative_clamped_to_zero(self):
        self.assertEqual(_common._format_age(-5), "0s")


class _FakeResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` returns."""

    def __init__(self, data, headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FetchUrlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cache_path(self, name="cache.txt"):
        return os.path.join(self.tmp, name)

    def test_successful_fetch_writes_file_and_leaves_no_temp_file(self):
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(b"hello world")
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)
        with open(cache_path, "rb") as f:
            self.assertEqual(f.read(), b"hello world")
        leftover = [n for n in os.listdir(self.tmp) if n.startswith(".fetch-tmp-")]
        self.assertEqual(leftover, [])

    def test_content_length_match_succeeds(self):
        cache_path = self._cache_path()
        data = b"exactly eleven"
        resp = _FakeResponse(data, headers={"Content-Length": str(len(data))})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)

    def test_content_length_mismatch_is_treated_as_failure(self):
        cache_path = self._cache_path()
        bad_resp = _FakeResponse(b"short", headers={"Content-Length": "999"})
        with mock.patch("urllib.request.urlopen", return_value=bad_resp):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(cache_path))

    def test_no_cache_and_fetch_fails_exits_1(self):
        cache_path = self._cache_path()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_stale_cache_served_on_fetch_failure(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400  # 8 days old (past the 7-day default)
        os.utime(cache_path, (old_time, old_time))

        err = io.StringIO()
        with mock.patch(
            "urllib.request.urlopen", side_effect=OSError("network down")
        ), contextlib.redirect_stderr(err):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("using cached copy", err.getvalue())

    def test_timeout_error_does_not_propagate_as_raw_traceback(self):
        # TimeoutError is an OSError subclass but not a urllib.error.URLError
        # — this is exactly the gap the widened except clause closes.
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_incomplete_read_does_not_propagate_as_raw_traceback(self):
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"", 10),
        ):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_fresh_cache_short_circuits_without_network_call(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("fresh")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        mock_urlopen.assert_not_called()
        self.assertEqual(result, cache_path)


class LoadLinesTest(unittest.TestCase):
    def test_invalid_utf8_is_replaced_not_raised(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "bad.txt")
        with open(path, "wb") as f:
            f.write(b"hello \xff\xfe world\n")
        lines = _common.load_lines(path)  # must not raise UnicodeDecodeError
        self.assertEqual(len(lines), 1)
        self.assertIn("hello", lines[0])
        self.assertIn("world", lines[0])

    def test_valid_utf8_round_trips_unchanged(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "good.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("非ASCII テキスト\n")
        lines = _common.load_lines(path)
        self.assertEqual(lines, ["非ASCII テキスト\n"])


if __name__ == "__main__":
    unittest.main()
